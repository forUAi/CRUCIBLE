# Reproducing the v0.1.0 verification

Every published figure can be reproduced from source. This guide covers
provisioning a clean guest, building the artifact, verifying its hash,
running the full gate, running individual categories, and checking the
evidence.

Commands are labelled by where they run:

- **[host]** — your workstation (macOS in the reference environment)
- **[guest]** — inside the Lima VM
- **[artifact]** — inside the extracted release artifact, in the guest

Paths are repository-relative. Replace `<repo>` with your checkout and
`<user>` with the guest username where they appear.

---

## 0. Requirements

**[host]** — [Lima](https://lima-vm.io) 2.x and Python 3.11+.

```bash
limactl --version
python3 --version
```

The guest needs Linux with cgroup v2, overlayfs, and root. Everything else is
installed by the declared provisioning below.

---

## 1. Provision a clean guest

The verification VM is declared in `.lima/crucible-release.yaml`. It mounts
**nothing** from the host — the artifact is copied in, so nothing can be
imported from a developer checkout.

**[host]**

```bash
limactl start --name=crucible-release .lima/crucible-release.yaml --tty=false
limactl list
```

Provisioning installs only the declared package set: `python3`,
`python3-venv`, `util-linux`, `e2fsprogs`, `mount`, `ca-certificates`,
`curl`, `git`, `iproute2`, `procps`, `rsync`, `quota`, and
`linux-modules-extra-$(uname -r)`.

The last two matter: the Ubuntu cloud image ships neither `setquota` nor the
`quota_v2` module, and without them ext4 cannot enable project-quota
tracking, so per-sandbox disk budgets cannot be enforced.

Wait for provisioning to finish before checking capabilities — `cloud-init`
runs after the VM reports Running:

**[guest]**

```bash
cloud-init status --wait
```

---

## 2. Build the artifact and verify its hash

**[host]**

```bash
cd <repo>
python3 release/make.py --out dist/
shasum -a 256 dist/crucible-0.1.0.tar.gz
```

Expected for the tagged release, built from commit
`d05a8b21116639655e26525c62ba2587cab825ad`:

```
db248379ee3910c8c959b51d076cbe59711b5a75ec59fc3a79318b42ae6bb6ae
```

`release/make.py` exits non-zero if the working tree is dirty, because an
artifact that cannot be traced to a commit cannot be verified against one.

### Reproducibility check

```bash
python3 release/make.py --out /tmp/dist-a/
python3 release/make.py --out /tmp/dist-b/
shasum -a 256 /tmp/dist-a/*.tar.gz /tmp/dist-b/*.tar.gz
```

Both hashes must be identical. Determinism comes from fixed member mtimes,
fixed uid/gid, sorted entries, a fixed compression level, and an explicitly
fixed gzip header mtime.

---

## 3. Copy the artifact into the guest

Copied, never mounted.

**[host]**

```bash
limactl copy dist/crucible-0.1.0.tar.gz        crucible-release:/tmp/
limactl copy dist/crucible-0.1.0.tar.gz.sha256 crucible-release:/tmp/
```

**[guest]**

```bash
sha256sum /tmp/crucible-0.1.0.tar.gz
cut -d' ' -f1 /tmp/crucible-0.1.0.tar.gz.sha256
```

Both must match the host hash.

```bash
sudo rm -rf ~/release && mkdir -p ~/release
tar xzf /tmp/crucible-0.1.0.tar.gz -C ~/release
cd ~/release/crucible-0.1.0
```

> If a previous run left root-owned `__pycache__` directories, `rm` needs
> `sudo` — that is why the command above uses it.

---

## 4. Preflight

**[artifact]**

```bash
sudo python3 -m crucible.cli --preflight .
```

Every mandatory capability must report present. The command exits non-zero
and refuses to run if one is missing, rather than continuing with that
containment property silently absent.

---

## 5. Run the complete release gate

**[artifact]**

```bash
sudo python3 release/verify.py \
     --artifact /tmp/crucible-0.1.0.tar.gz \
     --evidence /tmp/evidence-final \
     --out /tmp/gate-final.json
```

This extracts the artifact into a pristine directory, verifies all 100 files
against the manifest, builds a fresh virtualenv, and runs every suite **from
the extracted tree** with a stripped environment and a private state root. The
tree hash is recomputed afterwards, so a suite that mutated the artifact is
caught.

Full per-suite logs land in `/tmp/evidence-final/`.

Expected: `12/12 suites passed`.

> **Note.** `workspace-dev` requires the pinned external corpus. In the
> tagged v0.1.0 artifact that gate exits 0 even when the corpus is absent,
> which is how it passed while scoring 0/17 — see
> [BENCHMARKS.md](BENCHMARKS.md#a-correction-to-the-workspace-gate). Later
> commits make it fetch its corpus and fail when it measures nothing.

---

## 6. Run individual categories

Each gate is a standalone command.

**[artifact]**, all as root except where noted:

```bash
# Unit and artifact-contract tests (no root needed)
python3 -m unittest discover -s tests

# Threat model: references resolve, and bind to an executed gate result
python3 security/tm_check.py
python3 security/tm_check.py --results /tmp/gate-final.json

# Lifecycle and crash cleanup (7 cases)
sudo python3 security/lifecycle_test.py --case all
sudo python3 security/lifecycle_test.py --case all --reap-with-run
sudo python3 security/lifecycle_test.py --case sigkill_pod

# Resource controls (6 controls)
sudo python3 security/resources.py --case all
sudo python3 security/resources.py --case concurrent
sudo python3 security/resources.py --case memory,cpu,timeout

# Network policy modes with positive and negative controls
sudo python3 security/netmodes.py

# Execution across four ecosystems
sudo python3 bench/execbench.py --repeat 1
sudo python3 bench/execbench.py --repeat 1 --only java

# Workspace discovery (fetches the pinned corpus)
python3 bench/wsbench.py --split dev --clone
python3 bench/wsbench.py --split validation --clone
```

---

## 7. Three-repetition adversarial matrix

Each repetition runs from an independent fresh store, so a snapshot hit
cannot skip the behaviour under test. A non-zero probe count is the evidence
that the hostile code actually executed.

**[artifact]**

```bash
sudo python3 security/contain.py --repeat 3 --run hostile-python
sudo python3 security/contain.py --repeat 3 --run hostile-node
sudo python3 security/contain.py --repeat 3 --run hostile-go
sudo python3 security/contain.py --repeat 3 --run hostile-java

sudo python3 security/contain.py --list
```

Each run reports `probes=N confined=True recorded=True host_clean=True
torn_down=True`. A run reporting `probes=0` is `INCONCLUSIVE`, never a pass.

---

## 8. Leak inventories

Take a census before and after, and diff them.

**[artifact]**

```bash
sudo python3 security/inventory.py --label before --out /tmp/before.json

# ... run whatever you want to measure ...

sudo python3 security/inventory.py --label after \
     --out /tmp/after.json --compare /tmp/before.json
```

The inventory counts a resource as CRUCIBLE's only on ownership evidence —
cgroup membership, a mount source or backing filename only CRUCIBLE writes,
or a directory a run recorded. It never matches on a process name.

Warm caches (OCI images, layer store, pinned fixtures, the live store mount)
are listed separately from leaks and are expected to persist.

For a byte-identical comparison:

```bash
sha256sum /tmp/before.json /tmp/after.json
```

---

## 9. Verify the published evidence

**[host]**, from the repository root:

```bash
cd evidence/v0.1.0
shasum -a 256 -c SHA256SUMS
```

`MANIFEST.json` records, for every published file, both the SHA-256 of the
published derivative and the SHA-256 of the **original** raw output, plus any
redaction applied. Redactions are mechanical only — ANSI escape stripping and
replacement of the ephemeral `mkdtemp` suffix — and cannot change a verdict,
a timing or a count.

---

## 10. Cleaning up

**[host]**

```bash
limactl stop crucible-release
limactl delete crucible-release
```

Attribute anything before deleting it. Inside the guest, the reaper reclaims
only what it can prove ownership of, and reports anything it cannot:

**[guest]**

```bash
sudo python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, '.')
from crucible import lifecycle
print(lifecycle.reap(Path('/var/lib/crucible')).summary())
"
```
