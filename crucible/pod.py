"""
CRUCIBLE :: pod.py

Sidecar services (postgres, redis, ...) and the app under test need to reach
each other while both stay cut off from the internet. Two isolated sandboxes
cannot talk; one shared sandbox is not isolation. The resolution is to stop
thinking of them as two machines that need networking between them.

Put them in the SAME network namespace.

That is precisely what a Kubernetes pod is, and the trick that makes it work
is the pause container: a do-nothing process whose only job is to hold the
namespace open so others can join it. Here that is `unshare --net sleep`,
and joining is `nsenter -t <pause> -n`.

What this buys, all from one primitive:

    app -> 127.0.0.1:5432 -> postgres      works (same netns, same loopback)
    app -> 1.1.1.1:443                     unreachable (netns has no route out)
    host                                   entirely unaffected

No veth pairs, no bridges, no NAT rules, no iproute2 -- none of which are
installed here anyway. Each member still gets its own mount, pid, uts and ipc
namespaces; only the network is deliberately shared.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .oci import image_config, pull_rootfs
from .schema import Service

STATE_ROOT = Path(os.environ.get("CRUCIBLE_STATE", "/var/lib/crucible"))

# Commands that behave when PID 1 is root and there is no init system.
# Where an image's own entrypoint is well-behaved we defer to it (postgres);
# where it is not worth the risk we invoke the daemon directly (redis).
SERVICE_CMD = {
    "redis": 'redis-server --protected-mode no --save "" --appendonly no',
    "postgres": "docker-entrypoint.sh postgres",
    "mysql": "docker-entrypoint.sh mysqld",
    "mongodb": "docker-entrypoint.sh mongod --bind_ip_all",
}

# Env the *app* needs so it can find the sidecar it just got.
SERVICE_URL = {
    "postgres": ("DATABASE_URL", "postgresql://crucible:crucible@127.0.0.1:5432/crucible"),
    "redis":    ("REDIS_URL", "redis://127.0.0.1:6379/0"),
    "mysql":    ("DATABASE_URL", "mysql://root:crucible@127.0.0.1:3306/crucible"),
    "mongodb":  ("MONGO_URL", "mongodb://127.0.0.1:27017/crucible"),
}

_LO_UP = """
import socket, struct, fcntl
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
f = struct.unpack('16sh', fcntl.ioctl(s, 0x8913, struct.pack('16sh', b'lo', 0)))[1]
fcntl.ioctl(s, 0x8914, struct.pack('16sh', b'lo', f | 1))
"""


def populate_dev(root: Path) -> None:
    """An OCI rootfs ships an empty /dev. Most daemons need a few nodes.

    Without /dev/null and /dev/urandom, redis and postgres fail in ways whose
    error messages point nowhere near the actual cause.
    """
    d = root / "dev"
    d.mkdir(parents=True, exist_ok=True)
    for name, maj, minor, mode in (
        ("null", 1, 3, 0o666), ("zero", 1, 5, 0o666), ("full", 1, 7, 0o666),
        ("random", 1, 8, 0o666), ("urandom", 1, 9, 0o666), ("tty", 5, 0, 0o666),
    ):
        p = d / name
        if not p.exists():
            try:
                os.mknod(p, mode | stat.S_IFCHR, os.makedev(maj, minor))
            except OSError:
                pass
        # mknod applies the umask, so a 0666 request lands as 0644 and any
        # service that drops privileges (postgres su-execs to `postgres`
        # before touching /dev/null) dies with a permission error pointing
        # nowhere near the cause. chmod explicitly.
        try:
            os.chmod(p, mode)
        except OSError:
            pass
    # The standard container /dev contract is more than device nodes. Shell
    # process substitution -- `<(...)`, which postgres's entrypoint uses --
    # compiles to /dev/fd/N, and /dev/fd must be a symlink to /proc/self/fd
    # or initdb fails with "could not open file /dev/fd/63". Cheap to provide,
    # baffling to debug when absent.
    for link, target in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                         ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"),
                         ("core", "/proc/kcore")):
        p = d / link
        if not p.exists() and not p.is_symlink():
            try:
                os.symlink(target, p)
            except OSError:
                pass

    shm = d / "shm"
    shm.mkdir(exist_ok=True)
    if subprocess.run(f"mountpoint -q {shm}", shell=True).returncode != 0:
        subprocess.run(f"mount -t tmpfs -o size=256m tmpfs {shm}",
                       shell=True, capture_output=True)


@dataclass
class RunningService:
    name: str
    proc: subprocess.Popen
    merged: Path
    port: int
    ready: bool = False
    detail: str = ""


class Pod:
    def __init__(self, pod_id: str, log=print):
        self.id = pod_id
        self.log = log
        self.dir = STATE_ROOT / "pods" / pod_id
        self.pause: Optional[subprocess.Popen] = None
        self.services: list[RunningService] = []

    # ------------------------------------------------------------------

    @property
    def pid(self) -> int:
        return self.pause.pid if self.pause else 0

    def start(self) -> None:
        """Hold a network namespace open. This is the pause container."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pause = subprocess.Popen(
            ["unshare", "--net", "sleep", "86400"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        time.sleep(0.25)
        self._in_ns(["python3", "-c", _LO_UP])
        self.log(f"  pod netns {self._ns_id()} up (egress cut, loopback live)")

    def _ns_id(self) -> str:
        try:
            return os.readlink(f"/proc/{self.pid}/ns/net")
        except OSError:
            return "?"

    def _in_ns(self, argv: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(["nsenter", "-t", str(self.pid), "-n"] + argv,
                              capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------

    def launch(self, svc: Service) -> RunningService:
        port = svc.ports[0] if svc.ports else 0
        self.log(f"  ▸ sidecar {svc.name}  {svc.image}")

        img_dir = STATE_ROOT / "images" / svc.image.replace("/", "_").replace(":", "_")
        pull_rootfs(svc.image, str(img_dir), log=lambda m: None)
        cfg = image_config(str(img_dir))

        merged = self.dir / "svc" / svc.name / "merged"
        upper = self.dir / "svc" / svc.name / "up"
        work = self.dir / "svc" / svc.name / "work"
        for p in (merged, upper, work):
            p.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            f"mount -t overlay crucible-svc-{svc.name} -o "
            f"lowerdir={img_dir},upperdir={upper},workdir={work} {merged}",
            shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            return RunningService(svc.name, _dead(), merged, port, False,
                                  f"overlay failed: {r.stderr.strip()[:200]}")
        populate_dev(merged)

        # image env first, our overrides second
        env = dict(cfg.get("env") or {})
        env.update(svc.env)
        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env.setdefault("HOME", "/root")
        cmd = svc.command or SERVICE_CMD.get(svc.name) or cfg.get("cmdline") or ""
        if not cmd:
            return RunningService(svc.name, _dead(), merged, port, False,
                                  "no command in image config and no override")

        script = ("#!/bin/sh\nmount -t proc proc /proc 2>/dev/null\n"
                  + "\n".join(f"export {k}={_q(v)}" for k, v in env.items())
                  + f"\ncd /\nexec {cmd}\n")
        (merged / ".crucible-svc.sh").write_text(script)
        (merged / ".crucible-svc.sh").chmod(0o755)

        proc = subprocess.Popen(
            ["nsenter", "-t", str(self.pid), "-n",
             "unshare", "--mount", "--uts", "--ipc", "--pid", "--fork", "--kill-child",
             "chroot", str(merged), "/bin/sh", "/.crucible-svc.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid)
        rs = RunningService(svc.name, proc, merged, port)
        self.services.append(rs)
        return rs

    def wait_ready(self, rs: RunningService, timeout: float = 60) -> bool:
        """Readiness is the port answering, probed from inside the pod netns."""
        if rs.port == 0:
            rs.ready = rs.proc.poll() is None
            return rs.ready
        deadline = time.time() + timeout
        code = ("import socket,sys\n"
                f"s=socket.socket();s.settimeout(2)\n"
                f"try: s.connect(('127.0.0.1',{rs.port}))\n"
                "except Exception: sys.exit(1)\n")
        while time.time() < deadline:
            if rs.proc.poll() is not None:
                rs.detail = f"exited early (code {rs.proc.returncode}): {_tail(rs.proc)}"
                return False
            if self._in_ns(["python3", "-c", code]).returncode == 0:
                rs.ready = True
                rs.detail = f"ready on :{rs.port} in {timeout - (deadline - time.time()):.1f}s"
                self.log(f"    \033[32mready\033[0m 127.0.0.1:{rs.port}")
                return True
            time.sleep(0.5)
        rs.detail = f"not listening on :{rs.port} after {timeout:.0f}s: {_tail(rs.proc)}"
        return False

    def env_for_app(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rs in self.services:
            if rs.ready and rs.name in SERVICE_URL:
                k, v = SERVICE_URL[rs.name]
                out[k] = v
        return out

    # ------------------------------------------------------------------

    def stop(self) -> None:
        for rs in self.services:
            try:
                os.killpg(os.getpgid(rs.proc.pid), 9)
            except (OSError, ProcessLookupError):
                pass
            subprocess.run(f"umount -l {rs.merged}", shell=True, capture_output=True)
        if self.pause:
            try:
                os.killpg(os.getpgid(self.pause.pid), 9)
            except (OSError, ProcessLookupError):
                pass
        self.services = []
        shutil.rmtree(self.dir, ignore_errors=True)


def _q(v) -> str:
    return "'" + str(v).replace("'", "'\\''") + "'"


def _dead() -> subprocess.Popen:
    return subprocess.Popen(["true"], stdout=subprocess.DEVNULL)


def _tail(proc: subprocess.Popen, n: int = 6) -> str:
    try:
        os.set_blocking(proc.stdout.fileno(), False)  # type: ignore[union-attr]
        data = proc.stdout.read() or ""               # type: ignore[union-attr]
    except (OSError, ValueError, AttributeError):
        return ""
    return " | ".join(data.strip().splitlines()[-n:])[:400]
