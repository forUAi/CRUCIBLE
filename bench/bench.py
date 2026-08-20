"""
CRUCIBLE :: bench/bench.py

Detection benchmark against real repositories.

`examples/` proves the planner does what I think it does on repos I wrote.
That is a tautology detector. This runs the analysis half against upstream
code nobody shaped for it, and scores three things:

    language      objective. The repo either is Java or it isn't.
    archetype     against a hand label. Labels are a judgement call and are
                  marked `ambiguous` where they genuinely are, so a miss on
                  one of those is reported separately and not counted as
                  wrong.
    lint errors   objective and label-free. An `error` means the plan cannot
                  verify what it claims to verify -- the plan disagrees with
                  its own evidence, which is true or false regardless of what
                  anyone thinks the repo is.

The label-free column is the one that matters for before/after, because it
needs no ground truth to be believed:

    python3 bench/bench.py --run --out after.json
    git worktree add /tmp/pre <pre-fix-sha>
    PYTHONPATH=/tmp/pre python3 bench/bench.py --run --out before.json
    python3 bench/bench.py --compare before.json after.json

Nothing here executes repository code. Clone, read, plan, lint.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# CRUCIBLE_ROOT lets the same harness run against a different checkout of the
# package -- that is how the before/after comparison stays honest: one script,
# one corpus, two implementations.
sys.path.insert(0, os.environ.get(
    "CRUCIBLE_ROOT", str(Path(__file__).resolve().parent.parent)))

from crucible.evidence import collect          # noqa: E402
from crucible.planner import plan as make_plan  # noqa: E402

try:
    from crucible.lint import errors as lint_errors, lint
except ImportError:      # running against a checkout from before lint.py
    lint = None
    def lint_errors(_):  # noqa: E704
        return []

CACHE = Path(os.environ.get("BENCH_CACHE", "/tmp/crucible-bench"))

# (slug, url, language, archetype, ambiguous?)
#
# Labels are what a competent engineer would say the repo *is*. A framework's
# own repository is a library even though it is all about serving HTTP; a
# starter template is a web app even though it does almost nothing.
REPOS = [
    # --- JVM: the ecosystem the fix was about -------------------------------
    ("spring-petclinic",  "https://github.com/spring-projects/spring-petclinic",
     "java", "web", False),
    ("gs-rest-service",   "https://github.com/spring-guides/gs-rest-service",
     "java", "web", True),        # repo holds initial/ + complete/ subprojects
    ("gs-accessing-data-jpa", "https://github.com/spring-guides/gs-accessing-data-jpa",
     "java", "web", True),
    ("micronaut-examples", "https://github.com/micronaut-projects/micronaut-examples",
     "java", "web", True),

    # --- Python -------------------------------------------------------------
    ("fastapi-template",  "https://github.com/fastapi/full-stack-fastapi-template",
     "python", "web", True),      # backend/ + frontend/ monorepo
    ("requests",          "https://github.com/psf/requests",
     "python", "library", False),
    ("flask",             "https://github.com/pallets/flask",
     "python", "library", False),
    ("rich",              "https://github.com/Textualize/rich",
     "python", "library", False),

    # --- Node ---------------------------------------------------------------
    ("express",           "https://github.com/expressjs/express",
     "node", "library", False),
    ("node-getting-started", "https://github.com/heroku/node-js-getting-started",
     "node", "web", False),
    ("nest",              "https://github.com/nestjs/nest",
     "node", "library", False),

    # --- Go -----------------------------------------------------------------
    ("gin",               "https://github.com/gin-gonic/gin",
     "go", "library", False),
    ("ripgrep",           "https://github.com/BurntSushi/ripgrep",
     "rust", "cli", False),
    ("axum",              "https://github.com/tokio-rs/axum",
     "rust", "library", False),

    # --- Ruby / PHP ---------------------------------------------------------
    ("ruby-getting-started", "https://github.com/heroku/ruby-getting-started",
     "ruby", "web", False),
    ("laravel",           "https://github.com/laravel/laravel",
     "php", "web", False),

    # --- declared-plan path -------------------------------------------------
    ("example-voting-app", "https://github.com/dockersamples/example-voting-app",
     None, None, True),           # polyglot monorepo, no single right answer
    ("compose-flask",     "https://github.com/docker/awesome-compose",
     None, None, True),
]


# ---------------------------------------------------------------------------

def clone_all(only: list[str] | None = None) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for slug, url, *_ in REPOS:
        if only and slug not in only:
            continue
        dest = CACHE / slug
        if dest.is_dir():
            print(f"  cached   {slug}")
            continue
        t0 = time.time()
        r = subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(dest)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  FAILED   {slug}: {r.stderr.strip()[:120]}")
            shutil.rmtree(dest, ignore_errors=True)
        else:
            mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1e6
            print(f"  cloned   {slug}  ({mb:.0f} MB, {time.time() - t0:.1f}s)")


def analyze_one(slug: str) -> dict:
    d = CACHE / slug
    t0 = time.time()
    ev = collect(str(d))
    p = make_plan(ev)
    findings = lint(p, ev) if lint else []
    return {
        "slug": slug,
        "ms": round((time.time() - t0) * 1000),
        "files": len(ev.files),
        "shape": ev.fingerprint(),
        "language": ev.top("language"),
        "pkgmgr": ev.top("pkgmgr"),
        "framework": ev.top("framework"),
        "base": p.base,
        "archetype": p.archetype,
        "run": p.run,
        "ports": p.ports,
        "oracle": p.oracle.get("kind"),
        "services": sorted(s.name for s in p.services),
        "steps": [s.cmd for s in p.steps],
        "lint_errors": [f.code for f in lint_errors(findings)],
        "lint_warns": [f.code for f in findings if f.severity == "warn"],
        "lint_detail": [str(f) for f in findings],
    }


def run(out: str | None) -> dict:
    rows, meta = [], {}
    for slug, _url, lang, arch, amb in REPOS:
        if not (CACHE / slug).is_dir():
            print(f"  skip     {slug} (not cloned)")
            continue
        try:
            r = analyze_one(slug)
        except Exception as e:                      # a crash is a benchmark result
            r = {"slug": slug, "crashed": f"{type(e).__name__}: {e}"}
        r["want_language"], r["want_archetype"], r["ambiguous"] = lang, arch, amb
        rows.append(r)
        meta[slug] = r
    report = {"rows": rows, "crucible": _describe_checkout()}
    if out:
        Path(out).write_text(json.dumps(report, indent=2) + "\n")
    _table(rows)
    return report


def _describe_checkout() -> str:
    pkg = Path(__import__("crucible").__file__).resolve().parent
    r = subprocess.run(["git", "-C", str(pkg), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return f"{pkg} @ {r.stdout.strip() or 'unknown'}"


def _table(rows: list[dict]) -> None:
    print(f"\n{'repo':<24}{'lang':<9}{'archetype':<11}{'oracle':<8}"
          f"{'port':<7}{'lint':<6}verdict")
    print("─" * 96)
    ok_l = ok_a = amb_miss = crashed = lint_bad = 0
    counted_l = counted_a = 0
    for r in rows:
        if r.get("crashed"):
            crashed += 1
            print(f"{r['slug']:<24}{'CRASH':<9}{r['crashed'][:56]}")
            continue
        lang_ok = r["want_language"] is None or r["language"] == r["want_language"]
        arch_ok = r["want_archetype"] is None or r["archetype"] == r["want_archetype"]
        if r["want_language"] is not None:
            counted_l += 1
            ok_l += lang_ok
        if r["want_archetype"] is not None and not r["ambiguous"]:
            counted_a += 1
            ok_a += arch_ok
        elif r["want_archetype"] is not None and not arch_ok:
            amb_miss += 1
        errs = r["lint_errors"]
        lint_bad += bool(errs)
        verdict = []
        if not lang_ok:
            verdict.append(f"lang≠{r['want_language']}")
        if not arch_ok:
            verdict.append(f"arch≠{r['want_archetype']}" + ("?" if r["ambiguous"] else ""))
        if errs:
            verdict.append(",".join(errs))
        print(f"{r['slug']:<24}{str(r['language']):<9}{r['archetype']:<11}"
              f"{str(r['oracle']):<8}{str(r['ports'] or '-'):<7}"
              f"{(str(len(errs)) if errs else '·'):<6}"
              f"{'ok' if not verdict else ' '.join(verdict)}")
    print("─" * 96)
    n = len(rows)
    print(f"{n} repos   language {ok_l}/{counted_l}   "
          f"archetype {ok_a}/{counted_a} (+{amb_miss} ambiguous miss)   "
          f"plans with a lint error {lint_bad}/{n}   crashes {crashed}")


def compare(a: str, b: str) -> None:
    ra = {r["slug"]: r for r in json.loads(Path(a).read_text())["rows"]}
    rb = {r["slug"]: r for r in json.loads(Path(b).read_text())["rows"]}
    print(f"\nA = {a}\nB = {b}\n")
    print(f"{'repo':<24}{'A archetype':<14}{'B archetype':<14}{'A lint':<9}{'B lint':<9}change")
    print("─" * 96)
    better = worse = same = 0
    for slug in sorted(set(ra) | set(rb)):
        x, y = ra.get(slug, {}), rb.get(slug, {})
        xa, ya = x.get("archetype", "-"), y.get("archetype", "-")
        xe, ye = len(x.get("lint_errors", [])), len(y.get("lint_errors", []))
        # Direction comes from the label, not from a guess about which way is
        # up: web->library is an improvement for gin and a regression for
        # petclinic, and only the ground truth knows which.
        want = y.get("want_archetype") or x.get("want_archetype")
        note = ""
        if xa != ya or xe != ye:
            if want and xa != want and ya == want:
                note = "fixed"
            elif want and xa == want and ya != want:
                note = "REGRESSED"
            elif ye < xe:
                note = "fewer lint errors"
            elif ye > xe:
                note = "MORE lint errors"
            else:
                note = "changed"
            better += note in ("fixed", "fewer lint errors")
            worse += note in ("REGRESSED", "MORE lint errors")
        else:
            same += 1
        print(f"{slug:<24}{xa:<14}{ya:<14}{xe:<9}{ye:<9}{note}")
    print("─" * 96)
    print(f"better {better}   worse {worse}   unchanged {same}")
    for label, rs in (("A", ra), ("B", rb)):
        run_cmds = [r.get("run", "") for r in rs.values()]
        tests = sum(1 for c in run_cmds if c and any(
            t in c for t in ("test", "pytest", "rspec", "phpunit")))
        webs = sum(1 for r in rs.values() if r.get("archetype") == "web")
        print(f"  {label}: {webs} web archetypes, "
              f"{tests} plans whose run command is a test suite")


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("--clone", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
        return 0
    if a.clone:
        clone_all(a.only)
    if a.run or not a.clone:
        run(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
