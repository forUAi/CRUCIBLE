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


def emit_one(name: str, data: dict) -> None:
    """One line per control, as soon as it is known.

    A single report at the end is lost the moment the kernel OOM-kills
    anything in the cgroup -- which is precisely the event the memory probe
    exists to observe. Emitting incrementally means a kill can only ever
    destroy the finding that caused it.
    """
    REPORT[name] = data
    print("RESOURCE_ONE " + json.dumps({name: data}), flush=True)


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
    emit_one(name, REPORT[name])


# --------------------------------------------------------------------------

def probe_memory(progress=None) -> dict:
    """Allocate until refused, touching every page.

    A lazy allocation is not a consumption, and under memory.max the kernel
    does not politely return MemoryError -- it SIGKILLs. So the running total
    is streamed to the parent after every block: when the kill lands, the
    last number written is the observed peak. Without that the probe dies
    silently and an enforced limit is indistinguishable from an absent one.
    """
    mb = 0
    chunks = []
    stopped = None
    try:
        while mb < 16384:
            block = bytearray(64 * 1024 * 1024)
            for off in range(0, len(block), 4096):
                block[off] = 1
            chunks.append(block)
            mb += 64
            if progress:
                progress(mb)
    except MemoryError:
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
    # Processes, not threads: the GIL serialises CPU-bound Python, so a
    # threaded spinner reports 1.0 cores on any machine and cannot tell a
    # 50% quota from an unlimited one.
    n = os.cpu_count() or 1
    wall0 = time.time()
    stop = time.time() + 4.0
    kids = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            _spin(stop)
            os._exit(0)
        kids.append(pid)
    for pid in kids:
        os.waitpid(pid, 0)
    wall = time.time() - wall0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = usage.ru_utime + usage.ru_stime
    return {"visible_cpus": n, "wall_seconds": round(wall, 2),
            "cpu_seconds": round(cpu, 2),
            "cores_used": round(cpu / wall, 2) if wall else 0}


def _spin(until: float) -> None:
    x = 0
    while time.time() < until:
        for _ in range(20000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF


def in_child_streaming(name: str, fn) -> None:
    """Like in_child, but the child streams progress line by line.

    The last line received before the kill is the observed peak.
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)

        def progress(mb: int) -> None:
            try:
                os.write(w, (json.dumps({"allocated_mb": mb}) + "\n").encode())
            except OSError:
                pass
        out = {}
        try:
            out = fn(progress)
        except BaseException as e:            # noqa: BLE001
            out = {"stopped": f"{type(e).__name__}: {str(e)[:80]}"}
        try:
            os.write(w, (json.dumps(dict(out, final=True)) + "\n").encode())
        except OSError:
            pass
        os._exit(0)
    os.close(w)
    last: dict = {}
    buf = b""
    while True:
        try:
            b = os.read(r, 65536)
        except OSError:
            break
        if not b:
            break
        buf += b
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                last.update(json.loads(line))
            except ValueError:
                pass
    os.close(r)
    _, status = os.waitpid(pid, 0)
    sig = status & 0x7F
    if sig:
        last["killed_by_signal"] = sig
        last.setdefault("stopped", f"SIGKILL after {last.get('allocated_mb', 0)} MB")
    last["exit_status"] = status >> 8
    last.setdefault("attempted_mb", 16384)
    emit_one(name, last)


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
        in_child_streaming("memory", probe_memory)
    if only in ("all", "background"):
        in_child("background", probe_background_child)

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
