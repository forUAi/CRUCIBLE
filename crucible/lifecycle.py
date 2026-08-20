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


STORE_IMG = "crucible-store.img"


def orphaned_store_mounts() -> list[tuple[str, str, str]]:
    """[(device, mountpoint, backing)] for store filesystems nothing can reach.

    A release verification points CRUCIBLE_STATE at a private directory, so
    ensure_private_store builds a loop-mounted ext4 image there. verify.py
    then rmtree'd that directory while the image was still mounted:
    shutil.rmtree cannot remove a mountpoint, ignore_errors swallowed the
    failure, and the backing file -- which sits beside the mountpoint, not
    under it -- was unlinked. The result is a mounted ~10 GB filesystem on a
    deleted file that nothing will ever unmount.

    Five of them had accumulated, one per release gate run, holding 5 GB of
    page cache in a 6 GiB VM and provoking seven OOM kills. That is what made
    a 14-second benchmark take 2000 seconds.

    Ownership is exact on two counts: the backing file carries a name only
    CRUCIBLE writes, and the kernel reports it as deleted, so no live
    configuration can still refer to it.
    """
    out = []
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for line in mounts:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/loop"):
            continue
        dev = parts[0]
        backing = _loop_backing(dev)
        if STORE_IMG in backing and backing.endswith("(deleted)"):
            out.append((dev, parts[1].encode().decode("unicode_escape"), backing))
    return out


def _loop_backing(dev: str) -> str:
    name = dev.rsplit("/", 1)[-1]
    try:
        return Path(f"/sys/block/{name}/loop/backing_file").read_text().strip()
    except OSError:
        return ""


def reclaim_store_mounts(log=print) -> int:
    """Unmount and detach store filesystems whose image is already deleted."""
    n = 0
    for dev, mp, backing in orphaned_store_mounts():
        _sh(f"umount -l {mp}")
        _sh(f"losetup -d {dev}")
        log(f"  reclaimed orphaned store {dev} at {mp} ({backing})")
        n += 1
    return n


def unattributable_netns(state_root: Path) -> list[dict]:
    """Live processes holding a private netns that no run record explains.

    A pause container from a run that predates this registry cannot be
    reclaimed: there is no cgroup naming it and no record listing it, and
    killing `sleep 86400` by name would take out somebody's backup script.
    One such process survived here for nearly three hours while every
    diagnostic stayed silent about it.

    So it is REPORTED instead, with the evidence needed to act on it by hand:
    pid, start time, the namespace held, and the working directory. That
    satisfies the rule that every surviving resource is attributable to an
    active run, to intentional cache state, or to a clearly reported defect --
    this is the third kind.
    """
    known = {r.cgroup for r in Registry(state_root).load_all() if r.alive()}
    try:
        init_net = os.readlink("/proc/1/ns/net")
    except OSError:
        return []
    out = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            net = os.readlink(f"/proc/{pid}/ns/net")
            if net == init_net:
                continue
            cg = Path(f"/proc/{pid}/cgroup").read_text().strip()
            if any(k and k in cg for k in known) or CG_PREFIX in cg:
                continue                      # a live run already owns it
            cwd = os.readlink(f"/proc/{pid}/cwd")
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        # Report only what plausibly came from here: a private netns AND a
        # working directory inside a CRUCIBLE tree. Neither alone is enough,
        # and neither is a licence to kill it.
        if "crucible" not in cwd.lower():
            continue
        out.append({"pid": pid, "comm": comm, "netns": net, "cwd": cwd,
                    "starttime": pid_starttime(pid), "cgroup": cg})
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
    stores_reclaimed: int = 0
    errors: list[str] = field(default_factory=list)

    def anything(self) -> bool:
        return bool(self.stores_reclaimed or self.runs_reaped
                    or self.processes_killed
                    or self.cgroups_removed or self.mounts_released
                    or self.dirs_removed)

    def summary(self) -> str:
        extra = (f", {self.stores_reclaimed} orphaned store mount(s)"
                 if self.stores_reclaimed else "")
        return (f"reaped {self.runs_reaped} abandoned run(s): "
                f"{self.processes_killed} process(es), "
                f"{self.cgroups_removed} cgroup(s), "
                f"{self.mounts_released} mount(s), "
                f"{self.dirs_removed} dir(s)" + extra)


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

    # Store filesystems are not owned by any single run -- they outlive every
    # one of them -- so they are reclaimed by evidence rather than by
    # registry lookup.
    if not dry_run:
        rep.stores_reclaimed = reclaim_store_mounts(lambda *_: None)

    # Reported, never auto-killed: there is no ownership evidence for these,
    # and acting without it is how a reaper kills a stranger.
    for o in unattributable_netns(state_root):
        rep.errors.append(
            f"unattributable netns holder: pid={o['pid']} comm={o['comm']!r} "
            f"ns={o['netns']} cwd={o['cwd']} -- predates run ownership or its "
            f"record was lost; verify and remove by hand")

    if rep.anything() or rep.errors:
        log(f"  {rep.summary()}")
        for e in rep.errors:
            log(f"  ! {e}")
    return rep
