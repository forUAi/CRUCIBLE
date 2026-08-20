<!-- Preserved from the original project README. This is the design
rationale CRUCIBLE was built from: why running a repository is treated as a
search problem with a verifier, why snapshots make wrong guesses affordable,
and why the sandbox is assembled from kernel primitives rather than a daemon.
Kept because the reasoning outlived the prose it was written for. -->

# CRUCIBLE

**Give it a repo. It figures out how to run it, in a sandbox, and writes down what it learned.**

```bash
python3 -m crucible.cli ./my-repo                    # infer, run, verify
python3 -m crucible.cli https://github.com/x/y       # clone first
python3 -m crucible.cli ./my-repo --plan-only        # analyze, don't execute
python3 -m crucible.cli ./my-repo --emit ./out       # Dockerfile + compose + lock
```

No Docker daemon. No KVM. No third-party packages — Python 3.11+ stdlib only.

---

## The thesis

Every tool in this space — buildpacks, Nixpacks, devcontainer autodetect — treats
"run this repo" as a **classification** problem: detect the language, look up a
recipe, hope. That works on the demo repo and falls over on anything real, because
the recipe is guessed once and never checked.

There's an asymmetry nobody exploits:

> **Verifying that an app runs is cheap and objective. Predicting how to run it is
> expensive and unreliable.**

That makes it a **search problem with a verifier**, not a lookup. You're allowed to
guess wrong, as long as failure is cheap.

Failure is only cheap if retrying doesn't mean rebuilding from scratch. So the whole
architecture hangs on one primitive: **copy-on-write snapshots after every successful
step.** Get that, and a wrong guess costs the delta instead of the build.

## Architecture

```
  repo
   ├─▶ evidence.py    deterministic fingerprint. Never guesses; every signal
   │                  cites the file that produced it.
   ├─▶ planner.py     Evidence ─▶ RunPlan. Author intent beats inference.
   ├─▶ engine.py      snapshot-hit? skip : exec, snapshot
   │                  boot sidecars ─▶ run ─▶ oracle ─▶ pass | diagnose ─▶ rewind
   ├─▶ repair.py      34 deterministic rules; LLM only for what they can't name
   ├─▶ oracle.py      archetype-specific success predicates
   ├─▶ pod.py         shared-netns sidecar services
   ├─▶ netlog.py      egress ledger (DNS + socket)
   └─▶ materialize.py ─▶ Dockerfile + compose.yml + crucible.lock.json
```

### Decisions that matter

**A Dockerfile is a plan, not a build format.** Everyone treats it as something
requiring a daemon. It isn't — `FROM`/`RUN`/`ENV`/`CMD` map almost 1:1 onto a step
list. Interpreting instead of building honors author intent on any substrate, and
makes each `RUN` independently snapshotted and independently repairable.

**Deterministic repairs before model repairs.** Build tooling emits highly structured
diagnostics, so environment failures are a short boring head, not a long tail. This
isn't cost optimization, it's correctness: a regex matching `fatal error:
libpq-fe.h` is *certain* about the fix; a model is merely confident. Spend the model
on genuine ambiguity.

**The cache key is repo *shape*, not identity.** Language, package managers, native
deps, service topology. A plan repaired the hard way on one FastAPI+psycopg service
warm-starts the next structurally identical one — different repo, possibly different
org. The system gets faster the more repos it sees.

**The oracle is archetype-relative and deliberately weak.** A web app passes if it
answers TCP — *any* status, because a 404 proves the server is alive, which is the
actual question. Asserting 200 fails every app whose root route is `/api`. Libraries
pass if tests run; workers if they survive N seconds. Every strengthening of the
oracle is a new false negative on a legitimate repo.

## Isolation

Docker wasn't available in the target environment, so the sandbox is built from the
primitives Docker itself uses — which turned out to be *more* portable, not less. It
works inside containers, in CI, and on hosts where installing a daemon is a six-week
ticket.

| | |
|---|---|
| mount ns + overlayfs | filesystem isolation **and** free snapshots |
| pid ns | process isolation (verified: `pid=1`, 4 procs visible) |
| net ns | build/run network split, and the pod model below |
| cgroups + rlimits | cpu / memory / pid / fd caps |
| `oci.py` | pulls any registry image with no daemon — ~150 lines of HTTP |

Two overlays are stacked, and the second is the important one:

```
/            lower = base rootfs     upper = layers/<chain-hash>/root
/workspace   lower = the user's repo upper = layers/<chain-hash>/ws
```

Overlaying the **workspace** gives the repo copy-on-write. The sandbox can `rm -rf`
or `git reset --hard` in it and the user's source is untouched — verified. Rewinding
a failed attempt is an unmount, not a restore-from-backup.

