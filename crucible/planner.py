"""
CRUCIBLE :: planner.py

Evidence -> RunPlan.

Priority order, highest-fidelity signal first. The repo author knew more
about their repo than any heuristic will, so anything they *declared* wins
over anything we *infer*:

    1. Dockerfile        -- parsed into a RunPlan, not built. See below.
    2. devcontainer.json -- postCreateCommand etc.
    3. Procfile          -- explicit process types
    4. compose.yml       -- for the service topology (always merged in)
    5. inference         -- language templates from evidence

On (1): everyone treats a Dockerfile as a build format that requires a
Docker daemon. It isn't. It's a declarative plan -- FROM/RUN/ENV/CMD map
almost 1:1 onto our Step list. Interpreting it instead of building it means
we honor the author's intent on any substrate, and -- more usefully -- each
RUN becomes an independently snapshotted, independently repairable step.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .evidence import FRAMEWORKS
from .schema import Evidence, RunPlan, Service, Step

# Language -> (base image, install cmd by pkgmgr, run fallback)
BASES = {
    "python": "python:{v}-slim",
    "node":   "node:{v}-slim",
    "go":     "golang:{v}",
    "rust":   "rust:{v}-slim",
    "ruby":   "ruby:{v}-slim",
    "java":   "eclipse-temurin:{v}-jdk",
    "php":    "php:{v}-cli",
    "elixir": "elixir:{v}",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:{v}",
}
DEFAULT_V = {"python": "3.12", "node": "22", "go": "1.23", "rust": "1", "ruby": "3.3",
             "java": "21", "php": "8.3", "elixir": "1.17", "dotnet": "8.0"}

INSTALL = {
    "npm":      "npm ci --no-audit --no-fund || npm install --no-audit --no-fund",
    "pnpm":     "corepack enable && pnpm install --frozen-lockfile || pnpm install",
    "yarn":     "corepack enable && yarn install --immutable || yarn install",
    "bun":      "bun install",
    "pip":      "pip install --no-cache-dir -r requirements.txt",
    "poetry":   "pip install poetry && poetry install --no-interaction --no-root",
    "uv":       "pip install uv && uv sync --frozen || uv sync",
    "pipenv":   "pip install pipenv && pipenv install --deploy --system",
    "pdm":      "pip install pdm && pdm install",
    "gomod":    "go mod download",
    "cargo":    "cargo fetch",
    "bundler":  "bundle install",
    "composer": "composer install --no-interaction",
    "maven":    "mvn -B -q dependency:go-offline",
    "gradle":   "gradle --no-daemon dependencies",
    "mix":      "mix local.hex --force && mix local.rebar --force && mix deps.get",
    "dotnet":   "dotnet restore",
}

BUILD = {
    "gomod":  "go build -o /tmp/app ./...",
    "cargo":  "cargo build --release",
    "maven":  "mvn -B -q package -DskipTests",
    "gradle": "gradle --no-daemon build -x test",
    "dotnet": "dotnet build -c Release",
    "mix":    "mix compile",
}

# Ports that belong to a dependency, never to the app. A repo containing the
# string 6379 is naming its Redis, not the port it serves on -- and letting one
# through makes the oracle probe the sidecar and declare the app healthy while
# the app is dead, which is the worst possible failure mode for a verifier.
SERVICE_PORTS = {5432, 6379, 27017, 3306, 9200, 5672, 9092, 11211, 2181, 9000}

SERVICE_IMAGES = {
    "postgres":      ("postgres:16-alpine", 5432, {"POSTGRES_PASSWORD": "crucible",
                                                   "POSTGRES_USER": "crucible",
                                                   "POSTGRES_DB": "crucible"}),
    "redis":         ("redis:7-alpine", 6379, {}),
    "mongodb":       ("mongo:7", 27017, {}),
    "mysql":         ("mysql:8", 3306, {"MYSQL_ROOT_PASSWORD": "crucible"}),
    "elasticsearch": ("elasticsearch:8.14.0", 9200, {"discovery.type": "single-node"}),
    "rabbitmq":      ("rabbitmq:3-alpine", 5672, {}),
    "kafka":         ("bitnami/kafka:3.7", 9092, {}),
}


def plan(ev: Evidence, prefer: str = "auto") -> RunPlan:
    if prefer in ("auto", "declared"):
        if "dockerfile" in ev.declared:
            p = _from_dockerfile(ev)
            if p:
                _merge_services(p, ev)
                return _finish(p)
        if "devcontainer" in ev.declared:
            p = _from_devcontainer(ev)
            if p:
                _merge_services(p, ev)
                return _finish(p)
    p = _infer(ev)
    if "procfile" in ev.declared:
        _apply_procfile(p, ev)
    _merge_services(p, ev)
    return _finish(p)


def _finish(p: RunPlan) -> RunPlan:
    """Invariants that hold whichever source produced the plan.

    The dependency-port rule was implemented once, inside `_app_ports`, which
    only the inference path calls. A Dockerfile saying `EXPOSE 6379` therefore
    went straight through: the oracle probed the redis sidecar, got an answer,
    and reported the app healthy while the app was dead. Same bug, different
    door -- so the rule belongs on the way out, where all three doors meet.
    """
    _deconflict_ports(p)
    return p


def _deconflict_ports(p: RunPlan) -> None:
    """The oracle must never probe a port a sidecar we booted is holding."""
    if not p.ports:
        return
    taken = {port for s in p.services for port in s.ports}
    app = [x for x in p.ports if x not in taken and x not in SERVICE_PORTS]
    if app == p.ports:
        return

    dropped = [x for x in p.ports if x not in app]
    p.ports = app
    p.note(f"dropped port(s) {dropped}: they belong to a dependency, not the app")

    if p.oracle.get("kind") != "http":
        return
    if app:
        p.oracle["port"] = app[0]
    else:
        # A web app whose only declared port belongs to a sidecar. We cannot
        # probe anything honestly, so weaken the oracle to the strongest claim
        # still supportable -- the process stayed up -- rather than passing on
        # the sidecar's health. The lint reports this as unverifiable.
        p.oracle = {"kind": "alive", "seconds": 20}
        p.note("no app port survives; oracle weakened to liveness only")


# ---------------------------------------------------------------------------
# 1. Dockerfile as a plan source
# ---------------------------------------------------------------------------

_DF_CONT = re.compile(r"\\\s*\n", re.M)


def _from_dockerfile(ev: Evidence) -> RunPlan | None:
    path = Path(ev.root) / ev.declared["dockerfile"]
    try:
        text = _DF_CONT.sub(" ", path.read_text(errors="replace"))
    except OSError:
        return None

    p = RunPlan()
    p.note(f"plan source: {ev.declared['dockerfile']} (interpreted, not built)")
    stages: list[str] = []
    workdir = "."
    n = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        instr, _, rest = line.partition(" ")
        instr, rest = instr.upper(), rest.strip()

        if instr == "FROM":
            img = rest.split(" AS ")[0].split(" as ")[0].strip()
            stages.append(img)
            # last stage wins -- multi-stage builds end at the runtime image
            p.base = img
        elif instr == "RUN":
            n += 1
            p.steps.append(Step(f"df-run-{n}", rest, network=True, cwd=workdir))
        elif instr == "WORKDIR":
            workdir = rest.strip("/") or "."
            p.workdir = workdir
        elif instr in ("ENV",):
            for k, v in _kv(rest):
                p.env[k] = v
        elif instr == "ARG":
            k, _, v = rest.partition("=")
            if v:
                p.env.setdefault(k.strip(), v.strip().strip('"\''))
        elif instr == "EXPOSE":
            for tok in rest.split():
                num = tok.split("/")[0]
                if num.isdigit():
                    p.ports.append(int(num))
        elif instr in ("CMD", "ENTRYPOINT"):
            p.run = _joinexec(rest) if instr == "CMD" or not p.run else f"{_joinexec(rest)} {p.run}"

    if len(stages) > 1:
        p.note(f"multi-stage ({len(stages)} stages) -- flattened, using final base {p.base}")
    if not p.run:
        return None
    p.ports = sorted(set(p.ports)) or _app_ports(ev)
    p.archetype = "web" if p.ports else "cli"
    p.oracle = {"kind": "http" if p.ports else "exit0", "port": p.ports[0] if p.ports else 0}
    return p


def _kv(rest: str) -> list[tuple[str, str]]:
    if "=" not in rest.split()[0] if rest.split() else True:
        k, _, v = rest.partition(" ")
        return [(k, v.strip().strip('"\''))]
    out = []
    try:
        for tok in shlex.split(rest):
            if "=" in tok:
                k, _, v = tok.partition("=")
                out.append((k, v))
    except ValueError:
        pass
    return out


def _joinexec(rest: str) -> str:
    rest = rest.strip()
    if rest.startswith("["):
        try:
            return " ".join(shlex.quote(a) if " " in a else a for a in json.loads(rest))
        except (json.JSONDecodeError, ValueError):
            pass
    return rest


def _from_devcontainer(ev: Evidence) -> RunPlan | None:
    path = Path(ev.root) / ev.declared["devcontainer"]
    try:
        raw = re.sub(r"//[^\n]*", "", path.read_text(errors="replace"))
        data = json.loads(re.sub(r",(\s*[}\]])", r"\1", raw))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    p = RunPlan()
    p.note(f"plan source: {ev.declared['devcontainer']}")
    p.base = data.get("image") or "ubuntu:24.04"
    p.env.update(data.get("containerEnv") or {})
    for k in ("onCreateCommand", "updateContentCommand", "postCreateCommand"):
        if c := data.get(k):
            p.steps.append(Step(k, c if isinstance(c, str) else " && ".join(c.values() if isinstance(c, dict) else c)))
    p.ports = [int(x) for x in data.get("forwardPorts", []) if str(x).isdigit()] or _app_ports(ev)
    if not p.steps:
        return None
    p.archetype = "web" if p.ports else "cli"
    p.oracle = {"kind": "http" if p.ports else "exit0", "port": p.ports[0] if p.ports else 0}
    return p


def _apply_procfile(p: RunPlan, ev: Evidence) -> None:
    try:
        txt = (Path(ev.root) / ev.declared["procfile"]).read_text(errors="replace")
    except OSError:
        return
    procs = dict(
        (m.group(1), m.group(2).strip())
        for m in re.finditer(r"^(\w+):\s*(.+)$", txt, re.M)
    )
    if cmd := (procs.get("web") or next(iter(procs.values()), "")):
        p.run = cmd
        p.note(f"run command from Procfile ({'web' if 'web' in procs else 'first entry'})")
        if "web" in procs:
            p.archetype = "web"


# ---------------------------------------------------------------------------
# 5. Inference
# ---------------------------------------------------------------------------

def _app_ports(ev: Evidence) -> list[int]:
    return [p for p in ev.ports if p not in SERVICE_PORTS]


def _infer(ev: Evidence) -> RunPlan:
    p = RunPlan()
    lang = ev.top("language") or "unknown"
    pm = _pick_pkgmgr(ev, lang)
    fw = ev.top("framework")

    ver = _version_for(ev, lang)
    tmpl = BASES.get(lang)
    p.base = tmpl.format(v=ver) if tmpl else "ubuntu:24.04"
    p.note(f"language={lang} ({', '.join(ev.why('language', lang)[:2]) or 'file mix'})")
    p.note(f"base={p.base} (runtime pin: {ver})")

    p.system_packages = sorted(set(ev.native_hints))
    if p.system_packages:
        p.note(f"native deps inferred from source: {', '.join(p.system_packages)}")

    # --- install ---
    if pm and pm in INSTALL:
        cmd = _wrapped(INSTALL[pm], ev, pm)
        if pm == "pip" and "requirements.txt" not in ev.files:
            cmd = "pip install --no-cache-dir -e . || pip install --no-cache-dir ."
        p.steps.append(Step(f"install/{pm}", cmd, network=True, timeout=1800))
        p.note(f"package manager={pm} ({', '.join(ev.why('pkgmgr', pm)[:2])})")
        if cmd.startswith("./"):
            p.note(f"using the repo's own build wrapper ({cmd.split()[0]})")

    # --- build ---
    if pm in BUILD:
        p.steps.append(Step(f"build/{pm}", _wrapped(BUILD[pm], ev, pm),
                            network=True, timeout=1800))
    elif lang == "node" and "build" in ev.scripts:
        # only build if a framework that needs it is present
        if fw in ("next", "nuxt", "vite", "remix", "astro", "nest"):
            p.steps.append(Step("build/npm", ev.scripts["build"], network=True, timeout=1800))

    # --- archetype + run ---
    arch, port, run = _pick_run(ev, lang, pm, fw)
    p.archetype, p.run = arch, run
    p.ports = sorted({port, *_app_ports(ev)} - {0}) if port else _app_ports(ev)
    p.note(f"archetype={arch} via {'framework ' + fw if fw else 'entrypoint heuristics'}")

    for k in ev.env_keys:
        p.env.setdefault(k, _synth_env(k))
    if ev.env_keys:
        p.note(f"synthesized {len(ev.env_keys)} env vars from .env.example")

    p.oracle = _oracle_for(arch, p.ports)
    return p


def _pick_pkgmgr(ev: Evidence, lang: str) -> str | None:
    ranked = ev.tally("pkgmgr")
    lang_pms = {
        "python": {"uv", "poetry", "pdm", "pipenv", "pip", "conda"},
        "node": {"pnpm", "yarn", "bun", "npm"},
        "go": {"gomod"}, "rust": {"cargo"}, "ruby": {"bundler"},
        "java": {"gradle", "maven"}, "php": {"composer"}, "elixir": {"mix"},
        "dotnet": {"dotnet"},
    }.get(lang, set())
    for name, _ in ranked:
        if name in lang_pms:
            return name
    return ranked[0][0] if ranked else None


def _version_for(ev: Evidence, lang: str) -> str:
    for val, _ in ev.tally("runtime"):
        rt, _, v = val.partition(":")
        if rt == lang or (rt == "nodejs" and lang == "node"):
            return v
    return DEFAULT_V.get(lang, "latest")


def _pick_run(ev: Evidence, lang: str, pm: str | None, fw: str | None) -> tuple[str, int, str]:
    entry = ev.entrypoints[0]["path"] if ev.entrypoints else ""
    aports = _app_ports(ev)

    if fw and fw in FRAMEWORKS:
        arch, port, hint = FRAMEWORKS[fw]
        port = _declared_port(ev, port)
        if fw == "django":
            return arch, port, "python manage.py migrate --noinput; python manage.py runserver 0.0.0.0:8000"
        if fw == "fastapi":
            mod = _guess_module(ev) or "main"
            return arch, port, f"uvicorn {mod}:app --host 0.0.0.0 --port {port}"
        if fw == "flask":
            mod = _guess_module(ev) or "app"
            return arch, port, f"FLASK_APP={mod} flask run --host=0.0.0.0 --port={port}"
        if fw == "streamlit":
            return arch, port, f"streamlit run {entry or 'app.py'} --server.address 0.0.0.0 --server.port {port}"
        if hint:
            return arch, port, hint
        # No hint. The framework still told us the two things that matter --
        # this is a web app, on this port -- and dropping that on the floor
        # is how a Spring Boot service ends up classified `library` and
        # "verified" by its own test suite. Synthesize a start command from
        # the ecosystem; only if that fails do we fall through to heuristics.
        if cmd := _synth_start(ev, lang, pm, fw, port):
            return arch, port, cmd

    if lang == "node":
        for cand in ("start", "dev", "serve"):
            if cand in ev.scripts:
                return ("web" if aports or fw else "cli"), (aports[0] if aports else 3000), f"npm run {cand}"
        if entry:
            return "cli", 0, f"node {entry}"
    if lang == "python" and entry:
        return ("web" if aports else "cli"), (aports[0] if aports else 0), f"python {entry}"
    # Compiled languages: absence of a main function is *positive* evidence of
    # a library, not a detection failure. Running `cargo run` on a crate with
    # only lib.rs fails with a confusing error; running its tests proves the
    # same thing (it builds, it works) and is what the author intended.
    if lang == "go":
        if any(f.endswith("main.go") for f in ev.files):
            return ("web" if aports else "cli"), (aports[0] if aports else 0), "/tmp/app"
        return "library", 0, "go test ./..."
    if lang == "rust":
        if any(f in ("src/main.rs", "main.rs") or f.startswith("src/bin/") for f in ev.files):
            return ("web" if aports else "cli"), (aports[0] if aports else 0), "cargo run --release"
        return "library", 0, "cargo test"
    if pm == "make" and "make run" in ev.scripts:
        return "cli", 0, "make run"

    # Nothing runnable found -> it is probably a library. Prove it with tests.
    return "library", 0, _test_cmd(lang, pm)


def _test_cmd(lang: str, pm: str | None) -> str:
    return {
        "python": "pytest -q || python -m unittest discover -v",
        "node": "npm test", "go": "go test ./...", "rust": "cargo test",
        "ruby": "bundle exec rspec || rake test", "java": "mvn -B test",
        "elixir": "mix test", "php": "vendor/bin/phpunit",
    }.get(lang, "make test")


# ---------------------------------------------------------------------------
# Start-command synthesis
#
# FRAMEWORKS carries a run hint for the handful of ecosystems whose start
# command is a one-liner, and an empty string for everything else. An empty
# hint used to mean "fall through", which quietly discarded the archetype and
# port the framework had just told us -- so every JVM, Rails, Laravel and
# Phoenix web service was filed as `library` and verified by running its
# tests. The oracle then passed a repo whose server had never been started.
#
# These are the same one-liners, written down.
# ---------------------------------------------------------------------------

_WRAPPERS = {                       # pkgmgr -> (tool, wrapper script, trait)
    "maven":  ("mvn", "mvnw", "maven-wrapper"),
    "gradle": ("gradle", "gradlew", "gradle-wrapper"),
}


def _wrapped(cmd: str, ev: Evidence, pm: str | None) -> str:
    """Prefer ./mvnw or ./gradlew when the repo ships one.

    Not a style preference: `eclipse-temurin:*-jdk` contains a JDK and nothing
    else -- no mvn, no gradle -- so the plain command is `not found` on the
    base image we just chose for it. The wrapper is both the author's pinned
    build-tool version and the only one that exists in the sandbox.
    """
    w = _WRAPPERS.get(pm or "")
    if not w:
        return cmd
    tool, script, trait = w
    if ev.has("trait", trait) and cmd.startswith(tool + " "):
        return f"./{script}{cmd[len(tool):]}"
    return cmd


def _declared_port(ev: Evidence, default: int) -> int:
    """An author-stated port beats a framework convention."""
    if v := ev.top("jvm.port"):
        try:
            return int(v)
        except ValueError:
            pass
    return default


def _jvm_jar(ev: Evidence, pm: str | None, fw: str | None) -> str:
    """Where the build put the artifact.

    Deterministic when the pom told us (`target/<artifactId>-<version>.jar`,
    or <finalName> when overridden) so the emitted Dockerfile is reviewable;
    a glob only when it didn't.
    """
    if fw == "quarkus" and pm == "maven":
        return "target/quarkus-app/quarkus-run.jar"
    if pm == "gradle":
        # Gradle emits both boot.jar and boot-plain.jar; the plain one has no
        # Main-Class and picking it fails with "no main manifest attribute".
        return '"$(ls -1 build/libs/*.jar | grep -v -- -plain | head -1)"'
    final = ev.top("jvm.finalname")
    if final:
        return f"target/{final}.jar"
    art, ver = ev.top("jvm.artifact"), ev.top("jvm.version")
    if art and ver:
        return f"target/{art}-{ver}.jar"
    return '"$(ls -1 target/*.jar | grep -v sources | head -1)"'


def _jvm_start(ev: Evidence, pm: str | None, fw: str | None, port: int) -> str:
    boot = fw in ("spring-boot", "quarkus", "micronaut")
    if not boot and (mc := ev.top("jvm.mainclass")):
        cp = "build/classes/java/main" if pm == "gradle" else "target/classes"
        return f"java -cp {cp} {mc}"
    jar = _jvm_jar(ev, pm, fw)
    prop = f" --server.port={port}" if fw == "spring-boot" else ""
    return f"java -jar {jar}{prop}"


def _synth_start(ev: Evidence, lang: str, pm: str | None,
                 fw: str | None, port: int) -> str | None:
    entry = ev.entrypoints[0]["path"] if ev.entrypoints else ""

    if lang in ("java", "kotlin", "scala"):
        return _jvm_start(ev, pm, fw, port)

    if lang == "ruby":
        if fw == "rails":
            return f"bundle exec rails server -b 0.0.0.0 -p {port}"
        return f"bundle exec ruby {entry or 'app.rb'} -o 0.0.0.0 -p {port}"

    if lang == "php":
        if fw == "laravel":
            return f"php artisan serve --host=0.0.0.0 --port={port}"
        return f"php -S 0.0.0.0:{port} -t {'public' if 'public/index.php' in ev.files else '.'}"

    if lang == "elixir":
        return "mix phx.server" if fw == "phoenix" else None

    if lang == "node":
        for cand in ("start", "dev", "serve"):
            if cand in ev.scripts:
                return f"npm run {cand}"
        if fw == "next":
            return f"npx next start -p {port}"
        if fw == "nuxt":
            return "node .output/server/index.mjs"
        if fw == "vite":
            return f"npx vite preview --host 0.0.0.0 --port {port}"
        return f"node {entry}" if entry else None

    if lang == "python":
        mod = _guess_module(ev)
        if fw == "gunicorn":
            return f"gunicorn {mod or 'app'}:app -b 0.0.0.0:{port}"
        if fw == "uvicorn":
            return f"uvicorn {mod or 'main'}:app --host 0.0.0.0 --port {port}"
        return f"python {entry}" if entry else None

    if lang == "go":
        return "/tmp/app"
    if lang == "rust":
        return "cargo run --release"
    return None


def _guess_module(ev: Evidence) -> str | None:
    for e in ev.entrypoints:
        if e["path"].endswith(".py"):
            return e["path"][:-3].replace("/", ".")
    return None


def _synth_env(key: str) -> str:
    k = key.upper()
    if "DATABASE_URL" in k or "POSTGRES_URL" in k:
        return "postgresql://crucible:crucible@127.0.0.1:5432/crucible"
    if "REDIS" in k:
        return "redis://127.0.0.1:6379/0"
    if "MONGO" in k:
        return "mongodb://127.0.0.1:27017/crucible"
    if any(t in k for t in ("SECRET", "KEY", "TOKEN", "PASSWORD", "SALT")):
        return "crucible-dev-placeholder-not-a-real-secret"
    if "PORT" in k:
        return "8000"
    if k in ("NODE_ENV", "ENV", "ENVIRONMENT", "APP_ENV"):
        return "development"
    if "HOST" in k:
        return "0.0.0.0"
    return "crucible"


def _oracle_for(arch: str, ports: list[int]) -> dict:
    if arch == "web" and ports:
        return {"kind": "http", "port": ports[0], "path": "/", "grace": 45}
    if arch == "worker":
        return {"kind": "alive", "seconds": 20}
    if arch == "library":
        return {"kind": "exit0"}
    return {"kind": "exit0"}


def _merge_services(p: RunPlan, ev: Evidence) -> None:
    wanted = {s.value for s in ev.signals if s.kind == "service"}
    for name in sorted(wanted):
        if name not in SERVICE_IMAGES or any(s.name == name for s in p.services):
            continue
        img, port, env = SERVICE_IMAGES[name]
        p.services.append(Service(name, img, [port], dict(env)))
        p.note(f"sidecar {name} ({img}) -- detected in source")
    _wire_services(p, ev)


# Spring reads connection settings from SPRING_* env vars, overriding whatever
# application.properties says -- which is the only lever we have, since the
# properties file is the repo's and points at the author's own database. A
# sidecar nobody is told about is just a slower way to fail.
_SPRING_WIRING = {
    "postgres": {"SPRING_DATASOURCE_URL": "jdbc:postgresql://127.0.0.1:5432/crucible",
                 "SPRING_DATASOURCE_USERNAME": "crucible",
                 "SPRING_DATASOURCE_PASSWORD": "crucible"},
    "mysql":    {"SPRING_DATASOURCE_URL": "jdbc:mysql://127.0.0.1:3306/crucible",
                 "SPRING_DATASOURCE_USERNAME": "root",
                 "SPRING_DATASOURCE_PASSWORD": "crucible"},
    "redis":    {"SPRING_DATA_REDIS_HOST": "127.0.0.1",
                 "SPRING_DATA_REDIS_PORT": "6379"},
    "mongodb":  {"SPRING_DATA_MONGODB_URI": "mongodb://127.0.0.1:27017/crucible"},
    "kafka":    {"SPRING_KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:9092"},
    "rabbitmq": {"SPRING_RABBITMQ_HOST": "127.0.0.1"},
}


def _wire_services(p: RunPlan, ev: Evidence) -> None:
    """Point the app at the sidecars we just booted."""
    if not p.services:
        return
    if ev.top("framework") in ("spring-boot",) or ev.has("framework", "spring-boot"):
        for svc in p.services:
            for k, v in _SPRING_WIRING.get(svc.name, {}).items():
                p.env.setdefault(k, v)
        p.note(f"wired {len(p.services)} sidecar(s) into SPRING_* env")
