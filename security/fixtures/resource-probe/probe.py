"""Consume each bounded resource until something stops us, and report what did.

One fixture rather than four, because the interesting question is not whether
any single limit fires but which control binds first and what the workload
observes when it does. A probe that reports "I was killed" proves nothing
about which ceiling killed it.

Everything is reported BEFORE the process can be killed for the next probe,
and each destructive probe runs in a child, so one ceiling firing does not
prevent the rest from being measured.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import threading
import time

REPORT: dict = {}


def emit() -> None:
    print("RESOURCE_REPORT " + json.dumps(REPORT), flush=True)


def in_child(name: str, fn) -> None:
    """Run a probe that may be killed, and keep its findings.

    The child writes its result to a pipe before it dies. Without this, a
    memory probe that gets OOM-killed reports nothing at all, which reads
    identically to a memory probe that was never bounded.
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        out = {}
        try:
            out = fn()
        except BaseException as e:            # noqa: BLE001 - reporting a kill
            out = {"stopped_by": f"{type(e).__name__}: {str(e)[:80]}"}
        try:
            os.write(w, json.dumps(out).encode()[:60000])
        except OSError:
            pass
        os._exit(0)
    os.close(w)
    chunks = []
    while True:
        try:
            b = os.read(r, 65536)
        except OSError:
            break
        if not b:
            break
        chunks.append(b)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    try:
        REPORT[name] = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        REPORT[name] = {}
    sig = status & 0x7F
    if sig:
        REPORT[name]["killed_by_signal"] = sig
    REPORT[name]["exit_status"] = status >> 8


# --------------------------------------------------------------------------

def probe_memory() -> dict:
    """Allocate until refused. Touch every page: a lazy allocation is not a
    consumption, and a limit that only fires on touch would look absent."""
    chunks, mb = [], 0
    stopped = None
    try:
        while mb < 16384:
            block = bytearray(64 * 1024 * 1024)
            for off in range(0, len(block), 4096):
                block[off] = 1
            chunks.append(block)
            mb += 64
    except MemoryError as e:
        stopped = f"MemoryError after {mb} MB"
    except OSError as e:
        stopped = f"OSError {e.errno} after {mb} MB"
    return {"attempted_mb": 16384, "allocated_mb": mb, "stopped": stopped}


def probe_threads() -> dict:
    """OS threads, which is what a pid cgroup counts."""
    made, stopped = 0, None
    hold = []
    stop = threading.Event()
    try:
        while made < 4000:
            t = threading.Thread(target=stop.wait, args=(30,), daemon=True)
            t.start()
            hold.append(t)
            made += 1
    except BaseException as e:                # noqa: BLE001
        stopped = f"{type(e).__name__} after {made} threads"
    stop.set()
    return {"attempted": 4000, "started": made, "stopped": stopped}


def probe_processes() -> dict:
    """Forks, counted by the same cgroup control as threads."""
    made, stopped, kids = 0, None, []
    try:
        while made < 2000:
            pid = os.fork()
            if pid == 0:
                time.sleep(20)
                os._exit(0)
            kids.append(pid)
            made += 1
    except OSError as e:
        stopped = f"OSError {e.errno} ({e.strerror}) after {made} forks"
    for p in kids:
        try:
            os.kill(p, 9)
            os.waitpid(p, 0)
        except OSError:
            pass
    return {"attempted": 2000, "forked": made, "stopped": stopped}


def probe_cpu() -> dict:
    """Spin on every visible CPU and compare CPU-seconds to wall-seconds.

    That ratio is the only honest measure of a cpu.max quota from inside: the
    process cannot read its own cgroup, and `nproc` reports the machine, not
    the allowance.
    """
    n = os.cpu_count() or 1
    wall0, cpu0 = time.time(), time.process_time()
    stop = time.time() + 4.0
    ts = []
    for _ in range(n):
        t = threading.Thread(target=_spin, args=(stop,), daemon=True)
        t.start()
        ts.append(t)
    for t in ts:
        t.join(20)
    wall = time.time() - wall0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu = usage.ru_utime + usage.ru_stime
    return {"visible_cpus": n, "wall_seconds": round(wall, 2),
            "cpu_seconds": round(cpu, 2),
            "cores_used": round(cpu / wall, 2) if wall else 0}


def _spin(until: float) -> None:
    x = 0
    while time.time() < until:
        for _ in range(20000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF


def probe_background_child() -> dict:
    """A process deliberately outliving its parent, to be reclaimed later."""
    try:
        p = subprocess.Popen(["sh", "-c", "sleep 900"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"spawned_pid": p.pid, "detail": "detached sleep 900"}
    except OSError as e:
        return {"stopped": str(e)}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    REPORT["uid"] = os.getuid()
    REPORT["visible_cpus"] = os.cpu_count()

    if only in ("all", "cpu"):
        in_child("cpu", probe_cpu)
    if only in ("all", "threads"):
        in_child("threads", probe_threads)
    if only in ("all", "processes"):
        in_child("processes", probe_processes)
    if only in ("all", "memory"):
        in_child("memory", probe_memory)
    if only in ("all", "background"):
        in_child("background", probe_background_child)

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
