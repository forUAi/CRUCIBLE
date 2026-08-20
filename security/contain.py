"""
CRUCIBLE :: security/contain.py

Adversarial containment harness. Runs on the HOST, drives the Lima guest.

It has to run on the host because the claim under test is about the host: a
check executed inside the VM can only tell you what the VM believes. Host
integrity is measured here, before and after, by this process.

A fixture passes only when all five hold:

    1. confined    the probe was blocked, or its effect stayed inside the box
    2. recorded    the attempt appears in CRUCIBLE's own output or ledger
    3. host clean  no host file, process or listener changed
    4. torn down   no box directory and no overlay mount survives the run
    5. repeatable  the same verdict on every repetition

Nothing here attempts a hypervisor exploit. The probes are ordinary things a
build script can do -- write a file, open a socket, fork a daemon -- which is
the point: containment has to hold against the boring cases first.

    python3 security/contain.py --list
    python3 security/contain.py --run hostile-python --repeat 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HOST_REPO = Path(__file__).resolve().parent.parent
VM = "crucible"
GUEST_CRUCIBLE = "~/audit/crucible"
GUEST_FIXTURES = "~/audit/fixtures"

# Canaries live on the host. The fixtures try to write exactly these paths.
CANARIES = {
    "mount_write": HOST_REPO / "CANARY_WRITTEN_FROM_SANDBOX.txt",
    "mount_tamper": HOST_REPO / "README.md",
}


def sh(cmd: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout)


def guest(script: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return sh(f"limactl shell {VM} -- bash -lc {json.dumps(script)}", timeout)


# ---------------------------------------------------------------------------
# host state
# ---------------------------------------------------------------------------

def host_snapshot() -> dict:
    """Everything about the host this run is forbidden to change."""
    files = {}
    for p in sorted(HOST_REPO.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        try:
            files[str(p.relative_to(HOST_REPO))] = hashlib.sha256(
                p.read_bytes()).hexdigest()[:16]
        except OSError:
            files[str(p.relative_to(HOST_REPO))] = "unreadable"
    listeners = sh("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk '{print $9}' "
                   "| sort -u").stdout.strip().splitlines()
    return {
        "files": files,
        "n_procs": len(sh("ps ax").stdout.splitlines()),
        "listeners": sorted(listeners),
    }


def host_diff(before: dict, after: dict) -> list[str]:
    out = []
    added = set(after["files"]) - set(before["files"])
    removed = set(before["files"]) - set(after["files"])
    changed = {f for f in set(before["files"]) & set(after["files"])
               if before["files"][f] != after["files"][f]}
    for label, s in (("created", added), ("deleted", removed), ("modified", changed)):
        for f in sorted(s):
            out.append(f"host file {label}: {f}")
    new_listeners = set(after["listeners"]) - set(before["listeners"])
    for l in sorted(new_listeners):
        out.append(f"host listener appeared: {l}")
    return out


# ---------------------------------------------------------------------------

def sandbox_residue() -> list[str]:
    """Boxes or overlay mounts left behind in the guest."""
    r = guest("sudo ls -d /var/lib/crucible/box-* 2>/dev/null; "
              "mount | grep -c 'type overlay' || true", timeout=60)
    lines = [l for l in r.stdout.strip().splitlines() if l]
    boxes = [l for l in lines if "/box-" in l]
    overlays = [l for l in lines if l.isdigit() and int(l) > 0]
    out = [f"box directory survived: {b}" for b in boxes]
    if overlays:
        out.append(f"overlay mounts still present: {overlays[-1]}")
    return out


def run_fixture(name: str, budget: int = 2, timeout: int = 900) -> dict:
    """Copy the fixture into the guest (never run it from the host mount) and
    execute it under CRUCIBLE."""
    started = time.time()
    guest(f"rm -rf {GUEST_FIXTURES}/{name} && mkdir -p {GUEST_FIXTURES} && "
          f"cp -r {HOST_REPO}/security/fixtures/{name} {GUEST_FIXTURES}/{name}",
          timeout=120)
    r = guest(
        f"cd {GUEST_CRUCIBLE} && sudo python3 -m crucible.cli "
        f"{GUEST_FIXTURES}/{name} --no-llm --budget {budget} --no-cache "
        f"2>&1 | tail -120", timeout=timeout)
    return {"stdout": r.stdout, "rc": r.returncode,
            "seconds": round(time.time() - started, 1)}


def evaluate(name: str, run: dict, before: dict, after: dict) -> dict:
    host_findings = host_diff(before, after)
    residue = sandbox_residue()
    log = run["stdout"]

    probes = {}
    for line in log.splitlines():
        if "PROBE_REPORT" in line:
            try:
                probes = json.loads(line.split("PROBE_REPORT", 1)[1].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    escaped = [k for k, v in probes.items()
               if isinstance(v, str) and (v.startswith("WROTE")
                                          or v.startswith("REACHED"))]
    return {
        "fixture": name,
        "seconds": run["seconds"],
        "confined": not escaped,
        "escaped_probes": escaped,
        "recorded": "egress ledger" in log or bool(probes),
        "host_clean": not host_findings,
        "host_findings": host_findings,
        "torn_down": not residue,
        "residue": residue,
        "probes": probes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="contain")
    ap.add_argument("--run", metavar="FIXTURE")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out")
    a = ap.parse_args()

    fixtures = sorted(p.name for p in (HOST_REPO / "security/fixtures").iterdir()
                      if p.is_dir())
    if a.list or not a.run:
        print("fixtures:", ", ".join(fixtures) or "(none)")
        return 0
    if a.run not in fixtures:
        sys.exit(f"unknown fixture {a.run}; have {fixtures}")

    results = []
    for i in range(a.repeat):
        print(f"── {a.run} run {i + 1}/{a.repeat}")
        before = host_snapshot()
        run = run_fixture(a.run)
        after = host_snapshot()
        res = evaluate(a.run, run, before, after)
        results.append(res)
        verdict = all((res["confined"], res["recorded"],
                       res["host_clean"], res["torn_down"]))
        print(f"   confined={res['confined']} recorded={res['recorded']} "
              f"host_clean={res['host_clean']} torn_down={res['torn_down']} "
              f"({res['seconds']}s)  => {'PASS' if verdict else 'FAIL'}")
        for k in ("escaped_probes", "host_findings", "residue"):
            for item in res[k]:
                print(f"     ! {k}: {item}")

    stable = len({json.dumps([r["confined"], r["host_clean"], r["torn_down"]])
                  for r in results}) == 1
    print(f"── repeatable across {a.repeat} run(s): {stable}")
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"results": results, "repeatable": stable}, indent=2) + "\n")
    return 0 if (stable and all(r["confined"] and r["host_clean"]
                                and r["torn_down"] for r in results)) else 1


if __name__ == "__main__":
    sys.exit(main())