Layers are content-addressed by **chain** hash — base + system packages + every step
beneath — and shared across runs. Keying on the step alone would be wrong: `pip
install -r requirements.txt` under `python:3.11` and under `node:22` are different
layers that happen to share a command string.

## Sidecars: the pod model

Services (postgres, redis) and the app must reach each other while both stay cut off
from the internet. Two isolated sandboxes can't talk; one shared sandbox isn't
isolation. The resolution is to stop treating them as two machines needing a network
between them and **put them in the same network namespace.**

That's what a Kubernetes pod is, and the trick that makes it work is the pause
container — a do-nothing process holding the namespace open so others can join.
Here that's `unshare --net sleep`, and joining is `nsenter -t <pause> -n`.

```
app ─▶ 127.0.0.1:5432 ─▶ postgres      works  (same netns, same loopback)
app ─▶ 1.1.1.1:443                     unreachable (netns has no route out)
host                                   entirely unaffected
```

No veth pairs, no bridges, no NAT, no iproute2 — none of which are installed here.
Each member still gets its own mount, pid, uts and ipc namespaces; only the network
is deliberately shared. Readiness is a real port probe, not a sleep: otherwise the
app races the database and the failure gets misdiagnosed as the app's fault, sending
the repair loop down the wrong path.

Sidecar commands come from the image's own config blob (`Entrypoint`/`Cmd`/`Env`),
fetched from the registry, rather than a table we'd have to maintain per service.

## Egress ledger

A repo runner that executes untrusted build scripts with network access is a dynamic
analysis harness whether it admits it or not. The only question is whether it throws
the telemetry away. Two cheap sensors:

- **DnsLedger** — a UDP forwarder on `127.0.0.2:53`; the sandbox's `resolv.conf`
  points at it, so every name the build resolves is logged and relayed upstream.
  Deliberately a forwarder, not a resolver: a ledger that breaks DNS gets switched
  off, and a sensor nobody runs measures nothing.
- **SocketSampler** — `/proc/net/tcp` lists every connection on the host, far too
  noisy. Instead we walk the sandboxed process's descendants, collect the socket
  inodes they hold in `/proc/<pid>/fd`, and keep only rows whose inode is in that
  set. That attributes each peer to *our* process tree.

The gap between them is the point. A peer in the socket table that no DNS answer ever
named was either hard-coded — the standard way to dodge DNS-based egress monitoring —
or resolved out of band. Real output from `examples/beacon`:

```
egress ledger
  build resolved 1 host(s): pypi.org
  build connected to 2 peer(s): 1.1.1.1:443, 151.101.0.223:443
  ! 1 peer(s) never named by DNS: 1.1.1.1:443 — hard-coded address or out-of-band resolution
  runtime egress: none — namespace has no route out
```

## Verified in this environment

Actually executed, not aspirational:

```
✓ pid/mount/net/uts/ipc isolation      pid=1 inside, 4 processes visible
✓ network CUT / ON                     egress unreachable, then reachable
✓ workspace copy-on-write              sandbox wrote MUTATED, host file unchanged
✓ OCI pull, no Docker                  alpine:3.20 in 1.2s, distro ≠ host
✓ image config from registry           redis entrypoint/cmd/ports read correctly
✓ repair loop (host base)              2 failures diagnosed → SUCCESS in 10.2s
✓ repair loop (pulled base)            python:3.12-slim → SUCCESS in 9.5s
✓ plan cache warm start                3 attempts → 1
✓ shape transfer                       different repo, same shape → first-try pass
✓ cross-run layer reuse                10.7s → 7.2s, 2 steps restored from disk
✓ redis sidecar                        PING→PONG, SET/GET round-trip
✓ postgres sidecar                     real initdb, wire-protocol auth reply
✓ app ↔ sidecar across sandboxes       app asserts PING before serving; :8000 answers
✓ egress ledger                        pypi.org + peer IPs captured
✓ DNS/socket delta                     hard-coded 1.1.1.1 flagged
✓ detection across 8 repos             python/node/go/rust/Dockerfile/library
```

Sample repair chain (repo with an undeclared dependency):

```
attempt 1  install/pip failed  → [rule p=0.99] PEP 668 externally-managed env
attempt 2  run failed          → [rule] module `toml` missing → pip install toml
attempt 3  install/pip skipped (snapshot hit) → ✓ port 8000 answered
```

### Bugs only running it could have found

Each is a design constraint, not a typo. Recorded because they're the actual content:

- **overlayfs `ELOOP`.** Using host `/` as the read-only lower puts the snapshot dirs
  *inside* a lower layer; overlayfs walks dentry parents looking for traps and
  refuses, with the memorably unhelpful "Too many levels of symbolic links". Dentry
  walks don't cross mount points — giving the layer store its own filesystem severs
  the ancestry chain and the whole class of error disappears.
