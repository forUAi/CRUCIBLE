# CRUCIBLE v0.1.0 — benchmark report

Every figure here was produced by executing `release/verify.py` against the
extracted release artifact in a freshly provisioned Lima VM. Raw
machine-readable output is in [`evidence/v0.1.0/`](evidence/v0.1.0/); each
table below is derived from the JSON file named beside it.

---

## Release identity

| | |
|---|---|
| Date | 2026-08-20 |
| Source commit | `d05a8b21116639655e26525c62ba2587cab825ad` |
| Artifact | `crucible-0.1.0.tar.gz` (100 files) |
| Artifact SHA-256 | `db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae` |
| Evidence bundle SHA-256 | `dbaf519a3958c109de05e0de6708d1e0409147e852c6279fd0441c03e4d0e64d` |
| Artifact mutated by its own tests | `false` |

## Environment

Raw: [`evidence/v0.1.0/environment.json`](evidence/v0.1.0/environment.json)

| | |
|---|---|
| Host | macOS 26.3.1, Apple silicon |
| Virtualisation | Lima 2.2.0, `vmType: vz` (Apple Virtualization.framework) |
| Guest OS | Ubuntu 24.04.4 LTS |
| Kernel | 6.8.0-137-generic, aarch64 |
| CPUs / memory | 4 vCPU / 5910 MB |
| cgroup | v2 (`cgroup2fs`), controllers `cpuset cpu io memory pids` |
| Python | 3.12.3 (standard library only) |
| Host mounts into the guest | **0** — the artifact is copied in, never mounted |

The verification VM is declared in
[`.lima/crucible-release.yaml`](.lima/crucible-release.yaml) and provisions
only its declared package set. It is deliberately separate from the
development VM, which does have a writable host mount.

## Release-verification command

```bash
# macOS host
python3 release/make.py --out dist/
limactl copy dist/crucible-0.1.0.tar.gz crucible-release:/tmp/
limactl copy dist/crucible-0.1.0.tar.gz.sha256 crucible-release:/tmp/

# Lima guest
tar xzf /tmp/crucible-0.1.0.tar.gz -C ~/release
cd ~/release/crucible-0.1.0
sudo python3 release/verify.py \
     --artifact /tmp/crucible-0.1.0.tar.gz \
     --evidence /tmp/evidence-final \
     --out /tmp/gate-final.json
```

---

## Release gate — 12/12

Raw: [`evidence/v0.1.0/gate-final.json`](evidence/v0.1.0/gate-final.json)

| # | Gate | Proves | Result | Wall |
|---|---|---|---|---|
| 1 | `preflight` | every containment capability present before anything runs | pass | 0.0 s |
| 2 | `unit` | 116 tests, including artifact-contract checks | pass | 0.1 s |
| 3 | `threat-model` | every claim resolves to real code and a suite | pass | 0.0 s |
| 4 | `workspace-dev` | workspace discovery against pinned external repositories | pass\* | 0.0 s |
| 5 | `lifecycle` | crash cleanup, including the bystander safety case | pass | 62.9 s |
| 6 | `execution` | four ecosystems build, launch and answer | pass | 566.5 s |
| 7 | `networking` | three policy modes with positive and negative controls | pass | 47.4 s |
| 8 | `resources` | cpu, memory, pids, timeout, goroutines, concurrency | pass | 136.0 s |
| 9 | `adversarial-python` | containment, 3 repetitions | pass | 50.2 s |
| 10 | `adversarial-node` | containment, 3 repetitions | pass | 42.2 s |
| 11 | `adversarial-go` | containment, 3 repetitions | pass | 77.4 s |
| 12 | `adversarial-java` | containment, 3 repetitions | pass | 46.7 s |

Every gate ran from the **extracted artifact**, never the developer checkout.
The artifact's tree hash was recomputed after the suites finished and was
unchanged. No gate was skipped, retried or weakened.

