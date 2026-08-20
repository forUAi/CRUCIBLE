"""
CRUCIBLE :: workspaces.py

A repository is a graph of projects, not a directory with a language.

Everything upstream of this module assumed one repo meant one application: it
collected evidence from the root, picked the strongest manifest, and planned
that. On a monorepo the result is confidently wrong rather than uncertain --
`vercel/turborepo` has 1091 package.json files and 75 Cargo.toml, and the
root is a pnpm workspace that runs nothing at all.

Discovery is declaration-first. A workspace root states its members --
`workspaces` in package.json, `packages:` in pnpm-workspace.yaml, `<modules>`
in a reactor pom, `use` in go.work -- and the author's list beats any
heuristic over directory names. Turborepo's list includes a *negation*
(`!packages/turbo`), which is exactly the kind of detail a heuristic invents
its way past.

Manifests no root claims are independent projects. `grpc/grpc-go` has ten
go.mod files and no go.work: nothing declares them, and they are still ten
modules.

Then each workspace gets a ROLE, because "runnable" is not the same question
as "buildable". `examples/` under a declared member list is still examples.
A workspace root with no start script is a container. A package with a `bin`
field is a CLI. These are recorded with the file and field that decided them,
so a wrong answer can be argued with.

Only workspaces whose role is deployable get a plan, and the plan comes from
the existing evidence -> planner pipeline pointed at that subtree. Nothing
here re-implements planning; it decides *what* to plan.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Directories that are never a deployable component, whatever their manifest
# says. A member list can legitimately include examples/ and docs/ -- turbo's
# does -- and they are still not the thing you deploy.
NON_DEPLOYABLE_SEGMENTS = {
    "example", "examples", "sample", "samples", "demo", "demos",
    "test", "tests", "testing", "e2e", "integration-tests", "fixtures",
    "benchmark", "benchmarks", "bench", "docs", "doc", "website", "site",
    "playground", "scripts", "tools", "tooling", "devtools",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist",
    "build", ".next", ".nuxt", "vendor", ".tox", ".gradle", ".idea", "out",
    "coverage", ".svelte-kit", ".turbo", ".yarn", "third_party",
}
# A manifest inside one of these belongs to source, not to a project. Vue
# ships packages-private/sfc-playground/src/download/template/package.json as
# a template FILE; reporting it as a workspace is a category error.
INNER_SEGMENTS = {"src", "lib", "templates", "template", "__tests__",
                  "test-fixtures", "testdata", "generators", "blueprints"}
MAX_DEPTH = 6
MAX_MANIFESTS = 4000

# Scripts that hand work to another workspace rather than doing it.
_DELEGATES = re.compile(
    r"\b(yarn\s+workspace|npm\s+-w\b|npm\s+--workspace|pnpm\s+--filter|"
    r"pnpm\s+-F\b|turbo\s+run|nx\s+run|lerna\s+run|lerna\s+exec)")

ROLE_DEPLOYABLE = {"service", "application", "worker", "cli"}


@dataclass
class Reason:
    """Why a decision was made, and what said so."""
    claim: str
    source: str            # repo-relative file that carries the evidence
    field: str = ""        # the manifest key, where there is one

    def __str__(self) -> str:
        where = f"{self.source}:{self.field}" if self.field else self.source
        return f"{self.claim} [{where}]"


@dataclass
class Workspace:
    path: str                                   # repo-relative; "." is the root
    manifests: list[str] = field(default_factory=list)
    language: str = ""
    build_system: str = ""
    role: str = "unknown"
    runnable: bool = False
    is_root: bool = False                       # declares members
    declared_by: str = ""                       # the root that claimed it
    members: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)
    rejected_because: str = ""
    status: str = "ok"                          # ok|ambiguous|unsupported|needs_configuration
    confidence: float = 0.0

    def why(self) -> list[str]:
        return [str(r) for r in self.reasons]

    def note(self, claim: str, source: str, field_: str = "") -> None:
        self.reasons.append(Reason(claim, source, field_))


@dataclass
class RepoGraph:
    root: str
    workspaces: list[Workspace] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def by_path(self, p: str) -> Optional[Workspace]:
        return next((w for w in self.workspaces if w.path == p), None)

    def deployable(self) -> list[Workspace]:
        return [w for w in self.workspaces if w.runnable]

    def rejected(self) -> list[Workspace]:
        return [w for w in self.workspaces if not w.runnable]

    def languages(self) -> list[str]:
        return sorted({w.language for w in self.workspaces if w.language})


# ---------------------------------------------------------------------------
# manifest discovery
# ---------------------------------------------------------------------------

MANIFEST_KIND = {
    "package.json": ("node", "npm"),
    "pnpm-workspace.yaml": ("node", "pnpm"),
    "go.mod": ("go", "gomod"),
    "go.work": ("go", "gomod"),
    "Cargo.toml": ("rust", "cargo"),
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "build.gradle.kts": ("kotlin", "gradle"),
    "settings.gradle": ("java", "gradle"),
    "settings.gradle.kts": ("kotlin", "gradle"),
    "pyproject.toml": ("python", "pip"),
    "setup.py": ("python", "pip"),
    "requirements.txt": ("python", "pip"),
    "Gemfile": ("ruby", "bundler"),
    "composer.json": ("php", "composer"),
    "mix.exs": ("elixir", "mix"),
}


def _walk_manifests(root: Path) -> tuple[dict[str, list[str]], bool]:
    """{workspace_dir: [manifest names]} for every directory holding one."""
    found: dict[str, list[str]] = {}
    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        if depth >= MAX_DEPTH:
            dirnames.clear()
        here = [f for f in sorted(filenames) if f in MANIFEST_KIND]
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        if any(seg in INNER_SEGMENTS for seg in parts[:-1]):
            here = []          # inside another project's source tree
        if here:
            found[rel_dir if rel_dir != "." else "."] = here
            count += len(here)
            if count > MAX_MANIFESTS:
                truncated = True
                break
    return found, truncated


# ---------------------------------------------------------------------------
# workspace roots and their declared members
# ---------------------------------------------------------------------------

def _match_members(root: Path, patterns: list[str]) -> list[str]:
    """Expand member globs, honouring `!` negation.

    pnpm and yarn both allow exclusions, and turbo's own workspace uses one
    (`!packages/turbo`). A discovery pass that ignores negation reports a
    member the author explicitly removed.
    """
    include = [p for p in patterns if not p.startswith("!")]
    exclude = [p[1:] for p in patterns if p.startswith("!")]

    candidates: set[str] = set()
    for pat in include:
        pat = str(pat).strip().strip("/")
        # apache/airflow lists "." as a member. pathlib refuses it as a glob
        # and the whole discovery crashed; a malformed member entry is the
        # repository's business, not a reason to fail the repository.
        if not pat or pat == ".":
            candidates.add(".")
            continue
        # `apps/*` means directories, not files; `examples` means that one dir.
        try:
            hits = sorted(root.glob(pat))
        except (ValueError, OSError, IndexError):
            continue
        for hit in hits:
            if hit.is_dir() and not any(part in SKIP_DIRS for part in hit.parts):
                candidates.add(str(hit.relative_to(root)))
    for pat in exclude:
        for hit in list(candidates):
            if fnmatch.fnmatch(hit, pat):
                candidates.discard(hit)
    return sorted(candidates)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(errors="replace"))
    except (OSError, ValueError):
        return {}


def _pnpm_packages(text: str) -> list[str]:
    """`packages:` from pnpm-workspace.yaml, without a YAML parser."""
    out, in_block = [], False
    for raw in text.splitlines():
        if re.match(r"^\s*packages\s*:", raw):
            in_block = True
            continue
        if in_block:
            m = re.match(r"""^\s*-\s*['"]?([^'"#]+?)['"]?\s*$""", raw)
            if m:
                out.append(m.group(1).strip())
            elif raw.strip() and not raw.startswith((" ", "\t", "-")):
                break
    return out


def _detect_roots(root: Path, manifests: dict[str, list[str]]) -> dict[str, Workspace]:
    """Workspaces that declare members. The declaration is authoritative."""
    roots: dict[str, Workspace] = {}

    def ws(path: str) -> Workspace:
        return roots.setdefault(path, Workspace(path=path, is_root=True))

    for wpath, names in manifests.items():
        base = root / ("" if wpath == "." else wpath)

        if "package.json" in names:
            data = _read_json(base / "package.json")
            raw = data.get("workspaces")
            pats = raw if isinstance(raw, list) else (raw or {}).get("packages", [])
            if pats:
                w = ws(wpath)
                w.language, w.build_system = "node", "npm"
                if (data.get("packageManager") or "").startswith("yarn"):
                    w.build_system = "yarn"
                w.members = _match_members(base, pats)
                w.note(f"declares {len(w.members)} workspace member(s)",
                       f"{wpath}/package.json".lstrip("./"), "workspaces")

        if "pnpm-workspace.yaml" in names:
            pats = _pnpm_packages((base / "pnpm-workspace.yaml").read_text(errors="replace"))
            if pats:
                w = ws(wpath)
                w.language, w.build_system = "node", "pnpm"
                w.members = _match_members(base, pats)
                w.note(f"pnpm workspace declaring {len(w.members)} member(s)",
                       f"{wpath}/pnpm-workspace.yaml".lstrip("./"), "packages")

        if "go.work" in names:
            txt = (base / "go.work").read_text(errors="replace")
            uses = re.findall(r"^\s*(?:use\s+)?\(?\s*\.?/?([\w./-]+)", txt, re.M)
            w = ws(wpath)
            w.language, w.build_system = "go", "gomod"
            w.members = sorted({u.strip("/") for u in uses
                                if (base / u).is_dir() and u not in ("go", "use")})
            w.note(f"go workspace with {len(w.members)} module(s)",
                   f"{wpath}/go.work".lstrip("./"), "use")

        if "Cargo.toml" in names:
            txt = (base / "Cargo.toml").read_text(errors="replace")
            if "[workspace]" in txt:
                m = re.search(r"members\s*=\s*\[([^\]]*)\]", txt, re.S)
                pats = re.findall(r"""['"]([^'"]+)['"]""", m.group(1)) if m else []
                w = ws(wpath)
                w.language, w.build_system = "rust", "cargo"
                w.members = _match_members(base, pats)
                w.note(f"cargo workspace with {len(w.members)} member(s)",
                       f"{wpath}/Cargo.toml".lstrip("./"), "workspace.members")

        if "pom.xml" in names:
            mods = _pom_modules(base / "pom.xml")
            if mods:
                w = ws(wpath)
                w.language, w.build_system = "java", "maven"
                w.members = [m for m in mods if (base / m).is_dir()]
                w.note(f"maven reactor with {len(w.members)} module(s)",
                       f"{wpath}/pom.xml".lstrip("./"), "modules")

        for sg in ("settings.gradle", "settings.gradle.kts"):
            if sg in names:
                txt = (base / sg).read_text(errors="replace")
                incs = re.findall(r"""include\s*\(?\s*['"]:?([\w.\-:]+)['"]""", txt)
                paths = [i.replace(":", "/") for i in incs]
                paths = [p for p in paths if (base / p).is_dir()]
                if paths:
                    w = ws(wpath)
                    w.language, w.build_system = "java", "gradle"
                    w.members = sorted(set(paths))
                    w.note(f"gradle build with {len(w.members)} subproject(s)",
                           f"{wpath}/{sg}".lstrip("./"), "include")

        if "pyproject.toml" in names:
            txt = (base / "pyproject.toml").read_text(errors="replace")
            m = re.search(r"\[tool\.uv\.workspace\][^\[]*?members\s*=\s*\[([^\]]*)\]",
                          txt, re.S)
            if m:
                pats = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
                w = ws(wpath)
                w.language, w.build_system = "python", "uv"
                w.members = _match_members(base, pats)
                w.note(f"uv workspace with {len(w.members)} member(s)",
                       f"{wpath}/pyproject.toml".lstrip("./"), "tool.uv.workspace.members")
    return roots


def _pom_modules(pom: Path) -> list[str]:
    try:
        import xml.etree.ElementTree as ET
        txt = pom.read_text(errors="replace")
        if "<!ENTITY" in txt:
            return []
        proj = ET.fromstring(txt)
    except Exception:
        return []
    for child in proj:
        if child.tag.rsplit("}", 1)[-1] == "modules":
            return [m.text.strip() for m in child if m.text]
    return []


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------

def _classify(root: Path, w: Workspace, manifests: list[str]) -> None:
    """Assign a role, recording what decided it."""
    base = root / ("" if w.path == "." else w.path)
    segs = {s.lower() for s in Path(w.path).parts if s != "."}

    if bad := (segs & NON_DEPLOYABLE_SEGMENTS):
        w.role = "example" if bad & {"example", "examples", "sample", "samples",
                                     "demo", "demos", "playground"} else (
            "docs" if bad & {"docs", "doc", "website", "site"} else "test")
        w.rejected_because = (f"path segment {sorted(bad)[0]!r} marks this as "
                              f"{w.role}, not a deployable component")
        w.note(f"role={w.role} from its location", w.path or ".", "path")
        return

    if "package.json" in manifests:
        data = _read_json(base / "package.json")
        scripts = data.get("scripts") or {}
        src = f"{w.path}/package.json".lstrip("./")
        starter = next((s for s in ("start", "serve", "dev") if s in scripts), None)

        if data.get("bin"):
            w.role, w.runnable = "cli", True
            w.note("declares a `bin` entry point", src, "bin")
            return

        # A root that declares members and whose start script hands off to
        # workspace tooling is a launcher, not the thing launched. Backstage's
        # root `start` runs `yarn workspace example-app start`; treating it as
        # a service makes the repository look like it deploys from its root.
        if w.is_root and w.members:
            # A root that declares members is a container. Its start script,
            # where it has one, launches something else: Backstage's root runs
            # `backstage-cli repo start`, which is a repo-level launcher and
            # not a service -- enumerating delegating command shapes was a
            # losing game, and the member list is the fact that decides.
            w.role = "workspace-root"
            if starter:
                w.status = "ambiguous"
                w.rejected_because = (
                    f"declares {len(w.members)} member(s) AND has a "
                    f"`{starter}` script ({str(scripts[starter])[:40]!r}); "
                    f"treated as a launcher -- plan a member explicitly to "
                    f"override")
                w.note("root with members and a start script", src,
                       f"scripts.{starter}")
            else:
                w.rejected_because = ("declares members and has no start "
                                      "script; a container, not a service")
                w.note("workspace root with nothing to start", src, "workspaces")
            return

        # A package that publishes a library entry point is consumed, not
        # deployed. Every Backstage plugin has `main: src/index.ts` and a
        # `start` that is a dev harness (`backstage-cli package start`) --
        # reading that script as a service produced 193 of them.
        entry = next((k for k in ("main", "module", "exports", "types")
                      if data.get(k)), None)
        # ...but only where it is genuinely an entry point for a CONSUMER.
        # heroku/node-js-getting-started is `private: true` with `main:
        # index.js` and `start: node index.js` -- a standalone app that names
        # its own entry file, not a package anyone imports. Applying the rule
        # to it rejected an application CRUCIBLE demonstrably runs. The two
        # signals that make it a library are being a declared member of a
        # workspace (something in this repo consumes it) or being publishable.
        consumed = bool(w.declared_by) or not data.get("private")
        if entry and consumed and not data.get("bin"):
            w.role = "library"
            w.rejected_because = (f"publishes a library entry point "
                                  f"(`{entry}`) and declares no `bin`")
            w.note(f"library entry point {entry}={data.get(entry)!r}", src, entry)
            return

        if starter:
            w.role, w.runnable = "service", True
            w.note(f"has a `{starter}` script", src, f"scripts.{starter}")
            return

        w.role = "library"
        w.rejected_because = "no bin and no start/serve/dev script"
        w.note("no start mechanism declared", src, "scripts")
        return

    if "go.mod" in manifests:
        src = f"{w.path}/go.mod".lstrip("./")
        mains = _go_mains(base)
        # Go states where a binary lives: the module root, or cmd/<name>/.
        # A `package main` anywhere else is a helper -- grpc-go keeps them in
        # interop/ and benchmark/, and counting those made the gRPC library
        # itself look like four deployable services. Listing every such
        # directory name is a losing game; the convention is the rule.
        entry = [m for m in mains
                 if "/" not in m or m.startswith("cmd/") or "/cmd/" in m]
        if entry:
            w.role, w.runnable = ("cli" if "cmd/" in entry[0] else "application"), True
            w.note(f"package main at {entry[0]}", entry[0], "func main")
            return
        w.role = "library"
        if mains:
            w.rejected_because = (
                f"`package main` exists only outside the module root and "
                f"cmd/ (e.g. {mains[0]}), which is a helper, not the "
                f"module's entry point")
            w.note(f"non-entry main at {mains[0]}", mains[0], "func main")
        else:
            w.rejected_because = "no `package main` anywhere in the module"
            w.note("no main package", src)
        return

    if "pom.xml" in manifests or "build.gradle" in manifests \
            or "build.gradle.kts" in manifests:
        if w.is_root and w.members:
            w.role = "workspace-root"
            w.rejected_because = "reactor/aggregator module with no application of its own"
            return
        w.role, w.runnable = "service", True
        w.note("JVM module planned on its own", w.path or ".")
        return

    if "pyproject.toml" in manifests or "setup.py" in manifests \
            or "requirements.txt" in manifests:
        w.role, w.runnable = "application", True
        w.note("python project planned on its own", w.path or ".")
        return

    if "Cargo.toml" in manifests:
        if (base / "src/main.rs").exists() or (base / "src/bin").is_dir():
            w.role, w.runnable = "cli", True
            w.note("has src/main.rs", f"{w.path}/src/main.rs".lstrip("./"))
        else:
            w.role = "library"
            w.rejected_because = "crate with no binary target"
        return

    w.role = "unknown"
    w.status = "ambiguous"
    w.rejected_because = "no manifest identified a build or run mechanism"


def _go_mains(base: Path, limit: int = 400) -> list[str]:
    """`package main` belonging to THIS module.

    A nested directory with its own go.mod is a different module, and walking
    into one made grpc-go's library root look like an application because a
    main lived in examples/. Module boundaries are where the walk stops.
    """
    out, n = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")
                       and d.lower() not in NON_DEPLOYABLE_SEGMENTS
                       and not (Path(dirpath) / d / "go.mod").exists()]
        for f in filenames:
            # A `package main` in a _test.go file is a test harness, not the
            # module's entry point. Prometheus was cited as runnable on the
            # evidence of cmd/prometheus/reload_test.go, which is the right
            # answer reached through the wrong file.
            if not f.endswith(".go") or f.endswith("_test.go"):
                continue
            n += 1
            if n > limit:
                return out
            p = Path(dirpath) / f
            try:
                if re.search(r"^package main\b", p.read_text(errors="replace")[:4000], re.M):
                    out.append(str(p.relative_to(base)))
                    if len(out) >= 3:
                        return out
            except OSError:
                pass
    return out


