"""
CRUCIBLE :: preflight.py

Check every containment capability before running anything, and refuse when a
mandatory one is missing.

The failure this prevents is the quiet one. On a host without `setquota` the
sandbox still builds, still runs, still reports SUCCESS -- and the disk budget
is not enforced, so an untrusted repository can fill the store. The budget
already reports itself UNAVAILABLE, but by then the run is underway and the
message is one line among hundreds. A capability that containment depends on
is a precondition, not a warning.

Mandatory means: without it, a containment property CRUCIBLE advertises is
silently absent. Those refuse. Everything else is reported and continues.

    python3 -m crucible.cli --preflight
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Capability:
    name: str
    ok: bool
    mandatory: bool
    detail: str
    protects: str

    @property
    def blocking(self) -> bool:
        return self.mandatory and not self.ok


def _has(*tools: str) -> tuple[bool, str]:
    missing = [t for t in tools if not shutil.which(t)]
    return (not missing), ("present" if not missing
                           else f"missing: {', '.join(missing)}")


def _cgroup_v2() -> tuple[bool, str]:
    root = Path("/sys/fs/cgroup")
    if not (root / "cgroup.controllers").exists():
        return False, "cgroup v2 not mounted"
    try:
        sub = (root / "cgroup.subtree_control").read_text().split()
    except OSError:
        return False, "cannot read cgroup.subtree_control"
    need = {"pids", "memory", "cpu"}
    missing = need - set(sub)
    if missing:
        return False, f"controllers not delegated: {', '.join(sorted(missing))}"
    return True, f"v2 with {', '.join(sorted(need))}"


def _cgroup_kill() -> tuple[bool, str]:
    """cgroup.kill is how a run's processes are terminated exactly.

    Without it cleanup falls back to signalling enumerated members, which is
    still ownership-based but races against a forking workload.
    """
    probe = Path("/sys/fs/cgroup/crucible-preflight")
    try:
        probe.mkdir(exist_ok=True)
    except OSError as e:
        return False, f"cannot create a cgroup: {e}"
    try:
        return ((probe / "cgroup.kill").exists(),
                "present" if (probe / "cgroup.kill").exists()
                else "absent (kernel < 5.14)")
    finally:
        try:
            probe.rmdir()
        except OSError:
            pass


def _overlayfs() -> tuple[bool, str]:
    try:
        fs = Path("/proc/filesystems").read_text()
    except OSError:
        return False, "cannot read /proc/filesystems"
    return ("overlay" in fs), ("supported" if "overlay" in fs
                               else "overlay not in /proc/filesystems")


def _mount_privilege() -> tuple[bool, str]:
    if os.geteuid() != 0:
        return False, "not root; mount namespaces and overlayfs need CAP_SYS_ADMIN"
    return True, "root"


def _quota_stack() -> tuple[bool, str]:
    ok, detail = _has("setquota", "repquota", "chattr")
    if not ok:
        return False, detail
    r = subprocess.run("modprobe quota_v2", shell=True, capture_output=True)
    if r.returncode != 0 and not Path("/proc/fs/quota").exists():
        try:
            mods = Path("/proc/modules").read_text()
        except OSError:
            mods = ""
        if "quota_v2" not in mods:
            return False, ("quota_v2 unavailable; ext4 cannot enable project "
                           "quota tracking and per-sandbox disk budgets "
                           "cannot be enforced")
    return True, "setquota + quota_v2"


CHECKS = [
    ("root privilege", _mount_privilege, True,
     "mount namespaces, overlayfs and cgroups"),
    ("cgroup v2 + controllers", _cgroup_v2, True,
     "cpu, memory and process ceilings, and run ownership"),
    ("overlayfs", _overlayfs, True,
     "filesystem isolation and copy-on-write snapshots"),
    ("namespace tools", lambda: _has("unshare", "nsenter"), True,
     "pid, mount, net, uts and ipc isolation"),
    ("loop store tools", lambda: _has("mkfs.ext4", "losetup", "mountpoint"), True,
     "the private layer store that carries disk quotas"),
    ("project quota stack", _quota_stack, True,
     "per-sandbox disk budgets; without it one repository can fill the store"),
    ("cgroup.kill", _cgroup_kill, False,
     "exact, atomic termination of a run's processes"),
    ("git", lambda: _has("git"), False,
     "cloning targets; only needed for URL inputs"),
]


def check() -> list[Capability]:
    out = []
    for name, fn, mandatory, protects in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:                       # noqa: BLE001
            ok, detail = False, f"check raised {type(e).__name__}: {e}"
        out.append(Capability(name, ok, mandatory, detail, protects))
    return out


def report(caps: list[Capability], log=print) -> int:
    """Print the roster. Returns the number of blocking failures."""
    blocking = [c for c in caps if c.blocking]
    for c in caps:
        mark = "✓" if c.ok else ("✗" if c.mandatory else "!")
        tag = "" if c.ok else ("  REQUIRED" if c.mandatory else "  degraded")
        log(f"  {mark} {c.name:24} {c.detail}{tag}")
        if not c.ok:
            log(f"      protects: {c.protects}")
    return len(blocking)


def enforce(log=print, waive: tuple[str, ...] = ()) -> None:
    """Refuse to run when a mandatory capability is absent.

    Waivers are explicit and named on the command line, so an operator who
    knowingly runs without disk budgets does so visibly rather than by
    discovering a warning afterwards.
    """
    caps = check()
    blocking = [c for c in caps if c.blocking and c.name not in waive]
    if not blocking:
        return
    log("\033[1m▸ preflight\033[0m")
    report(caps, log)
    names = ", ".join(c.name for c in blocking)
    raise SystemExit(
        f"\nrefusing to run: {names} unavailable. Each one is a containment "
        f"property CRUCIBLE would otherwise claim and not have. Install the "
        f"declared dependencies, or waive explicitly with "
        f"--waive-capability=<name>.")
