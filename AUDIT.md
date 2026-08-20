# CRUCIBLE audit

Everything here was produced by running the system, on this machine, in the
Lima guest. Where a claim is untested it says so. Where a gate is unmet it
says so.

Reproduce:

```bash
limactl start .lima/crucible.yaml          # Ubuntu 24.04 aarch64, vz
limactl shell crucible -- bash -lc '
  git clone /Users/<you>/Projects/crucible ~/audit/crucible
  cd ~/audit/crucible && python3 -m unittest discover -s tests
  sudo python3 -u -m crucible.cli <repo> --no-llm --budget 6 --no-cache'
```

`python3 -u` matters: stdout is line-buffered now, but a killed run under an
older build loses its last buffer and the traceback surfaces above the output
that explains it.

---

## 1. Audit of dd7ba8c

Seven bugs claimed. All seven are real and the fixes are present in the code.
Two had regression tests (`tests/test_cache_key.py`); five did not, so nothing
would have noticed them returning. Those five now have tests in
`tests/test_dd7ba8c_regressions.py`.

| # | Claim | Verified how | Test |
|---|---|---|---|
| 1 | `pull_rootfs` defaulted to `amd64`; every exec died `Exec format error` on arm64 | Live pulls log `platform: linux/arm64/v8`; `_pick_manifest` refuses unknown platforms instead of taking `manifests[0]` | added |
| 2 | `_install_system` discarded its result; `allow_fail` forces `ok=True`, so a failed `apt-get` was unreadable | Reproduced live — the `/tmp` bug below made apt fail and the new check printed `system packages FAILED (exit 100)` | added |
| 3 | `/etc/resolv.conf` is a symlink into `/run`; `write_text` followed it into `except OSError: pass` and the sandbox got no resolver | Code inspection; DNS resolves in live runs (egress ledger records `pypi.org`, `repo.maven.apache.org`) | added |
| 4 | `--base` was monkeypatched onto `planner.plan`, but a plan-cache hit returns before the planner runs, silently revoking the override | `Engine._apply_base_override` applies to cached and fresh plans alike | added |
| 5 | New repair rules: distro-owned package with no RECORD; `psycopg` → `psycopg[binary]` | `diagnose()` returns a patch for both logs | added |
| 6 | Layer chain key omitted manifest content, so an unrelated repo adopted another's `site-packages` | `manifest_digest()` folds every manifest into the chain seed | pre-existing (9) |
| 7 | `to_compose` emitted pod loopback addresses into a compose file whose services each get their own netns | `compose_host_rewrite` maps sidecar host:port to service:port | pre-existing (9) |

Bug 6 is the serious one: a false cache hit returns a **pass** whose emitted
Dockerfile installs nothing the repo declares.

### Not fixed by that commit, found here

`.lima/crucible.yaml` mounted `~/Projects/crucible` into the guest
**writable**. See §4.

---

## 2. Defects found by executing real repositories

None of these were visible from reading the code. Each was found by running a
real repository and each has a fix in this branch.