# ---------------------------------------------------------------------------
# dependencies between workspaces
# ---------------------------------------------------------------------------

def _link(root: Path, graph: RepoGraph) -> None:
    by_name: dict[str, str] = {}
    for w in graph.workspaces:
        base = root / ("" if w.path == "." else w.path)
        if "package.json" in w.manifests:
            nm = _read_json(base / "package.json").get("name")
            if nm:
                by_name[nm] = w.path

    for w in graph.workspaces:
        base = root / ("" if w.path == "." else w.path)
        if "package.json" in w.manifests:
            data = _read_json(base / "package.json")
            deps = {**(data.get("dependencies") or {}),
                    **(data.get("devDependencies") or {})}
            for name, spec in deps.items():
                if name in by_name and (
                        str(spec).startswith(("workspace:", "file:", "link:"))
                        or by_name[name] != w.path):
                    if by_name[name] != w.path:
                        w.depends_on.append(by_name[name])
        if "go.mod" in w.manifests:
            txt = (base / "go.mod").read_text(errors="replace")
            for rel in re.findall(r"^\s*replace\s+\S+\s+=>\s+(\.\.?/\S+)", txt, re.M):
                target = os.path.normpath(os.path.join(w.path, rel))
                if graph.by_path(target):
                    w.depends_on.append(target)
        w.depends_on = sorted(set(w.depends_on))


