"""
CRUCIBLE :: repair.py

Failure is the input, a revised RunPlan is the output.

The central bet: environment failures are NOT a long tail. They are a short,
boring head. About 25 regexes cover the large majority of real-world "this
repo won't build" errors, because build tooling emits highly structured
diagnostics. Missing header, missing module, wrong interpreter version, port
taken, no database listening -- these are the same twelve errors forever.

So repair is two-tier:
    tier 1  deterministic rules   free, instant, ~70-80% of failures
    tier 2  LLM patch proposal    only for what tier 1 cannot name

Tier 1 first is not a cost optimization. It is a correctness property: a
regex that matches `fatal error: libpq-fe.h: No such file` is *certain*
about the fix, while a model is merely confident. Spend the model on genuine
ambiguity.

Every patch must change the plan fingerprint. A repair that produces an
already-attempted plan is a cycle, and the engine kills it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .schema import ExecResult, RunPlan, Service, Step

# ---------------------------------------------------------------------------
# lookup tables
# ---------------------------------------------------------------------------

HEADER_TO_APT = {
    "libpq-fe.h": "libpq-dev", "Python.h": "python3-dev", "ffi.h": "libffi-dev",
    "openssl/ssl.h": "libssl-dev", "zlib.h": "zlib1g-dev", "jpeglib.h": "libjpeg-dev",
    "libxml/xmlversion.h": "libxml2-dev", "mysql.h": "default-libmysqlclient-dev",
    "sqlite3.h": "libsqlite3-dev", "curl/curl.h": "libcurl4-openssl-dev",
    "png.h": "libpng-dev", "freetype/config/ftheader.h": "libfreetype-dev",
    "cblas.h": "libopenblas-dev", "GL/gl.h": "libgl1-mesa-dev",
    "SDL2/SDL.h": "libsdl2-dev", "krb5.h": "libkrb5-dev", "ldap.h": "libldap2-dev",
    "sasl/sasl.h": "libsasl2-dev", "systemd/sd-daemon.h": "libsystemd-dev",
}

CMD_TO_APT = {
    "gcc": "build-essential", "cc": "build-essential", "g++": "build-essential",
    "make": "build-essential", "cmake": "cmake", "git": "git", "curl": "curl",
    "wget": "wget", "unzip": "unzip", "pkg-config": "pkg-config",
    "python3": "python3", "pip": "python3-pip", "pip3": "python3-pip",
    "ffmpeg": "ffmpeg", "convert": "imagemagick", "psql": "postgresql-client",
    "redis-cli": "redis-tools", "openssl": "openssl", "ssh": "openssh-client",
    "tar": "tar", "xz": "xz-utils", "bzip2": "bzip2", "patch": "patch",
    "autoconf": "autoconf", "libtool": "libtool", "gfortran": "gfortran",
    "java": "default-jre", "node": "nodejs", "rustc": "rustc", "cargo": "cargo",
}

SO_TO_APT = {
    "libGL.so.1": "libgl1", "libglib-2.0.so.0": "libglib2.0-0",
    "libsm.so.6": "libsm6", "libxext.so.6": "libxext6", "libxrender.so.1": "libxrender1",
    "libgomp.so.1": "libgomp1", "libnss3.so": "libnss3", "libgbm.so.1": "libgbm1",
    "libasound.so.2": "libasound2t64", "libpq.so.5": "libpq5",
}

SERVICE_IMAGES = {
    "postgres": ("postgres:16-alpine", 5432, {"POSTGRES_PASSWORD": "crucible",
                                              "POSTGRES_USER": "crucible",
                                              "POSTGRES_DB": "crucible"}),
    "redis": ("redis:7-alpine", 6379, {}),
    "mysql": ("mysql:8", 3306, {"MYSQL_ROOT_PASSWORD": "crucible"}),
    "mongodb": ("mongo:7", 27017, {}),
}


@dataclass
class Patch:
    reason: str                                  # human-readable diagnosis
    confidence: float                            # 1.0 = certain
    apply: Callable[[RunPlan], None]
    source: str = "rule"                         # rule | llm
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# tier 1: deterministic rules
# ---------------------------------------------------------------------------
# Each entry: (compiled regex, builder(match, plan, step) -> Patch | None)

RULES: list[tuple[re.Pattern, Callable]] = []


def rule(pattern: str, flags=re.I | re.M):
    def deco(fn):
        RULES.append((re.compile(pattern, flags), fn))
        return fn
    return deco


def _add_pkgs(plan: RunPlan, pkgs: list[str]) -> None:
    for p in pkgs:
        if p not in plan.system_packages:
            plan.system_packages.append(p)


# --- missing native headers / libs -----------------------------------------

@rule(r"fatal error:\s*([\w/.\-+]+\.h[p]{0,2}):\s*No such file")
def _r_header(m, plan, step):
    hdr = m.group(1)
    pkg = HEADER_TO_APT.get(hdr) or HEADER_TO_APT.get(hdr.split("/")[-1])
    if not pkg:
        pkg = f"lib{hdr.split('/')[-1].split('.')[0]}-dev"
        conf = 0.55
    else:
        conf = 0.97
    return Patch(f"missing C header {hdr} -> apt {pkg}", conf,
                 lambda p: _add_pkgs(p, [pkg, "build-essential"]), tags=["native"])


@rule(r"error while loading shared libraries:\s*([\w.\-+]+\.so[\d.]*)")
@rule(r"ImportError:\s*([\w.\-+]+\.so[\d.]*):\s*cannot open shared object")
def _r_so(m, plan, step):
    so = m.group(1)
    pkg = SO_TO_APT.get(so) or SO_TO_APT.get(so.lower())
    if not pkg:
        return None
    return Patch(f"missing shared object {so} -> apt {pkg}", 0.95,
                 lambda p: _add_pkgs(p, [pkg]), tags=["native"])


@rule(r"(?:No package '([\w.\-+]+)' found|Package ([\w.\-+]+) was not found in the pkg-config)")
def _r_pkgconfig(m, plan, step):
    name = m.group(1) or m.group(2)
    return Patch(f"pkg-config missing {name} -> apt lib{name}-dev", 0.6,
                 lambda p: _add_pkgs(p, [f"lib{name}-dev", "pkg-config"]), tags=["native"])


# --- missing commands -------------------------------------------------------

@rule(r"(?:command not found|not found:|No such file or directory):?\s*([\w.\-+]+)\b")
@rule(r"([\w.\-+]+):\s*(?:command )?not found")
def _r_cmd(m, plan, step):
    cmd = m.group(1).strip()
    if cmd in ("sh", "bash", "/bin/sh"):
        return None
    pkg = CMD_TO_APT.get(cmd)
    if not pkg:
        return None
    return Patch(f"`{cmd}` not on PATH -> apt {pkg}", 0.9,
                 lambda p: _add_pkgs(p, [pkg]), tags=["toolchain"])


# --- python -----------------------------------------------------------------

@rule(r"ModuleNotFoundError:\s*No module named ['\"]([\w.]+)['\"]")
def _r_pymod(m, plan, step):
    mod = m.group(1).split(".")[0]
    alias = {"cv2": "opencv-python-headless", "PIL": "pillow", "yaml": "pyyaml",
             "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
             "jwt": "pyjwt", "psycopg2": "psycopg2-binary", "OpenSSL": "pyopenssl",
             "attr": "attrs", "dateutil": "python-dateutil", "google": "protobuf"}
    pkg = alias.get(mod, mod)
    return Patch(f"python module `{mod}` missing -> pip install {pkg}", 0.9,
                 lambda p: p.steps.insert(
                     _idx(p, step),
                     Step(f"fix-pip-{pkg}", f"pip install --no-cache-dir {pkg}", network=True)),
                 tags=["deps"])


@rule(r"error:\s*externally-managed-environment")
def _r_pep668(m, plan, step):
    return Patch("PEP 668 externally-managed env -> allow system pip installs", 0.99,
                 lambda p: p.env.update({"PIP_BREAK_SYSTEM_PACKAGES": "1"}), tags=["python"])


@rule(r"(?:requires Python|Requires-Python)\s*[>=^~]{1,2}\s*(\d+\.\d+)")
def _r_pyver(m, plan, step):
    want = m.group(1)
    return Patch(f"package requires Python >= {want} -> rebase to python:{want}-slim", 0.93,
                 lambda p: setattr(p, "base", f"python:{want}-slim"), tags=["rebase"])


@rule(r"No matching distribution found for ([\w.\-\[\]]+)")
def _r_nodist(m, plan, step):
    name = m.group(1)
    return Patch(f"no wheel for {name} on this interpreter -> try newer base + build tools", 0.5,
                 lambda p: _add_pkgs(p, ["build-essential", "python3-dev"]), tags=["deps"])


# --- node -------------------------------------------------------------------

@rule(r"Cannot find module ['\"]([@\w./\-]+)['\"]")
def _r_nodemod(m, plan, step):
    mod = m.group(1)
    if mod.startswith(".") or mod.startswith("/"):
        return None
    return Patch(f"node module `{mod}` missing -> npm install {mod}", 0.85,
                 lambda p: p.steps.insert(
                     _idx(p, step), Step(f"fix-npm-{mod}", f"npm install {mod}", network=True)),
                 tags=["deps"])


@rule(r"npm (?:ERR!|error).*(?:can only install packages when your package\.json and package-lock\.json|lock file.*out of sync|EUSAGE)")
def _r_npmci(m, plan, step):
    return Patch("lockfile out of sync with package.json -> `npm install` instead of `npm ci`", 0.95,
                 lambda p: _rewrite(p, "npm ci", "npm install"), tags=["deps"])


@rule(r"(?:Unsupported engine|engine ['\"]?node['\"]?.*required.*?(\d+))")
def _r_nodeengine(m, plan, step):
    want = m.group(1) if m.lastindex else "22"
    return Patch(f"engine requires node {want} -> rebase to node:{want}-slim", 0.85,
                 lambda p: setattr(p, "base", f"node:{want}-slim"), tags=["rebase"])


@rule(r"ERR_PNPM_NO_LOCKFILE|Headless installation requires a .*lock")
def _r_pnpmlock(m, plan, step):
    return Patch("frozen-lockfile install with no lockfile -> unfrozen install", 0.95,
                 lambda p: _rewrite(p, "--frozen-lockfile", ""), tags=["deps"])


@rule(r"JavaScript heap out of memory|FATAL ERROR:.*Allocation failed")
def _r_heap(m, plan, step):
    return Patch("V8 heap exhausted -> raise --max-old-space-size", 0.9,
                 lambda p: p.env.update({"NODE_OPTIONS": "--max-old-space-size=4096"}),
                 tags=["resources"])


# --- go / rust / java -------------------------------------------------------

@rule(r"go:\s*(?:cannot find main module|go\.mod file not found)")
def _r_gomod(m, plan, step):
    return Patch("no go.mod in cwd -> search for module root", 0.8,
                 lambda p: _rewrite(p, "go build", "cd $(dirname $(find . -name go.mod | head -1)) && go build"),
                 tags=["layout"])


@rule(r"go\.mod requires go >= ([\d.]+)")
def _r_gover(m, plan, step):
    v = m.group(1)
    return Patch(f"go.mod requires {v} -> rebase to golang:{v}", 0.95,
                 lambda p: setattr(p, "base", f"golang:{v}"), tags=["rebase"])


@rule(r"(?:package|error).*requires rustc ([\d.]+)|rustc ([\d.]+) is not supported")
def _r_rustver(m, plan, step):
    v = m.group(1) or m.group(2)
    return Patch(f"toolchain too old -> rebase to rust:{v}", 0.85,
                 lambda p: setattr(p, "base", f"rust:{v}-slim"), tags=["rebase"])


@rule(r"(?:class file has wrong version|invalid target release|Unsupported class file major version)\s*:?\s*(\d+)")
def _r_javaver(m, plan, step):
    n = int(m.group(1))
    jdk = {61: 17, 65: 21, 55: 11, 52: 8}.get(n, 21)
    return Patch(f"bytecode target {n} -> rebase to temurin {jdk}", 0.8,
                 lambda p: setattr(p, "base", f"eclipse-temurin:{jdk}-jdk"), tags=["rebase"])


# --- runtime / connectivity -------------------------------------------------

@rule(r"(?:EADDRINUSE|Address already in use|bind: address already in use).*?(\d{2,5})?")
def _r_port(m, plan, step):
    return Patch("port already bound -> shift to a free port", 0.95,
                 _shift_port, tags=["runtime"])


@rule(r"(?:could not connect to server|Connection refused).*(?:5432|postgres)")
def _r_pg(m, plan, step):
    return Patch("postgres unreachable -> boot a postgres sidecar", 0.9,
                 lambda p: _add_service(p, "postgres"), tags=["services"])


@rule(r"(?:Error \d+ connecting to|ConnectionError).*(?:6379|redis)")
def _r_redis(m, plan, step):
    return Patch("redis unreachable -> boot a redis sidecar", 0.9,
                 lambda p: _add_service(p, "redis"), tags=["services"])


@rule(r"(?:KeyError|Missing|not set|undefined):?\s*['\"]?([A-Z][A-Z0-9_]{3,})['\"]?")
def _r_env(m, plan, step):
    from .planner import _synth_env
    key = m.group(1)
    if key in ("PATH", "HOME", "TRUE", "FALSE", "NULL", "NONE"):
        return None
    return Patch(f"required env var {key} unset -> synthesize a dev placeholder", 0.7,
                 lambda p: p.env.setdefault(key, _synth_env(key)), tags=["config"])


@rule(r"(?:SSL: CERTIFICATE_VERIFY_FAILED|unable to get local issuer certificate|x509: certificate signed)")
def _r_ssl(m, plan, step):
    return Patch("TLS trust store missing -> install ca-certificates", 0.95,
                 lambda p: _add_pkgs(p, ["ca-certificates"]), tags=["network"])


@rule(r"(?:Temporary failure resolving|Could not resolve host|getaddrinfo (?:ENOTFOUND|failed))")
def _r_dns(m, plan, step):
    return Patch("DNS failed inside sandbox -> re-enable network for this step", 0.8,
                 lambda p: [setattr(s, "network", True) for s in p.steps], tags=["network"])


@rule(r"Permission denied|EACCES")
def _r_perm(m, plan, step):
    return Patch("permission denied -> relax perms on workspace", 0.6,
                 lambda p: p.steps.insert(0, Step("fix-perms", "chmod -R u+rwX /workspace", network=False)),
                 tags=["fs"])


@rule(r"(?:Killed|signal 9|exit code 137|Out of memory|Cannot allocate memory|MemoryError)")
def _r_oom(m, plan, step):
    return Patch("OOM-killed -> raise memory ceiling and disable parallel jobs", 0.9,
                 lambda p: p.env.update({"CRUCIBLE_MEM_MB": "6144", "MAKEFLAGS": "-j1",
                                         "CARGO_BUILD_JOBS": "1"}), tags=["resources"])


@rule(r"apt-get.*(?:E: Unable to locate package|has no installation candidate)\s*([\w.\-+]+)?")
def _r_aptmiss(m, plan, step):
    bad = (m.group(1) or "").strip()
    return Patch(f"apt package `{bad or '?'}` not in index -> drop it and continue", 0.8,
                 lambda p: p.system_packages.remove(bad) if bad in p.system_packages else None,
                 tags=["toolchain"])


@rule(r"No such file or directory: ['\"]?requirements\.txt")
def _r_noreq(m, plan, step):
    return Patch("requirements.txt absent -> install the project itself", 0.9,
                 lambda p: _rewrite(p, "pip install --no-cache-dir -r requirements.txt",
                                    "pip install --no-cache-dir -e . || pip install ."),
                 tags=["deps"])


# ---------------------------------------------------------------------------
# patch helpers
# ---------------------------------------------------------------------------

def _idx(plan: RunPlan, step: Step) -> int:
    for i, s in enumerate(plan.steps):
        if s.key() == step.key():
            return i
    return len(plan.steps)


def _rewrite(plan: RunPlan, old: str, new: str) -> None:
    for s in plan.steps:
        if old in s.cmd:
            s.cmd = s.cmd.replace(old, new)
    if old in plan.run:
        plan.run = plan.run.replace(old, new)


def _shift_port(plan: RunPlan) -> None:
    if not plan.ports:
        plan.ports = [8000]
    old = plan.ports[0]
    new = old + 1 if old < 65000 else 8080
    plan.ports[0] = new
    plan.run = re.sub(rf"\b{old}\b", str(new), plan.run)
    for k, v in plan.env.items():
        if v == str(old):
            plan.env[k] = str(new)
    if plan.oracle.get("port") == old:
        plan.oracle["port"] = new


def _add_service(plan: RunPlan, name: str) -> None:
    if any(s.name == name for s in plan.services) or name not in SERVICE_IMAGES:
        return
    img, port, env = SERVICE_IMAGES[name]
    plan.services.append(Service(name, img, [port], dict(env)))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def diagnose(result: ExecResult, plan: RunPlan, step: Step,
             llm=None, seen: Optional[set] = None) -> Optional[Patch]:
    """Return the highest-confidence applicable patch, or None."""
    log = result.tail(200)
    seen = seen or set()
    cands: list[Patch] = []

    for rx, builder in RULES:
        for m in rx.finditer(log):
            try:
                p = builder(m, plan, step)
            except Exception:
                p = None
            if p and p.reason not in seen:
                cands.append(p)
            break  # first match per rule is enough

    if result.timed_out and "timeout" not in seen:
        cands.append(Patch("step timed out -> double the budget", 0.85,
                           lambda pl: [setattr(s, "timeout", min(s.timeout * 2, 5400))
                                       for s in pl.steps], tags=["resources"]))

    if cands:
        return max(cands, key=lambda p: p.confidence)

    # tier 2: hand genuine ambiguity to the model
    if llm is not None:
        return llm_patch(llm, result, plan, step)
    return None


# ---------------------------------------------------------------------------
# tier 2: LLM fallback
# ---------------------------------------------------------------------------

LLM_SYSTEM = """You repair broken build plans for a sandboxed repo runner.
You will get: the failing shell command, the last lines of its output, and the
current plan as JSON.