| # | Defect | Found by | Consequence |
|---|---|---|---|
| 1 | `/tmp` created 0755 root-owned — `mkdir` applies the umask, same class as the documented `/dev/null` `mknod` bug | heroku go app | apt drops to `_apt` for GPG, cannot write `/tmp/apt.conf.XXXX`, reports the repo unsigned; **every apt repair silently could not take effect** |
| 2 | Web plans never exported `PORT` | heroku node app | app bound 5006, oracle probed 3000, 45s grace burned against a port nothing would open |
| 3 | Multi-stage Dockerfiles flattened: base from the final `FROM`, RUN steps from every stage | heroku go app | runtime image lacks the toolchain **by design**; `git: command not found` for a tool the build image has, unrepairable because the base was wrong |
| 4 | All stages' `ENV` merged | heroku go app | runtime `ENV HOME /app` leaked into build steps; buildpack died on `could not lock config file /app/.gitconfig` |
| 5 | Dockerfile app directory never exists | heroku go app | `COPY . /app` + `WORKDIR /app` + `CMD /app/bin/...` all dangle against `/workspace` |
| 6 | `useradd` in a final stage fails `already exists` on the build image | heroku go app | single-rootfs tax; intent satisfied, exit code disagreeing |
| 7 | `val.lstrip("1.")` to strip a legacy JDK prefix | Spring Petclinic | `lstrip` takes a character **set**: `17`→`7`, `11`→`''`. Petclinic got `eclipse-temurin:7-jdk`, a tag that does not exist |
| 8 | Ports sorted numerically; `ports[0]` drives the oracle and `PORT` | Spring Petclinic | a `:80` grepped from a README outranked the framework's 8080 |
| 9 | Base image `ENV` ignored | Spring Petclinic | `./mvnw` died `JAVA_HOME is not defined correctly` on the base chosen to provide Java. `pod.py` has read image config for sidecars all along; the app's sandbox never did |
| 10 | Step deadline only checked after a line of output | hostile fixture | a process that goes quiet is **never** timed out; a fixture held a build step 13m against a 30m budget |
| 11 | Waited for pipe EOF, not child exit | hostile fixture | `Popen(start_new_session=True)` in a build script inherits stdout; waiting for EOF waits for the daemon |
| 12 | No cleanup after a killed run | accumulated | 674 MB in four `box-*` dirs, three still holding live overlay mounts, pinning the loop device |
| 13 | Layer store fixed at 4 GB | Spring Petclinic | filled at 100%; run crashed with a raw `OSError` traceback after a 76s Maven build had already succeeded |
| 14 | stdout block-buffered when piped | diagnosing #13 | a killed run loses its last buffer; stderr surfaced the traceback *above* the output that preceded it by two minutes |
| 15 | Driver on the classpath read as a required service | Spring Petclinic | booted mysql **and** postgres for an app that runs on in-memory H2 and needs neither |
| 16 | A module-level `def` inserted mid-class swallowed every method below it | my own edit | `_mount`, `_umount`, `exec`, `spawn`, `_pump` left the class. Compiled; 41 planning tests passed; surfaced only as an `AttributeError` in teardown after a two-minute image pull. The planning tests never touch the backend — `tests/test_backend_contract.py` now asserts the class structurally |

---

## 3. Capability matrix

Six states, per the audit request. **Integration-tested** means it ran end to
end in Lima against a real repository, at least once, with the oracle
satisfied.

### Implemented and integration-tested

| Capability | Evidence |
|---|---|
| Python web app, real deps, sidecar, verified | `examples/py-fastapi`: pip install, real `postgres:16-alpine` booted with no Docker, uvicorn verified on :8000, 17.4s |
| Node/npm web app | `heroku/node-js-getting-started` @63c6674: `npm ci`, express, verified on :3000, **9.8s cold** |
| Go build via buildpack, multi-stage Dockerfile | `heroku/go-getting-started` @3e3b414: buildpack compile, verified on :8080, 23.8s, one repair |
| Java/Maven wrapper build **and launch** | `spring-petclinic` @88e37c1: `./mvnw` build, then `java -jar target/spring-petclinic-4.0.0-SNAPSHOT.jar` verified on :8080 with a real postgres sidecar |
| OCI pull, no Docker daemon | `eclipse-temurin:17-jdk`, `node:22-slim`, `heroku/heroku:24-build`, `postgres:16-alpine` pulled and executed |
| Base-image ENV honoured | `image env: JAVA_HOME, JAVA_VERSION, LANG, LANGUAGE, LC_ALL, PATH` |
| Network split build/run | egress ledger records build peers; `runtime egress: none — namespace has no route out` |
| Repair loop on real failures | `useradd already exists` → guard; `git not on PATH` → apt |
| Snapshot reuse | `⤳ df-run-1 (snapshot hit, skipped)` |
| Abandoned-box reaper | `reaped 1 abandoned box(es) from earlier runs` |

### Implemented, unit-tested only

Plan linting; workspace/monorepo *detection* (not planning); JVM manifest
parsing; multi-stage Dockerfile interpretation (integration-tested on one
repo); `needs_configuration` status; containment staging off host-backed
mounts; store sizing. 59 tests, all passing on macOS and in the guest.

