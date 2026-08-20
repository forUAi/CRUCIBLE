**An evidence-backed sandbox runtime for policy-controlled execution of
untrusted code.**

CRUCIBLE v0.1.0 is verified from a reproducible release artifact in a fresh
Lima VM across Python, Node, Go and Java adversarial fixtures.

## Verified scope

| | |
|---|---|
| Release gates | **12 / 12 passed** from the artifact ([one caveat](../blob/main/BENCHMARKS.md#a-correction-to-the-workspace-gate)) |
| Adversarial language runs | **12 / 12** — 3 repetitions × Python, Node, Go, Java |
| Execution ecosystems | **4 / 4** built, launched and answered a health probe |
| Resource controls | **6 / 6** enforced |
| Network policy checks | **4 / 4** behaved as declared |
| Lifecycle & crash-cleanup | **7 / 7** clean, including SIGKILL while a netns is held |
| Concurrent boxes | independent memory boundaries under simultaneous load |
| Reproducible build | rebuild from the verified commit was **byte-identical** |
| Leak inventory | before and after inventories were **byte-identical** |
| Threat model | **16 / 16** claims bound to an executed assertion |

## Release identity

```
source commit    d05a8b21116639655e26525c62ba2587cab825ad
artifact         crucible-0.1.0.tar.gz
artifact sha256  db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae
evidence sha256  dbaf519a3958c109de05e0de6708d1e0409147e852c6279fd0441c03e4d0e64d
```

The `v0.1.0` tag points at the exact source that produced the artifact above.
Commits after it carry publication documentation, curated evidence and a
correction to the workspace benchmark; **none of them produced this
artifact**.

## Tested language matrix

Each repetition runs from an independent fresh store, so a cache hit cannot
skip the behaviour under test. A non-zero probe count is the evidence that
the hostile code actually executed.

| Language | Repetitions | Probes each | Result |
|---|---|---|---|
| Python | 3 | 11 | PASS — confined, recorded, host clean, torn down |
| Node | 3 | 11 | PASS — including rejection of a forged probe report |
| Go | 3 | 12 | PASS |
| Java | 3 | 11 | PASS |

## Observed resource enforcement

| Control | Configured | Observed |
|---|---|---|
| Processes / threads | `pids.max=512` | stopped at 508 threads and 508 forks |
| Memory | `memory.max=512 MB` | 448 MB, then SIGKILL inside the cgroup |
| CPU | `cpu.max=50%` of one core | 0.50 core-seconds per wall-second |
| Step timeout | `--step-timeout 5s` | step killed at 5.03 s (grace bound 1.0 s) |
| Concurrent boxes | 512 MB and 2048 MB | 448 MB and 1984 MB, independently bounded |

Goroutines are not tasks: 200,000 of them multiplex onto `GOMAXPROCS` OS
threads, so a pid cgroup never counts them; they are bounded by `memory.max`.

## Scope statement

What this release demonstrates is exactly the tables above: four language
ecosystems, six resource controls, four network policy checks, seven
lifecycle cases and twelve adversarial runs, executed from a reproducible
artifact in a freshly provisioned VM with no host mounts.

The guest kernel is the containment boundary; CRUCIBLE does not defend
against a kernel or hypervisor exploit from inside the guest. It bounds one
sandbox at a time — aggregate capacity across concurrent jobs belongs to the
deployment platform. Under the `open` network policy, outbound connections
including direct-IP ones are permitted by design.

## Documentation

- [Benchmarks](../blob/main/BENCHMARKS.md) — full matrices, timings, environment, preserved anomalies
- [Threat model](../blob/main/THREAT_MODEL.md) — 16 claims, each tied to an executed assertion
- [Security](../blob/main/SECURITY.md) — assumptions, network semantics, reporting
- [Reproducing](../blob/main/REPRODUCING.md) — reproduce every result from scratch
- [Evidence](../tree/main/evidence/v0.1.0) — raw machine-readable results and `SHA256SUMS`

## Verifying the assets

```bash
shasum -a 256 crucible-0.1.0.tar.gz
# db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae

tar xzf crucible-v0.1.0-evidence.tar.gz
cd evidence/v0.1.0 && shasum -a 256 -c SHA256SUMS
```
