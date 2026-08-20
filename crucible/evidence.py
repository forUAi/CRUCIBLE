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
    "quarkus":   ("web", 8080, ""),
    "micronaut": ("web", 8080, ""),
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
    _parse_jvm(rootp, ev)
    _parse_ruby(rootp, ev)
    _parse_php(rootp, ev)
    _parse_elixir(rootp, ev)

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
        "spring-boot": "java", "quarkus": "java", "micronaut": "java",
        "phoenix": "elixir",
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
        if nm := data.get("name"):
            _add_identity(ev, nm, rel)
        # Only the root package's scripts are runnable as `npm run X` from the
        # repo root. Merging a nested package's scripts made @nestjs/core look
        # like it had a start script when the sample project did.
        if "/" not in rel:
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


PY_FRAMEWORKS = ("django", "flask", "fastapi", "streamlit", "gradio",
                 "celery", "uvicorn", "gunicorn")


def _req_name(spec: str) -> str:
    """`Flask>=3.0,<4 ; python_version>'3.9'` -> `flask`."""
    return re.split(r"[<>=!~\[;\s(]", spec.strip(), maxsplit=1)[0].strip().lower()


def _pyproject_deps(txt: str) -> tuple[list[str], str | None]:
    """Read declared dependencies out of a pyproject, structurally.

    A regex over this file cannot work. pallets/flask's own pyproject contains
    `name = "Flask"`, `"Framework :: Flask"`, a console script
    `flask = "flask.cli:main"` and `source = ["flask", "tests"]` -- four
    self-references and not one dependency. Reading the actual dependency
    tables is the difference between "mentions flask" and "depends on flask".
    """
    try:
        import tomllib
    except ImportError:                       # pragma: no cover -- py<3.11
        return [], None
    try:
        data = tomllib.loads(txt)
    except Exception:
        return [], None

    out: list[str] = []
    proj = data.get("project") or {}
    out += [d for d in (proj.get("dependencies") or []) if isinstance(d, str)]
    for group in (proj.get("optional-dependencies") or {}).values():
        out += [d for d in group if isinstance(d, str)]
    for group in (data.get("dependency-groups") or {}).values():   # PEP 735
        out += [d for d in group if isinstance(d, str)]
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    out += list((poetry.get("dependencies") or {}).keys())
    for group in (poetry.get("group") or {}).values():
        out += list((group.get("dependencies") or {}).keys())
    return out, (proj.get("name") or poetry.get("name"))


def _parse_python(root: Path, ev: Evidence) -> None:
    for rel in [f for f in ev.files
                if f.endswith(("requirements.txt", "pyproject.toml", "setup.py"))]:
        txt = _read(root / rel, 200_000)
        deps: list[str] = []
        name: str | None = None

        if rel.endswith("pyproject.toml"):
            raw, name = _pyproject_deps(txt)
            deps = [_req_name(d) for d in raw]
        elif rel.endswith("requirements.txt"):
            for line in txt.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    deps.append(_req_name(line))
        else:                                   # setup.py
            if m := re.search(r"install_requires\s*=\s*\[([^\]]*)\]", txt, re.S):
                deps = [_req_name(x) for x in re.findall(r"['\"]([^'\"]+)['\"]",
                                                        m.group(1))]
            if m := re.search(r"""name\s*=\s*['\"]([\w.\-]+)['\"]""", txt):
                name = m.group(1)

        seen = set(deps)
        for fw in PY_FRAMEWORKS:
            if fw in seen:
                ev.signals.append(Signal("framework", fw, 0.95, rel))
        if name:
            _add_identity(ev, name, rel)
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
    if m := re.search(r"^module\s+(\S+)", txt, re.M):
        _add_identity(ev, m.group(1).replace(".", "/"), "go.mod")
    if m := re.search(r"^go\s+(\d+\.\d+)", txt, re.M):
        ev.signals.append(Signal("runtime", f"go:{m.group(1)}", 0.99, "go.mod"))
    # Skip the `module` line: it names this repo, not a dependency. Scanning
    # it made github.com/gin-gonic/gin look like an app built on gin.
    deps = "\n".join(l for l in txt.splitlines()
                     if not l.strip().startswith("module "))
    for fw in ("gin-gonic", "fiber", "echo", "chi"):
        if fw in deps:
            ev.signals.append(Signal("framework", fw, 0.9, "go.mod"))