- **A fresh netns has loopback DOWN.** "Network off" silently also meant "127.0.0.1
  broken", killing any app binding localhost and making the health probe report false
  negatives. `iproute2` is absent from slim images (and from this host), so it's fixed
  with a raw `SIOCSIFFLAGS` ioctl run via `nsenter` using the *host's* Python — works
  against a rootfs with no userland at all.
- **TLS interception.** This network terminates TLS at an egress proxy with a private
  CA. The host trusts it; a pulled image doesn't, and no amount of `apt-get install
  ca-certificates` helps because the cert isn't public. The sandbox now inherits host
  CA trust: it isolates filesystem and processes, not the org's PKI.
- **`mknod` applies the umask.** `/dev/null` came out `0644` root-owned, so postgres
  — which drops to the `postgres` user before writing to it — died with a permission
  error pointing nowhere near the cause. Needs an explicit `chmod`.
- **`/dev/fd` must symlink to `/proc/self/fd`.** Shell process substitution `<(...)`
  compiles to `/dev/fd/N`; postgres's entrypoint uses it, and `initdb` failed with
  "could not open file /dev/fd/63". The container `/dev` contract is more than nodes.
- **Dependency ports aren't app ports.** A repo containing `6379` is naming its Redis,
  not its own port. Letting one through made the oracle probe the sidecar and declare
  the app healthy while the app was dead — the worst possible failure for a verifier.
- **The `idna` codec rejects a non-strict `errors` argument** and raises
  `UnicodeError`, which silently emptied every DNS name parse. Labels are ASCII on
  the wire regardless.
- **Poll-first sampling reports no egress at all.** Build steps routinely open and
  close a connection inside 100ms; a sampler that sleeps before its first look sees
  an empty table.

## Output

Success produces a **recipe**, not just a green check:

```dockerfile
# Generated by CRUCIBLE from an observed successful run.
# Every line below was validated by execution, not inferred.
#   · language=python (requirements.txt)
#   · base=python:3.12-slim (runtime pin: 3.12)
#   · gen1: python module `toml` missing -> pip install toml
FROM python:3.12-slim
...
```

Plus `compose.yml` with the sidecars that were actually booted, and
`crucible.lock.json` carrying the full attempt history and the egress ledger. Every
package in that Dockerfile is justified by a real failure it fixed, and a human can
review it line by line.

## Limitations — read this part

- **Namespace isolation is not a security boundary for hostile code.** Shared kernel,
  full syscall surface. For untrusted repos use gVisor or Firecracker — the
  `SandboxBackend` interface exists (four verbs), the implementations are stubs.
- **Socket sampling is lossy.** It polls at 15ms and can miss a connection shorter
  than the interval. Only kernel-side capture (eBPF, netfilter, conntrack) closes
  that gap properly. The DNS ledger is exact; the socket ledger is a lower bound.
- **Runtime DNS is not logged.** The pod netns has no route out, so runtime egress is
  impossible rather than observed. Logging *attempts* would need a resolver inside
  the pod netns — worth doing, since "app tried to resolve X at runtime" is a strong
  signal.
- **Requires mount privileges** (root or `CAP_SYS_ADMIN`). A rootless path via user
  namespaces + fuse-overlayfs is feasible and unwritten.
- **Monorepos are detected, not handled.** Workspace members are recorded in Evidence
  and then ignored by the planner.
- **`--base host` inherits the host toolchain** — fast and convenient, not
  reproducible across machines. Pulled OCI bases are.
- Windows/macOS repos, GPU workloads, and anything needing systemd are out of scope.

## Roadmap

1. **Speculative parallel plans.** When evidence is ambiguous, launch 3 candidate
   plans in parallel sandboxes and take the first that satisfies the oracle. Snapshots
   make the branches cheap; `SandboxBackend.fork()` is already in the interface.
2. **Kernel-side egress capture** to replace sampling, plus a pod-netns resolver so
   runtime resolution attempts are recorded rather than merely prevented.
3. **Monorepo planning** — per-workspace plans with a shared base layer.

## Layout

```
crucible/
  schema.py       Evidence / RunPlan / ExecResult
  evidence.py     deterministic fingerprinting
  planner.py      Evidence -> RunPlan, Dockerfile interpretation
  repair.py       34 rules + LLM fallback
  oracle.py       archetype-specific verification
  engine.py       the loop
  pod.py          shared-netns sidecar services
  netlog.py       DNS + socket egress ledger
  materialize.py  RunPlan -> Dockerfile / compose / lock
  oci.py          daemonless registry pull
  backends/       base.py (interface) + namespace.py (ns + overlayfs + cgroups)
  cli.py
examples/         8 synthetic repos exercising different detection paths
```

`ANTHROPIC_API_KEY` optionally enables tier-2 repair; without it the deterministic
rules run alone.