# ---------------------------------------------------------------------------

# Roles reached by a rule that only says "there is a project here". These get
# a second opinion from the planner rather than being trusted.
_WEAK_ROLES = {"application", "service"}


def resolve_roles(graph: RepoGraph, limit: int = 40) -> None:
    """Ask the real planner whether each candidate is actually an application.

    The structural pass can say "there is a Python project here"; it cannot
    say whether that project is Flask the framework or an app built on Flask.
    The planner already answers that -- 11/12 on the archetype benchmark,
    with the framework-source and provenance rules behind it -- so this
    module does not get a second, worse copy of that logic.

    Only candidates whose role came from a weak rule are re-examined, which
    keeps the cost proportional to how ambiguous the repository is.
    """
    from .evidence import collect
    from .planner import plan as make_plan

    root = Path(graph.root)
    weak = [w for w in graph.workspaces
            if w.runnable and w.role in _WEAK_ROLES
            and "package.json" not in w.manifests]
    if len(weak) > limit:
        graph.notes.append(
            f"{len(weak)} candidates needed planner adjudication; only the "
            f"first {limit} were checked, so roles beyond that are structural "
            f"guesses and recall here is not complete")
        weak = weak[:limit]

    for w in weak:
        target = root if w.path == "." else root / w.path
        try:
            p = make_plan(collect(str(target)))
        except Exception as e:
            w.status = "ambiguous"
            w.note(f"planner could not adjudicate: {type(e).__name__}", w.path or ".")
            continue
        if p.archetype in ("web", "cli", "worker"):
            w.role = {"web": "service"}.get(p.archetype, p.archetype)
            w.runnable = True
            w.note(f"planner archetype={p.archetype}, run={p.run[:44]!r}",
                   w.path or ".", "archetype")
        else:
            w.role, w.runnable = "library", False
            w.rejected_because = (f"planner archetype={p.archetype}: no "
                                  f"application entry point in this workspace")
            w.note(f"planner archetype={p.archetype}", w.path or ".", "archetype")
        if p.status != "ok":
            w.status = p.status