### Adversarially tested

One fixture, `security/fixtures/hostile-python`, 11 probes, run 3× from a
fresh layer store: **3/3 PASS, deterministic (12.4 / 12.3 / 12.4s), host
clean, no residue.**

```
python3 security/contain.py --run hostile-python --repeat 3
```

| Probe | Result | Reading |
|---|---|---|
| `host_mount_visible` | `none` | no host path reachable from an OCI base |
| `host_mount_write` | `no host mount reachable` | host canary never appeared |
| `escape_workspace_write` | `WROTE /etc/…`, `WROTE /root/…` | **confined**, not escaped — those are the overlay upper and die with the box |
| `symlink_traversal` | `WROTE via symlink into /etc` | same: inside the box |
| `pid_namespace` | `pid=3 visible_pids=2` | pid isolation holds |
| `cloud_metadata` | `ConnectionRefusedError` | refused here; **would reach on a cloud host** — not blocked by policy |
| `outbound_hardcoded_ip` | `REACHED 1.1.1.1:443 without DNS` | **known policy gap**: build egress is recorded, not restricted |
| `runtime_egress` | blocked | the pod netns has no route out |
| `fork_pressure` | `400 processes, no cap hit` | **inconclusive** — the probe stops below the 512 pid cap |
| `disk_pressure` | `wrote 1024 MiB inside the box` | **no per-box disk quota** |
| `credentials` | `env: [GPG_KEY], paths: []` | no SSH/AWS/docker socket; `GPG_KEY` comes from the python image's own ENV, not the host |

The harness reports `known_policy_gap` separately from `escaped` rather than
folding either into the verdict. Build-phase egress is unrestricted **by
design today** — calling that a pass would hide it, calling it an escape
would make every run red and stop meaning anything.

Two harness bugs worth recording, because both produced false greens:

- It first reported **PASS on a run that captured zero probes** — absence of
  evidence read as evidence of absence. A fixture whose probes never report
  is now `INCONCLUSIVE` and exits non-zero.
- Repetition 2 came back with zero probes because it hit the **layer cache
  and skipped the probe step entirely**. Repeatability is meaningless when
  run N is a cache hit on run N−1; the harness now wipes the layer store
  before every repetition.

### Execution benchmark

`sudo python3 bench/execbench.py --repeat 2`, layer store wiped before every
repetition:

| Target | Result | Cold times | Attempts | Launch command |
|---|---|---|---|---|
| python/fastapi | verified ×2 | 14.3s / 14.4s | 1 | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| node/express | verified ×2 | 11.9s / 12.0s | 1 | `node index.js` |
| go/buildpack | verified ×2 | 35.5s / 34.5s | 2 | `/app/bin/go-getting-started` |
| java/spring-maven | verified ×2 | 95.6s / 110.2s | 1 | `java -jar target/spring-petclinic-4.0.0-SNAPSHOT.jar` |

**8/8 matched expectation, nondeterministic: none.** The Go target takes two
attempts both times and applies the identical repair both times, which is
determinism rather than flakiness.

`./mvnw`, `java -jar`, `npm ci`, Go builds and Python applications all
complete inside the VM. `pnpm` and `yarn` are **not** proven — no target in
this set uses them, and asserting them from the planner's command string
would be exactly the "generated a plausible command" claim this audit is
supposed to reject.

Three harness bugs were fixed on the way here, all of which produced wrong
numbers rather than errors: `~` expanded to `/root` under sudo so three
targets reported `failed` in 0.0s; the guest home is `vishalchandupatla.guest`
and not `/home/<SUDO_USER>`; and an early return with missing keys crashed the
printer and discarded two good results. A benchmark that can report a harness
error as a repository failure is worse than no benchmark.

### Externally benchmarked

18 repositories, **planning only** (`bench/bench.py`): archetype 11/12 on
unambiguous labels, 0/18 self-contradicting plans. This is the analysis half.
It is not an execution benchmark.

### Unsupported / not implemented

See §5.

---

## 4. Security boundary

### Findings

