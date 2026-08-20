"""
CRUCIBLE :: bench/execbench.py

Execution benchmark. Runs inside the Lima guest.

`bench/bench.py` scores the analysis half -- does the plan make sense. This
scores the half that matters more: does the repository actually build, launch
and answer, from a fresh sandbox snapshot, repeatably.

Each repetition wipes the layer store first. Without that, run 2 is a cache
hit on run 1 and the repetitions are not repetitions -- the containment
harness learned this the embarrassing way when its second run silently
skipped the step it was supposed to be testing.

Outcomes are distinguished, never merged:

    verified      built, launched, the oracle was satisfied
    failed        CRUCIBLE could not make it run
    exhausted     the sandbox ran out of a resource; says nothing about the repo
    unsupported   needs credentials or a platform we do not have
    timeout       exceeded the wall clock

A skipped or unsupported repository is never counted as a success.

    sudo python3 bench/execbench.py --repeat 3 --out /tmp/exec.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# (label, path, expected outcome). Paths are guest-local on purpose: a repo on
# a host-backed mount gets staged by containment.py, which is correct but adds
# a copy this benchmark should not be measuring.
TARGETS = [
    ("python/fastapi", str(ROOT / "examples/py-fastapi"), "verified"),
    ("node/express", "~/audit/repos/nodeapp", "verified"),
    ("go/buildpack", "~/audit/repos/goapp", "verified"),
    ("java/spring-maven", "~/audit/repos/petclinic", "verified"),
]


# Every result carries the same keys, so a early-return path cannot crash the
# printer -- which it did, losing two good results with a KeyError.
BLANK = {"outcome": "", "seconds": 0.0, "rc": None, "attempts": 0,
         "detail": "", "run_cmd": "", "repairs": [], "status": "ok"}


def resolve(path: str) -> Path:
    """Expand ~ against the invoking user, not root.

    execbench runs under sudo, where Path.expanduser() resolves ~ to /root and
    every guest-local target silently vanished. All three non-example targets
    reported `failed` in 0.0s, which is also the wrong *word*: a path that
    does not exist is a harness error, not a repository that will not build.
    """
    import os
    import pwd
    if not path.startswith("~"):
        return Path(path)
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            # Ask the password database. Guessing /home/<user> is wrong here:
            # this guest's home is /home/vishalchandupatla.guest.
            return Path(pwd.getpwnam(user).pw_dir) / path.lstrip("~/")
        except KeyError:
            pass
    return Path(path).expanduser()


def run_one(path: str, budget: int, timeout: int) -> dict:
    target = resolve(path)
    if not target.is_dir():
        return dict(BLANK, outcome="harness_error",
                    detail=f"target does not exist: {target}")
    Path("/var/lib/crucible").mkdir(parents=True, exist_ok=True)
    subprocess.run("rm -rf /var/lib/crucible/layers /var/lib/crucible/plans",
                   shell=True, capture_output=True)
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, "-u", "-m", "crucible.cli", str(target),
             "--no-llm", "--budget", str(budget), "--no-cache",
             "--step-timeout", str(timeout)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout * 3)
        out = ANSI.sub("", r.stdout + r.stderr)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        return dict(BLANK, outcome="timeout", seconds=round(time.time() - t0, 1),
                    detail=f"exceeded {timeout * 3}s wall clock")

    elapsed = round(time.time() - t0, 1)
    summary = next((l for l in out.splitlines()
                    if l.startswith(("SUCCESS", "FAILED", "EXHAUSTED"))), "")
    outcome = ("verified" if summary.startswith("SUCCESS") else
               "exhausted" if summary.startswith("EXHAUSTED") else "failed")
    attempts = len(re.findall(r"^▸ attempt ", out, re.M))
    return {
        "outcome": outcome,
        "seconds": elapsed,
        "rc": rc,
        "attempts": attempts,
        "detail": summary[:120],
        "run_cmd": (re.search(r"▸ run\s+(.+?)\s+\(network", out) or [None, ""])[1],
        "repairs": re.findall(r"⟳ repair \[[^\]]+\] (.+)", out)[:4],
        "status": (re.search(r"^\s*status\s+(\w+)", out, re.M) or [None, "ok"])[1],
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="execbench")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--budget", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--out")
    ap.add_argument("--only")
    a = ap.parse_args()

    rows = []
    for label, path, expect in TARGETS:
        if a.only and a.only not in label:
            continue
        for i in range(a.repeat):
            res = run_one(path, a.budget, a.timeout)
            res.update(target=label, expect=expect, rep=i + 1)
            rows.append(res)
            mark = "✓" if res["outcome"] == expect else "✗"
            print(f"{mark} {label:22} rep{i + 1}  {res['outcome']:11} "
                  f"{res['seconds']:>6}s  {res['attempts']} attempt(s)  "
                  f"{res['run_cmd'][:44]}")
            for rp in res["repairs"]:
                print(f"      repair: {rp[:70]}")

    print("─" * 92)
    bad = [r for r in rows if r["outcome"] == "harness_error"]
    for r in bad:
        print(f"  !! harness error, not a result: {r['detail']}")
    ok = sum(1 for r in rows if r["outcome"] == r["expect"])
    by_target: dict[str, set] = {}
    for r in rows:
        by_target.setdefault(r["target"], set()).add(r["outcome"])
    flaky = [t for t, s in by_target.items() if len(s) > 1]
    print(f"{ok}/{len(rows)} runs matched expectation   "
          f"targets {len(by_target)}   nondeterministic: {flaky or 'none'}")
    for t, s in by_target.items():
        times = [r["seconds"] for r in rows if r["target"] == t]
        print(f"  {t:22} {sorted(s)}  times {times}")
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"rows": rows, "nondeterministic": flaky}, indent=2) + "\n")
    return 0 if ok == len(rows) and not flaky and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