def discover(repo: str) -> RepoGraph:
    """Build the workspace graph for a repository."""
    root = Path(repo).resolve()
    graph = RepoGraph(root=str(root))
    manifests, truncated = _walk_manifests(root)
    graph.truncated = truncated
    if truncated:
        graph.notes.append(
            f"manifest scan stopped at {MAX_MANIFESTS}; the graph is partial "
            f"and workspace recall is not complete for this repository")

    roots = _detect_roots(root, manifests)

    # Who claimed whom. A member list is authoritative over any guess.
    claimed: dict[str, str] = {}
    for rpath, rws in roots.items():
        for m in rws.members:
            full = m if rpath == "." else f"{rpath}/{m}"
            claimed[full] = rpath

    for wpath, names in sorted(manifests.items()):
        w = roots.get(wpath) or Workspace(path=wpath)
        w.manifests = names
        if not w.language:
            for n in names:
                lang, bs = MANIFEST_KIND[n]
                if lang:
                    w.language, w.build_system = lang, bs
                    w.note(f"language={lang} build={bs}",
                           f"{wpath}/{n}".lstrip("./"))
                    break
        w.declared_by = claimed.get(wpath, "")
        if w.declared_by:
            w.note(f"declared as a member of {w.declared_by or '<root>'}",
                   f"{w.declared_by}/package.json".lstrip("./") or ".", "workspaces")
        _classify(root, w, names)
        graph.workspaces.append(w)

    # A declared member with no manifest of its own is still a member; record
    # it so recall is measured against the author's list, not ours.
    for full, owner in sorted(claimed.items()):
        if not graph.by_path(full) and (root / full).is_dir():
            w = Workspace(path=full, declared_by=owner, role="unknown",
                          status="ambiguous",
                          rejected_because="declared as a member but carries no manifest")
            w.note(f"declared by {owner or '<root>'} with no manifest found", owner or ".")
            graph.workspaces.append(w)

    _link(root, graph)
    resolve_roles(graph)
    return graph
