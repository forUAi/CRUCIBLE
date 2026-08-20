"""
CRUCIBLE :: diskbudget.py

A per-sandbox writable-storage limit, enforced by the kernel at write time.

An untrusted repository must not be able to fill the store. It is trivially
easy: a probe fixture wrote 1 GiB into its sandbox with nothing stopping it,
and the same loop would have taken the whole 18 GB store down with every
other job, the layer cache and the evidence on it.

Three designs were considered.

*Watch usage and kill the job.* Rejected: it is not enforcement. `du` is
racy, the write has already landed by the time you notice, and a fast writer
outruns the poll. It also cannot tell you *why* a build failed.

*Give every sandbox its own loop-mounted filesystem.* This enforces
absolutely, and it breaks the feature the whole architecture rests on:
`snapshot()` is a directory rename from `live/` into `layers/`, which is O(1)
only while both are the same filesystem. Across filesystems it silently
degrades to a recursive copy, and the repair loop stops being affordable.
Rejected for that reason, not for cost.

*ext4 project quotas.* Chosen. A project id is an attribute on a directory
tree; the limit is enforced by the kernel on every allocation; the tree stays
on the same filesystem so a snapshot is still a rename. It is also what
container runtimes actually use for `--storage-opt size=`.

Exceeding the budget surfaces as `EDQUOT` from the write that crossed the
line, which the engine already classifies as resource exhaustion rather than
a broken repository -- a distinction that matters, because they are not the
same result.

Quotas need the store to be a filesystem CRUCIBLE created (so it can be made
with the `quota,project` features and mounted `prjquota`). Where that is
impossible the budget is reported UNAVAILABLE and the caller decides; it is
never silently treated as enforced.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Project ids are 32-bit. Reserve low numbers and hash box ids into the rest,
# which keeps assignment stateless -- a reaper does not need a lookup table to
# know which project a directory belongs to.
_PROJ_BASE = 10_000
_PROJ_SPAN = 1_000_000


def project_id_for(box_id: str) -> int:
    h = int(hashlib.sha256(box_id.encode()).hexdigest()[:8], 16)
    return _PROJ_BASE + (h % _PROJ_SPAN)


def _sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


@dataclass
class BudgetState:
    enforced: bool
    reason: str = ""
    limit_mb: int = 0
    project: int = 0
    mountpoint: str = ""

    def describe(self) -> str:
        if self.enforced:
            return f"{self.limit_mb} MB (ext4 project {self.project})"
        return f"UNAVAILABLE ({self.reason})"


def tooling_present() -> bool:
    return bool(shutil.which("setquota") and shutil.which("chattr"))


def mkfs_options() -> str:
    """Features the store filesystem must be created with for quotas to work."""
    return "-O quota,project -E quotatype=prjquota"


def mount_options() -> str:
    return "prjquota"


def quota_active(mountpoint: Path) -> bool:
    """Are project quotas actually on for this mount?

    Checked against /proc/mounts rather than by asking the tooling, because
    the answer that matters is whether the *kernel* is enforcing.
    """
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] == str(mountpoint):
                return "prjquota" in parts[3]
    except OSError:
        pass
    return False


def apply(box_dir: Path, box_id: str, limit_mb: int, mountpoint: Path,
          log=print) -> BudgetState:
    """Confine everything written under `box_dir` to `limit_mb`."""
    if limit_mb <= 0:
        return BudgetState(False, "no budget requested")
    if not tooling_present():
        return BudgetState(False, "setquota/chattr not installed")
    if not quota_active(mountpoint):
        return BudgetState(False, f"{mountpoint} is not mounted with prjquota")

    proj = project_id_for(box_id)
    box_dir.mkdir(parents=True, exist_ok=True)

    # -p sets the project, +P makes it inherit, so files created later are
    # covered too. Without inheritance the limit applies to an empty directory
    # and nothing else, which is the vacuous version of this feature.
    r = _sh(f"chattr -R -p {proj} {box_dir} && chattr -R +P {box_dir}")
    if r.returncode != 0:
        return BudgetState(False, f"chattr failed: {r.stderr.strip()[:80]}")

    blocks = limit_mb * 1024                      # setquota speaks 1K blocks
    r = _sh(f"setquota -P {proj} 0 {blocks} 0 0 {mountpoint}")
    if r.returncode != 0:
        return BudgetState(False, f"setquota failed: {r.stderr.strip()[:80]}")

    log(f"  disk budget: {limit_mb} MB (ext4 project {proj})")
    return BudgetState(True, "", limit_mb, proj, str(mountpoint))


def usage_mb(project: int, mountpoint: Path) -> Optional[float]:
    """Blocks currently charged to this project, in MB."""
    r = _sh(f"repquota -P -O csv {mountpoint}")
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) > 2 and parts[0].strip() == f"#{project}":
            try:
                return int(parts[2]) / 1024
            except ValueError:
                return None
    return None


def release(project: int, mountpoint: Path) -> None:
    """Drop the limit once the box is gone, so project ids can be reused."""
    _sh(f"setquota -P {project} 0 0 0 0 {mountpoint}")
