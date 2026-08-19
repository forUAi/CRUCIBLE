"""
CRUCIBLE :: oci.py

Pull an OCI/Docker image to a plain directory with no daemon, no root
requirement for the download, and no `docker` binary.

Why bother: requiring Docker is the single biggest portability tax on tools
like this. It rules out CI runners, locked-down enterprise hosts, and nested
containers. But an image is just a JSON manifest plus a stack of tarballs
behind an HTTP API with a token endpoint. Reimplementing the pull is ~120
lines and removes the dependency entirely.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path

DEFAULT_REGISTRY = "registry-1.docker.io"
AUTH = "https://auth.docker.io/token"
ACCEPT = ",".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


def _split_ref(ref: str) -> tuple[str, str, str]:
    """'ubuntu:24.04' -> (registry, 'library/ubuntu', '24.04')"""
    registry = DEFAULT_REGISTRY
    if ref.count("/") >= 2 or ("." in ref.split("/")[0] and "/" in ref):
        registry, ref = ref.split("/", 1)
    name, _, tag = ref.partition(":")
    tag = tag or "latest"
    if "/" not in name and registry == DEFAULT_REGISTRY:
        name = f"library/{name}"
    return registry, name, tag


def _get(url: str, token: str | None = None, accept: str = ACCEPT) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _token(registry: str, name: str) -> str | None:
    if registry != DEFAULT_REGISTRY:
        return None
    url = f"{AUTH}?service=registry.docker.io&scope=repository:{name}:pull"
    return json.loads(_get(url, accept="application/json"))["token"]


def pull_rootfs(ref: str, dest: str, arch: str = "amd64", log=print) -> str:
    """Download `ref` and extract its layers into `dest`. Idempotent."""
    dest_p = Path(dest)
    stamp = dest_p / ".crucible-image"
    if stamp.exists() and stamp.read_text().strip() == ref:
        log(f"  rootfs cached: {ref}")
        return str(dest_p)

    registry, name, tag = _split_ref(ref)
    tok = _token(registry, name)
    base = f"https://{registry}/v2/{name}"

    manifest = json.loads(_get(f"{base}/manifests/{tag}", tok))

    # multi-arch index -> pick our platform
    if "manifests" in manifest:
        pick = next(
            (m for m in manifest["manifests"]
             if m.get("platform", {}).get("architecture") == arch
             and m.get("platform", {}).get("os") == "linux"),
            manifest["manifests"][0],
        )
        manifest = json.loads(_get(f"{base}/manifests/{pick['digest']}", tok))

    # The config blob carries Entrypoint/Cmd/Env -- i.e. how the image author
    # said to run it. Fetching it means sidecar commands come from the image
    # rather than from a table we have to keep up to date for every service.
    cfg: dict = {}
    try:
        digest = manifest.get("config", {}).get("digest")
        if digest:
            raw = json.loads(_get(f"{base}/blobs/{digest}", tok, accept="*/*"))
            c = raw.get("config", {}) or {}
            ep = c.get("Entrypoint") or []
            cmd = c.get("Cmd") or []
            cfg = {
                "entrypoint": ep, "cmd": cmd,
                "cmdline": " ".join(ep + cmd),
                "env": dict(e.split("=", 1) for e in (c.get("Env") or []) if "=" in e),
                "workdir": c.get("WorkingDir") or "/",
                "ports": [int(k.split("/")[0]) for k in (c.get("ExposedPorts") or {})
                          if k.split("/")[0].isdigit()],
            }
    except Exception:
        cfg = {}

    layers = manifest.get("layers", [])
    if dest_p.exists():
        shutil.rmtree(dest_p, ignore_errors=True)
    dest_p.mkdir(parents=True, exist_ok=True)

    for i, layer in enumerate(layers, 1):
        log(f"  layer {i}/{len(layers)}  {layer['digest'][7:19]}  "
            f"{layer.get('size', 0) / 1e6:.1f} MB")
        blob = _get(f"{base}/blobs/{layer['digest']}", tok, accept="*/*")
        raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            _extract_with_whiteouts(tf, dest_p)

    stamp.write_text(ref)
    if cfg:
        (dest_p / ".crucible-config.json").write_text(json.dumps(cfg, indent=1))
    return str(dest_p)


def image_config(dest: str) -> dict:
    """Read the cached image config written by pull_rootfs."""
    try:
        return json.loads((Path(dest) / ".crucible-config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_with_whiteouts(tf: tarfile.TarFile, dest: Path) -> None:
    """OCI layers encode deletions as `.wh.<name>` marker files."""
    for member in tf:
        base = os.path.basename(member.name)
        if base.startswith(".wh."):
            if base == ".wh..wh..opq":
                d = dest / os.path.dirname(member.name)
                if d.is_dir():
                    for child in d.iterdir():
                        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            else:
                target = dest / os.path.dirname(member.name) / base[4:]
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
            continue
        try:
            tf.extract(member, dest, filter="tar")
        except (OSError, tarfile.TarError, TypeError):
            try:
                tf.extract(member, dest)
            except Exception:
                pass
