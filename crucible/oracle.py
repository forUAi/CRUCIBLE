"""
CRUCIBLE :: oracle.py

"Did it work?" has no universal answer, and pretending it does is why generic
runners fail. Success is archetype-relative:

    web       binds a port and answers TCP/HTTP. Any status code counts --
              a 404 proves the server is alive, which is the actual question.
              Asserting 200 would fail every app whose root route is /api.
    worker    stays alive N seconds without crashing (no port to probe).
    library   the test suite executes. Nothing else can prove a library runs.
    cli       exits 0 on --help. Cheap, and almost universally implemented.
    static    an index.html exists after build.

The oracle must be *weak enough to be true* and *strong enough to be useful*.
Every strengthening of it is a new false negative on some legitimate repo.

Probing an app inside an isolated network namespace: enter the running
process's netns with nsenter and dial 127.0.0.1 from there. This keeps the
runtime network cut intact while still letting us verify -- we get isolation
and observability instead of trading one for the other.
"""

from __future__ import annotations

import shutil
import socket
import os
import subprocess
from pathlib import Path
import time
from dataclasses import dataclass


@dataclass
class Verdict:
    ok: bool
    detail: str
    evidence: str = ""


HAS_NSENTER = shutil.which("nsenter") is not None


def _probe_in_netns(pid: int, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    """TCP connect to 127.0.0.1:port from inside pid's network namespace."""
    code = (
        "import socket,sys\n"
        f"s=socket.socket();s.settimeout({timeout})\n"
        "try:\n"
        f"    s.connect(('127.0.0.1',{port}))\n"
        "    s.sendall(b'GET / HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n')\n"
        "    d=s.recv(200)\n"
        "    print('OPEN', d.split(b'\\r\\n')[0].decode('utf8','replace'))\n"
        "except Exception as e:\n"
        "    print('SHUT', e); sys.exit(1)\n"
    )
    argv = (["nsenter", "-t", str(pid), "-n"] if HAS_NSENTER else []) + ["python3", "-c", code]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 3)
        return r.returncode == 0, r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def _probe_direct(port: int, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True, f"OPEN 127.0.0.1:{port}"
    except OSError as e:
        return False, str(e)


def machine_pressure() -> dict:
    """Load and free memory, sampled when a probe is about to give up.

    An oracle that times out reports a verdict about the repository. That is
    only honest if the machine was healthy: the node target was recorded as
    `failed` once, at 57s, while three repositories were being cloned and the
    page cache was churning -- and it verified in 12.8s on the next two runs.
    A timing failure attributed to the repository is a wrong answer, not a
    conservative one.
    """
    out = {"load1": None, "avail_mb": None, "runq": None}
    try:
        parts = Path("/proc/loadavg").read_text().split()
        out["load1"] = float(parts[0])
        out["runq"] = parts[3]
    except (OSError, ValueError, IndexError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["avail_mb"] = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return out


def under_pressure(p: dict, cpus: int = 0) -> bool:
    cpus = cpus or (os.cpu_count() or 1)
    return bool((p.get("load1") or 0) > cpus * 1.5
                or (p.get("avail_mb") is not None and p["avail_mb"] < 256))


def verify(spec: dict, proc: subprocess.Popen | None, log_tail=lambda: "") -> Verdict:
    kind = spec.get("kind", "exit0")

    # ---- web: poll until the port answers or the grace period expires ----
    if kind == "http":
        port = int(spec.get("port") or 8000)
        grace = float(spec.get("grace", 45))
        deadline = time.time() + grace
        last = ""
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                return Verdict(False, f"process exited (code {proc.returncode}) before binding "
                                      f"port {port}", log_tail())
            ok, detail = (_probe_in_netns(proc.pid, port) if proc else _probe_direct(port))
            last = detail
            if ok:
                return Verdict(True, f"port {port} answered", detail)
            time.sleep(1.0)
        # Attribute the failure before returning it. A grace period that
        # expires on a loaded machine is a statement about the machine.
        press = machine_pressure()
        if under_pressure(press):
            return Verdict(
                False,
                f"INCONCLUSIVE: nothing listening on {port} after {grace:.0f}s, "
                f"but the machine was under pressure (load1={press['load1']}, "
                f"{press['avail_mb']} MB available) -- this is not a verdict "
                f"about the repository",
                last or log_tail())
        return Verdict(False, f"nothing listening on {port} after {grace:.0f}s", last or log_tail())

    # ---- worker/daemon: survive without crashing ----
    if kind == "alive":
        secs = float(spec.get("seconds", 20))
        t0 = time.time()
        while time.time() - t0 < secs:
            if proc is not None and proc.poll() is not None:
                return Verdict(False, f"exited after {time.time()-t0:.1f}s "
                                      f"(code {proc.returncode})", log_tail())
            time.sleep(0.5)
        return Verdict(True, f"stayed alive {secs:.0f}s", "")

    # ---- library/cli: exit code is the signal ----
    if proc is not None:
        try:
            code = proc.wait(timeout=float(spec.get("timeout", 900)))
        except subprocess.TimeoutExpired:
            proc.kill()
            return Verdict(False, "run command never terminated", log_tail())
        return Verdict(code == 0, f"exited {code}", log_tail())

    return Verdict(False, "no process to verify", "")


def classify_failure(v: Verdict) -> str:
    """Map a failed verdict to a hint the repair engine can act on."""
    e = (v.evidence + " " + v.detail).lower()
    if "nothing listening" in v.detail:
        return "port-mismatch"
    if "exited" in v.detail and ("traceback" in e or "error" in e):
        return "crash"
    if "before binding" in v.detail:
        return "startup-crash"
    return "unknown"
