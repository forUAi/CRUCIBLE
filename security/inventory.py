"""
CRUCIBLE :: security/inventory.py

An attributable census of every execution resource, for before/after diffing.

Nothing here matches on a process name. A resource counts as CRUCIBLE's only
when the kernel or our own registry says so: cgroup membership, a mount
source we wrote, a backing filename we wrote, a directory a run recorded, or
a private network namespace held by a process whose working directory is
inside a CRUCIBLE tree. The last of those is reported, never acted on.

Warm caches are listed separately from leaks, because deleting a legitimate
cache to make an inventory look clean is a worse outcome than the leak.

    python3 security/inventory.py --label before --out before.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import lifecycle                                   # noqa: E402
from crucible.backends.namespace import IMAGE_ROOT, STATE_ROOT   # noqa: E402


def _loops() -> list[dict]:
    out = []
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for line in mounts:
        p = line.split()
        if len(p) >= 2 and p[0].startswith("/dev/loop"):
            backing = lifecycle._loop_backing(p[0])
            out.append({"dev": p[0], "mount": p[1], "backing": backing,
                        "deleted": backing.endswith("(deleted)"),
                        "ours": lifecycle.STORE_IMG in backing})
    return out


def _overlays() -> list[dict]:
    return [{"source": s, "mount": m}
            for s, m in lifecycle.overlay_mounts_named()]


def _netns_holders() -> list[dict]:
    return lifecycle.unattributable_netns(STATE_ROOT)


def _cgroups() -> list[dict]:
    if not lifecycle.cgroup_available():
        return []
    return [{"name": g.name, "members": lifecycle.cgroup_members(g.name)}
            for g in sorted(lifecycle.CGROUP_ROOT.glob("crucible-*"))]


def _dirs(pattern: str, base: Path) -> list[str]:
    try:
        return sorted(str(d) for d in base.glob(pattern))
    except OSError:
        return []


def _canaries() -> list[str]:
    """Files a hostile fixture would create if it escaped."""
    hits = []
    for p in ("/etc/crucible-node-canary", "/etc/crucible-go-canary",
              "/etc/crucible-java-canary", "/etc/crucible-python-canary",
              "/etc/crucible-go-traversal"):
        if Path(p).exists():
            hits.append(p)
    for home in Path("/Users").glob("*") if Path("/Users").is_dir() else []:
        c = home / "Projects/crucible/CANARY_WRITTEN_FROM_SANDBOX.txt"
        if c.exists():
            hits.append(str(c))
    return hits


def snapshot() -> dict:
    loops = _loops()
    return {
        "leaks": {
            "orphan_store_mounts": [
                {"dev": d, "mount": m, "backing": b}
                for d, m, b in lifecycle.orphaned_store_mounts()],
            "loop_mounts_deleted_backing": [l for l in loops if l["deleted"]],
            "overlay_mounts": _overlays(),
            "crucible_cgroups": _cgroups(),
            "box_dirs": _dirs("box-*", STATE_ROOT),
            "pod_dirs": _dirs("*", STATE_ROOT / "pods"),
            "registry_records": [r.run_id for r in
                                 lifecycle.Registry(STATE_ROOT).load_all()],
            "release_tmpdirs": _dirs("crucible-release-*", Path("/tmp")),
            "concurrent_state_roots": _dirs("crucible-conc-*", Path("/var/lib")),
            "unattributable_netns_holders": _netns_holders(),
            "escape_canaries": _canaries(),
        },
        "warm_cache": {
            # Expected to persist. Content-addressed, immutable, and shared on
            # purpose: rebuilding these inside every state root is what made a
            # release gate cost 10 GB and most of its runtime.
            "image_cache": {"path": str(IMAGE_ROOT),
                            "entries": len(_dirs("*", IMAGE_ROOT / "images"))},
            "layer_store": {"path": str(STATE_ROOT / "layers"),
                            "entries": len(_dirs("*", STATE_ROOT / "layers"))},
            "bench_fixtures": {
                "path": "/var/lib/crucible-bench-fixtures",
                "entries": len(_dirs("*", Path("/var/lib/crucible-bench-fixtures")))},
            "store_mount": [l for l in loops if l["ours"] and not l["deleted"]],
        },
    }


def leak_count(snap: dict) -> int:
    return sum(len(v) for v in snap["leaks"].values())


def main() -> int:
    ap = argparse.ArgumentParser(prog="inventory")
    ap.add_argument("--label", default="inventory")
    ap.add_argument("--out")
    ap.add_argument("--compare", help="an earlier snapshot to diff against")
    a = ap.parse_args()

    snap = snapshot()
    n = leak_count(snap)
    print(f"\n── {a.label}: {n} attributable leak(s)")
    for k, v in snap["leaks"].items():
        if v:
            print(f"   {k}: {len(v)}")
            for item in v[:4]:
                print(f"      {item}")
    print("   warm cache (expected to persist):")
    for k, v in snap["warm_cache"].items():
        print(f"      {k}: {v if not isinstance(v, dict) else v.get('entries', v)}")

    if a.compare:
        before = json.loads(Path(a.compare).read_text())
        b, c = leak_count(before), n
        print(f"\n   before={b} leak(s), after={c} leak(s), delta={c - b:+d}")
        if c > b:
            print("   ✗ execution leaked resources")
        else:
            print("   ✓ no net leak")
    if a.out:
        Path(a.out).write_text(json.dumps(snap, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
