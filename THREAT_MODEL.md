# CRUCIBLE threat model

Operational, not decorative. Its job is to answer one question for an
engineer: *does this change need a new test?*

Every claim below carries an `EVIDENCE:` line naming the test that proves it,
or is marked `UNVERIFIED`. `security/tm_check.py` fails if a claim names a
test that does not exist, so this file cannot drift into fiction quietly.

```bash
python3 security/tm_check.py                       # references resolve
python3 security/tm_check.py --results release.json  # ...and actually ran
```

`EVIDENCE:` names the code. `ASSERTS:` names the suite and the specific
assertion inside it that must have **executed and passed**, in a recorded
release run, against a named artifact hash. A file that exists is not
evidence that anything ran; `--results` is the difference between the two.

---

## Assets

| Asset | Why it matters |
|---|---|
| The host machine | Developer laptop or CI runner. Compromise here is total. |
| Host credentials | SSH agent, cloud tokens, registry logins, browser sessions. |
| CRUCIBLE's control plane | The engine process, the run registry, the reaper. Subvert it and containment decisions are made by the attacker. |
| Other tenants' jobs | Concurrent runs, their sandboxes and their results. |
| The shared store | Layer cache, base images, evidence. Denial here stops everyone. |
| Evidence | Lock files, ledgers, step logs. Worthless if the workload can forge or suppress them. |

## Untrusted inputs

Everything from the repository: source, manifests, lockfiles, Dockerfiles,
devcontainer JSON, build scripts, package lifecycle hooks, build-tool
plugins, and anything fetched during the dependency phase. Also the
repository's *output* — step logs are attacker-controlled text.

## Attacker capabilities

Assume the repository executes arbitrary code as uid 0 inside the sandbox,
during build and at runtime, and that it is trying to escape. It can fork,
write files, open sockets, spawn daemons that outlive their parent, emit
misleading or malformed output, and take as long as it likes.

It cannot (by assumption, and this is the boundary) exploit a kernel or
hypervisor vulnerability. That is the residual risk in §Accepted.

## Trust boundaries

```
  host                          ← trusted
  ─────────── Lima / vz ───────  boundary 1: virtual machine
  guest kernel                  ← trusted by the guest, shared by all sandboxes
  ─────────── namespaces ──────  boundary 2: mount/pid/net/uts/ipc + cgroup
  sandbox (uid 0)               ← hostile
```

Boundary 1 is the security boundary. Boundary 2 is a *containment* boundary:
it separates jobs from each other and bounds their resources, and it is not
claimed to withstand a kernel exploit.

**Host responsibilities.** Provide the VM, hold no secrets inside it, and
never execute repository code. **Guest responsibilities.** Namespaces,
cgroups, quotas, the reaper, the egress ledger. **Enterprise-network
responsibilities.** Whether traffic may leave at all, and whether an
application may bypass a corporate proxy by dialling an IP directly. That is
a network control, and CRUCIBLE does not duplicate it.

## Build time vs runtime

They are different policies and must not be conflated.

| | Build | Runtime |
|---|---|---|
| Network | reachable (dependencies must resolve) | none — the pod netns has no route out |
| Attacker goal | exfiltrate, poison the cache, persist | reach the host, reach other jobs |
| Current control | **observed, not restricted** | namespace with no route |

---

## Claims

### C1 — Repository code never executes on the host
Every command runs under `unshare` inside the guest.
EVIDENCE: `security/contain.py`
ASSERTS: adversarial-python::host_clean

### C2 — A repository cannot reach host storage
Even when the operator points CRUCIBLE at a path on a host-shared mount, the
source is copied to guest-local storage before it becomes an overlay lower.
EVIDENCE: `tests/test_planning.py::TestContainmentStaging`, `crucible/containment.py`, `security/contain.py`
ASSERTS: unit::TestContainmentStaging

### C3 — Writes outside the workspace stay inside the box
Writing `/etc` or `/root` succeeds and lands in the overlay upper, which dies
with the box. That is containment working, not an escape.
EVIDENCE: `security/contain.py`, `security/fixtures/hostile-python`
ASSERTS: adversarial-python::escape_workspace_write

### C4 — Runtime egress is impossible
EVIDENCE: `security/fixtures/hostile-python/app.py`, `security/contain.py`
ASSERTS: adversarial-node::runtime_egress

### C5 — A repository cannot exhaust the shared store
Per-sandbox ext4 project quota, enforced by the kernel, with
`CAP_SYS_RESOURCE` dropped so uid 0 cannot bypass it.
EVIDENCE: `security/fixtures/disk-bomb`, `crucible/diskbudget.py`
ASSERTS: execution::disk-budget

### C6 — A repository cannot silence its own diagnosis
Step logs live outside the quota'd tree, so exhausting the budget does not
prevent recording why.
EVIDENCE: `crucible/backends/namespace.py`, `security/fixtures/disk-bomb/fill.py`
ASSERTS: execution::disk-budget

### C7 — A killed engine leaks nothing permanently
Ownership is recorded as cgroup membership plus a registry stamped with the
owner's pid and start time; a later run reclaims what a crashed one left.
EVIDENCE: `security/lifecycle_test.py`, `crucible/lifecycle.py`
ASSERTS: lifecycle::sigkill_pod

### C8 — Cleanup never touches anything that is not ours
`cgroup.kill` cannot reach a non-member; the registry's start-time stamp
prevents acting on a recycled pid.
EVIDENCE: `security/lifecycle_test.py`, `crucible/lifecycle.py`
ASSERTS: lifecycle::bystander

