"""
CRUCIBLE :: evidence.py

Deterministic repo fingerprinting. Zero inference, zero LLM, zero network.
Every Signal cites the file that produced it.

Design rule: this module is NEVER allowed to guess. If it can't read a fact
off disk, it emits nothing and lets the planner deal with the ambiguity.
That separation is what makes the system debuggable -- when a plan is wrong
you can always ask "was the evidence wrong, or the inference?"
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .schema import Evidence, Signal

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist",
    "build", ".next", ".nuxt", "vendor", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages", ".gradle", ".idea", ".terraform", "bower_components",
    ".cargo", "Pods", ".dart_tool", "coverage", ".svelte-kit", "out",
}
MAX_FILES = 20000
READ_LIMIT = 400_000  # bytes; manifests are small, source files we only grep


# ---------------------------------------------------------------------------
# Manifest table: filename -> (language, pkgmgr, weight)
# The weight encodes *how conclusive* the file is. A lockfile is conclusive
# about its package manager; a .py file is weak evidence of Python because
# a JS repo can contain a build script.
# ---------------------------------------------------------------------------

MANIFESTS: dict[str, tuple[str, str, float]] = {
    "package.json":       ("node",   "npm",      0.90),
    "pnpm-lock.yaml":     ("node",   "pnpm",     0.99),
    "yarn.lock":          ("node",   "yarn",     0.99),
    "package-lock.json":  ("node",   "npm",      0.99),
    "bun.lockb":          ("node",   "bun",      0.99),
    "bun.lock":           ("node",   "bun",      0.99),
    "deno.json":          ("deno",   "deno",     0.95),
    "deno.jsonc":         ("deno",   "deno",     0.95),
    "pyproject.toml":     ("python", "pip",      0.90),
    "requirements.txt":   ("python", "pip",      0.90),
    "Pipfile":            ("python", "pipenv",   0.95),
    "poetry.lock":        ("python", "poetry",   0.99),
    "uv.lock":            ("python", "uv",       0.99),
    "pdm.lock":           ("python", "pdm",      0.99),
    "environment.yml":    ("python", "conda",    0.95),
    "setup.py":           ("python", "pip",      0.85),
    "go.mod":             ("go",     "gomod",    0.99),
    "go.work":            ("go",     "gomod",    0.99),
    "Cargo.toml":         ("rust",   "cargo",    0.99),
    "Gemfile":            ("ruby",   "bundler",  0.95),
    "composer.json":      ("php",    "composer", 0.95),
    "pom.xml":            ("java",   "maven",    0.95),
    "build.gradle":       ("java",   "gradle",   0.95),
    "build.gradle.kts":   ("kotlin", "gradle",   0.95),
    "mix.exs":            ("elixir", "mix",      0.99),
    "pubspec.yaml":       ("dart",   "pub",      0.95),
    "Package.swift":      ("swift",  "spm",      0.95),
    "CMakeLists.txt":     ("cpp",    "cmake",    0.85),
    "meson.build":        ("cpp",    "meson",    0.85),
    "stack.yaml":         ("haskell","stack",    0.95),
    "cabal.project":      ("haskell","cabal",    0.90),
    "DESCRIPTION":        ("r",      "renv",     0.70),
    "Makefile":           ("",       "make",     0.40),
    "justfile":           ("",       "just",     0.50),
    "Taskfile.yml":       ("",       "task",     0.50),
}

EXT_LANG = {
    ".py": "python", ".ts": "node", ".tsx": "node", ".js": "node", ".jsx": "node",
    ".mjs": "node", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".java": "java", ".kt": "kotlin", ".ex": "elixir", ".exs": "elixir",
    ".swift": "swift", ".dart": "dart", ".c": "cpp", ".cc": "cpp", ".cpp": "cpp",
    ".hs": "haskell", ".R": "r", ".scala": "scala", ".cs": "dotnet",
    ".ipynb": "notebook", ".sh": "shell",
}

# Version pin files: filename -> (runtime, parser)
PINS = {
    ".nvmrc":            ("node",   lambda t: t.strip().lstrip("v")),
    ".node-version":     ("node",   lambda t: t.strip().lstrip("v")),
    ".python-version":   ("python", lambda t: t.strip()),
    ".ruby-version":     ("ruby",   lambda t: t.strip()),
    "rust-toolchain":    ("rust",   lambda t: t.strip()),
    ".java-version":     ("java",   lambda t: t.strip()),
}

# Runtime service detection: what an import/config string implies
SERVICE_PATTERNS = [
    (r"\b(psycopg2?|asyncpg|pg8000|postgresql://|POSTGRES_|sqlalchemy\.postgres)", "postgres"),
    (r"\b(redis\.|aioredis|ioredis|redis://|REDIS_URL)",                            "redis"),
    (r"\b(pymongo|mongoose|mongodb://|MONGO_URI)",                                  "mongodb"),
    (r"\b(mysql\.connector|pymysql|mysql2|mysql://)",                               "mysql"),
    (r"\b(elasticsearch|opensearch-py|ELASTICSEARCH_)",                             "elasticsearch"),
    (r"\b(kafka-python|confluent_kafka|kafkajs|KAFKA_BROKER)",                      "kafka"),
    (r"\b(pika|amqp://|RABBITMQ_)",                                                 "rabbitmq"),
]

# Native build requirements: source pattern -> apt packages
NATIVE_PATTERNS = [
    (r"\bpsycopg2\b(?!-binary)",  ["libpq-dev", "gcc"]),
    (r"\b(lxml|libxml)",          ["libxml2-dev", "libxslt1-dev"]),
    (r"\bmysqlclient\b",          ["default-libmysqlclient-dev", "pkg-config"]),
    (r"\b(Pillow|PIL)\b",         ["libjpeg-dev", "zlib1g-dev"]),
    (r"\b(cv2|opencv)",           ["libgl1", "libglib2.0-0"]),
    (r"\b(cffi|cryptography)\b",  ["libffi-dev", "libssl-dev"]),
    (r"\bnode-gyp\b",             ["python3", "make", "g++"]),
    (r"\b(canvas|sharp)\b",       ["libcairo2-dev", "libpango1.0-dev"]),
    (r"\b(numpy|scipy|pandas)\b", ["gfortran", "libopenblas-dev"]),
    (r"\bplaywright\b",           ["libnss3", "libatk1.0-0", "libgbm1"]),
    (r"\b(ffmpeg|pydub|moviepy)", ["ffmpeg"]),
]

# Framework -> (archetype, default port, run hint)
FRAMEWORKS = {
    "django":    ("web", 8000, "python manage.py runserver 0.0.0.0:8000"),
    "flask":     ("web", 5000, "flask run --host=0.0.0.0"),
    "fastapi":   ("web", 8000, "uvicorn {module}:app --host 0.0.0.0 --port 8000"),
    "uvicorn":   ("web", 8000, ""),
    "gunicorn":  ("web", 8000, ""),
    "express":   ("web", 3000, ""),
    "fastify":   ("web", 3000, ""),
    "next":      ("web", 3000, ""),
    "nuxt":      ("web", 3000, ""),
    "vite":      ("web", 5173, ""),
    "remix":     ("web", 3000, ""),
    "nest":      ("web", 3000, ""),
    "rails":     ("web", 3000, ""),
    "sinatra":   ("web", 4567, ""),
    "gin-gonic": ("web", 8080, ""),
    "fiber":     ("web", 3000, ""),
    "echo":      ("web", 8080, ""),
    "axum":      ("web", 3000, ""),
    "actix-web": ("web", 8080, ""),
    "rocket":    ("web", 8000, ""),
    "spring-boot": ("web", 8080, ""),
    "phoenix":   ("web", 4000, ""),
    "laravel":   ("web", 8000, ""),
    "streamlit": ("web", 8501, "streamlit run {entry} --server.address 0.0.0.0"),
    "gradio":    ("web", 7860, ""),
    "celery":    ("worker", 0, ""),
}

PORT_RE = re.compile(
    r"(?:listen\(|PORT\s*[=:]\s*|port\s*[=:]\s*|EXPOSE\s+|--port[= ]|:)(\d{2,5})\b"
)


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".venv")]
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth > 6:
            dirnames.clear()
        for f in filenames:
            out.append(Path(dirpath) / f)
            if len(out) >= MAX_FILES:
                return out
    return out


def _read(p: Path, limit: int = READ_LIMIT) -> str:
    try:
        with open(p, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def collect(root: str) -> Evidence:
    """Walk a repo and produce Evidence. Pure function of the filesystem."""
    rootp = Path(root).resolve()
    ev = Evidence(root=str(rootp))
    paths = _walk(rootp)
    ev.files = sorted(str(p.relative_to(rootp)) for p in paths)

    add = ev.signals.append
    depth_of = lambda rel: rel.count("/")

    # ---- 1. manifests ---------------------------------------------------
    for p in paths:
        rel = str(p.relative_to(rootp))
        name = p.name
        if name in MANIFESTS:
            lang, pm, w = MANIFESTS[name]
            # a manifest at repo root is far more meaningful than one 4 levels deep
            decay = 0.5 ** depth_of(rel)
            if lang:
                add(Signal("language", lang, w * decay, rel))
            add(Signal("pkgmgr", pm, w * decay, rel))
        if name.endswith(".csproj") or name.endswith(".sln"):
            add(Signal("language", "dotnet", 0.95, rel))
            add(Signal("pkgmgr", "dotnet", 0.95, rel))

    # ---- 2. file extensions (weak, volume-weighted) ---------------------
    ext_counts: dict[str, int] = {}
    for p in paths:
        lang = EXT_LANG.get(p.suffix)
        if lang:
            ext_counts[lang] = ext_counts.get(lang, 0) + 1
    total = sum(ext_counts.values()) or 1
    for lang, n in ext_counts.items():
        add(Signal("language", lang, min(0.55, 0.55 * n / total * 3), f"<{n} *{lang} files>"))

    # ---- 3. version pins ------------------------------------------------
    for p in paths:
        if p.name in PINS and depth_of(str(p.relative_to(rootp))) == 0:
            rt, parse = PINS[p.name]
            v = parse(_read(p, 200))
            if v:
                add(Signal("runtime", f"{rt}:{v}", 0.99, str(p.relative_to(rootp))))
        if p.name == ".tool-versions":
            for line in _read(p, 4000).splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    add(Signal("runtime", f"{parts[0]}:{parts[1]}", 0.99, ".tool-versions"))

    # ---- 4. declared container / process config -------------------------
    for p in paths:
        rel = str(p.relative_to(rootp))
        if depth_of(rel) > 2:
            continue
        n = p.name
        if n == "Dockerfile" or n.startswith("Dockerfile."):
            ev.declared["dockerfile"] = rel
        elif n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            ev.declared["compose"] = rel
        elif n == "devcontainer.json" or rel.endswith(".devcontainer/devcontainer.json"):
            ev.declared["devcontainer"] = rel
        elif n == "Procfile":
            ev.declared["procfile"] = rel
        elif n in ("flake.nix", "shell.nix", "default.nix"):
            ev.declared["nix"] = rel
        elif n == "Makefile":
            ev.declared["makefile"] = rel

    # ---- 5. structured manifest parsing --------------------------------
    _parse_package_json(rootp, ev)
    _parse_python(rootp, ev)
    _parse_go(rootp, ev)
    _parse_cargo(rootp, ev)

    # ---- 6. content grep: services, native deps, ports, frameworks ------
    grep_budget = 900
    corpus_parts: list[str] = []
    for p in paths:
        if grep_budget <= 0:
            break
        if p.suffix in EXT_LANG or p.name in MANIFESTS or p.suffix in (".toml", ".yaml", ".yml", ".env", ".cfg", ".ini"):
            corpus_parts.append(_read(p, 60_000))
            grep_budget -= 1
    corpus = "\n".join(corpus_parts)

    for pat, svc in SERVICE_PATTERNS:
        if re.search(pat, corpus, re.I):
            add(Signal("service", svc, 0.8, "<source scan>"))
    for pat, pkgs in NATIVE_PATTERNS:
        if re.search(pat, corpus):
            ev.native_hints.extend(pkgs)
    ev.native_hints = sorted(set(ev.native_hints))

    # Framework names collide across ecosystems ("echo" is a Go framework AND
    # a shell builtin; "rocket", "fiber", "next" are all common English). Gate
    # each framework on its own language actually being present, or the grep
    # turns every npm script into a false positive.
    FW_LANG = {
        "django": "python", "flask": "python", "fastapi": "python", "uvicorn": "python",
        "gunicorn": "python", "streamlit": "python", "gradio": "python", "celery": "python",
        "express": "node", "fastify": "node", "next": "node", "nuxt": "node",
        "vite": "node", "remix": "node", "nest": "node",
        "gin-gonic": "go", "fiber": "go", "echo": "go",
        "axum": "rust", "actix-web": "rust", "rocket": "rust",
        "rails": "ruby", "sinatra": "ruby", "laravel": "php",
        "spring-boot": "java", "phoenix": "elixir",
    }
    present = {v for v, w in ev.tally("language") if w >= 0.35}
    for fw in FRAMEWORKS:
        need = FW_LANG.get(fw)
        if need and need not in present:
            continue
        if re.search(rf"\b{re.escape(fw)}\b", corpus, re.I):
            add(Signal("framework", fw, 0.75, "<source scan>"))

    ports = {int(m) for m in PORT_RE.findall(corpus) if 80 <= int(m) <= 65535}
    ev.ports = sorted(p for p in ports if p in {
        80, 3000, 3001, 4000, 4200, 4567, 5000, 5173, 5432, 6379, 7860,
        8000, 8080, 8081, 8501, 8888, 9000, 9090,
    })

    # ---- 7. env keys from .env.example ---------------------------------
    for p in paths:
        if p.name in (".env.example", ".env.sample", ".env.template", ".env.dist"):
            for line in _read(p, 20_000).splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    ev.env_keys.append(line.split("=", 1)[0].strip())
    ev.env_keys = sorted(set(ev.env_keys))

    # ---- 8. entrypoint candidates --------------------------------------
    ev.entrypoints = _rank_entrypoints(rootp, ev)

    # ---- 9. Makefile / justfile targets --------------------------------
    for key, path in (("makefile", ev.declared.get("makefile")), ):
        if path:
            for m in re.finditer(r"^([a-zA-Z][\w\-]*):(?!=)", _read(rootp / path, 30_000), re.M):
                ev.scripts[f"make {m.group(1)}"] = f"make {m.group(1)}"

    return ev


# ---------------------------------------------------------------------------
# Manifest parsers -- structured, so we get exact deps not regex guesses
# ---------------------------------------------------------------------------

def _parse_package_json(root: Path, ev: Evidence) -> None:
    for rel in [f for f in ev.files if f.endswith("package.json") and f.count("/") <= 2]:
        try:
            data = json.loads(_read(root / rel))
        except (json.JSONDecodeError, ValueError):
            continue
        for k, v in (data.get("scripts") or {}).items():
            ev.scripts[k] = f"npm run {k}"
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for d in deps:
            base = d.split("/")[-1].replace("@", "")
            if base in FRAMEWORKS:
                ev.signals.append(Signal("framework", base, 0.95, rel))
            if d in ("next", "nuxt", "vite", "@remix-run/dev", "astro"):
                ev.signals.append(Signal("framework", d.split("/")[0].lstrip("@"), 0.95, rel))
        if pm := data.get("packageManager"):
            ev.signals.append(Signal("pkgmgr", pm.split("@")[0], 0.99, rel))
        if eng := (data.get("engines") or {}).get("node"):
            v = re.sub(r"[^\d.]", "", eng.split("||")[0]).strip(".")
            if v:
                ev.signals.append(Signal("runtime", f"node:{v.split('.')[0]}", 0.9, rel))
        if data.get("workspaces"):
            ws = data["workspaces"]
            ev.workspaces = ws if isinstance(ws, list) else ws.get("packages", [])
        if data.get("type") == "module":
            ev.signals.append(Signal("trait", "esm", 0.9, rel))


def _parse_python(root: Path, ev: Evidence) -> None:
    for rel in [f for f in ev.files if f.endswith(("requirements.txt", "pyproject.toml", "setup.py"))]:
        txt = _read(root / rel, 200_000)
        for fw in ("django", "flask", "fastapi", "streamlit", "gradio", "celery", "uvicorn", "gunicorn"):
            if re.search(rf"^\s*['\"]?{fw}\b", txt, re.M | re.I) or f'"{fw}' in txt.lower():
                ev.signals.append(Signal("framework", fw, 0.95, rel))
        if m := re.search(r'requires-python\s*=\s*["\'][^\d]*(\d+\.\d+)', txt):
            ev.signals.append(Signal("runtime", f"python:{m.group(1)}", 0.9, rel))
        if "[tool.poetry]" in txt:
            ev.signals.append(Signal("pkgmgr", "poetry", 0.95, rel))
        if "[tool.uv]" in txt:
            ev.signals.append(Signal("pkgmgr", "uv", 0.9, rel))


def _parse_go(root: Path, ev: Evidence) -> None:
    if "go.mod" not in ev.files:
        return
    txt = _read(root / "go.mod", 100_000)
    if m := re.search(r"^go\s+(\d+\.\d+)", txt, re.M):
        ev.signals.append(Signal("runtime", f"go:{m.group(1)}", 0.99, "go.mod"))
    for fw in ("gin-gonic", "fiber", "echo", "chi"):
        if fw in txt:
            ev.signals.append(Signal("framework", fw, 0.9, "go.mod"))


def _parse_cargo(root: Path, ev: Evidence) -> None:
    if "Cargo.toml" not in ev.files:
        return
    txt = _read(root / "Cargo.toml", 100_000)
    if "[workspace]" in txt:
        ev.workspaces.append("<cargo workspace>")
    for fw in ("axum", "actix-web", "rocket", "warp", "tokio"):
        if re.search(rf"^{re.escape(fw)}\s*=", txt, re.M):
            ev.signals.append(Signal("framework", fw, 0.9, "Cargo.toml"))


# ---------------------------------------------------------------------------
# Entrypoint ranking
# ---------------------------------------------------------------------------

ENTRY_SCORES = [
    (re.compile(r"^(src/)?main\.(py|go|rs|ts|js)$"),            1.00),
    (re.compile(r"^(src/)?(app|server|index)\.(py|ts|js|mjs)$"), 0.95),
    (re.compile(r"^manage\.py$"),                                0.98),
    (re.compile(r"^cmd/[^/]+/main\.go$"),                        0.95),
    (re.compile(r"^(src/)?bin/.*$"),                             0.70),
    (re.compile(r"^app/main\.py$"),                              0.90),
    (re.compile(r"^(src/)?cli\.(py|ts|js)$"),                    0.80),
    (re.compile(r"^wsgi\.py$|^asgi\.py$"),                       0.85),
    (re.compile(r"^.*\.ipynb$"),                                 0.50),
]


def _rank_entrypoints(root: Path, ev: Evidence) -> list[dict]:
    out = []
    for rel in ev.files:
        for rx, score in ENTRY_SCORES:
            if rx.match(rel):
                kind = "notebook" if rel.endswith(".ipynb") else "module"
                # bonus if the file actually looks executable
                txt = _read(root / rel, 8000)
                if '__main__' in txt or "func main(" in txt or "fn main(" in txt:
                    score = min(1.0, score + 0.1)
                out.append({"path": rel, "kind": kind, "score": round(score, 2)})
                break
    return sorted(out, key=lambda e: -e["score"])[:8]
