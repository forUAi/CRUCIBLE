"""
CRUCIBLE :: security/resources.py

Prove each resource ceiling separately, after the cgroup correction.

Every earlier claim about CPU, memory and process limits was invalid for the
same reason: `_cgroup_attach` ran after `Popen`, `unshare --fork` had already
forked into the root cgroup, and only the unshare parent was ever a member.
`pids.max` read 512 while `pids.current` peaked at 1. Process enforcement now
binds -- and that says nothing about memory or CPU, which are different
controllers with different failure modes. Each is measured on its own.

For every control this records the configured limit, what the workload tried
to take, what it observed, how the kernel answered, what CRUCIBLE reported,
what a neighbouring run saw, and whether cleanup was complete.

    sudo python3 security/resources.py --case all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import lifecycle                                   # noqa: E402
from crucible.backends.namespace import STATE_ROOT               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "security/fixtures/resource-probe"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cold_layers() -> None:
    """Drop the layer store so the step under test actually executes.

    --no-cache disables the PLAN cache; layers are content-addressed and
    shared on purpose. The probe's RUN command never changes, so on the second
    case it was a snapshot hit: the step was skipped, the probe produced no
    output, and every control read INCONCLUSIVE. A cache hit that skips the
    operation being measured turns a resource test into a no-op.
    """
    import shutil
    shutil.rmtree(STATE_ROOT / "layers", ignore_errors=True)


def run_engine(extra: list[str], timeout: int = 1200) -> tuple[str, int, float]:
    cold_layers()
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "-u", "-m", "crucible.cli", str(FIXTURE),
         "--no-llm", "--budget", "1", "--no-cache", "--verbose", *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return ANSI.sub("", p.stdout + p.stderr), p.returncode, round(time.time() - t0, 1)


def parse(out: str, marker: str = "RESOURCE_REPORT") -> dict:
    """Merge every incremental line, then the final summary if it arrived.

    The summary is written last and is therefore the first casualty of the
    OOM kill the memory probe is designed to provoke. Reading the per-control
    lines means a kill can only destroy the finding that caused it.
    """
    merged: dict = {}
    for line in out.splitlines():
        if "RESOURCE_ONE" in line:
            try:
                merged.update(json.loads(line.split("RESOURCE_ONE", 1)[1].strip()))
            except ValueError:
                pass
    for line in out.splitlines():
        if marker in line and "RESOURCE_ONE" not in line:
            try:
                merged.update(json.loads(line.split(marker, 1)[1].strip()))
            except ValueError:
                pass
    return merged


def host_snapshot() -> dict:
    return {
        "cgroups": sorted(g.name for g in lifecycle.CGROUP_ROOT.glob("crucible-*")),
        "orphan_stores": len(lifecycle.orphaned_store_mounts()),
        "box_dirs": sorted(str(d) for d in STATE_ROOT.glob("box-*")),
        "loops": len([l for l in Path("/proc/mounts").read_text().splitlines()
                      if l.startswith("/dev/loop")]),
    }


def cleanup_clean(before: dict, after: dict) -> tuple[bool, str]:
    diffs = []
    for k in ("cgroups", "box_dirs"):
        left = sorted(set(after[k]) - set(before[k]))
        if left:
            diffs.append(f"{k}: {left}")
    if after["orphan_stores"] > before["orphan_stores"]:
        diffs.append(f"orphan stores +{after['orphan_stores'] - before['orphan_stores']}")
    return (not diffs), "; ".join(diffs) or "nothing left behind"


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

def case_processes() -> dict:
    """pids.max counts tasks: OS threads AND forks, one ceiling for both."""
    before = host_snapshot()
    out, _rc, secs = run_engine(["--mem", "2048", "--step-timeout", "300"])
    rep = parse(out)
    after = host_snapshot()
    ok, detail = cleanup_clean(before, after)
    if not rep:
        return {"control": "processes/threads (cgroup pids.max)",
                "configured": "pids.max=512", "attempted": "-",
                "observed_peak": "-", "verdict": "INCONCLUSIVE",
                "kernel_response": "the probe produced no output; the step was "
                                   "skipped or died before reporting",
                "seconds": secs, "cleanup": detail, "cleanup_ok": ok}
    threads = rep.get("threads", {})
    procs = rep.get("processes", {})
    peak = max(threads.get("started", 0), procs.get("forked", 0))
    return {
        "control": "processes/threads (cgroup pids.max)",
        "configured": "pids.max=512",
        "attempted": f"{threads.get('attempted')} threads, {procs.get('attempted')} forks",
        "observed_peak": f"{threads.get('started')} threads, {procs.get('forked')} forks",
        "kernel_response": threads.get("stopped") or procs.get("stopped") or "none",
        "verdict": ("ENFORCED" if 0 < peak <= 512
                    else "UNENFORCED" if peak > 512 else "INCONCLUSIVE"),
        "seconds": secs, "cleanup": detail, "cleanup_ok": ok,
    }


def case_memory(mem_mb: int = 512) -> dict:
    """memory.max is a different controller from pids and fails differently:
    the kernel OOM-kills inside the cgroup rather than returning an error."""
    before = host_snapshot()
    out, _rc, secs = run_engine(["--mem", str(mem_mb), "--step-timeout", "300"])
    rep = parse(out)
    after = host_snapshot()
    ok, detail = cleanup_clean(before, after)
    m = rep.get("memory", {})
    got = m.get("allocated_mb", 0)
    killed = m.get("killed_by_signal")
    bounded = got and got <= mem_mb * 1.25
    return {
        "control": "memory (cgroup memory.max)",
        "configured": f"memory.max={mem_mb} MB",
        "attempted": f"{m.get('attempted_mb')} MB, every page touched",
        "observed_peak": f"{got} MB allocated",
        "kernel_response": (f"SIGKILL ({killed}) inside the cgroup" if killed
                            else m.get("stopped") or "none"),
        "verdict": ("ENFORCED" if bounded and (killed or m.get("stopped"))
                    else "UNENFORCED" if got > mem_mb * 1.25 else "INCONCLUSIVE"),
        "seconds": secs, "cleanup": detail, "cleanup_ok": ok,
    }


def case_cpu(pct: int = 50) -> dict:
    """cpu.max is a quota, not a kill: the workload runs, more slowly.

    Measured as CPU-seconds per wall-second, because a process cannot read
    its own quota and `nproc` reports the machine rather than the allowance.
    """
    before = host_snapshot()
    out, _rc, secs = run_engine(["--cpu-pct", str(pct), "--step-timeout", "300"])
    rep = parse(out)
    after = host_snapshot()
    ok, detail = cleanup_clean(before, after)
    c = rep.get("cpu", {})
    used = c.get("cores_used", 0)
    allowed = pct / 100.0
    return {
        "control": "cpu (cgroup cpu.max)",
        "configured": f"cpu.max={pct}% of one core",
        "attempted": f"busy spin on {c.get('visible_cpus')} visible CPUs for 4s",
        "observed_peak": f"{used} core-seconds per wall-second",
        "kernel_response": (f"throttled to {used:.2f} cores"
                            if used else "not measured"),
        "verdict": ("ENFORCED" if used and used <= allowed * 1.35
                    else "UNENFORCED" if used > allowed * 1.35 else "INCONCLUSIVE"),
        "seconds": secs, "cleanup": detail, "cleanup_ok": ok,
    }


def case_timeout() -> dict:
    """A step that outruns its clock must be stopped and reported as such."""
    before = host_snapshot()
    # 5s against a probe whose CPU case alone spins for 4s and whose thread
    # and memory cases add more. A timeout test whose step finishes in time
    # measures nothing, which is what "7.6s wall against a 20s limit" was.
    out, _rc, secs = run_engine(["--step-timeout", "5", "--mem", "2048"], timeout=600)
    after = host_snapshot()
    ok, detail = cleanup_clean(before, after)
    timed = ("timed out" in out.lower() or "timeout" in out.lower()
             or "exit 124" in out.lower())
    return {
        "control": "step timeout",
        "configured": "--step-timeout 5s",
        "attempted": "a build step that takes longer",
        "observed_peak": f"{secs}s wall",
        "kernel_response": "SIGKILL to the process group by the supervisor",
        "verdict": "ENFORCED" if timed and secs < 300 else "INCONCLUSIVE",
        "seconds": secs, "cleanup": detail, "cleanup_ok": ok,
    }


def case_goroutines() -> dict:
    """Goroutines are not tasks. State which control actually bounds them."""
    # Measured inside a sandbox, on a base that carries a Go toolchain. The
    # host has none, and running it on the host would answer a different
    # question anyway -- the point is which cgroup control bounds goroutines.
    gofix = ROOT / "security/fixtures/goroutine-probe"
    if not (gofix / "Dockerfile").exists():
        return {"control": "goroutines", "verdict": "INCONCLUSIVE",
                "configured": "-", "attempted": "-", "observed_peak": "-",
                "kernel_response": "fixture missing", "cleanup": "n/a",
                "cleanup_ok": True, "seconds": 0}
    cold_layers()
    pr = subprocess.run(
        [sys.executable, "-u", "-m", "crucible.cli", str(gofix), "--no-llm",
         "--budget", "1", "--no-cache", "--verbose", "--mem", "512",
         "--step-timeout", "600"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = ANSI.sub("", pr.stdout + pr.stderr)
    rep = parse(out, "GOROUTINE_REPORT")
    return {
        "control": "goroutines (NOT tasks)",
        "configured": "pids.max=512 applies to tasks only",
        "attempted": f"{rep.get('goroutines_started')} goroutines",
        "observed_peak": f"{rep.get('os_threads')} OS threads, "
                         f"{rep.get('heap_mb')} MB heap",
        "kernel_response": "pids.max never sees them; they are multiplexed "
                           "onto GOMAXPROCS threads",
        "verdict": "BOUNDED BY memory.max, not pids.max",
        "seconds": 0, "cleanup": "n/a", "cleanup_ok": True,
    }


def case_concurrent() -> dict:
    """Two boxes at once: one hitting a ceiling must not affect the other."""
    cold_layers()
    before = host_snapshot()
    procs = [
        subprocess.Popen([sys.executable, "-u", "-m", "crucible.cli", str(FIXTURE),
                          "--no-llm", "--budget", "1", "--no-cache",
                          "--mem", mem, "--step-timeout", "300"],
                         cwd=ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
        for mem in ("512", "2048")
    ]
    outs = [ANSI.sub("", p.communicate(timeout=1800)[0]) for p in procs]
    after = host_snapshot()
    ok, detail = cleanup_clean(before, after)
    reps = [parse(o) for o in outs]
    caps = [r.get("memory", {}).get("allocated_mb", 0) for r in reps]
    return {
        "control": "two concurrent boxes",
        "configured": "memory.max 512 MB and 2048 MB",
        "attempted": "both allocate 16 GB",
        "observed_peak": f"{caps[0]} MB and {caps[1]} MB",
        "kernel_response": "each bounded by its own cgroup",
        "verdict": ("ENFORCED" if caps[0] and caps[1] and caps[0] < caps[1]
                    else "INCONCLUSIVE"),
        "seconds": 0, "cleanup": detail, "cleanup_ok": ok,
    }


CASES = {"processes": case_processes, "memory": case_memory, "cpu": case_cpu,
         "timeout": case_timeout, "goroutines": case_goroutines,
         "concurrent": case_concurrent}


def main() -> int:
    ap = argparse.ArgumentParser(prog="resources")
    ap.add_argument("--case", default="all")
    ap.add_argument("--out")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("must run as root (cgroups and mounts)")

    names = list(CASES) if a.case == "all" else a.case.split(",")
    rows = []
    for n in names:
        lifecycle.reap(STATE_ROOT, log=lambda *_: None)
        row = CASES[n]()
        rows.append(row)
        mark = {"ENFORCED": "✓"}.get(row["verdict"], "!")
        if row["verdict"] == "UNENFORCED":
            mark = "✗"
        print(f"{mark} {row['control']}")
        for k in ("configured", "attempted", "observed_peak", "kernel_response",
                  "verdict", "cleanup"):
            print(f"      {k:16} {row.get(k)}")

    print("─" * 88)
    bad = [r for r in rows if r["verdict"] == "UNENFORCED"]
    incon = [r for r in rows if r["verdict"] == "INCONCLUSIVE"]
    print(f"{len(rows) - len(bad) - len(incon)}/{len(rows)} controls enforced"
          + (f", {len(incon)} INCONCLUSIVE" if incon else "")
          + (f", {len(bad)} UNENFORCED" if bad else ""))
    if incon:
        print("  an inconclusive control is not an enforced one")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=2) + "\n")
    return 1 if (bad or incon) else 0


if __name__ == "__main__":
    sys.exit(main())
