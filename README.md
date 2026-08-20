# CRUCIBLE

**An evidence-backed sandbox runtime for policy-controlled execution of
untrusted code.**

CRUCIBLE takes an unfamiliar repository, works out how to build and run it,
executes it inside a hardened boundary under an explicit network and resource
policy, verifies that it actually ran, records what it did, and tears
everything down.

CRUCIBLE v0.1.0 is verified from a reproducible release artifact in a fresh
Lima VM across Python, Node, Go and Java adversarial fixtures.

---

## Verified in this release

Every line below was produced by executing
[`release/verify.py`](release/verify.py) against the extracted release
artifact in a freshly provisioned VM with no host mounts. Raw output is in
[`evidence/v0.1.0/`](evidence/v0.1.0/).

| | |
|---|---|
| Release gates | **12 / 12 passed** as executed ([one caveat](BENCHMARKS.md#a-correction-to-the-workspace-gate)) |
| Adversarial language runs | **12 / 12 passed** (3 repetitions × 4 languages) |
| Execution ecosystems | **4 / 4 verified** — Python, Node, Go, Java |
| Resource controls | **6 / 6 enforced** |
| Network policy checks | **4 / 4 behaved as declared** |
| Lifecycle & crash-cleanup cases | **7 / 7 clean** |
| Concurrent boxes | independent memory boundaries demonstrated under simultaneous load |
| Reproducible build | rebuild from the verified commit was **byte-identical** |
| Leak inventory | before and after inventories were **byte-identical** |
| Threat model | **16 / 16** claims linked to an executed assertion |

Source commit `d05a8b21116639655e26525c62ba2587cab825ad` ·
artifact `crucible-0.1.0.tar.gz` ·
SHA-256 `db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae`

---

## The problem

Running an unfamiliar repository means answering two questions at once: *how
do I build and start this?* and *what will it do to my machine while I find
out?*

Most tooling answers the first by classification — detect the language, look
up a recipe, hope — and the second by trust. CRUCIBLE treats the first as a
**search problem with a verifier** (guessing wrong is fine if failure is
cheap and checkable) and the second as a **policy problem** (what a workload
may reach is declared, not assumed).

## Key capabilities

- **Plan inference with provenance.** Every conclusion cites the file and
  manifest field that produced it. Workspaces are discovered as a graph, so a
  monorepo yields a plan per runnable component rather than one guess at the
  root.
- **Execution without a Docker daemon.** OCI images are pulled directly;
  isolation is built from mount, pid, net, uts and ipc namespaces, overlayfs
  and cgroup v2.
- **Policy-controlled networking.** Hermetic, enterprise-proxy and open-lab
  modes, enforced where the namespace is created.
- **Per-box resource ceilings.** CPU, memory and process/thread limits
  applied through cgroups the workload actually belongs to.
- **Verified outcomes.** An archetype-specific oracle decides whether the
  thing really ran, and distinguishes that from sandbox failure, policy
  denial, resource exhaustion and machine contention.
- **Complete teardown**, including after SIGKILL.

## Architecture

```
repository or harness
      ↓
CRUCIBLE execution plan          evidence → workspace graph → plan, with provenance
      ↓
policy-controlled isolated box   namespaces + overlayfs + cgroups, network policy applied
      ↓
recorded outcome and evidence    oracle verdict, egress ledger, resource observations
      ↓
complete teardown                cgroup-owned processes, mounts, stores, namespaces
```

| Module | Role |
|---|---|
| `crucible/evidence.py` | deterministic fingerprinting; never guesses |
| `crucible/workspaces.py` | repository as a graph of runnable components |
| `crucible/planner.py` | evidence → run plan; author intent beats inference |
| `crucible/lint.py` | checks the plan against its own evidence before execution |
| `crucible/engine.py` | the build/run/verify/repair loop |
| `crucible/netpolicy.py` | network policy, enforced at the namespace boundary |
| `crucible/preflight.py` | refuses to start when a containment capability is absent |
| `crucible/lifecycle.py` | ownership registry, cgroup reaping, crash recovery |
| `crucible/diskbudget.py` | per-sandbox storage budgets via ext4 project quotas |
| `crucible/backends/namespace.py` | the sandbox substrate |

## Containment model

The boundary is the **guest kernel**, and ownership is recorded rather than
inferred:

- Every process a run starts joins a **cgroup created for that run**.
  Termination uses `cgroup.kill`, so it reaches exactly that run's members.
- A **run registry** records the cgroup, directories and mount sources,
  stamped with the owner's pid *and* that pid's start time, so a recycled pid
  can never authorise acting on an unrelated process.
- Cleanup never matches on a process name. A later run can reclaim what an
  earlier crashed run abandoned because it can prove the earlier owner is
  gone.

Trust boundaries, guest-kernel assumptions and the division of
responsibility between CRUCIBLE and the surrounding deployment platform are
in [THREAT_MODEL.md](THREAT_MODEL.md) and [SECURITY.md](SECURITY.md).

## Supported execution

Four ecosystems build, launch and answer a health probe inside the sandbox:

| Ecosystem | Verified target | Start command produced |
|---|---|---|
| Python | FastAPI service | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| Node | Express app | `node index.js` |
| Go | buildpack-based service | compiled binary |
| Java | Spring Petclinic | `java -jar target/spring-petclinic-*.jar` |

Maven and Gradle wrappers are preferred when a repository ships them, because
the JDK base images carry no build tool.

## Resource controls

Measured under adversarial load; full matrix in
[BENCHMARKS.md](BENCHMARKS.md).

| Control | Configured | Observed |
|---|---|---|
| Processes / threads | `pids.max=512` | stopped at 508 threads and 508 forks |
| Memory | `memory.max=512 MB` | 448 MB allocated, then SIGKILL inside the cgroup |
| CPU | `cpu.max=50%` of one core | throttled to 0.50 core-seconds per wall-second |
| Step timeout | `--step-timeout 5s` | step killed at 5.03 s |
| Concurrent boxes | 512 MB and 2048 MB | 448 MB and 1984 MB, independently bounded |

**Goroutines are not tasks.** 200,000 goroutines multiplex onto `GOMAXPROCS`
OS threads, so a pid cgroup never counts them; they are bounded by
`memory.max`. This is stated because the distinction matters when sizing a
Go workload.

### Step timeout contract

`--step-timeout` bounds the **execution step**, not the whole run. The
permitted termination grace — the interval between the deadline and the step
actually dying — is **1.0 s**, and was measured at **0.03 s**. Engine setup,
image pull, snapshot and teardown sit outside that deadline and are reported
separately.

### Disk control

CRUCIBLE applies a per-sandbox writable-storage budget using **ext4 project
quotas**, and `preflight` refuses to start when the quota stack is
unavailable rather than running with the budget silently unenforced.

In this release the quota stack is verified as **present and enforced as a
capability** (`preflight` gate, and the store is mounted `prjquota` with a
project id assigned per box). The adversarial write-beyond-quota fixture
exists at `security/fixtures/disk-bomb` but is **not part of the 12-gate
run**, so the published evidence covers capability verification rather than
an adversarial disk exhaustion result. See
[BENCHMARKS.md](BENCHMARKS.md#disk-control) for the exact scope.

## Network policy

| Mode | Build egress | Runtime egress | Use |
|---|---|---|---|
| `hermetic` | none | none | dependencies must already be cached |
| `proxy` | through the operator's `HTTP(S)_PROXY` | none by default | enterprise networks |
| `open` | unrestricted | none by default | laboratory and CI |

Enforcement is at the namespace boundary, so a repair rule that asks for
network under a restrictive policy is simply not given a route. `proxy` mode
passes an allowlist of proxy variables — never the host environment — and
**refuses to start** if no proxy is configured, rather than falling back to a
direct connection that would be reported as proxied.

Under `open`, outbound connections including direct-IP ones are permitted:
that is the selected policy, not a gap. CRUCIBLE does not attempt to prevent
an application from bypassing a corporate proxy by dialling an address
directly; that belongs to the enterprise network. See
[SECURITY.md](SECURITY.md).

## Lifecycle and cleanup

Seven cases pass, including SIGKILL while the pod holds a network namespace,
SIGKILL mid-build with mounts live, SIGTERM, SIGINT, a **bystander** case
proving a reap during a healthy concurrent run touches nothing of that run's,
and repeated reaping being safe.

Leaking *at the moment of a crash* is expected and reported — a SIGKILLed
process cannot tidy up after itself. The contract is eventual cleanup by the
next run, and that mechanism is exercised directly.

## Requirements

- **Linux guest** with cgroup v2 (`cpu`, `memory`, `pids` delegated),
  overlayfs, `unshare`/`nsenter`, and root or `CAP_SYS_ADMIN`.
- **Python 3.11+**, standard library only — no third-party runtime
  dependencies.
- `mkfs.ext4`, `losetup`, `mountpoint` for the layer store.
- `setquota`, `repquota`, `chattr` and the `quota_v2` kernel module for disk
  budgets.
- On macOS, a [Lima](https://lima-vm.io) VM provides the guest; a declared
  configuration is in [`.lima/crucible-release.yaml`](.lima/crucible-release.yaml).
- Network access to a registry for OCI base images (or a warm image cache).

`preflight` verifies all of this and refuses to run if a mandatory capability
is missing.

## Quick start

```bash
# 1. Provision the guest (macOS host)
limactl start --name=crucible .lima/crucible-release.yaml

# 2. Confirm the containment capabilities are present (guest, as root)
sudo python3 -m crucible.cli --preflight .

# 3. Plan a repository without executing it
python3 -m crucible.cli ./my-repo --plan-only

# 4. Discover runnable components in a monorepo
python3 -m crucible.cli ./my-monorepo --workspaces

# 5. Execute under an explicit policy (guest, as root)
sudo python3 -m crucible.cli ./my-repo \
     --network hermetic --mem 2048 --cpu-pct 100 --disk-mb 4096 \
     --step-timeout 600
```

### Minimal execution example

```bash
sudo python3 -m crucible.cli ./examples/py-fastapi --network open
```

```
▸ network policy  open (build=open, runtime=none)
  base: python:3.12-slim
  disk budget: 4096 MB (ext4 project 402119)
  limits: pids.max=512, memory.max=2147483648
  → install/pip: pip install --no-cache-dir -r requirements.txt
  pod netns up (egress cut, loopback live)
  ▸ sidecar postgres  postgres:16-alpine   ready 127.0.0.1:5432
  ▸ run  uvicorn main:app --host 0.0.0.0 --port 8000   (network CUT)
  ✓ port 8000 answered

SUCCESS  port 8000 answered   [15.1s, 1 attempt(s)]
  network policy: open (build=open, runtime=none)
```

### Policy configuration

Policy is explicit on every invocation and is recorded on every result and in
`crucible.lock.json`:

```bash
# Hermetic: no egress at any phase.
sudo python3 -m crucible.cli ./repo --network hermetic

# Enterprise proxy: the operator supplies the proxy; CRUCIBLE passes only
# proxy variables through, and refuses to start if none is configured.
sudo HTTPS_PROXY=http://proxy.corp:3128 \
     python3 -m crucible.cli ./repo --network proxy

# Open laboratory, with explicit runtime egress as well.
sudo python3 -m crucible.cli ./repo --network open --online-run
```

## Result classifications

CRUCIBLE distinguishes outcomes that other runners collapse into "failed".
**An unavailable executor or an inconclusive run is never reported as safe.**

| Outcome | Meaning |
|---|---|
| `SUCCESS` | the oracle observed the workload doing its job |
| `FAILED` | the repository did not build or start |
| `EXHAUSTED` | the sandbox hit a resource ceiling — the allowance, not the repository |
| policy-denied | a step was refused egress by the active network policy |
| timed out | the step exceeded its deadline; the clock, not the repository |
| `INCONCLUSIVE` | the machine was under pressure, or a probe produced no output — **not a pass and not a verdict about the repository** |
| `MEASUREMENT_FAILED` | the harness could not observe the operation under test |

## Integration

CRUCIBLE is designed to be driven by an external harness (such as PRISM's
Tier B/C execution) through a stable surface: a repository snapshot,
workspace selection, plan, resource limits and network policy in; a
classified outcome with evidence, timings, command hashes, resource
observations and cleanup status out.

The result contract above is the integration contract: a caller must be able
to tell "contained and verified" from "we could not tell". The stable API
module is not yet part of this release; today the CLI and
`crucible.lock.json` provide the same information.

## Benchmarks, reproduction and evidence

- [docs/DESIGN.md](docs/DESIGN.md) — the design rationale this was built from
- [BENCHMARKS.md](BENCHMARKS.md) — full matrices, timings and environment
- [REPRODUCING.md](REPRODUCING.md) — reproduce every result from scratch
- [THREAT_MODEL.md](THREAT_MODEL.md) — 16 claims, each tied to an executed assertion
- [SECURITY.md](SECURITY.md) — reporting, assumptions, deployment responsibilities
- [`evidence/v0.1.0/`](evidence/v0.1.0/) — raw machine-readable results
- [`evidence/v0.1.0/SHA256SUMS`](evidence/v0.1.0/SHA256SUMS) — checksums for every published file

```bash
# Verify the published evidence
cd evidence/v0.1.0 && shasum -a 256 -c SHA256SUMS
```

## Status

v0.1.0 is a first tagged release. Its verified scope is exactly what the
tables above record: four language ecosystems, six resource controls, four
network policy checks, seven lifecycle cases and twelve adversarial runs,
executed from a reproducible artifact in a fresh VM. Scope beyond that —
other ecosystems, other kernels, deployment-wide capacity management — is
not covered by this evidence and is described in
[THREAT_MODEL.md](THREAT_MODEL.md) and [SECURITY.md](SECURITY.md).