### C9 — A build step cannot run forever
The deadline is enforced independently of output, and the supervisor reads a
file rather than a pipe a descendant can hold open.
EVIDENCE: `tests/test_backend_contract.py`, `crucible/backends/namespace.py`
ASSERTS: unit::TestBackendContract

### C10 — Evidence cannot be forged by malformed output
Probe output is ANSI-stripped and JSON-parsed; unparseable output yields
`INCONCLUSIVE`, never `PASS`.
EVIDENCE: `security/contain.py`
ASSERTS: adversarial-node::malformed_output

### C11 — No host secrets are present in the sandbox
EVIDENCE: `security/fixtures/hostile-python/probe.py`, `security/contain.py`
ASSERTS: adversarial-java::credentials

### C12 — Build-time egress follows the declared policy
Build connectivity is a *policy*, not a defect. Three modes, each with a
control in both directions: `hermetic` denies egress and the run fails naming
the policy; `open` permits it; `proxy` routes through the operator's proxy and
the recording proxy shows the pypi traffic; `proxy` with nothing configured
refuses rather than falling back to a direct connection.

**Not claimed:** CRUCIBLE does not stop an application dialling an IP directly
instead of using the proxy. That is the enterprise network's control, and a
token version here would advertise enforcement that does not exist. Under
`open` — the default — `outbound_hardcoded_ip` reaching `1.1.1.1:443` is the
declared behaviour, not a gap.
EVIDENCE: `security/netmodes.py`, `crucible/netpolicy.py`
ASSERTS: networking::hermetic_denies_and_is_named

### C13 — Process and thread counts are bounded
`pids.max` counts tasks, and the probe now exceeds it: 4000 threads and 2000
forks attempted against a 512 ceiling, stopped at 508 of each.

**Limitation:** goroutines are not tasks. 200,000 of them multiplex onto
GOMAXPROCS OS threads, so `pids.max` never sees them; they are bounded by
`memory.max` instead, which is measured separately.
EVIDENCE: `crucible/backends/namespace.py`, `security/resources.py`
ASSERTS: resources::processes_enforced_508_of_512

### C14 — Concurrent jobs are independently bounded
Two boxes run simultaneously with separate state roots and separate cgroups:
one at `memory.max=512 MB` allocated 448 MB, the other at 2048 MB allocated
1984 MB, both SIGKILL(9) inside their own cgroup. Neither consumed the other's
allocation and both runs completed.

**Limitation:** this proves per-box independence, not a global ceiling. The
only aggregate bound is the size of the shared store, and N boxes can still
collectively fill it. There is no admission control.
EVIDENCE: `security/resources.py`
ASSERTS: resources::concurrent_boxes_independently_bounded

### C15 — Java, Go and Node repositories are contained as well as Python
Four adversarial fixtures, each probing its own ecosystem's surface: npm
`preinstall` hooks, Go's stdlib exec/syscall/goroutines, JVM build-plugin
execution with OS threads and no SecurityManager, and Python. Three
repetitions each from independent fresh stores, all with non-zero probe
counts, host clean and torn down.
EVIDENCE: `security/fixtures/hostile-java`, `security/fixtures/hostile-go`, `security/fixtures/hostile-node`, `security/contain.py`
ASSERTS: adversarial-java::three_repetitions_pass

### C16 — The shipped artifact behaves like the developer checkout
The gate extracts the artifact, verifies all 100 files against its manifest,
builds a fresh venv, and runs every suite from the extracted tree with a
stripped environment. The tree hash is recomputed afterwards, so a suite that
mutated the artifact is caught. Contract tests read the tree they run inside,
which under the gate is the artifact.
EVIDENCE: `release/verify.py`, `tests/test_artifact_contract.py`
ASSERTS: unit::artifact_contract_tests

---

## Intentionally unsupported

- Defending against a kernel or hypervisor exploit from inside the guest.
- Repositories that require credentials, a GPU, systemd, or Windows/macOS.
- Preventing an application from bypassing a corporate proxy by dialling an
  IP address directly. That is the enterprise network's job, by product
  decision, and adding a token check here would claim an enforcement that
  does not exist.

## Accepted risks

| Risk | Why accepted |
|---|---|
| Guest kernel is shared by all sandboxes | Namespaces are the containment boundary; the VM is the security boundary. A kernel exploit escapes to the guest, not to the host. |
| Build-time exfiltration | Dependencies must resolve. Mitigated by recording, not prevention, until C12 lands. |
| Socket sampling is lossy | 15 ms poll; it is a lower bound and says so. The DNS ledger is exact. |
| The VM is reused across runs | Boxes are disposable; the VM is not re-created per job. A guest-level compromise persists until the VM is recreated. |

## Failure behaviour

Fail closed and say so. A budget that cannot be enforced reports
`UNAVAILABLE` with a reason rather than being treated as enforced. A backend
that cannot name its own processes is refused. An adversarial run with no
captured probes is `INCONCLUSIVE`. Resource exhaustion is `EXHAUSTED`, which
is not the same result as a broken repository.

## When a change needs a new test

Add one if the change: crosses a trust boundary; creates a process, mount,
namespace or file that outlives a step; relaxes a limit; adds a way for the
repository to influence CRUCIBLE's own output; or moves a claim from
UNVERIFIED to claimed.
