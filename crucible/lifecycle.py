"""
CRUCIBLE :: lifecycle.py

Every resource a run creates is owned state with an owner, and cleanup is a
question about ownership, not about names.

The leak that motivated this module: a run was SIGKILLed while its pod was
up, and `sleep 86400` -- the pause container holding the network namespace --
was orphaned to init and sat there for hours. `Pod.down()` kills it, and
`Pod.down()` never ran. Cleanup that only happens on the way out is not
cleanup; a process that can be killed cannot be relied on to tidy up after
being killed.

The obvious repair is to find strays by name later. That is the wrong answer
and dangerous: `pkill -f "sleep 86400"` will happily kill someone's backup
script. Twice in this project a `pgrep` pattern matched the inspection
command that contained it, and once a benchmark reported four repositories as
broken because `~` expanded to /root. Name matching is not ownership
evidence.

So ownership is recorded, and it is recorded in two places that a dying
process cannot forget to update:

  1. A cgroup per run. Membership is tracked by the kernel, survives the
     engine, and `cgroup.kill` terminates exactly its members and nothing
     else. There is no pattern to get wrong.

  2. A registry file per run, naming the cgroup, the directories and the
     mount sources, stamped with the owner's pid AND that pid's start time.
     The start time is what makes it safe: pids are reused, and a stale
     registry pointing at a recycled pid would otherwise authorise killing a
     stranger.

A later run reaps what an earlier crashed run abandoned, because the later
run can prove the earlier owner is gone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CGROUP_ROOT = Path("/sys/fs/cgroup")
CG_PREFIX = "crucible-"


def _sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# pid identity
# ---------------------------------------------------------------------------

def pid_starttime(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat: clock ticks since boot when it started.

    (pid, starttime) identifies a process for the lifetime of the boot. pid
    alone does not -- the kernel recycles them, and a registry that trusted a
    bare pid would eventually authorise killing an unrelated process that
    happened to inherit the number.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm can contain spaces and parentheses; everything after the last ')'
    # is positional.
    try:
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[19])          # stat field 22, 0-indexed here as 19
    except (ValueError, IndexError):
        return None


def owner_alive(pid: int, starttime: Optional[int]) -> bool:
    """True only if this exact process is still running."""
    if pid <= 0:
        return False
    now = pid_starttime(pid)
    if now is None:
        return False
    if starttime is None:
        return True                      # pre-registry record; pid exists
    return now == starttime


# ---------------------------------------------------------------------------
# cgroups: kernel-tracked membership
# ---------------------------------------------------------------------------

def cgroup_available() -> bool:
    return (CGROUP_ROOT / "cgroup.controllers").exists()


def cgroup_path(name: str) -> Path:
    return CGROUP_ROOT / name


def cgroup_create(name: str, mem_mb: int = 0, pid_max: int = 0,
                  cpu_pct: int = 0) -> bool:
    if not cgroup_available():
        return False
    g = cgroup_path(name)
    try:
        g.mkdir(exist_ok=True)
    except OSError:
        return False
    if mem_mb:
        _write(g / "memory.max", str(mem_mb * 1024 * 1024))
    if pid_max:
        _write(g / "pids.max", str(pid_max))
    if cpu_pct:
        _write(g / "cpu.max", f"{cpu_pct * 1000} 100000")
    return True


def cgroup_attach(name: str, pid: int) -> bool:
    return _write(cgroup_path(name) / "cgroup.procs", str(pid))


def cgroup_members(name: str) -> list[int]:
    try:
        return [int(l) for l in
                (cgroup_path(name) / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []


def cgroup_kill(name: str) -> int:
    """Kill exactly the members of this cgroup. Returns how many were there.

    `cgroup.kill` (cgroup v2, Linux 5.14+) is atomic and recursive: no pid
    list to race against, no pattern to mismatch, and by construction it
    cannot reach a process that is not a member.
    """
    g = cgroup_path(name)
    if not g.is_dir():
        return 0
    n = len(cgroup_members(name))
    if not _write(g / "cgroup.kill", "1"):
        # Older kernel: fall back to signalling the members we can enumerate.
        # Still exact -- membership came from the kernel, not from a name.
        for pid in cgroup_members(name):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    for _ in range(20):
        if not cgroup_members(name):
            break
        time.sleep(0.05)
    return n


def cgroup_remove(name: str) -> None:
    try:
        cgroup_path(name).rmdir()
    except OSError:
        pass


def _write(p: Path, v: str) -> bool:
    try:
        p.write_text(v)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    run_id: str
    pid: int
    starttime: Optional[int]
    cgroup: str = ""
    dirs: list[str] = field(default_factory=list)
    mount_sources: list[str] = field(default_factory=list)
    started: float = 0.0

    def alive(self) -> bool:
        return owner_alive(self.pid, self.starttime)


class Registry:
    """One JSON file per run under <state>/runs/."""

    def __init__(self, state_root: Path):
        self.dir = Path(state_root) / "runs"

    def path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def open(self, run_id: str, cgroup: str = "") -> RunRecord:
        rec = RunRecord(run_id=run_id, pid=os.getpid(),
                        starttime=pid_starttime(os.getpid()),
                        cgroup=cgroup, started=time.time())
        self.write(rec)
        return rec

    def write(self, rec: RunRecord) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path(rec.run_id).with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(rec), indent=2))
            tmp.replace(self.path(rec.run_id))   # atomic; never a torn record
        except OSError:
            pass

    def load_all(self) -> list[RunRecord]:
        out = []
        if not self.dir.is_dir():
            return out
        for f in sorted(self.dir.glob("*.json")):
            try:
                out.append(RunRecord(**json.loads(f.read_text())))
            except (OSError, ValueError, TypeError):
                f.unlink(missing_ok=True)        # unreadable record is garbage
        return out

    def close(self, run_id: str) -> None:
        self.path(run_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# reaping
# ---------------------------------------------------------------------------

def overlay_mounts_named(prefix: str = CG_PREFIX) -> list[tuple[str, str]]:
    """[(source, mountpoint)] for overlays WE named.

    The backend mounts with `mount -t overlay crucible-<box-id>`, so the
    source field is a name only CRUCIBLE writes. That is ownership evidence,
    unlike matching on the mountpoint path.
    """
    out = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "overlay" and parts[0].startswith(prefix):
                out.append((parts[0],
                            parts[1].encode().decode("unicode_escape")))
    except OSError:
        pass
    return out


@dataclass
class ReapReport:
    runs_examined: int = 0
    runs_reaped: int = 0
    processes_killed: int = 0
    cgroups_removed: int = 0
    mounts_released: int = 0
    dirs_removed: int = 0
    skipped_live: int = 0
    errors: list[str] = field(default_factory=list)

    def anything(self) -> bool:
        return bool(self.runs_reaped or self.processes_killed
                    or self.cgroups_removed or self.mounts_released
                    or self.dirs_removed)

    def summary(self) -> str:
        return (f"reaped {self.runs_reaped} abandoned run(s): "
                f"{self.processes_killed} process(es), "
                f"{self.cgroups_removed} cgroup(s), "
                f"{self.mounts_released} mount(s), "
                f"{self.dirs_removed} dir(s)")


def reap(state_root: Path, log=print, dry_run: bool = False) -> ReapReport:
    """Release everything owned by runs that are demonstrably gone.

    Order matters: kill the processes first, because a live process holds the
    mount busy and a busy mount blocks the directory removal.
    """
    rep = ReapReport()
    reg = Registry(state_root)

    live_cgroups: set[str] = set()
    for rec in reg.load_all():
        rep.runs_examined += 1
        if rec.alive():
            rep.skipped_live += 1
            if rec.cgroup:
                live_cgroups.add(rec.cgroup)
            continue
        if dry_run:
            rep.runs_reaped += 1
            continue
        rep.runs_reaped += 1
        if rec.cgroup:
            rep.processes_killed += cgroup_kill(rec.cgroup)
            cgroup_remove(rec.cgroup)
            rep.cgroups_removed += 1
        for src, mp in overlay_mounts_named():
            if src == rec.cgroup or src.startswith(f"{CG_PREFIX}{rec.run_id}"):
                _sh(f"umount -l {mp}")
                rep.mounts_released += 1
        for d in rec.dirs:
            # A dir may still hold nested mounts (workspace overlay, /dev/shm)
            # that were never in our list; release them before deleting.
            for src, mp in overlay_mounts_named():
                if mp.startswith(d.rstrip("/") + "/"):
                    _sh(f"umount -l {mp}")
                    rep.mounts_released += 1
            _sh(f"umount -l {d}/merged/dev/shm")
            shutil.rmtree(d, ignore_errors=True)
            if not Path(d).exists():
                rep.dirs_removed += 1
        reg.close(rec.run_id)

    # Cgroups with no registry entry at all: a run that died before it could
    # write one, or a record already removed. Only reap the empty ones or
    # those whose members are all gone -- never one a live run still owns.
    if not dry_run and cgroup_available():
        for g in sorted(CGROUP_ROOT.glob(f"{CG_PREFIX}*")):
            if g.name in live_cgroups:
                continue
            if cgroup_members(g.name):
                continue                       # somebody is in there; leave it
            cgroup_remove(g.name)
            rep.cgroups_removed += 1

    if rep.anything():
        log(f"  {rep.summary()}")
    return rep
