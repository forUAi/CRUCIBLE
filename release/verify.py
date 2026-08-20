"""
CRUCIBLE :: release/verify.py

Prove the artifact works, not the developer checkout.

Runs in the guest, as root. Extracts the tarball into a pristine directory,
checks every file against the manifest, creates a fresh virtualenv, and runs
the suites FROM THE EXTRACTED TREE -- never from the working copy. The whole
point is to catch a capability that only works because of something on the
developer's machine.

Two properties it is built to establish:

  * the tested tree IS the shipped tree. The tree hash is recomputed after
    the suites run, so a test that mutated the artifact is caught rather than
    quietly passing.
  * no undeclared state. `--isolate` strips the environment down and points
    the run at a private state root, so a suite that only passes because
    /var/lib/crucible was already warm fails here.

    sudo python3 release/verify.py --artifact dist/crucible-0.1.0.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

# Each suite: (name, argv, needs_root, why it is in a release gate)
SUITES = [
    ("preflight", [sys.executable, "-m", "crucible.cli", "--preflight", "."],
     True, "every containment capability is present before anything runs"),
    ("unit", [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
     False, "the analysis half, and every structural contract"),
    ("threat-model", [sys.executable, "security/tm_check.py"],
     False, "every claimed protection resolves to something real"),
    ("workspace-dev", [sys.executable, "bench/wsbench.py", "--split", "dev"],
     False, "workspace discovery against pinned external repositories"),
    ("lifecycle", [sys.executable, "security/lifecycle_test.py", "--case", "all"],
     True, "crash cleanup, including the bystander safety case"),
    ("execution", [sys.executable, "bench/execbench.py", "--repeat", "1",
                   "--out", "EVIDENCE/execution.json"],
     True, "four ecosystems actually build, launch and answer"),
    ("networking", [sys.executable, "security/netmodes.py",
                    "--out", "EVIDENCE/networking.json"], True,
     "hermetic denies egress, proxy routes through it, open permits it"),
    ("resources", [sys.executable, "security/resources.py", "--case", "all",
                   "--out", "EVIDENCE/resources.json"],
     True, "cpu, memory, pids, disk, timeout and two concurrent boxes"),
    # Three repetitions each, from independent fresh stores. One passing run
    # is a sample, not a property.
    ("adversarial-python", [sys.executable, "security/contain.py", "--repeat",
                            "3", "--run", "hostile-python"], True, "containment"),
    ("adversarial-node", [sys.executable, "security/contain.py", "--repeat",
                          "3", "--run", "hostile-node"], True, "containment"),
    ("adversarial-go", [sys.executable, "security/contain.py", "--repeat",
                        "3", "--run", "hostile-go"], True, "containment"),
    ("adversarial-java", [sys.executable, "security/contain.py", "--repeat",
                          "3", "--run", "hostile-java"], True, "containment"),
]


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(root)).encode())
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def extract(artifact: Path, into: Path) -> tuple[Path, dict]:
    with tarfile.open(artifact, "r:gz") as tf:
        names = tf.getnames()
        top = {n.split("/")[0] for n in names}
        if len(top) != 1:
            raise SystemExit(f"artifact has {len(top)} top-level entries; refusing")
        tf.extractall(into, filter="data")
    root = into / top.pop()
    manifest = json.loads((root / "MANIFEST.json").read_text())
    return root, manifest


def check_manifest(root: Path, manifest: dict) -> list[str]:
    bad = []
    for rel, want in manifest["files"].items():
        p = root / rel
        if not p.exists():
            bad.append(f"missing from artifact: {rel}")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != want:
            bad.append(f"content differs: {rel}")
    return bad


def clean_env(state_root: Path) -> dict:
    """A deliberately impoverished environment.

    Anything the release needs must be declared, not inherited. HOME is a
    fresh directory so a warm ~/.crucible plan cache cannot make a capability
    look supported.
    """
    home = state_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "CRUCIBLE_STATE": str(state_root / "state"),
        # Shared, and deliberately not the developer's home: immutable pulled
        # layers are legitimate warm cache, and rebuilding them inside each
        # ephemeral state root cost ~10 GB and most of the gate's runtime.
        "CRUCIBLE_IMAGES": os.environ.get("CRUCIBLE_IMAGES",
                                          "/var/lib/crucible-release-images"),
        "CRUCIBLE_CACHE": str(home / ".crucible" / "plans.json"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def teardown(workdir: Path, env: dict, keep: bool) -> None:
    """Release the state root before deleting the directory that holds it.

    rmtree cannot remove a mountpoint and, with ignore_errors, does not say
    so. Five gate runs each left a loop-mounted ext4 image behind, on a
    backing file rmtree had already unlinked: 5 GB of page cache in a 6 GiB
    VM, seven OOM kills, and a 14-second benchmark taking 2000 seconds.
    """
    state = Path(env["CRUCIBLE_STATE"])
    if subprocess.run(f"mountpoint -q {state}", shell=True).returncode == 0:
        dev = ""
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == str(state):
                dev = parts[0]
                break
        subprocess.run(f"umount -l {state}", shell=True, capture_output=True)
        if dev.startswith("/dev/loop"):
            subprocess.run(f"losetup -d {dev}", shell=True, capture_output=True)
        print(f"released state store {dev or state}")
    if keep:
        print(f"kept {workdir}")
        return
    shutil.rmtree(workdir, ignore_errors=True)
    if workdir.exists():
        print(f"  ! {workdir} survived removal; something is still mounted "
              f"inside it")


def main() -> int:
    ap = argparse.ArgumentParser(prog="verify")
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--only", help="comma-separated suite names")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--evidence", help="directory for full suite logs")
    a = ap.parse_args()

    artifact = Path(a.artifact).resolve()
    if not artifact.is_file():
        sys.exit(f"no such artifact: {artifact}")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sidecar = artifact.with_suffix(artifact.suffix + ".sha256")
    print(f"artifact  {artifact.name}")
    print(f"sha256    {digest}")
    if sidecar.exists():
        declared = sidecar.read_text().split()[0]
        if declared != digest:
            sys.exit(f"checksum mismatch: sidecar says {declared}")
        print("checksum  matches the published sidecar")

    workdir = Path(tempfile.mkdtemp(prefix="crucible-release-"))
    root, manifest = extract(artifact, workdir)
    print(f"version   {manifest['version']}  (commit {manifest['source_commit'][:12]}"
          f"{', DIRTY' if manifest.get('source_dirty') else ''})")

    problems = check_manifest(root, manifest)
    if problems:
        for p in problems[:10]:
            print(f"  ✗ {p}")
        sys.exit("artifact does not match its own manifest")
    print(f"manifest  {len(manifest['files'])} files verified")

    before = tree_hash(root)
    env = clean_env(workdir)
    is_root = os.geteuid() == 0

    # A fresh virtualenv, from the interpreter alone. CRUCIBLE is stdlib-only
    # by design, so this both provisions and *tests* that claim: an
    # accidental third-party import fails here.
    venv = workdir / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    py = str(venv / "bin" / "python")
    print(f"venv      {py}")

    # Outside the artifact: writing evidence into the tree under test would
    # change its hash and be reported as the artifact mutating itself.
    evidence = Path(a.evidence or (workdir.parent /
                                   f"crucible-evidence-{digest[:12]}"))
    evidence.mkdir(parents=True, exist_ok=True)
    print(f"evidence  {evidence}")

    wanted = set(a.only.split(",")) if a.only else None
    results = []
    for name, argv, needs_root, why in SUITES:
        if wanted and name not in wanted:
            continue
        if needs_root and not is_root:
            results.append({"suite": name, "outcome": "skipped",
                            "detail": "needs root", "why": why})
            print(f"  ~ {name:20} SKIPPED (needs root)")
            continue
        cmd = [py if arg is sys.executable else arg for arg in argv]
        cmd = [c.replace("EVIDENCE", str(evidence)) for c in cmd]
        t0 = time.time()
        r = subprocess.run(cmd, cwd=root, env=env, capture_output=True,
                           text=True, timeout=3600)
        secs = round(time.time() - t0, 1)
        ok = r.returncode == 0
        # The FULL output, to a file outside the artifact. A 400-character
        # tail lost the only record of why a target failed, and the failure
        # did not reproduce -- so it could never be attributed to CRUCIBLE,
        # the fixture, the repository or the machine. Evidence that survives
        # only on success is not evidence.
        log_path = evidence / f"{name}.log"
        log_path.write_text(r.stdout + r.stderr)
        results.append({"suite": name, "outcome": "pass" if ok else "FAIL",
                        "rc": r.returncode, "seconds": secs, "why": why,
                        "log": str(log_path),
                        "tail": (r.stdout + r.stderr)[-400:] if not ok else ""})
        print(f"  {'✓' if ok else '✗'} {name:20} "
              f"{'pass' if ok else 'FAIL rc=' + str(r.returncode):10} {secs:>7}s")
        if not ok:
            for line in (r.stdout + r.stderr).strip().splitlines()[-6:]:
                print(f"        {line[:110]}")

    after = tree_hash(root)
    mutated = before != after
    if mutated:
        print("  ✗ the artifact was MUTATED by its own test run; the tested "
              "tree is not the shipped tree")

    skipped = [r for r in results if r["outcome"] == "skipped"]
    failed = [r for r in results if r["outcome"] == "FAIL"]
    print("─" * 78)
    print(f"{len(results) - len(failed) - len(skipped)}/{len(results)} suites passed"
          + (f", {len(skipped)} SKIPPED" if skipped else "")
          + (f", {len(failed)} FAILED" if failed else ""))
    if skipped:
        print("  a skipped suite is not a pass; run as root for the full gate")

    if a.out:
        Path(a.out).write_text(json.dumps(
            {"artifact": str(artifact), "sha256": digest,
             "version": manifest["version"], "commit": manifest["source_commit"],
             "tree_hash_before": before, "tree_hash_after": after,
             "mutated": mutated, "evidence_dir": str(evidence),
             "suites": results}, indent=2) + "\n")
    teardown(workdir, env, a.keep)

    return 1 if (failed or skipped or mutated) else 0


if __name__ == "__main__":
    sys.exit(main())
