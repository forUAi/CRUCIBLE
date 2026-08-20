"""
CRUCIBLE :: security/lifecycle_test.py

Crash-cleanup proof. Runs in the guest, as root.

A successful shutdown proves nothing about crash cleanup -- the code that
tidies up is exactly the code a crash skips. So every case here kills the
engine somewhere it has live state, and then asks whether anything survived.

Inventory is taken by ownership evidence only:

    processes   membership in a cgroup CRUCIBLE created (kernel-tracked)
    mounts      overlay whose SOURCE is a name only CRUCIBLE writes
    dirs        paths named in the run registry
    cgroups     /sys/fs/cgroup/crucible-*

No pattern matching against process names. A `pgrep -f "sleep 86400"` would
find the pause container and also anyone's backup script; twice in this
project a pgrep pattern matched the inspection command that contained it.

    sudo python3 security/lifecycle_test.py --case all
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import lifecycle                                  # noqa: E402
from crucible.backends.namespace import STATE_ROOT              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = str(ROOT / "examples/py-fastapi")


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def inventory() -> dict:
    cgroups = {}
    if lifecycle.cgroup_available():
        for g in sorted(lifecycle.CGROUP_ROOT.glob("crucible-*")):
            cgroups[g.name] = lifecycle.cgroup_members(g.name)
    return {
        "cgroups": cgroups,
        "owned_processes": sorted(p for m in cgroups.values() for p in m),
        "overlay_mounts": [mp for _s, mp in lifecycle.overlay_mounts_named()],
        "box_dirs": sorted(str(d) for d in STATE_ROOT.glob("box-*")),
        "pod_dirs": sorted(str(d) for d in (STATE_ROOT / "pods").glob("*"))
                    if (STATE_ROOT / "pods").is_dir() else [],
        "registry": sorted(r.run_id for r in lifecycle.Registry(STATE_ROOT).load_all()),
    }


def leaked(inv: dict) -> list[str]:
    out = []
    for name, members in inv["cgroups"].items():
        out.append(f"cgroup {name} ({len(members)} live process(es))"
                   if members else f"cgroup {name} (empty)")
    out += [f"overlay mount {m}" for m in inv["overlay_mounts"]]
    out += [f"box dir {d}" for d in inv["box_dirs"]]
    out += [f"pod dir {d}" for d in inv["pod_dirs"]]
    out += [f"registry entry {r}" for r in inv["registry"]]
    return out


def wipe() -> None:
    """Fresh environment. Uses the reaper itself where it can, force elsewhere."""
    lifecycle.reap(STATE_ROOT, log=lambda *_: None)
    for name in [g.name for g in lifecycle.CGROUP_ROOT.glob("crucible-*")] \
            if lifecycle.cgroup_available() else []:
        lifecycle.cgroup_kill(name)
        lifecycle.cgroup_remove(name)
    for _s, mp in lifecycle.overlay_mounts_named():
        subprocess.run(f"umount -l {mp}", shell=True, capture_output=True)
    for d in list(STATE_ROOT.glob("box-*")) + list((STATE_ROOT / "pods").glob("*")):
        shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(STATE_ROOT / "runs", ignore_errors=True)
    shutil.rmtree(STATE_ROOT / "layers", ignore_errors=True)
    shutil.rmtree(STATE_ROOT / "plans", ignore_errors=True)


# ---------------------------------------------------------------------------
# running and crashing
# ---------------------------------------------------------------------------

def start_engine(repo: str = REPO, extra: list[str] | None = None):
    log = open("/tmp/lifecycle-run.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "crucible.cli", repo,
         "--no-llm", "--budget", "2", "--no-cache", "--step-timeout", "300",
         *(extra or [])],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT), log


def wait_for(pred, timeout: float, interval: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def pod_is_up() -> bool:
    """A pause container is live inside one of our cgroups.

    Detected by cgroup membership, not by looking for `sleep`: the point is
    that we can identify our own processes without guessing.
    """
    if not lifecycle.cgroup_available():
        return False
    for g in lifecycle.CGROUP_ROOT.glob("crucible-*"):
        for pid in lifecycle.cgroup_members(g.name):
            try:
                if "sleep" in Path(f"/proc/{pid}/comm").read_text():
                    return True
            except OSError:
                pass
    return False


def steps_running() -> bool:
    return any(lifecycle.cgroup_members(g.name)
               for g in lifecycle.CGROUP_ROOT.glob("crucible-*"))


CASES: dict[str, dict] = {
    "normal":        dict(kill=None, wait=None,
                          why="baseline: graceful completion"),
    "sigkill_pod":   dict(kill=9, wait=pod_is_up,
                          why="SIGKILL while the pause container holds a netns"),
    "sigkill_build": dict(kill=9, wait=steps_running,
                          why="SIGKILL during a build step, mounts live"),
    "sigterm_pod":   dict(kill=15, wait=pod_is_up,
                          why="SIGTERM while the pod is up"),
    "sigint_build":  dict(kill=2, wait=steps_running,
                          why="SIGINT during a build step"),
}


def run_case(name: str, spec: dict, reap_with_run: bool) -> dict:
    wipe()
    before = inventory()
    proc, log = start_engine()

    killed = False
    if spec["kill"] is not None:
        if wait_for(spec["wait"], timeout=180):
            os.kill(proc.pid, spec["kill"])
            killed = True
        else:
            proc.kill()
            log.close()
            return {"case": name, "outcome": "setup_failed",
                    "detail": "never reached the state under test"}
    try:
        proc.wait(timeout=240)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
    log.close()

    time.sleep(1.0)
    after_crash = inventory()
    survived = leaked(after_crash)

    # The contract is EVENTUAL cleanup: a killed process cannot tidy up after
    # itself, so a later run must. That later run is the mechanism, and it has
    # to be exercised, not assumed.
    recovered, rep = [], None
    if survived:
        if reap_with_run:
            p2, l2 = start_engine()
            p2.wait(timeout=300)
            l2.close()
        else:
            rep = lifecycle.reap(STATE_ROOT, log=lambda *_: None)
        time.sleep(0.5)
        recovered = leaked(inventory())

    return {
        "case": name,
        "why": spec["why"],
        "killed": killed,
        "signal": spec["kill"],
        "survived_crash": survived,
        "after_recovery": recovered,
        "outcome": "clean" if not recovered else "LEAKED",
        "reaped": rep.summary() if rep else ("via a later run" if survived else "n/a"),
    }


def case_bystander() -> dict:
    """A live run must survive a reap. This is the dangerous direction.

    Cleanup that is merely aggressive passes every leak test and destroys
    production. Here a healthy run is in flight, holding a cgroup, mounts and
    a pod, while reap() executes -- twice, because repeated cleanup has to be
    safe too. Its resources must be untouched and it must still succeed.
    """
    wipe()
    proc, log = start_engine()
    if not wait_for(pod_is_up, timeout=180):
        proc.kill(); log.close()
        return {"case": "bystander", "outcome": "setup_failed",
                "detail": "run never reached the pod phase"}

    before = inventory()
    victim_cgroups = {k: v for k, v in before["cgroups"].items() if v}
    rep1 = lifecycle.reap(STATE_ROOT, log=lambda *_: None)
    rep2 = lifecycle.reap(STATE_ROOT, log=lambda *_: None)   # idempotent?
    after = inventory()

    harmed = []
    for name, members in victim_cgroups.items():
        still = set(lifecycle.cgroup_members(name))
        gone = [p for p in members if p not in still]
        if name not in after["cgroups"]:
            harmed.append(f"reaper deleted the live run's cgroup {name}")
        elif gone:
            harmed.append(f"reaper killed live pids {gone} in {name}")
    for mp in before["overlay_mounts"]:
        if mp not in after["overlay_mounts"]:
            harmed.append(f"reaper unmounted the live run's {mp}")

    rc = proc.wait(timeout=300)
    log.close()
    out = Path("/tmp/lifecycle-run.log").read_text(errors="replace")
    survived_ok = "SUCCESS" in out
    if not survived_ok:
        harmed.append(f"the live run did not complete (rc={rc})")
    if rep1.processes_killed or rep2.processes_killed:
        harmed.append(f"reap killed {rep1.processes_killed}+{rep2.processes_killed} "
                      f"processes while a run was healthy")

    return {"case": "bystander", "why": "reap twice while a healthy run is live",
            "outcome": "clean" if not harmed else "LEAKED",
            "survived_crash": [], "after_recovery": harmed,
            "reaped": f"skipped_live={rep1.skipped_live}, "
                      f"second pass reaped {rep2.runs_reaped}"}


def case_double_reap() -> dict:
    """Reaping an already-clean environment must be a no-op, not an error."""
    wipe()
    a = lifecycle.reap(STATE_ROOT, log=lambda *_: None)
    b = lifecycle.reap(STATE_ROOT, log=lambda *_: None)
    bad = []
    if a.anything() or b.anything():
        bad.append(f"reap acted on an empty environment: {a.summary()} / {b.summary()}")
    if leaked(inventory()):
        bad.append("wipe left state behind")
    return {"case": "double_reap", "why": "repeated cleanup is safe",
            "outcome": "clean" if not bad else "LEAKED",
            "survived_crash": [], "after_recovery": bad, "reaped": "no-op"}


EXTRA = {"bystander": case_bystander, "double_reap": case_double_reap}


def main() -> int:
    ap = argparse.ArgumentParser(prog="lifecycle_test")
    ap.add_argument("--case", default="all")
    ap.add_argument("--reap-with-run", action="store_true",
                    help="recover by starting a real run rather than calling reap()")
    ap.add_argument("--out")
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("must run as root (mounts and cgroups)")

    names = (list(CASES) + list(EXTRA)) if a.case == "all" else [a.case]
    results = []
    for n in names:
        res = EXTRA[n]() if n in EXTRA else run_case(n, CASES[n], a.reap_with_run)
        results.append(res)
        mark = {"clean": "✓", "LEAKED": "✗"}.get(res["outcome"], "!")
        print(f"{mark} {n:16} {res['outcome']:12} {res.get('why', '')}")
        for s in res.get("survived_crash", []):
            print(f"      survived the crash: {s}")
        for s in res.get("after_recovery", []):
            print(f"      STILL LEAKED after recovery: {s}")

    print("─" * 88)
    bad = [r for r in results if r["outcome"] != "clean"]
    print(f"{len(results) - len(bad)}/{len(results)} cases clean"
          + (f"   FAILURES: {[r['case'] for r in bad]}" if bad else ""))
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=2) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
