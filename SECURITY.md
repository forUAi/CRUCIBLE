# Security

## Reporting a vulnerability

Please report security issues through **GitHub private vulnerability
reporting** on this repository: open the **Security** tab and choose *Report
a vulnerability*. That channel is private to the maintainers until an
advisory is published.

If private reporting is not enabled for your account, open a
[Security Advisory](../../security/advisories) draft rather than a public
issue.

Please do not open a public issue for a suspected containment escape.

### What to include

A report is most actionable when it contains:

- The CRUCIBLE version or commit, and the artifact SHA-256 if you have it.
- Guest OS, kernel version, and cgroup version.
- The active **network policy** (`hermetic`, `proxy`, `open`) and the
  resource flags in use (`--mem`, `--cpu-pct`, `--disk-mb`,
  `--step-timeout`).
- Whether `preflight` reported every capability as present.
- A minimal fixture or repository that reproduces the behaviour.
- What you expected the boundary to prevent, and what you observed instead.
- Raw output where possible: the run log, `crucible.lock.json`, and a
  `security/inventory.py` snapshot from before and after.

### Disclosure

We ask for a reasonable period to investigate and ship a fix before public
disclosure, and we will keep you informed of progress. Please avoid testing
against systems or networks you do not own or have permission to test.

## Supported versions

| Version | Supported |
|---|---|
| v0.1.0 | ✅ current release |

CRUCIBLE is at its first tagged release; only the latest version receives
fixes.

## Security assumptions

These are the conditions CRUCIBLE's containment claims depend on. If one does
not hold, the corresponding claim does not either.

**The guest kernel is the boundary.** Isolation is built from Linux
namespaces, overlayfs and cgroup v2. A workload shares the guest kernel and
retains a full syscall surface. CRUCIBLE does **not** defend against a kernel
or hypervisor exploit from inside the guest. For workloads where that is part
of the threat model, run CRUCIBLE inside a VM boundary you are willing to
lose — which is how it is verified, in a disposable Lima VM — or use a
gVisor/Firecracker-class substrate underneath.

**The host is protected by the VM, not by CRUCIBLE.** On macOS the guest is a
Lima VM. The verification VM declares **no host mounts**; the release
artifact is copied in. A development configuration that mounts a host
directory into the guest weakens this, and the repository ships both so the
difference is visible.

**Privilege.** CRUCIBLE requires root or `CAP_SYS_ADMIN` in the guest for
mounts, namespaces and cgroups. `CAP_SYS_RESOURCE` is explicitly dropped
inside the sandbox — without that, a workload running as uid 0 writes
straight past a disk quota and can raise its own rlimits.

**Preflight is mandatory.** CRUCIBLE refuses to start when a containment
capability it advertises is unavailable, rather than running with that
property silently absent. Waivers exist but must be named explicitly on the
command line.

## Network policy semantics

The policy is declared per run, enforced where the network namespace is
created, and recorded on every result.

| Mode | Build egress | Runtime egress |
|---|---|---|
| `hermetic` | none | none |
| `proxy` | via the operator's `HTTP(S)_PROXY` only | none unless `--online-run` |
| `open` | unrestricted | none unless `--online-run` |

**What is enforced.** A step cannot obtain egress the active policy denies —
enforcement is at the namespace boundary, not at the caller, so a repair rule
that requests network under `hermetic` is simply not given a route. `proxy`
mode passes an allowlist of proxy variables and never the host environment,
so a token exported in the operator's shell does not reach the sandbox.
`proxy` with no proxy configured **refuses to start** rather than falling
back to a direct connection that would be reported as proxied.

**What is intentionally permitted by policy.** Under `open`, outbound
connections succeed, including connections to literal IP addresses. That is
the selected mode, not a containment gap.

**What is out of scope.** CRUCIBLE does not attempt to prevent an application
from bypassing a corporate proxy by dialling an address directly. Enforcing
that is the enterprise network's responsibility — a token implementation here
would advertise an enforcement that does not exist. Runtime DNS is not logged
because the runtime namespace has no route out; attempts are prevented rather
than observed.

## Division of responsibility

CRUCIBLE bounds **one sandbox at a time**. The deployment platform around it
owns everything aggregate.

| CRUCIBLE enforces | The deployment platform must enforce |
|---|---|
| Per-box CPU, memory and process/thread ceilings | Aggregate capacity across concurrent jobs |
| Per-box writable-storage budget | Total storage headroom and cache eviction |
| Per-step execution timeout | Job-level scheduling and queue limits |
| Per-run ownership and cleanup, including after SIGKILL | Node health, eviction and restart |
| The declared network policy for a run | The network perimeter, egress filtering, proxy enforcement |
| Refusal to start without its capabilities | Provisioning those capabilities |

There is **no global resource ceiling and no admission control**: N concurrent
boxes can still collectively fill the shared store. Capacity management is a
deployment concern.

## Evidence

Containment claims are enumerated in [THREAT_MODEL.md](THREAT_MODEL.md), each
tied to an implementation, a named assertion, and an executed suite result
against a specific artifact hash. Raw results are in
[`evidence/v0.1.0/`](evidence/v0.1.0/); scope and limits of what was measured
are in [BENCHMARKS.md](BENCHMARKS.md).
