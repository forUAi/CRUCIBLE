"""
CRUCIBLE :: backends/namespace.py

A container runtime built from the primitives Docker itself uses, with no
daemon and no Docker binary:

    mount namespace  + overlayfs   -> filesystem isolation AND free snapshots
    pid namespace                  -> process isolation
    net namespace (optional)       -> the build/run network split
    uts + ipc namespaces           -> hostname / shm isolation
    cgroups                        -> cpu + memory + pid caps
    rlimits                        -> fd / fsize / core caps

Two overlays are stacked, and the second one is the important one:

    /            lower = base rootfs      upper = layers/NNN/root
    /workspace   lower = the user's repo  upper = layers/NNN/ws

Overlaying the *workspace* means the repo gets copy-on-write for free. The
sandbox can `rm -rf`, `git reset --hard`, or npm-install into it and the
user's actual source tree is never touched. Rewinding a failed attempt is an
unmount, not a restore-from-backup.

SNAPSHOT MODEL
    layers/000-<key>/{root,ws,work-root,work-ws}
    layers/001-<key>/...
    live/{root,ws,work-root,work-ws}      <- current writable head

    snapshot(key): unmount -> rename live/ to layers/NNN-key/ -> fresh live
    restore(key):  unmount -> rebuild lowerdir chain up to key -> fresh live

Cost of a snapshot is a directory rename. That is the entire reason the
repair loop is affordable.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..schema import ExecResult, Step
from .base import SandboxBackend

STATE_ROOT = Path(os.environ.get("CRUCIBLE_STATE", "/var/lib/crucible"))


def _sh(cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


_STORE_READY = False
# preexec_fn runs in the forked child and cannot close over self; a one-slot
# module global carries the per-sandbox file-size ceiling across the fork.
_FSIZE_MB = [0]


def _ram_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 2048


def default_store_mb(floor: int = 4096, ceiling: int = 65536) -> int:
    """Half the free space where the store will live, within bounds.

    A fixed 4 GB is not a policy, it is a guess, and it is wrong in both
    directions: too small for one JVM base plus a Maven cache plus a mysql
    sidecar (that combination filled it and crashed the run with ENOSPC), and
    wasteful on a laptop with little room. Ask the disk.
    """
    try:
        st = os.statvfs(STATE_ROOT.parent if STATE_ROOT.parent.exists() else "/")
        free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        return floor
    return max(floor, min(ceiling, free_mb // 2))


def ensure_private_store(size_mb: int = 4096, log=print) -> None:
    """Give the layer store its own filesystem. Non-negotiable, and subtle.

    overlayfs refuses to mount when one layer is an ancestor of another --
    it walks dentry parents looking for "traps" and returns ELOOP. Using the
    host rootfs `/` as the read-only lower means our snapshot directories,
    living under /var/lib/crucible, are inside a lower layer. The first mount
    survives; the second (after a snapshot adds `/` alongside a layer dir)
    dies with the memorably unhelpful "Too many levels of symbolic links".

    dentry-parent walks do not cross mount points. So mounting *any* separate
    filesystem at the store path severs the ancestry chain and the whole class
    of error disappears. Disk-backed loop image preferred (builds are large
    and RAM here is not); tmpfs as fallback.
    """
    global _STORE_READY
    if _STORE_READY:
        return
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if _sh(f"mountpoint -q {STATE_ROOT}").returncode == 0:
        _STORE_READY = True
        return

    img = STATE_ROOT.parent / "crucible-store.img"
    have_loop = shutil.which("losetup") and shutil.which("mkfs.ext4")
    if have_loop:
        try:
            from ..diskbudget import mkfs_options, mount_options
            if not img.exists():
                _sh(f"truncate -s {size_mb}M {img}")
                # Project quotas have to be baked in at mkfs time; they cannot
                # be turned on later for an image already in use.
                _sh(f"mkfs.ext4 -q -F {mkfs_options()} {img}")
            r = _sh(f"mount -o loop,{mount_options()} {img} {STATE_ROOT}")
            if r.returncode != 0:
                log(f"  ! loop store mount failed: {r.stderr.strip()[:120]}")
            if r.returncode == 0:
                log(f"  store: ext4 loop image ({size_mb} MB) at {STATE_ROOT}")
                _STORE_READY = True
                return
        except OSError:
            pass

    # Last resort, and deliberately small. tmpfs is RAM: sizing it from free
    # DISK put an 18 GB store on a 6 GiB VM and the kernel OOM-killed a
    # process mid-run. It also cannot carry project quotas, so a budget on it
    # reports UNAVAILABLE rather than pretending.
    ram_mb = _ram_mb()
    tmpfs_mb = max(512, min(size_mb, ram_mb // 4))
    if _sh(f"mount -t tmpfs -o size={tmpfs_mb}m tmpfs {STATE_ROOT}").returncode == 0:
        log(f"  \033[33m! store: tmpfs ({tmpfs_mb} MB, capped to a quarter of "
            f"{ram_mb} MB RAM) -- disk budgets cannot be enforced here\033[0m")
        _STORE_READY = True
    else:
        log("  ! could not create a private store -- snapshots may fail on host base")


class NamespaceBackend(SandboxBackend):
    name = "namespace"
    supports_snapshots = True

    def __init__(self, box_id: str, log=print, mem_mb: int = 2048,
                 cpu_pct: int = 100, pid_max: int = 512, store_mb: int = 4096,
                 disk_mb: int = 0):
        self.id = box_id
        self.log = log
        self.mem_mb, self.cpu_pct, self.pid_max = mem_mb, cpu_pct, pid_max
        self.store_mb = store_mb
        self.disk_mb = disk_mb              # per-sandbox writable budget
        from ..diskbudget import BudgetState
        self.budget = BudgetState(False, "not applied")
        self.dir = STATE_ROOT / box_id
        self.base_dir = self.dir / "base"
        # Layers are content-addressed by CHAIN hash and shared across boxes and
        # across runs, so a step proven good once is never re-executed. Keying
        # on the step alone would be wrong: the filesystem a step produces
        # depends on the base image and every step beneath it, so `pip install
        # -r requirements.txt` under python:3.11 and under node:22 are
        # different layers that happen to share a command string.
        self.layers_dir = STATE_ROOT / "layers"
        self.live = self.dir / "live"
        self.merged = self.dir / "merged"
        self.repo: Optional[Path] = None
        self.stack: list[str] = []          # ordered snapshot keys, oldest first
        self.pod = None                     # optional shared-netns Pod
        self.dns = None                     # optional DnsLedger
        self.peers: dict = {}               # (ip, port) -> first seen
        self.image_env: dict[str, str] = {}  # ENV declared by the base image
        self.drain_grace = 2.0              # seconds to keep reading after exit
        self.stage_host_backed = True       # copy off host-shared storage first
        self.staged_from: Optional[str] = None
        self._mounted = False
        self._cg: list[Path] = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def up(self, base: str, repo_path: str, system_packages: list[str]) -> None:
        self.repo = Path(repo_path).resolve()
        # The store must exist before anything is written into it. Creating
        # the box directory first put it on the root filesystem, and mounting
        # the store over /var/lib/crucible then shadowed it -- so the tree the
        # quota was applied to was not always the tree the sandbox used.
        ensure_private_store(self.store_mb, self.log)
        self._claim()
        for d in (self.layers_dir, self.merged):
            d.mkdir(parents=True, exist_ok=True)

        # Untrusted code must not execute against a lower layer that lives on
        # storage the host shares. See containment.py -- the overlay protects
        # the tree from an honest build, not the host from a hostile one.
        from ..containment import host_backed_mount, stage_source
        hb = host_backed_mount(self.repo)
        if hb and self.stage_host_backed:
            self.log(f"  ! repo is on a host-backed mount ({hb[1]} at {hb[0]})")
            self.staged_from = str(self.repo)
            self.repo = stage_source(self.repo, self.dir / "src", self.log)
        elif hb:
            self.log(f"  !! EXECUTING AGAINST A HOST-BACKED MOUNT ({hb[1]}) -- "
                     f"staging disabled; the host filesystem is reachable")

        if base in ("host", "", None):
            # Fastest path: the host rootfs is the read-only lower layer.
            # Everything the host has (compilers, apt, python) is available,
            # but every write lands in the overlay. Zero-cost "image pull".
            self.base_dir = Path("/")
            self.log(f"  base: host rootfs (read-only lower)")
        else:
            from ..oci import cache_dir_for, image_config, pull_rootfs
            cache = cache_dir_for(STATE_ROOT, base)
            self.log(f"  base: pulling {base}")
            pull_rootfs(base, str(cache), log=self.log)
            self.base_dir = cache
            # An image's config blob is part of the image, not decoration.
            # eclipse-temurin declares JAVA_HOME there and puts the JDK on
            # PATH; ignoring it meant ./mvnw died with "JAVA_HOME is not
            # defined correctly" on the very base chosen to provide Java.
            # pod.py already reads this for sidecars -- the app never did.
            # oci.pull_rootfs normalises the config blob: lowercase keys and
            # `env` already a dict.
            cfg = image_config(str(cache)) or {}
            self.image_env = _env_from_config(cfg)
            if self.image_env:
                self.log(f"  image env: {', '.join(sorted(self.image_env))}")

        from ..diskbudget import apply as apply_budget
        self.budget = apply_budget(self.dir, self.id, self.disk_mb,
                                   STATE_ROOT, self.log)
        _FSIZE_MB[0] = self.disk_mb
        if self.disk_mb and not self.budget.enforced:
            self.log(f"  \033[33m! disk budget UNENFORCED: "
                     f"{self.budget.reason}\033[0m")

        self._fresh_live()
        self._mount()
        self._cgroup_setup()
        if system_packages:
            self._install_system(system_packages)

    def down(self) -> None:
        self._umount()
        self._cgroup_teardown()

    def destroy(self) -> None:
        self.down()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _claim(self) -> None:
        """Stamp this box with the pid that owns it, so a later run can tell
        an abandoned box from one in use."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "owner.pid").write_text(str(os.getpid()))
        except OSError:
            pass

    def _fresh_live(self) -> None:
        shutil.rmtree(self.live, ignore_errors=True)
        for sub in ("root", "ws", "work-root", "work-ws"):
            (self.live / sub).mkdir(parents=True, exist_ok=True)

    def _lower_chain(self, sub: str, floor: Path) -> str:
        """overlayfs lowerdir order is highest-priority-first."""
        parts = [str(self.layers_dir / k / sub) for k in reversed(self.stack)]
        parts.append(str(floor))
        return ":".join(p for p in parts if Path(p).exists())

    def _mount(self) -> None:
        if self._mounted:
            return
        self.merged.mkdir(parents=True, exist_ok=True)
        r = _sh(
            f"mount -t overlay crucible-{self.id} -o "
            f"lowerdir={self._lower_chain('root', self.base_dir)},"
            f"upperdir={self.live}/root,workdir={self.live}/work-root "
            f"{self.merged}"
        )
        if r.returncode != 0:
            raise RuntimeError(f"rootfs overlay failed: {r.stderr.strip()}")

        ws = self.merged / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        r = _sh(
            f"mount -t overlay crucible-{self.id}-ws -o "
            f"lowerdir={self._lower_chain('ws', self.repo)},"
            f"upperdir={self.live}/ws,workdir={self.live}/work-ws {ws}"
        )
        if r.returncode != 0:
            # degrade to a read-only bind rather than dying
            _sh(f"mount --bind {self.repo} {ws} && mount -o remount,ro,bind {ws}")
            self.log("  ! workspace COW unavailable, mounted read-only")

        for d in ("proc", "sys", "dev", "tmp", "run", "var/tmp"):
            (self.merged / d).mkdir(parents=True, exist_ok=True)
        # mkdir applies the umask, exactly as mknod does for /dev/null. A /tmp
        # that comes out 0755 root-owned breaks every tool that drops
        # privileges before using it: apt runs its GPG verification as `_apt`
        # and fails with "Couldn't create temporary file /tmp/apt.conf.XXXX",
        # which surfaces as an unsigned-repository error and takes the whole
        # apt-get down. The sticky bit is part of the container /tmp contract.
        for d in ("tmp", "var/tmp"):
            try:
                os.chmod(self.merged / d, 0o1777)
            except OSError as e:
                self.log(f"  ! could not chmod /{d}: {e}")
        from ..pod import populate_dev
        populate_dev(self.merged)
        # DNS for network-enabled steps. When a ledger is attached, point the
        # sandbox at it instead of the real resolver so every name the build
        # looks up is recorded on the way through.
        try:
            (self.merged / "etc").mkdir(parents=True, exist_ok=True)
            rc = self.merged / "etc/resolv.conf"
            # Unlink first, and never write *through* whatever is already
            # there. On a systemd host /etc/resolv.conf is a symlink into
            # /run, and /run is a separate mount -- so it is NOT part of an
            # overlay whose lower is `/`. Inside the sandbox that symlink
            # dangles, write_text() follows it to a directory that does not
            # exist, and the OSError below used to swallow the whole thing:
            # the sandbox got no resolver at all, apt died with "Temporary
            # failure resolving", and the repair loop blamed the repo.
            if rc.is_symlink() or rc.exists():
                rc.unlink(missing_ok=True)
            if self.dns is not None and self.dns.active:
                rc.write_text(f"nameserver {self.dns.bind}\noptions timeout:3\n")
            else:
                # Resolve on the host side too: the source may itself be a
                # symlink into a filesystem the sandbox cannot see.
                src = Path("/etc/resolv.conf").resolve()
                rc.write_text(src.read_text() if src.exists()
                              else "nameserver 1.1.1.1\n")
        except OSError as e:
            self.log(f"  ! could not write /etc/resolv.conf ({e}) -- "
                     f"name resolution will fail inside the sandbox")

        # Propagate the host CA trust store into the sandbox.
        #
        # Corporate and CI networks routinely terminate TLS at an egress proxy
        # using a private root CA. The host trusts it because someone installed
        # it; a freshly pulled image has a pristine public-only trust store and
        # every `pip install` dies with "self-signed certificate in chain".
        # No amount of `apt-get install ca-certificates` fixes that -- the cert
        # is not public. Inheriting host trust is the correct default: the
        # sandbox isolates the filesystem and processes, not the org's PKI.
        self.ca_bundle = ""
        for src in ("/etc/ssl/certs/ca-certificates.crt",
                    "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(src).exists():
                dst = self.merged / src.lstrip("/")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
                    self.ca_bundle = src
                    break
                except OSError:
                    pass
        self._mounted = True

    def _umount(self) -> None:
        if not self._mounted:
            return
        for _ in range(3):
            _sh(f"umount -l {self.merged}/workspace")
            _sh(f"umount -l {self.merged}")
            if not _sh(f"mountpoint -q {self.merged}").returncode == 0:
                break
            time.sleep(0.15)
        self._mounted = False

    # ------------------------------------------------------------------
    # snapshots -- the load-bearing feature
    # ------------------------------------------------------------------

    def snapshot(self, key: str) -> None:
        self._umount()
        target = self.layers_dir / key
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.live), str(target))
        self.stack.append(key)
        self._fresh_live()
        self._mount()

    def restore(self, key: Optional[str]) -> bool:
        if key is not None and key not in self.stack:
            return False
        self._umount()
        if key is None:
            self.stack = []
        else:
            self.stack = self.stack[: self.stack.index(key) + 1]
        self._fresh_live()
        self._mount()
        return True

    def has_snapshot(self, key: str) -> bool:
        return (self.layers_dir / key).exists()

    def adopt(self, key: str) -> bool:
        """Re-attach a snapshot that exists on disk from a previous run.
        This is what makes the plan cache actually save time across sessions."""
        if not self.has_snapshot(key) or key in self.stack:
            return False
        self._umount()
        self.stack.append(key)
        self._fresh_live()
        self._mount()
        return True

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def exec(self, step: Step, env: dict[str, str], stream=None) -> ExecResult:
        if not self._mounted:
            self._mount()

        merged_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root", "TERM": "xterm", "LANG": "C.UTF-8",
            "DEBIAN_FRONTEND": "noninteractive", "PYTHONUNBUFFERED": "1",
            "CI": "1", "NO_COLOR": "1",
            **self.image_env,           # the base image speaks for itself
            **self._ca_env(), **env, **step.env,
        }
        exports = "\n".join(
            f"export {k}={_q(v)}" for k, v in merged_env.items()
        )

        script = f"""#!/bin/sh
mount -t proc proc /proc 2>/dev/null
{exports}
cd /workspace/{step.cwd.strip('./') or '.'} 2>/dev/null || cd /workspace
{step.cmd}
"""
        sp = self.merged / ".crucible-step.sh"
        sp.write_text(script)
        sp.chmod(0o755)

        argv = self._ns_argv(step) + ["chroot", str(self.merged), "/bin/sh",
                                      "/.crucible-step.sh"]

        t0 = time.time()
        buf: list[str] = []
        timed_out = False
        # Step output goes to a FILE, not to a pipe we hold.
        #
        # A pipe reaches EOF when every holder closes it, and a build script
        # can hand our write end to anything. A hostile setup.py spawned a
        # detached `sleep 900`; it inherited the descriptor, pip's own build
        # subprocess became a zombie whose pipe was still held, pip blocked
        # reading it, and the step stalled with CRUCIBLE waiting on a
        # descriptor a grandchild controlled. Reading a file removes the
        # dependency entirely: nothing a descendant does can block the
        # supervisor, and the deadline is the only thing that decides.
        logpath = self.dir / f"step-{step.name.replace('/', '_')}.log"
        try:
            logpath.parent.mkdir(parents=True, exist_ok=True)
            sink = open(logpath, "wb")
        except OSError as e:
            return ExecResult(False, 127, "", f"could not open step log: {e}", 0.0)
        try:
            proc = subprocess.Popen(
                argv, stdout=sink, stderr=subprocess.STDOUT,
                preexec_fn=self._child_limits,
            )
        except OSError as e:
            sink.close()
            return ExecResult(False, 127, "", f"spawn failed: {e}", 0.0)

        self._cgroup_attach(proc.pid)
        if not step.network and self.pod is None:
            bring_up_loopback(proc.pid)   # pod netns already has lo up
        sampler = None
        if step.network:
            from ..netlog import SocketSampler
            sampler = SocketSampler(proc.pid)
            sampler.start()
        try:
            timed_out = self._pump(proc, step, buf, stream, t0, logpath)
            if timed_out:
                _kill(proc)
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill(proc)
            timed_out, code = True, 124
        except KeyboardInterrupt:
            _kill(proc)
            raise

        sink.close()
        if sampler is not None:
            sampler.stop()
            self.peers.update(sampler.peers)

        out = "\n".join(buf)
        ok = (code == 0 and not timed_out) or step.allow_fail
        return ExecResult(ok, code, out, "", round(time.time() - t0, 2), timed_out)

    def _pump(self, proc, step: Step, buf: list, stream, t0: float,
              logpath: Path) -> bool:
        """Tail the step's log while enforcing its deadline. Returns timed_out.

        Two properties, both learned by watching a fixture defeat the obvious
        version:

        The clock is checked on every pass, not after a line arrives. The
        original `for line in proc.stdout` blocks in read(), so a process that
        goes quiet was never timed out at all and the budget was decorative.

        Nothing a descendant does can stall the supervisor, because the
        supervisor is reading a file rather than a descriptor the descendant
        holds. That is why the step's stdout is a file: EOF on a pipe means
        every holder closed it, and a build script gets to decide who the
        holders are.
        """
        deadline = t0 + step.timeout
        pending = b""
        pos = 0
        exited_at = None

        def drain() -> None:
            nonlocal pending, pos
            try:
                with open(logpath, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            parts = pending.split(b"\n")
            pending = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace")
                buf.append(line)
                if stream:
                    stream(line)

        try:
            while True:
                if time.time() > deadline:
                    drain()
                    return True
                drain()
                if proc.poll() is not None:
                    if exited_at is None:
                        exited_at = time.time()
                    elif time.time() - exited_at > self.drain_grace:
                        break
                time.sleep(0.05)
        finally:
            drain()
            if pending:
                buf.append(pending.decode("utf-8", "replace"))
        return False

    def spawn(self, step: Step, env: dict[str, str]) -> subprocess.Popen:
        """Start a long-running process (the `run` command) without waiting."""
        merged_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                      "HOME": "/root", "PYTHONUNBUFFERED": "1",
                      **self.image_env,
                      **self._ca_env(), **env, **step.env}
        exports = "\n".join(f"export {k}={_q(v)}" for k, v in merged_env.items())
        script = (f"#!/bin/sh\nmount -t proc proc /proc 2>/dev/null\n"
                  f"{exports}\n"
                  f"cd /workspace/{step.cwd.strip('./') or '.'} 2>/dev/null || cd /workspace\n"
                  f"exec {step.cmd}\n")
        sp = self.merged / ".crucible-run.sh"
        sp.write_text(script)
        sp.chmod(0o755)
        proc = subprocess.Popen(
            self._ns_argv(step) + ["chroot", str(self.merged), "/bin/sh",
                                   "/.crucible-run.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            preexec_fn=self._child_limits,
        )
        self._cgroup_attach(proc.pid)
        if not step.network and self.pod is None:
            bring_up_loopback(proc.pid)   # pod netns already has lo up
        return proc

    # ------------------------------------------------------------------
    # limits
    # ------------------------------------------------------------------

    def _ca_env(self) -> dict[str, str]:
        """Every ecosystem invented its own name for the same file."""
        ca = getattr(self, "ca_bundle", "")
        if not ca:
            return {}
        return {"SSL_CERT_FILE": ca, "REQUESTS_CA_BUNDLE": ca, "CURL_CA_BUNDLE": ca,
                "PIP_CERT": ca, "NODE_EXTRA_CA_CERTS": ca, "GIT_SSL_CAINFO": ca,
                "CARGO_HTTP_CAINFO": ca, "SSL_CERT_DIR": "/etc/ssl/certs"}

    def _ns_argv(self, step: Step) -> list[str]:
        """Namespace flags for one step.

        Three cases, and the middle one is the reason this exists:

          network=True        no net isolation -- build steps need egress
          network=False, pod  JOIN the pod netns via nsenter: still no egress,
                              but 127.0.0.1 now reaches the sidecars
          network=False       fresh empty netns via unshare --net
        """
        ns = ["unshare", "--mount", "--uts", "--ipc", "--pid", "--fork", "--kill-child"]
        if step.network:
            return ns
        if self.pod is not None and self.pod.pid:
            return ["nsenter", "-t", str(self.pod.pid), "-n"] + ns
        return ns + ["--net"]

    @staticmethod
    def _drop_cap_sys_resource() -> None:
        """Remove CAP_SYS_RESOURCE from the sandboxed process.

        Without this a disk budget is decorative. Sandboxed builds run as uid
        0, and `ignore_hardlimit()` in the kernel's quota code lets anything
        holding CAP_SYS_RESOURCE write straight past a quota hard limit: a
        200 MB write against a 64 MB project limit succeeded in full.

        Dropping it is right on its own terms -- the same capability lets a
        process raise its own rlimits and dip into the filesystem's reserved
        blocks, both of which a sandbox should not be able to do. Nothing a
        build legitimately needs lives behind it; mounting /proc needs
        CAP_SYS_ADMIN, which is untouched.
        """
        import ctypes

        CAP_SYS_RESOURCE = 24
        PR_CAPBSET_DROP = 24
        _LINUX_CAPABILITY_VERSION_3 = 0x20080522

        libc = ctypes.CDLL(None, use_errno=True)

        class Header(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

        class Data(ctypes.Structure):
            _fields_ = [("effective", ctypes.c_uint32),
                        ("permitted", ctypes.c_uint32),
                        ("inheritable", ctypes.c_uint32)]

        hdr = Header(_LINUX_CAPABILITY_VERSION_3, 0)
        data = (Data * 2)()
        if libc.capget(ctypes.byref(hdr), ctypes.byref(data)) != 0:
            return
        mask = ~(1 << CAP_SYS_RESOURCE) & 0xFFFFFFFF
        data[0].effective &= mask
        data[0].permitted &= mask
        data[0].inheritable &= mask
        libc.capset(ctypes.byref(hdr), ctypes.byref(data))
        # Out of the bounding set too, so it cannot be regained across exec.
        libc.prctl(PR_CAPBSET_DROP, CAP_SYS_RESOURCE, 0, 0, 0)

    @staticmethod
    def _child_limits() -> None:
        import resource
        os.setsid()
        NamespaceBackend._drop_cap_sys_resource()
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_NPROC, (4096, 4096))
            # Tie the single-file ceiling to the sandbox budget. It was a
            # flat 8 GiB, so a disk bomb against a 512 MB budget was stopped
            # by EFBIG at 8 GiB rather than by the budget -- which is the
            # wrong control reporting the wrong reason.
            fsize = min(8 << 30, (_FSIZE_MB[0] << 20) if _FSIZE_MB[0] else (8 << 30))
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            resource.setrlimit(resource.RLIMIT_NOFILE, (8192, 8192))
        except (ValueError, OSError):
            pass

    @property
    def cgroup(self) -> str:
        """The cgroup that owns every process this box starts.

        Part of the backend contract, not an optional extra: it is how a later
        run proves which processes belong to an abandoned one.
        """
        return f"crucible-{self.id}"

    def _cgroup_setup(self) -> None:
        v2 = Path("/sys/fs/cgroup/cgroup.controllers")
        try:
            if v2.exists():
                g = Path(f"/sys/fs/cgroup/crucible-{self.id}")
                g.mkdir(exist_ok=True)
                _w(g / "memory.max", str(self.mem_mb * 1024 * 1024))
                _w(g / "pids.max", str(self.pid_max))
                _w(g / "cpu.max", f"{self.cpu_pct * 1000} 100000")
                self._cg = [g]
            else:
                for ctl, f, v in (
                    ("memory", "memory.limit_in_bytes", str(self.mem_mb * 1024 * 1024)),
                    ("pids", "pids.max", str(self.pid_max)),
                ):
                    g = Path(f"/sys/fs/cgroup/{ctl}/crucible-{self.id}")
                    if Path(f"/sys/fs/cgroup/{ctl}").exists():
                        g.mkdir(exist_ok=True)
                        _w(g / f, v)
                        self._cg.append(g)
        except OSError:
            pass
        if not self._cg:
            self.log("  ! cgroups unavailable -- rlimits only")

    def _cgroup_attach(self, pid: int) -> None:
        for g in self._cg:
            for f in ("cgroup.procs", "tasks"):
                if (g / f).exists():
                    _w(g / f, str(pid))
                    break

    def _cgroup_teardown(self) -> None:
        for g in self._cg:
            try:
                g.rmdir()
            except OSError:
                pass
        self._cg = []

    def _install_system(self, pkgs: list[str]) -> None:
        """Install the base-image packages a repair asked for.

        Non-fatal on purpose -- a partially satisfied install can still unblock
        the step -- but no longer *silent*. The result used to be discarded, so
        a failed `apt-get` was indistinguishable from a successful one: the
        repair loop applied the right patch, watched the identical error come
        back, found no new rule for it, and reported `unrepairable` for a
        failure it had diagnosed correctly and merely failed to fix. A repair
        loop that cannot see its own remedy fail will misattribute every time.
        """
        self.log(f"  system packages: {' '.join(pkgs)}")
        step = Step("system-packages",
                    "apt-get update -qq && apt-get install -y -qq --no-install-recommends "
                    + " ".join(pkgs),
                    network=True, timeout=900, allow_fail=True)
        res = self.exec(step, {})
        # NOT `res.ok`: allow_fail forces that True by construction
        # (`ok = (code == 0 and not timed_out) or step.allow_fail`). The exit
        # code is the only thing here that still carries the truth.
        if res.code != 0 or res.timed_out:
            self.log(f"  ! system packages FAILED (exit {res.code}) -- the repair that "
                     f"asked for `{' '.join(pkgs)}` did not take effect")
            for line in res.tail(4).splitlines():
                self.log(f"    {line}")


def reap_abandoned(log=print) -> int:
    """Unmount and delete boxes whose owning process is gone.

    A run that is killed -- timeout, SIGKILL, a crash inside the engine --
    never reaches down(), so its overlay mounts stay live and its directory
    stays on disk. Four such boxes had accumulated 674 MB and were still
    holding overlay mounts, which is also what pinned the loop device the
    store lives on. Cleanup after a crash cannot be the crashing process's
    job; it has to happen on the way in.
    """
    if not STATE_ROOT.is_dir():
        return 0
    reaped = 0
    for d in sorted(STATE_ROOT.glob("box-*")):
        pid_file = d / "owner.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            try:
                os.kill(pid, 0)
                continue                      # still running, leave it alone
            except PermissionError:
                continue                      # exists, owned by someone else
            except ProcessLookupError:
                pass
        merged = d / "merged"
        for target in (merged / "workspace", merged / "dev/shm", merged):
            _sh(f"umount -l {target} 2>/dev/null")
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            reaped += 1
    if reaped:
        log(f"  reaped {reaped} abandoned box(es) from earlier runs")
    return reaped

    # ------------------------------------------------------------------
    # overlay plumbing
    # ------------------------------------------------------------------



def _env_from_config(cfg: dict) -> dict[str, str]:
    """ENV out of a cached image config, in either shape it can arrive in."""
    env = cfg.get("env")
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    raw = env if isinstance(env, list) else (
        (cfg.get("config") or cfg.get("Config") or {}).get("Env") or [])
    out: dict[str, str] = {}
    for item in raw:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            if k:
                out[k] = v
    return out


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "'\\''") + "'"


def _w(p: Path, v: str) -> None:
    try:
        p.write_text(v)
    except OSError:
        pass


_LO_UP_SRC = """
import socket, struct, fcntl
SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ifr = struct.pack('16sh', b'lo', 0)
flags = struct.unpack('16sh', fcntl.ioctl(s, SIOCGIFFLAGS, ifr))[1]
fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack('16sh', b'lo', flags | IFF_UP))
"""


def bring_up_loopback(pid: int) -> bool:
    """Raise `lo` inside pid's network namespace.

    A fresh netns has loopback DOWN. So `--net` isolation does not merely cut
    egress -- it also breaks 127.0.0.1, which silently kills any app that
    binds localhost and makes the health probe report a false negative.

    `ip link set lo up` is the usual incantation, but iproute2 is absent from
    slim images (and from this host). Bringing the interface up is just a
    SIOCSIFFLAGS ioctl, so we do it directly, and we run it via nsenter using
    the *host's* Python: the sandbox rootfs is then irrelevant, and this works
    against a scratch image with no userland at all.
    """
    try:
        return subprocess.run(
            ["nsenter", "-t", str(pid), "-n", "python3", "-c", _LO_UP_SRC],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