def _parse_cargo(root: Path, ev: Evidence) -> None:
    if "Cargo.toml" not in ev.files:
        return
    txt = _read(root / "Cargo.toml", 100_000)
    if m := re.search(r'''^\s*name\s*=\s*["']([\w.\-]+)["']''', txt, re.M):
        _add_identity(ev, m.group(1), "Cargo.toml")
    if "[workspace]" in txt:
        ev.workspaces.append("<cargo workspace>")
        # A workspace root has no [package]; its members carry the identity.
        if mm := re.search(r"members\s*=\s*\[([^\]]*)\]", txt, re.S):
            for member in re.findall(r'''["']([\w.\-]+)\*?["']''', mm.group(1)):
                if not member.endswith("-"):
                    _add_identity(ev, member, "Cargo.toml")
    for fw in ("axum", "actix-web", "rocket", "warp", "tokio"):
        if re.search(rf"^{re.escape(fw)}\s*=", txt, re.M):
            ev.signals.append(Signal("framework", fw, 0.9, "Cargo.toml"))


# ---------------------------------------------------------------------------
# Project identity
#
# "This repo depends on Flask" and "this repo IS Flask" produce the same
# corpus grep and mean opposite things. Recording what the project calls
# itself lets the planner tell them apart -- without it, every framework's own
# repository plans as an app built on itself.
# ---------------------------------------------------------------------------

def _identity_tokens(name: str) -> set[str]:
    """Names a project might be known by, from one manifest name field."""
    out: set[str] = set()
    for tok in re.split(r"[/@:]", name.strip().lower()):
        tok = tok.strip()
        if not tok:
            continue
        out.add(tok)
        # `@nestjs/core` is the nest framework; `fastify` is not `fastifyjs`.
        if tok.endswith("js") and len(tok) > 4:
            out.add(tok[:-2])
    return out


def _add_identity(ev: Evidence, name: str, rel: str) -> None:
    for tok in _identity_tokens(name):
        ev.signals.append(Signal("project.name", tok, 0.95, rel))


# ---------------------------------------------------------------------------
# Ruby / PHP / Elixir
#
# These three had no structured parser, so their frameworks were only ever
# found by grepping a merged corpus -- which loses the one thing that makes a
# signal trustworthy, the file it came from. A dependency declared in a
# Gemfile and the word "rails" in a comment were indistinguishable.
# ---------------------------------------------------------------------------

def _parse_ruby(root: Path, ev: Evidence) -> None:
    if "Gemfile" not in ev.files and not any(f.endswith(".gemspec") for f in ev.files):
        return
    add = ev.signals.append
    if "Gemfile" in ev.files:
        txt = _read(root / "Gemfile", 100_000)
        for fw in ("rails", "sinatra", "hanami", "roda"):
            if re.search(rf"^\s*gem\s+['\"]{fw}['\"]", txt, re.M):
                add(Signal("framework", fw, 0.95, "Gemfile"))
        if m := re.search(r"^\s*ruby\s+['\"]([\d.]+)['\"]", txt, re.M):
            add(Signal("runtime", f"ruby:{m.group(1)}", 0.97, "Gemfile"))
    for rel in [f for f in ev.files if f.endswith(".gemspec") and f.count("/") == 0]:
        if m := re.search(r"""\.name\s*=\s*['"]([\w.\-]+)['"]""",
                          _read(root / rel, 40_000)):
            _add_identity(ev, m.group(1), rel)


def _parse_php(root: Path, ev: Evidence) -> None:
    if "composer.json" not in ev.files:
        return
    try:
        data = json.loads(_read(root / "composer.json", 200_000))
    except (json.JSONDecodeError, ValueError):
        return
    add = ev.signals.append
    if nm := data.get("name"):
        _add_identity(ev, nm, "composer.json")
    req = {**(data.get("require") or {}), **(data.get("require-dev") or {})}
    for dep in req:
        low = dep.lower()
        if low.startswith("laravel/"):
            add(Signal("framework", "laravel", 0.95, "composer.json"))
        if low.startswith("symfony/"):
            add(Signal("framework", "symfony", 0.9, "composer.json"))
        if low.startswith("slim/"):
            add(Signal("framework", "slim", 0.9, "composer.json"))
    if v := req.get("php"):
        if m := re.search(r"(\d+\.\d+)", str(v)):
            add(Signal("runtime", f"php:{m.group(1)}", 0.9, "composer.json"))