Reply with ONLY a JSON object, no prose, no markdown fences:
{"reason": "<one line diagnosis>",
 "confidence": 0.0-1.0,
 "ops": [ {"op":"add_pkgs","pkgs":["libfoo-dev"]},
          {"op":"set_base","base":"python:3.11-slim"},
          {"op":"set_env","key":"K","value":"V"},
          {"op":"insert_step","name":"n","cmd":"...","before":true},
          {"op":"rewrite","old":"...","new":"..."},
          {"op":"add_service","name":"postgres"} ] }

Rules: prefer the smallest change that could work. Never invent a package that
would not exist in Debian/PyPI/npm. If you cannot diagnose it, return
{"reason":"unknown","confidence":0,"ops":[]}."""


def llm_patch(llm, result: ExecResult, plan: RunPlan, step: Step) -> Optional[Patch]:
    """`llm` is any callable: (system: str, user: str) -> str."""
    import json as _json
    payload = _json.dumps({
        "command": step.cmd,
        "output_tail": result.tail(80),
        "plan": {"base": plan.base, "system_packages": plan.system_packages,
                 "steps": [s.cmd for s in plan.steps], "run": plan.run,
                 "env": list(plan.env)},
    })[:12000]
    try:
        raw = llm(LLM_SYSTEM, payload)
        data = _json.loads(re.sub(r"^```\w*|```$", "", raw.strip(), flags=re.M).strip())
    except Exception:
        return None
    ops = data.get("ops") or []
    if not ops or float(data.get("confidence", 0)) <= 0:
        return None

    def _apply(p: RunPlan) -> None:
        for op in ops:
            k = op.get("op")
            if k == "add_pkgs":
                _add_pkgs(p, [str(x) for x in op.get("pkgs", [])])
            elif k == "set_base" and op.get("base"):
                p.base = str(op["base"])
            elif k == "set_env":
                p.env[str(op["key"])] = str(op.get("value", ""))
            elif k == "insert_step":
                s = Step(str(op.get("name", "llm-fix")), str(op["cmd"]), network=True)
                p.steps.insert(_idx(p, step) if op.get("before", True) else _idx(p, step) + 1, s)
            elif k == "rewrite":
                _rewrite(p, str(op["old"]), str(op["new"]))
            elif k == "add_service":
                _add_service(p, str(op["name"]))

    return Patch(str(data.get("reason", "model-proposed fix")),
                 min(float(data.get("confidence", 0.5)), 0.85),
                 _apply, source="llm", tags=["llm"])
