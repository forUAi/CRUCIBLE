"""
CRUCIBLE :: bench/wsbench.py

Workspace-discovery benchmark over the pinned corpus.

Scores only what `bench/corpus.py` labelled with confidence. A repository with
no label for a metric is counted `unlabelled` and excluded from that metric's
denominator -- it is not counted as a pass, and it is not given an invented
label to make the denominator bigger.

    python3 bench/wsbench.py --split dev
    python3 bench/wsbench.py --split dev,validation --out /tmp/ws.json
    python3 bench/wsbench.py --split holdout --unlock   # requires the flag

The holdout refuses to run without `--unlock`, because the value of a holdout
is entirely in not having looked at it. The flag is a speed bump against
one's own curiosity, not a security control.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.corpus import CORPUS, Repo                      # noqa: E402
from crucible.workspaces import discover                    # noqa: E402

# Overridable so a release run can point at a persistent location rather than
# re-cloning the corpus into /tmp on every invocation.
CACHE = Path(os.environ.get("CRUCIBLE_CORPUS", "/tmp/crucible-bench"))


def fetch_corpus(repos, log=print) -> int:
    """Clone any pinned repository that is not present.

    Blobless partial clones: the workspace graph is built from manifests and
    directory structure, so full file contents are not needed and a corpus of
    32 repositories would otherwise be several GB.
    """
    import subprocess
    CACHE.mkdir(parents=True, exist_ok=True)
    got = 0
    for r in repos:
        dest = CACHE / r.slug.split("/")[-1]
        if (dest / ".git").is_dir():
            got += 1
            continue
        shutil.rmtree(dest, ignore_errors=True)
        p = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1",
             "--filter=blob:none", r.url, str(dest)],
            capture_output=True, text=True, timeout=1800)
        if p.returncode == 0 and (dest / ".git").is_dir():
            got += 1
        else:
            log(f"  ! clone failed for {r.slug}: "
                f"{(p.stderr or p.stdout).strip()[:120]}")
    return got


def local(r: Repo) -> Path:
    return CACHE / r.slug.split("/")[-1]


def score_one(r: Repo) -> dict:
    d = local(r)
    if not d.is_dir():
        return {"slug": r.slug, "outcome": "not_fetched"}
    t0 = time.time()
    try:
        g = discover(str(d))
    except Exception as e:
        return {"slug": r.slug, "outcome": "crash",
                "detail": f"{type(e).__name__}: {e}"}
    secs = round(time.time() - t0, 1)

    dep = g.deployable()
    paths = [w.path for w in dep]
    checks: dict[str, Optional[bool]] = {}

    checks["language"] = (None if not r.languages else
                          bool(set(r.languages) & set(g.languages())))
    checks["root_runnable"] = (None if r.root_runnable is None else
                               (("." in paths) == r.root_runnable))
    if r.min_runnable is None and r.max_runnable is None:
        checks["count"] = None
    else:
        ok = True
        if r.min_runnable is not None:
            ok = ok and len(dep) >= r.min_runnable
        if r.max_runnable is not None:
            ok = ok and len(dep) <= r.max_runnable
        checks["count"] = ok
    checks["must_include"] = (None if not r.must_include else
                              all(p in paths for p in r.must_include))
    checks["must_exclude"] = (None if not r.must_exclude else
                              not any(p in paths for p in r.must_exclude))
    # Label-free, like the lint column in the planning benchmark: a rejected
    # workspace that gives no reason is a defect regardless of ground truth.
    checks["explained"] = all(w.rejected_because or w.role == "library"
                              for w in g.rejected()) and \
                          all(w.why() for w in g.workspaces)

    graded = {k: v for k, v in checks.items() if v is not None}
    return {
        "slug": r.slug, "split": r.split, "band": r.band, "shape": r.shape,
        "outcome": "scored", "seconds": secs,
        "workspaces": len(g.workspaces), "deployable": len(dep),
        "languages": g.languages(), "truncated": g.truncated,
        "paths": paths[:8],
        "checks": checks,
        "passed": sum(1 for v in graded.values() if v),
        "graded": len(graded),
        "ambiguous": sum(1 for w in g.workspaces if w.status == "ambiguous"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="wsbench")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--unlock", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--clone", action="store_true",
                    help="fetch any pinned repository that is not present")
    a = ap.parse_args()

    splits = [s.strip() for s in a.split.split(",")]
    if "sealed" in splits:
        sys.exit("the sealed split is reserved for the next checkpoint and has "
                 "no unlock. Running it here would consume it exactly as the "
                 "checkpoint-1 holdout was consumed")
    if "holdout" in splits and not a.unlock:
        sys.exit("refusing to run the holdout without --unlock; its value is "
                 "entirely in not having looked at it")
    if "holdout" in splits:
        from bench.corpus import HOLDOUT_CONSUMED
        print(f"NOTE: this holdout was CONSUMED at checkpoint "
              f"{HOLDOUT_CONSUMED['checkpoint']} -- "
              f"{HOLDOUT_CONSUMED['why_consumed']}. It no longer measures "
              f"generalisation; use the sealed split at the next checkpoint.\n")

    repos = [r for r in CORPUS if r.split in splits]
    if a.clone:
        n = fetch_corpus(repos)
        print(f"corpus: {n}/{len(repos)} repository(ies) present\n")
    rows = [score_one(r) for r in repos]

    print(f"{'repo':34}{'split':11}{'ws':>5}{'dep':>5}  checks  result")
    print("─" * 96)
    tally: dict[str, list[int]] = {}
    for row in rows:
        if row["outcome"] != "scored":
            print(f"{row['slug']:34}{'':11}{'':5}{'':5}  {row['outcome']}"
                  f"  {row.get('detail', '')[:40]}")
            continue
        for k, v in row["checks"].items():
            if v is None:
                continue
            tally.setdefault(k, [0, 0])
            tally[k][1] += 1
            tally[k][0] += bool(v)
        failed = [k for k, v in row["checks"].items() if v is False]
        mark = "ok" if not failed else "FAIL " + ",".join(failed)
        print(f"{row['slug']:34}{row['split']:11}{row['workspaces']:>5}"
              f"{row['deployable']:>5}  {row['passed']}/{row['graded']}     {mark}")
        if failed:
            print(f"{'':34}  deployable={row['paths']}")

    print("─" * 96)
    scored = [r for r in rows if r["outcome"] == "scored"]
    print(f"{len(scored)}/{len(rows)} repositories scored"
          + (f"   NOT FETCHED: {[r['slug'] for r in rows if r['outcome'] == 'not_fetched']}"
             if any(r["outcome"] == "not_fetched" for r in rows) else "")
          + (f"   CRASHES: {[r['slug'] for r in rows if r['outcome'] == 'crash']}"
             if any(r["outcome"] == "crash" for r in rows) else ""))
    for k, (good, total) in sorted(tally.items()):
        pct = 100.0 * good / total if total else 0.0
        print(f"  {k:16} {good:2}/{total:<2}  {pct:5.1f}%")
    unl = sum(1 for r in scored for v in r["checks"].values() if v is None)
    print(f"  {'unlabelled':16} {unl} check(s) skipped, not counted as passes")

    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=2) + "\n")

    # A benchmark that measured nothing must not report success. On a machine
    # without the pinned corpus every repository comes back `not_fetched`,
    # and this exited 0 -- so a release gate recorded a PASS for a suite that
    # scored 0/17. That is the same vacuous-pass failure this project rejects
    # everywhere else.
    missing = [r["slug"] for r in rows if r["outcome"] == "not_fetched"]
    crashed = [r["slug"] for r in rows if r["outcome"] == "crash"]
    if not scored:
        print(f"\n  ! nothing was scored; {len(missing)} repository(ies) are "
              f"not present. Fetch the corpus with `--clone` before treating "
              f"this as a measurement.")
        return 2
    if missing:
        print(f"\n  ! {len(missing)} repository(ies) not fetched and therefore "
              f"not measured; this run covers {len(scored)}/{len(rows)}")
        return 2
    if crashed:
        return 1
    failed = [r for r in scored
              if any(v is False for v in r["checks"].values())]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