def _parse_elixir(root: Path, ev: Evidence) -> None:
    if "mix.exs" not in ev.files:
        return
    txt = _read(root / "mix.exs", 100_000)
    add = ev.signals.append
    for fw in ("phoenix", "plug_cowboy", "nerves"):
        if re.search(rf"\{{:{fw},", txt):
            add(Signal("framework", "phoenix" if fw == "phoenix" else fw, 0.95, "mix.exs"))
    if m := re.search(r"app:\s*:(\w+)", txt):
        _add_identity(ev, m.group(1), "mix.exs")
    if m := re.search(r"elixir:\s*['\"][^\d]*([\d.]+)", txt):
        add(Signal("runtime", f"elixir:{m.group(1)}", 0.95, "mix.exs"))


# ---------------------------------------------------------------------------
# JVM
#
# Java is the ecosystem where a corpus grep is least defensible. A pom is XML
# with the exact answers in it -- artifact, version, packaging, JDK release,
# every dependency -- and the run command literally cannot be synthesized
# without them, because the thing you run is a jar whose filename is
# <artifactId>-<version>.jar. Every other language here has a structured
# parser; this is the one that was missing.
#
# Facts land as signals under dotted kinds ("jvm.artifact") so they flow
# through Evidence.top()/why() like everything else and stay attributable to
# the file that produced them.
# ---------------------------------------------------------------------------

# JDBC and Spring property names for services. The generic SERVICE_PATTERNS
# above are written for driver imports (`psycopg2`, `ioredis`); the JVM names
# its dependencies in XML and its endpoints in a URL scheme nothing else uses.
JVM_SERVICE_PATTERNS = [
    (r"jdbc:postgresql:|\bpostgresql\b.*<scope>|org\.postgresql|r2dbc:postgresql:", "postgres"),
    (r"jdbc:mysql:|com\.mysql|mysql-connector", "mysql"),
    (r"jdbc:mariadb:|org\.mariadb", "mysql"),
    (r"spring\.data\.mongodb|mongodb://|mongodb-driver", "mongodb"),
    (r"spring\.data\.redis|spring\.redis\.|\b(lettuce|jedis)\b|redis://", "redis"),
    (r"spring\.kafka|\bkafka-clients\b", "kafka"),
    (r"spring\.rabbitmq|\bamqp-client\b", "rabbitmq"),
    (r"spring\.elasticsearch|elasticsearch-java|opensearch-rest", "elasticsearch"),
]


def _jdk_release(val: str) -> str:
    """`1.8` -> `8`, `17` -> `17`.

    Legacy JDK version strings carry a `1.` prefix; modern ones do not. This
    was `val.lstrip("1.")`, which strips a *character set*: 17 became 7, and
    11 became the empty string. Petclinic pinned java.version=17 and got
    `eclipse-temurin:7-jdk`, a tag that does not exist.
    """
    val = val.strip()
    if val.startswith("1.") and len(val) > 2:
        return val[2:]
    return val


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _flatten_yaml(txt: str) -> str:
    """Indent-based flatten of simple YAML to dotted keys.

    Spring accepts the same settings as `application.properties`
    (`spring.data.redis.host=x`) or as nested `application.yml`, and real
    repos use both. Everything downstream matches dotted names, so a nested
    file was invisible -- a redis dependency stated in YAML produced no
    sidecar at all.

    Deliberately not a YAML parser: no anchors, lists, flow mappings or
    multi-line scalars. It turns nesting into dotted paths and stops there,
    because that is the only thing being asked of it.
    """
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "- ", "---")) or ":" not in line:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, val = line.partition(":")
        key, val = key.strip().strip("'\""), val.strip()
        if not key:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if val:
            out.append(f"{path}={val}")
        else:
            stack.append((indent, key))
    return "\n".join(out)