**The Lima mount was writable.** `~/Projects/crucible` was mounted
`writable: true`, so a repository under that path became a workspace overlay
whose *lower layer was host-owned bytes*. The overlay redirects writes to the
upper — that protects the tree from an honest build script, and does not keep
a hostile one away from the host.

Two changes, and the code one is the control:

- `crucible/containment.py` makes the rule positional: a repo whose path
  resolves onto `virtiofs`/`9p`/`sshfs`/`NFS`/`CIFS`/`vboxsf` is copied to
  guest-local storage before it becomes the lower layer. Symlinks are copied
  as symlinks, never followed — following one pulls host content in through a
  link the repo controls.
- The mount is now `writable: false`, as defence in depth.

### Properties, honestly

| Required property | State |
|---|---|
| No repository code executes on the host | **Holds.** Every command runs under `unshare` inside the guest |
| No writable host-repository mount during execution | **Holds now**, via `containment.py` staging + read-only mount |
| Copy source into ephemeral guest filesystem | **Holds** for host-backed paths |
| No network by default during runtime | **Holds.** Verified: `runtime egress: none` |
| Build/run policy split | **Holds.** Build steps `network=True`, run in a netns with no route out |
| CPU / memory / pid / fd limits | **Implemented** (cgroup v2 + rlimits). Not adversarially verified |
| Process-group termination | **Implemented** (`os.killpg`, `unshare --kill-child`). Partially verified |
| Complete cleanup after crash or timeout | **Holds, and is now proven** — see §7. The earlier claim was wrong |
| Immutable base image / disposable VM state | **Partial.** Boxes are disposable; the VM is reused across runs |
| No host SSH agent / cloud creds / docker socket | **Not verified.** No probe has confirmed absence |
| Controlled allowlisted dependency proxy | **Not implemented.** Build egress is observed, not restricted |
| Explicit artifact export allowlist | **Not implemented** |
| Structured audit log of attempted side effects | **Partial.** DNS + socket ledger only; no filesystem or exec audit |

---

## 5. Unmet release gates

Stated plainly. None of these are close.

1. **32 pinned external repositories benchmarked** — 18 are benchmarked for
   *planning*; 4 have been executed end to end. No dev/validation/holdout
   split exists.
2. **Language identification ≥98%** — 14/16 (87.5%) on the 18-repo set.
3. **Workspace/archetype accuracy ≥95%** — 11/12 (91.7%) on unambiguous
   labels, and that is archetype only; workspace planning does not exist.
4. **Contradictory plans 0%** — **met** on the current corpus (0/18).
5. **≥90% of credential-free repos build, launch, verify, terminate** —
   4/4 verified, 2 repetitions each, 8/8 deterministic. A 4-repository sample
   cannot support a 90% claim; the number needs the 32-repo corpus, not more
   confidence in these four.
6. **Deterministic repeated execution** — **partially met.** 4 targets × 2
   repetitions from a wiped layer store: identical outcomes, identical repair
   chains, times within 1% except Java (95.6s / 110.2s, Maven network
   variance). The protocol asks for three repetitions; this is two.
7. **All adversarial containment tests pass** — one fixture exists, for one
   language. No Java/Go/Node fixtures, no fork bomb, no symlink traversal
   result, no metadata-endpoint probe result.
8. **Zero unapproved host effects** — **met for what was tested.** Across
   every run in this session: host canary never written, no canary outside
   the sandbox, host tree hash unchanged, no new host listener, and Docker is
   not installed on the host so nothing was containerised there. Scope is one
   fixture and four repositories, not a proof.
9. **Monorepo planning across four ecosystems** — **not implemented.**
   Workspaces are detected and recorded, then ignored, exactly as the README
   already admits.
10. **Execution from a clean release artifact** — not attempted.
11. **Threat model documented** — this file is a start, not a threat model.
12. **PRISM stable execution API** — **not implemented.**

## 6. Correction: the earlier cleanup claim was wrong

An earlier revision of this file said cleanup after crash or timeout
**holds**. That was inaccurate and is retracted.

