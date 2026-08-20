"""
CRUCIBLE :: security/netmodes.py

Every network policy, with a positive and a negative control.

A policy is only meaningful if refusing it is observable. So hermetic must
FAIL to install a dependency, open must succeed, and proxy must succeed *and*
the proxy must be able to show the traffic. A mode that quietly falls back to
a direct connection would pass a naive check while reporting the wrong thing.

    sudo python3 security/netmodes.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import lifecycle                                   # noqa: E402
from crucible.backends.namespace import STATE_ROOT               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "examples/py-fastapi"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PROXY_PORT = 3129


# ---------------------------------------------------------------------------
# a CONNECT proxy that records what passed through it
# ---------------------------------------------------------------------------

class RecordingProxy:
    """Minimal CONNECT proxy. Its log is the evidence that proxy mode routed."""

    def __init__(self, port: int = PROXY_PORT):
        self.port = port
        self.seen: list[str] = []
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(64)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, c: socket.socket) -> None:
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = c.recv(4096)
                if not chunk:
                    return
                req += chunk
            line = req.split(b"\r\n")[0].decode("latin1")
            parts = line.split()
            if len(parts) < 2 or parts[0] != "CONNECT":
                c.close()
                return
            host, _, port = parts[1].partition(":")
            self.seen.append(f"{host}:{port or 443}")
            up = socket.create_connection((host, int(port or 443)), timeout=20)
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._pump(c, up)
        except Exception:
            try:
                c.close()
            except OSError:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        import selectors
        sel = selectors.DefaultSelector()
        for s in (a, b):
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ)
        try:
            while True:
                events = sel.select(timeout=60)
                if not events:
                    return
                for key, _ in events:
                    other = b if key.fileobj is a else a
                    try:
                        data = key.fileobj.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            sel.close()
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------

def cold_layers() -> None:
    """The install step must actually run, or a hermetic denial proves
    nothing -- a snapshot hit would 'succeed' with no network at all."""
    shutil.rmtree(STATE_ROOT / "layers", ignore_errors=True)


def run(mode: str, env_extra: dict | None = None) -> tuple[str, int, float]:
    cold_layers()
    env = dict(os.environ, **(env_extra or {}))
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "-u", "-m", "crucible.cli", str(TARGET), "--no-llm",
         "--budget", "1", "--no-cache", "--network", mode,
         "--step-timeout", "300"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)
    return ANSI.sub("", p.stdout + p.stderr), p.returncode, round(time.time() - t0, 1)


def case_hermetic() -> dict:
    out, rc, secs = run("hermetic")
    denied = "denies build egress" in out
    failed = "FAILED" in out and "SUCCESS" not in out
    named = "policy denied it egress" in out
    return {"mode": "hermetic", "expected": "install fails, policy named",
            "observed": ("denied and failed, attributed to the policy"
                         if denied and failed and named
                         else f"denied={denied} failed={failed} named={named}"),
            "pass": denied and failed and named, "rc": rc, "seconds": secs}


def case_open() -> dict:
    out, rc, secs = run("open")
    ok = "SUCCESS" in out
    return {"mode": "open", "expected": "install succeeds",
            "observed": "SUCCESS" if ok else "did not succeed",
            "pass": ok, "rc": rc, "seconds": secs}


def case_proxy() -> dict:
    proxy = RecordingProxy()
    proxy.start()
    time.sleep(0.5)
    url = f"http://127.0.0.1:{proxy.port}"
    try:
        out, rc, secs = run("proxy", {"HTTP_PROXY": url, "HTTPS_PROXY": url})
    finally:
        time.sleep(0.5)
        seen = sorted(set(proxy.seen))
        proxy.stop()
    ok = "SUCCESS" in out
    routed = any("pypi.org" in s or "pythonhosted" in s for s in seen)
    return {"mode": "proxy", "expected": "install succeeds AND traffic is seen "
                                         "by the proxy",
            "observed": f"success={ok}, proxy saw {seen[:4] or 'nothing'}",
            "pass": ok and routed, "rc": rc, "seconds": secs}


def case_proxy_refused() -> dict:
    """Negative control: proxy mode with no proxy configured must refuse.

    A fallback to a direct connection would succeed, be labelled `proxy`, and
    have bypassed it entirely.
    """
    env = {k: "" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                           "https_proxy")}
    p = subprocess.run(
        [sys.executable, "-u", "-m", "crucible.cli", str(TARGET), "--no-llm",
         "--network", "proxy"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={k: v for k, v in dict(os.environ, **env).items() if v})
    out = ANSI.sub("", p.stdout + p.stderr)
    refused = "requires HTTP_PROXY" in out and p.returncode != 0
    return {"mode": "proxy (unconfigured)", "expected": "refused, not a fallback",
            "observed": "refused" if refused else out.strip()[-90:],
            "pass": refused, "rc": p.returncode, "seconds": 0}


CASES = [case_hermetic, case_open, case_proxy, case_proxy_refused]


def main() -> int:
    ap = argparse.ArgumentParser(prog="netmodes")
    ap.add_argument("--out")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("must run as root (mounts and namespaces)")

    rows = []
    for fn in CASES:
        lifecycle.reap(STATE_ROOT, log=lambda *_: None)
        row = fn()
        rows.append(row)
        print(f"{'✓' if row['pass'] else '✗'} {row['mode']:22} {row['observed']}")

    bad = [r for r in rows if not r["pass"]]
    print("─" * 84)
    print(f"{len(rows) - len(bad)}/{len(rows)} network policies behaved as declared")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=2) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
