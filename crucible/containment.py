"""
CRUCIBLE :: containment.py

Where the repository's bytes are allowed to live while its code runs.

The workspace overlay gives the repo copy-on-write, so the sandbox can
`rm -rf` it and the source underneath is untouched. That is a *correctness*
property -- it protects the tree from an honest build script. It is not a
containment property, and the difference matters as soon as the lower layer
sits on a filesystem the host shares.

Lima mounts host directories into the guest over virtiofs. Point CRUCIBLE at
a repo inside such a mount and the overlay's lower is host-owned storage: the
overlay redirects writes to the upper, but the guest still has the host's
bytes mapped, and anything that steps outside the overlay -- a symlink out of
the workspace, a `mount --bind` fallback, `--base host` making `/` the lower
-- is touching the host directly. The same applies to 9p, sshfs, NFS and
VirtualBox shares.

So the rule is positional, not permissional: untrusted code never executes
against a lower layer on host-backed storage. Copy it onto the guest's own
disk first. The copy is cheap next to a build, it is thrown away with the
box, and it makes the containment argument independent of how careful every
other code path is.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Filesystems that are a window onto another machine's storage. Anything here
# under the repo path means the bytes are not ours.
HOST_BACKED_FSTYPES = {
    "virtiofs",      # Lima / Colima / Docker Desktop default on macOS
    "9p", "9pfs",    # QEMU / WSL2
    "fuse.sshfs", "sshfs",
    "nfs", "nfs4",
    "cifs", "smb3",
    "vboxsf",        # VirtualBox shared folders
    "fuse.virtiofs",
    "fuse.gvfsd-fuse",
}

MOUNTS = Path("/proc/mounts")


def _mounts() -> list[tuple[str, str]]:
    """[(mountpoint, fstype)], longest mountpoint first."""
    out: list[tuple[str, str]] = []
    try:
        for line in MOUNTS.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                # /proc/mounts octal-escapes spaces and friends
                out.append((parts[1].encode().decode("unicode_escape"), parts[2]))
    except OSError:
        return []
    return sorted(out, key=lambda mp: -len(mp[0]))


def host_backed_mount(path: str | Path) -> tuple[str, str] | None:
    """(mountpoint, fstype) if `path` sits on host-shared storage, else None."""
    try:
        target = str(Path(path).resolve())
    except OSError:
        return None
    for mountpoint, fstype in _mounts():
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            return (mountpoint, fstype) if fstype in HOST_BACKED_FSTYPES else None
    return None


# Never copied into the sandbox: version-control internals are large and
# irrelevant to a build, and the rest are caches that a build will rebuild.
_SKIP = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
         ".gradle", ".m2", "target", ".next", ".nuxt", ".terraform"}


def stage_source(repo: str | Path, dest: str | Path, log=print) -> Path:
    """Copy a repository onto guest-local storage and return the new path.

    Symlinks are copied as symlinks, not followed: following them would pull
    host content in through a link the repo controls, which is the very thing
    this function exists to prevent. A link pointing outside the tree simply
    dangles inside the sandbox, which is the correct outcome.
    """
    repo, dest = Path(repo).resolve(), Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    n_files = n_bytes = 0

    def _ignore(dirname, names):
        return [n for n in names if n in _SKIP]

    def _copy(src, dst, *, follow_symlinks=True):
        nonlocal n_files, n_bytes
        n_files += 1
        try:
            n_bytes += os.lstat(src).st_size
        except OSError:
            pass
        return shutil.copy2(src, dst, follow_symlinks=False)

    shutil.copytree(repo, dest, symlinks=True, ignore=_ignore,
                    copy_function=_copy, ignore_dangling_symlinks=True)
    log(f"  staged source: {n_files} files, {n_bytes / 1e6:.1f} MB -> guest-local {dest}")
    return dest


def describe(repo: str | Path) -> str:
    """One line for the audit log about where this repo's bytes came from."""
    hb = host_backed_mount(repo)
    if hb:
        return f"host-backed ({hb[1]} at {hb[0]}) -- staged to guest-local storage"
    return "guest-local"