def _parse_jvm(root: Path, ev: Evidence) -> None:
    poms = [f for f in ev.files if f.endswith("pom.xml") and f.count("/") <= 3]
    gradles = [f for f in ev.files
               if f.rsplit("/", 1)[-1] in ("build.gradle", "build.gradle.kts")
               and f.count("/") <= 3]
    props = [f for f in ev.files
             if re.search(r"(^|/)application(-[\w]+)?\.(properties|ya?ml)$", f)]
    if not (poms or gradles or props):
        return

    add = ev.signals.append
    jvm_corpus: list[str] = []

    # ---- wrappers ------------------------------------------------------
    # A repo shipping mvnw/gradlew has pinned its build tool on purpose, and
    # the base JDK images carry neither `mvn` nor `gradle`. Using the wrapper
    # is both the author's intent and the only thing that works unaided.
    if "mvnw" in ev.files:
        add(Signal("trait", "maven-wrapper", 0.99, "mvnw"))
    if "gradlew" in ev.files:
        add(Signal("trait", "gradle-wrapper", 0.99, "gradlew"))

    # ---- pom.xml -------------------------------------------------------
    for rel in sorted(poms, key=lambda f: f.count("/")):
        txt = _read(root / rel, 400_000)
        jvm_corpus.append(txt)
        if "<!ENTITY" in txt:      # entity-expansion bomb; the grep still applies
            continue
        try:
            import xml.etree.ElementTree as ET
            proj = ET.fromstring(txt)
        except Exception:
            continue

        kids = {_strip_ns(c.tag): c for c in proj}
        root_pom = rel.count("/") == 0

        # Multi-module reactor: record members, mirroring the npm/cargo path.
        mods = kids.get("modules")
        if mods is not None:
            for m in mods:
                if m.text:
                    ev.workspaces.append(m.text.strip())

        if root_pom:
            for key, kind in (("artifactId", "jvm.artifact"), ("version", "jvm.version"),
                              ("packaging", "jvm.packaging")):
                el = kids.get(key)
                if el is not None and el.text:
                    add(Signal(kind, el.text.strip(), 0.99, rel))
                    if kind == "jvm.artifact":
                        _add_identity(ev, el.text.strip(), rel)

            build = kids.get("build")
            if build is not None:
                for c in build:
                    if _strip_ns(c.tag) == "finalName" and c.text:
                        add(Signal("jvm.finalname", c.text.strip(), 0.99, rel))

            # JDK release: several spellings, all authoritative.
            p = kids.get("properties")
            if p is not None:
                for c in p:
                    name, val = _strip_ns(c.tag), (c.text or "").strip()
                    if name in ("java.version", "maven.compiler.release",
                                "maven.compiler.source", "kotlin.jvm.target") and val:
                        add(Signal("runtime", f"java:{_jdk_release(val)}", 0.97, rel))

        parent = kids.get("parent")
        if parent is not None:
            pa = {_strip_ns(c.tag): (c.text or "").strip() for c in parent}
            if pa.get("artifactId") == "spring-boot-starter-parent":
                add(Signal("framework", "spring-boot", 0.99, rel))

        deps = kids.get("dependencies")
        if deps is not None:
            for d in deps:
                dd = {_strip_ns(c.tag): (c.text or "").strip() for c in d}
                aid, gid = dd.get("artifactId", ""), dd.get("groupId", "")
                if aid.startswith("spring-boot-starter"):
                    add(Signal("framework", "spring-boot", 0.99, rel))
                if aid == "spring-boot-starter-web" or aid == "spring-boot-starter-webflux":
                    add(Signal("trait", "jvm-http-server", 0.99, rel))
                if gid == "io.quarkus":
                    add(Signal("framework", "quarkus", 0.99, rel))
                if gid.startswith("io.micronaut"):
                    add(Signal("framework", "micronaut", 0.99, rel))

        plugins = txt
        if "spring-boot-maven-plugin" in plugins:
            add(Signal("trait", "spring-boot-repackage", 0.99, rel))

    # ---- gradle --------------------------------------------------------
    for rel in sorted(gradles, key=lambda f: f.count("/")):
        txt = _read(root / rel, 200_000)
        jvm_corpus.append(txt)
        if re.search(r"org\.springframework\.boot|spring-boot-starter", txt):
            add(Signal("framework", "spring-boot", 0.97, rel))
        if "spring-boot-starter-web" in txt:
            add(Signal("trait", "jvm-http-server", 0.97, rel))
        if "io.quarkus" in txt:
            add(Signal("framework", "quarkus", 0.97, rel))
        if "io.micronaut" in txt:
            add(Signal("framework", "micronaut", 0.97, rel))
        if m := re.search(r"languageVersion\s*[=.]\s*JavaLanguageVersion\.of\((\d+)\)", txt):
            add(Signal("runtime", f"java:{m.group(1)}", 0.97, rel))
        elif m := re.search(r"(?:sourceCompatibility|targetCompatibility)\s*=\s*['\"]?(?:1\.)?(\d+)", txt):
            add(Signal("runtime", f"java:{m.group(1)}", 0.95, rel))
        if m := re.search(r"""mainClass(?:Name)?\s*[=.]\s*['"]([\w.$]+)['"]""", txt):
            add(Signal("jvm.mainclass", m.group(1), 0.97, rel))
        if m := re.search(r"""^\s*(?:archivesBaseName|rootProject\.name)\s*=\s*['"]([\w.\-]+)['"]""",
                          txt, re.M):
            add(Signal("jvm.artifact", m.group(1), 0.9, rel))
        if m := re.search(r"""^\s*version\s*=\s*['"]([\w.\-]+)['"]""", txt, re.M):
            add(Signal("jvm.version", m.group(1), 0.9, rel))

    if "settings.gradle" in ev.files or "settings.gradle.kts" in ev.files:
        s = _read(root / ("settings.gradle" if "settings.gradle" in ev.files
                          else "settings.gradle.kts"), 40_000)
        for m in re.finditer(r"""include\s*\(?\s*['"]:?([\w.\-:]+)['"]""", s):
            ev.workspaces.append(m.group(1).replace(":", "/"))
        if m := re.search(r"""rootProject\.name\s*=\s*['"]([\w.\-]+)['"]""", s):
            add(Signal("jvm.artifact", m.group(1), 0.92, "settings.gradle"))

    # ---- application.properties / application.yml ----------------------
    # The app's own port, stated by the author. Without this a Spring service
    # is probed on 8080 by convention and a repo that moved it looks dead.
    for rel in sorted(props, key=lambda f: f.count("/")):
        txt = _read(root / rel, 80_000)
        # Flatten YAML so both spellings reach the same matchers.
        if rel.endswith((".yml", ".yaml")):
            txt = txt + "\n" + _flatten_yaml(txt)
        jvm_corpus.append(txt)
        if m := re.search(r"^\s*server\.port\s*[=:]\s*['\"]?(\d{2,5})", txt, re.M):
            add(Signal("jvm.port", m.group(1), 0.99, rel))
        if m := re.search(r"^\s*spring\.application\.name\s*[=:]\s*(\S+)", txt, re.M):
            add(Signal("jvm.appname", m.group(1).strip("'\""), 0.8, rel))

    # ---- services: available vs required -------------------------------
    #
    # A driver on the classpath says the app CAN talk to that database, not
    # that it must. Spring Petclinic declares both mysql-connector-j and
    # org.postgresql so either profile works, and runs on in-memory H2 when
    # you pick neither. Reading both as requirements booted two mutually
    # exclusive databases for an app that needed none, and the run failed on
    # a sidecar the repo never asked for.
    #
    # The requirement lives in configuration, and only in the DEFAULT
    # configuration: application-mysql.properties describes a profile you
    # have to opt into, so a datasource there is available, not active.
    required: set[str] = set()
    for rel in sorted(props, key=lambda f: f.count("/")):
        if re.search(r"application-[\w]+\.(properties|ya?ml)$", rel):
            continue                       # profile-specific: opt-in
        txt = _read(root / rel, 80_000)
        if rel.endswith((".yml", ".yaml")):
            txt = txt + "\n" + _flatten_yaml(txt)
        for pat, svc in JVM_SERVICE_PATTERNS:
            if re.search(pat, txt, re.I):
                required.add(svc)
                add(Signal("service", svc, 0.95, rel))

    blob = "\n".join(jvm_corpus)
    for pat, svc in JVM_SERVICE_PATTERNS:
        if svc not in required and re.search(pat, blob, re.I):
            add(Signal("service.optional", svc, 0.85, "<jvm manifests>"))


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
