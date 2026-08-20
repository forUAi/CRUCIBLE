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
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.corpus import CORPUS, Repo                      # noqa: E402
from crucible.workspaces import discover                    # noqa: E402

CACHE = Path("/tmp/crucible-bench")


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
    a = ap.parse_args()

    splits = [s.strip() for s in a.split.split(",")]
    if "holdout" in splits and not a.unlock:
        sys.exit("refusing to run the holdout without --unlock; its value is "
                 "entirely in not having looked at it")

    repos = [r for r in CORPUS if r.split in splits]
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