\* `workspace-dev` passed without measuring anything — the fresh VM has no
copy of the pinned corpus, so all 17 repositories returned `not_fetched` and
the benchmark exited 0 anyway. See
[the correction below](#a-correction-to-the-workspace-gate). Eleven of the
twelve gates measured what they claim to.

---

## Adversarial language matrix — 12/12

Raw: `evidence/v0.1.0/adversarial-{python,node,go,java}.log`

Each fixture probes surfaces specific to its ecosystem: npm `preinstall`
lifecycle hooks; Go's stdlib `exec`, raw syscalls and goroutines; JVM
build-plugin execution with OS threads and no SecurityManager; and Python.
Every repetition runs from an independent fresh store, so a snapshot hit
cannot skip the behaviour under test — a non-zero probe count is the evidence
that the hostile code actually executed.

| Language | Rep | Probes | Confined | Recorded | Host clean | Torn down | Wall | Result |
|---|---|---|---|---|---|---|---|---|
| Python | 1 | 11 | ✓ | ✓ | ✓ | ✓ | 17.2 s | **PASS** |
| Python | 2 | 11 | ✓ | ✓ | ✓ | ✓ | 16.7 s | **PASS** |
| Python | 3 | 11 | ✓ | ✓ | ✓ | ✓ | 16.7 s | **PASS** |
| Node | 1 | 11 | ✓ | ✓ | ✓ | ✓ | 13.7 s | **PASS** |
| Node | 2 | 11 | ✓ | ✓ | ✓ | ✓ | 13.7 s | **PASS** |
| Node | 3 | 11 | ✓ | ✓ | ✓ | ✓ | 13.7 s | **PASS** |
| Go | 1 | 12 | ✓ | ✓ | ✓ | ✓ | 25.7 s | **PASS** |
| Go | 2 | 12 | ✓ | ✓ | ✓ | ✓ | 25.3 s | **PASS** |
| Go | 3 | 12 | ✓ | ✓ | ✓ | ✓ | 25.7 s | **PASS** |
| Java | 1 | 11 | ✓ | ✓ | ✓ | ✓ | 34.9 s | **PASS** |
| Java | 2 | 11 | ✓ | ✓ | ✓ | ✓ | 15.2 s | **PASS** |
| Java | 3 | 11 | ✓ | ✓ | ✓ | ✓ | 15.2 s | **PASS** |

Java repetition 1 at 34.9 s against 15.2 s for repetitions 2 and 3 is the
cold OCI pull of `eclipse-temurin:21-jdk`; the image cache is warm from the
second repetition onward.

### Forged-output rejection

The Node fixture deliberately emits ANSI escapes, an OSC title sequence, and
a **forged `PROBE_REPORT` line** claiming a host escape. In every repetition
the harness reported the true value rather than the planted one. Claimed
escapes are corroborated against the host snapshot, which the fixture cannot
write.

---

## Execution matrix — 4/4

Raw: [`evidence/v0.1.0/execution.json`](evidence/v0.1.0/execution.json)

| Ecosystem | Target (pinned) | Outcome | Wall | Start command |
|---|---|---|---|---|
| Python | `examples/py-fastapi` (in-artifact) | verified | 15.1 s | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| Node | `heroku/node-js-getting-started` @ `63c6674c` | verified | 12.7 s | `node index.js` |
| Go | `heroku/go-getting-started` @ `3e3b414d` | verified | 63.4 s | compiled binary |
| Java | `spring-projects/spring-petclinic` @ `88e37c15` | verified | 475.3 s | `java -jar target/spring-petclinic-4.0.0-SNAPSHOT.jar` |

External targets are fetched from pinned full commit SHAs, not branch tips.

### Cold vs warm, and a preserved anomaly

Times vary substantially with cache state, and the Java target is the most
sensitive because it pulls a JDK image and populates a Maven repository:

| Java run | Conditions | Wall | Outcome |
|---|---|---|---|
| First cold gate on the fresh VM | no image cache, no layers | 105.7 s | **failed** |
| Warm, development VM | image + layers cached | 132.6 s | verified |
| Image cache deliberately cleared | cold pull, warm store | 130.2 s | verified |
| Final gate, fresh VM | ephemeral state root, shared image cache | 475.3 s | verified |

**The 105.7 s failure is preserved here deliberately and is not explained.**
It occurred on the first cold gate run. That gate retained only a 400-character
tail of the suite output, so the cause cannot be attributed to CRUCIBLE, the
fixture, the repository or the machine. It did not recur in the three
subsequent runs above. Full per-suite output capture was added afterwards
(`release/verify.py --evidence`), so a future occurrence would be
attributable; it is not retroactively explainable.

The 475.3 s figure in the final gate is also unexplained variance against
132.6 s for the same target and commit. It passed, and the spread is
recorded rather than averaged away.

---

## Resource-control matrix — 6/6

Raw: [`evidence/v0.1.0/resources.json`](evidence/v0.1.0/resources.json)

| Control | Configured | Attempted | Observed | Kernel response | Verdict |
|---|---|---|---|---|---|
| Processes / threads | `pids.max=512` | 4000 threads, 2000 forks | **508 threads, 508 forks** | `RuntimeError` at 508 | **ENFORCED** |
| Memory | `memory.max=512 MB` | 16384 MB, every page touched | **448 MB** | SIGKILL (9) inside the cgroup | **ENFORCED** |
| CPU | `cpu.max=50%` of one core | busy spin on 4 visible CPUs | **0.50 core-s per wall-s** | throttled | **ENFORCED** |
| Step timeout | `--step-timeout 5 s` | a step that runs longer | **step 5.03 s** (grace 0.03 s) | SIGKILL to the process group | **ENFORCED** |
| Goroutines | `pids.max` applies to tasks only | 200,000 goroutines | 4 OS threads, 90 MB heap | never counted by `pids.max` | **bounded by `memory.max`** |
| Concurrent boxes | 512 MB and 2048 MB | both allocate 16 GB simultaneously | **448 MB and 1984 MB** | each SIGKILLed in its own cgroup | **ENFORCED** |

Each measurement drops the layer store first, so a snapshot hit cannot skip
the probe. Probes report per-control as soon as each result is known, because
a single summary at the end is the first casualty of the OOM kill the memory
probe exists to provoke.

### Why these numbers were previously invalid

An earlier revision reported these controls as enforced when they were not.
`_cgroup_attach` ran *after* `Popen`, and `unshare --fork` had already forked
its child into the root cgroup — so only the `unshare` parent was ever a
member. `pids.max` read 512 while `pids.current` peaked at **1**, and a Java
fixture inside that sandbox started 2000 OS threads uncapped. The child now
joins its cgroup in `preexec_fn`, before `exec`, so every descendant inherits
membership, and `_cgroup_setup` reads the limits back and warns when one did
not take.

### Step timeout contract

`--step-timeout` bounds the **execution step**, not the run. Permitted
termination grace — the interval between the deadline and the step actually
dying — is **1.0 s**; measured **0.03 s**. The 7.6 s total wall time for that
case is engine setup, image handling, snapshot and teardown around the step,
which the flag does not claim to bound. An earlier report conflated the two
and described a 5 s limit as completing in 7.7 s.

### Disk control

The per-sandbox disk budget uses ext4 project quotas. `preflight` treats the
quota stack (`setquota`, `repquota`, `chattr`, `quota_v2`) as **mandatory**
and refuses to start without it, so a budget is never silently unenforced.

**Scope of the published evidence:** the `preflight` gate verifies the quota
stack is present and functional, and each run reports the project id and
budget it applied (`disk budget: 4096 MB (ext4 project 402119)`). An
adversarial write-beyond-quota fixture exists at
`security/fixtures/disk-bomb`, but **it is not one of the six controls in the
`resources` gate**, so this release publishes *capability verification* for
disk control rather than an adversarial exhaustion result.

---

## Networking matrix — 4/4

Raw: [`evidence/v0.1.0/networking.json`](evidence/v0.1.0/networking.json)

| Mode | Expected | Observed | Result |
|---|---|---|---|
| `hermetic` | install fails, policy named as the cause | denied and failed, attributed to the policy | **PASS** |
| `open` | install succeeds | SUCCESS | **PASS** |
| `proxy` | succeeds **and** the proxy sees the traffic | proxy recorded `pypi.org:443`, `files.pythonhosted.org:443` | **PASS** |
| `proxy`, unconfigured | refuses rather than falling back | refused | **PASS** |

The proxy case uses a recording CONNECT proxy, so "routed through the proxy"
is demonstrated by the proxy's own log rather than inferred from success. The
unconfigured case is the important negative control: a silent fallback to a
direct connection would succeed, be labelled `proxy`, and have bypassed it.

Enforcement is at the namespace boundary. A test drives the real `_r_dns`
repair — which sets `network=True` on every step in the plan — and then
asserts the sandbox still refused to provide a route.

**Direct-IP egress under `open` is the selected policy, not a gap.** CRUCIBLE
does not attempt to stop an application dialling an address instead of using
a configured proxy; that is the enterprise network's control. See
[SECURITY.md](SECURITY.md).

---

## Lifecycle matrix — 7/7

Raw: [`evidence/v0.1.0/lifecycle.log`](evidence/v0.1.0/lifecycle.log)

| Case | What it kills | Result |
|---|---|---|
| `normal` | nothing (baseline) | clean |
| `sigkill_pod` | SIGKILL while the pause container holds a netns | clean |
| `sigkill_build` | SIGKILL mid build step, mounts live | clean |
| `sigterm_pod` | SIGTERM while the pod is up | clean |
| `sigint_build` | SIGINT mid build step | clean |
| `bystander` | nothing — reaps twice **while a healthy run is live** | clean |
| `double_reap` | nothing — reaps an already-clean environment twice | clean |

Leaking *at the moment of the crash* is expected and is reported rather than
hidden: a SIGKILLed process cannot tidy up after itself. The contract is
eventual cleanup by the next run, and the recovery path is exercised both by
calling `reap()` directly and by starting a real subsequent run.

`bystander` is the case that matters most. Cleanup that is merely aggressive
passes every leak test and destroys production; this asserts that a reap
during a healthy concurrent run kills none of its processes, releases none of
its mounts, and leaves it able to complete.

---

## Concurrent-box isolation

Raw: [`evidence/v0.1.0/resources.json`](evidence/v0.1.0/resources.json)
(`two concurrent boxes`)

Two boxes executing **simultaneously**, each with its own state root, backing
store, ext4 project and cgroup:

| Box | `memory.max` | Allocated before the kernel intervened | Kernel response |
|---|---|---|---|
| 0 | 512 MB | **448 MB** | SIGKILL (9) inside its own cgroup |
| 1 | 2048 MB | **1984 MB** | SIGKILL (9) inside its own cgroup |

Both runs completed. Neither box consumed the other's allocation, and one
reaching its ceiling did not alter the other's classification.

### Root cause of the previous zero measurement

This case previously reported `0 MB and 0 MB` and classified itself
`INCONCLUSIVE`, which reads like caution. Two independent defects, and the
obvious one was not responsible:

1. **Backing-store collision.** The store image path was
   `STATE_ROOT.parent / "crucible-store.img"`, so every state root under
   `/var/lib` — `crucible`, `crucible-conc-0`, `crucible-conc-1` — resolved
   to one file. The second box mounted the first box's ext4 a second time and
   neither had an independent store. The name now encodes `STATE_ROOT.name`.
   Fixing this alone did **not** change the measurement.
2. **The measurement itself.** The concurrent case omitted `--verbose`, so
   the engine never streamed step output and the probe's report lines never
   reached the harness. A harness that cannot see its own probe reported zero
   and called it a result.

Both are covered by regression tests, and the verdict now distinguishes
`MEASUREMENT_FAILED` from `UNENFORCED` and `INCONCLUSIVE`. A run whose step
came from the layer cache is rejected outright: a snapshot hit skips the
operation under test and emits nothing, which is otherwise indistinguishable
from a limit that was never applied.

---

## Leak inventory

Raw: [`before-final.json`](evidence/v0.1.0/before-final.json) ·
[`after-final.json`](evidence/v0.1.0/after-final.json)

Both inventories hash to `b3f60e846130318a…` — **byte-identical** before and
after the full 12-gate run, which includes SIGKILL, timeout and concurrent
execution.

Checked, by ownership evidence rather than by name: loop devices, mounts with
deleted backing files, overlay and store mounts, CRUCIBLE cgroups, network
namespaces and their holder processes, pause processes, box state
directories, pod directories, run-registry records, release temporary
directories, concurrent-test state, and host escape canaries.

**Zero attributable leaks.**

### Retained resources — expected, not leaks

| Resource | Count | Why it persists |
|---|---|---|
| OCI image cache | 7 entries | content-addressed and immutable; shared across state roots by design |
| Layer store | 3 entries | content-addressed build layers; what makes a repaired plan affordable |
| Pinned benchmark fixtures | 3 | pinned repository checkouts for the execution matrix |
| Live store mount | 1 loop device | the working store, backing file present and in use |

These are listed separately from leaks. Deleting a legitimate immutable cache
to make an inventory look empty would be a worse outcome than the leak it
hides.

---

## Reproducible build

```bash
python3 release/make.py --out dist-a/
python3 release/make.py --out dist-b/
shasum -a 256 dist-a/*.tar.gz dist-b/*.tar.gz
```

Two builds from source commit `d05a8b21116639655e26525c62ba2587cab825ad`
produced **byte-identical** artifacts:

```
db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae
db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae
```

Determinism comes from fixed member mtimes, fixed uid/gid, sorted entries,
a fixed compression level, and an explicitly fixed gzip header mtime — the
last of which was a real defect: gzip writes its own timestamp, so fixing tar
member times alone was not enough and two builds of one tree differed.

---

## Threat-model verification

Raw: [`evidence/v0.1.0/threat-model.log`](evidence/v0.1.0/threat-model.log)

```
against artifact crucible-0.1.0 sha256:db248379ee3910c8…
16 claims: 16 PROVEN by an executed suite, 0 UNVERIFIED, 0 broken
```

Each claim names its implementation (`EVIDENCE:`) and the gate suite and
assertion that must have executed and passed (`ASSERTS:`) in a recorded
release run against a named artifact hash. A file that exists is not evidence
that anything ran. Full text: [THREAT_MODEL.md](THREAT_MODEL.md).

---

## Workspace discovery

Raw: [`evidence/v0.1.0/workspace-dev.log`](evidence/v0.1.0/workspace-dev.log)

### A correction to the workspace gate

**The `workspace-dev` gate passed in the verified run without measuring
anything.** The fresh verification VM has no copy of the pinned external
corpus, so all 17 development-split repositories returned `not_fetched`, and
`wsbench` exited 0 regardless. The gate was therefore recorded as a PASS for
a run that scored **0/17**.

This is stated plainly because it is the same vacuous-pass failure this
project rejects elsewhere, and it was found while preparing this report
rather than during the run. The substantive result for artifact
`db248379…` is **11 gates that measured something, plus one that did not**.

`wsbench` has since been corrected to exit non-zero when nothing was scored,
when any pinned repository is missing, and when any graded check fails — none
of which it previously did. The release gate now invokes it with `--clone` so
it fetches its own corpus. **That correction is not present in the tagged
`v0.1.0` artifact**, which was built before the defect was found.

### Measured results, on a machine with the corpus present

Measured against a pinned external corpus of 32 repositories partitioned by
hash into development, validation and holdout sets, plus a sealed set
reserved for the next checkpoint.

| Split | Repositories scored | Graded checks | Accuracy |
|---|---|---|---|
| development | 17 / 17 | 47 | 100 % |
| validation | 9 / 9 | 25 | 100 % |

Accuracy and **labelling coverage are reported separately**, because 100 %
accuracy on a partially labelled subset does not mean complete validation:
38 checks on the development split and 20 on validation remain unlabelled and
are skipped rather than counted as passes. Label coverage is 69 % on
development and 69 % on validation.

The checkpoint-1 holdout is recorded as **consumed** — it ran once, disagreed
on one repository, and that disagreement was adjudicated into a corrected
label, so it can no longer measure generalisation. A new 12-repository sealed
split is reserved and the benchmark refuses to run it.

---

## Reproducing any of this

See [REPRODUCING.md](REPRODUCING.md) for step-by-step instructions covering
VM provisioning, artifact build and hash verification, preflight, the full
gate, individual gate categories, the three-repetition adversarial matrix,
leak inventories and the reproducibility check.

```bash
# Verify the published evidence matches its manifest
cd evidence/v0.1.0 && shasum -a 256 -c SHA256SUMS
```