It was asserted from a *successful* shutdown, which proves nothing about the
crash path — the code that tidies up is exactly the code a crash skips. The
reaper at the time cleaned filesystem and overlay state only. A completed run
left CRUCIBLE's `sleep 86400` pause container, holding the pod's network
namespace, orphaned to init for several hours.

Two root causes, and the second is the real one:

1. `Pod.down()` kills the pause via `killpg`, and `Pod.down()` never ran.
2. Nothing recorded that the pause **belonged** to CRUCIBLE. The reaper could
   not have cleaned it even if it had looked, because the only way to find it
   was to match on a process name — and `pkill -f "sleep 86400"` will kill an
   unrelated backup script. Twice in this project a `pgrep` pattern matched
   the inspection command that contained it.

Ownership is now recorded in two places a dying process cannot forget to
update: a **cgroup per run** (kernel-tracked membership; `cgroup.kill` is
atomic and cannot reach a non-member) and a **run registry** naming the
cgroup, directories and mount sources, stamped with the owner's pid *and*
that pid's start time — pids are recycled, and a stale record pointing at a
reused pid would otherwise authorise killing a stranger.

A third defect surfaced while proving it: the property naming a box's cgroup
had been dropped during an edit, and `getattr(box, "cgroup", "")` turned that
missing ownership boundary into an empty string. The pod ran completely
untracked and the only symptom was an orphan hours later. A backend that
cannot say which processes are its own is now refused outright.

## 7. Crash-cleanup evidence

```bash
sudo python3 security/lifecycle_test.py --case all                  # reap() directly
sudo python3 security/lifecycle_test.py --case all --reap-with-run  # via a later run
```

Every case starts from a wiped environment and kills the engine in a state
where it holds live resources. Inventory is taken by ownership evidence only
— cgroup membership, overlay mounts whose *source* is a name only CRUCIBLE
writes, and directories named in the registry. No process-name matching.

| Case | What it kills | Survives the crash | After recovery |
|---|---|---|---|
| `normal` | nothing (baseline) | nothing | clean |
| `sigkill_pod` | SIGKILL while the pause container holds a netns | cgroup + 1 live process, 2 overlays, box dir, pod dir, registry entry | **clean** |
| `sigkill_build` | SIGKILL mid build step | cgroup + 1 live process, 2 overlays, box dir, registry entry | **clean** |
| `sigterm_pod` | SIGTERM while the pod is up | cgroup + 1 live process, 2 overlays, box dir, pod dir, registry entry | **clean** |
| `sigint_build` | SIGINT mid build step | nothing (handler runs) | clean |
| `bystander` | nothing — reaps **twice while a healthy run is live** | n/a | **clean**: no live pid killed, no live mount released, the run still returned SUCCESS |
| `double_reap` | nothing — reaps an already-clean environment twice | n/a | **clean**: no-op both times |

**7/7 clean, both with `reap()` called directly and with recovery performed
by a real later CRUCIBLE run.**

Leaking *at the moment of the crash* is expected and is reported, not hidden:
a SIGKILLed process cannot tidy up after itself. The contract is eventual
cleanup by the next run, and that is the mechanism the second column
exercises.

The `bystander` case is the one that matters most. Cleanup that is merely
aggressive passes every leak test and destroys production.

### Still unproven here

Concurrent *crashed* runs (two abandoned runs reaped in one pass), host-side
controller interruption, and cleanup under a full disk. The reaper is written
to handle them; they are not yet in the matrix.

## 8. Honest maturity label

**Working prototype with a verified execution path on four ecosystems.**

Not enterprise-ready, and not close on containment assurance. The execution
half demonstrably works — a Spring Boot Maven build, a Go buildpack, an npm
app and a Python app all really build and really run inside namespaces on a
real Linux kernel, with a real database sidecar and no Docker daemon. That is
a genuine capability and it survived contact with real repositories.

What is missing is not polish, it is evidence: a corpus large enough to make
a rate meaningful, repeated runs to show determinism, an adversarial suite
broad enough to make "contained" a claim rather than a hope, and a workspace
planner without which the monorepo half of the real world is out of scope.
