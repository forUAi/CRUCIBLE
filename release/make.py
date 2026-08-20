"""
CRUCIBLE :: release/make.py

Build a versioned, checksummed release artifact.

Deterministic on purpose: fixed mtimes, fixed uid/gid, sorted entries, fixed
compression level. Two builds of the same tree must produce the same bytes,
because the whole point of the checksum is to let the verifier prove that the
thing it tested is the thing that ships. A tarball that embeds the build
clock cannot support that claim.

    python3 release/make.py --out dist/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What ships. Tests and fixtures are INCLUDED: a release you cannot verify in
# place is a release whose verification happened somewhere else.
INCLUDE = ["crucible", "tests", "security", "bench", "examples", "release",
           "README.md", "SETUP.md", "AUDIT.md", "THREAT_MODEL.md",
           ".lima/crucible.yaml"]
EXCLUDE_SUFFIX = (".pyc", ".pyo")
EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache"}
EPOCH = 1700000000          # fixed; never time.time()


def version() -> str:
    v = (ROOT / "crucible" / "__init__.py").read_text()
    for line in v.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"\'')
    return "0.0.0"


def _files() -> list[Path]:
    out: list[Path] = []
    for entry in INCLUDE:
        p = ROOT / entry
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and not f.name.endswith(EXCLUDE_SUFFIX) \
                        and not (set(f.parts) & EXCLUDE_DIRS):
                    out.append(f)
    return sorted(out)


def build(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ver = version()
    name = f"crucible-{ver}"
    tar_path = out_dir / f"{name}.tar.gz"

    files = _files()
    # Manifest first: the verifier checks each file individually, so a
    # single-byte change anywhere is attributable rather than just "the hash
    # moved".
    manifest = {
        "name": "crucible",
        "version": ver,
        "source_commit": _git("rev-parse", "HEAD"),
        "source_dirty": bool(_git("status", "--porcelain")),
        "files": {},
    }
    for f in files:
        rel = str(f.relative_to(ROOT))
        manifest["files"][rel] = hashlib.sha256(f.read_bytes()).hexdigest()

    blob = json.dumps(manifest, indent=2, sort_keys=True).encode()

    # gzip writes its OWN mtime into the header, so fixing tar member times
    # was not enough: two builds of one tree produced different bytes. Drive
    # the gzip layer explicitly with a fixed mtime.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        def add(rel: str, data: bytes, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size = len(data)
            info.mtime = EPOCH
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tf.addfile(info, io.BytesIO(data))

        add("MANIFEST.json", blob)
        for f in files:
            rel = str(f.relative_to(ROOT))
            mode = 0o755 if f.stat().st_mode & 0o100 else 0o644
            add(rel, f.read_bytes(), mode)

    import gzip
    packed = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=packed, mode="wb",
                       compresslevel=9, mtime=EPOCH) as gz:
        gz.write(raw.getvalue())
    data = packed.getvalue()
    tar_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (out_dir / f"{name}.tar.gz.sha256").write_text(f"{digest}  {name}.tar.gz\n")

    return {"artifact": str(tar_path), "sha256": digest, "version": ver,
            "files": len(files), "bytes": len(data),
            "source_commit": manifest["source_commit"],
            "source_dirty": manifest["source_dirty"]}


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="make")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    info = build(Path(a.out))
    for k, v in info.items():
        print(f"  {k:15} {v}")
    if info["source_dirty"]:
        print("  ! working tree is dirty; this artifact is not reproducible "
              "from the recorded commit")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
