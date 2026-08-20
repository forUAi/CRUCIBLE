"""CRUCIBLE :: cli.py"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import lint as lint_mod
from . import materialize
from .engine import Engine
from .evidence import collect
from .planner import plan as make_plan


def _clone(url: str) -> str:
    d = tempfile.mkdtemp(prefix="crucible-src-")
    print(f"▸ clone {url}")
    r = subprocess.run(["git", "clone", "--depth", "1", url, d],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"clone failed: {r.stderr.strip()[:400]}")
    return d


def _llm_from_env():
    """Optional tier-2 repair. Absent key => deterministic rules only."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import urllib.request

    def call(system: str, user: str) -> str:
        body = json.dumps({
            "model": os.environ.get("CRUCIBLE_MODEL", "claude-sonnet-4-6"),
            "max_tokens": 1200, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"content-type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
        return "".join(b.get("text", "") for b in data.get("content", []))

    return call


def main(argv=None) -> int:
    # Line-buffer stdout. Piped output is block-buffered by default, so a run
    # that is killed -- OOM, timeout, SIGKILL -- loses its last buffer, and
    # stderr (unbuffered) surfaces ahead of the stdout that explains it. A
    # real ENOSPC crash showed its traceback above the evidence header that
    # preceded it by two minutes, which is not a diagnosable artifact.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        prog="crucible", description="Run any repo in a sandbox. Learn how, then write it down.")
    ap.add_argument("target", help="path to a repo, or a git URL")
    ap.add_argument("--budget", type=int, default=6, help="max repair attempts")
    ap.add_argument("--mem", type=int, default=2048, help="memory cap (MB)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stream every step's output; build steps are untrusted "
                         "code and their output is the only record of what ran")
    ap.add_argument("--step-timeout", type=int, default=None,
                    help="cap every step's wall clock (seconds); the plan's own "
                         "timeouts still apply when lower")
    ap.add_argument("--disk-mb", type=int, default=4096,
                    help="per-sandbox writable storage budget in MB (0 = none)")
    ap.add_argument("--store-mb", type=int, default=None,
                    help="layer store size in MB (default: half the free disk)")
    ap.add_argument("--base", default=None, help="override base image ('host' = host rootfs)")
    ap.add_argument("--prefer", choices=["auto", "declared", "infer"], default="auto")
    ap.add_argument("--online-run", action="store_true",
                    help="leave network up during run (default: cut it)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="deterministic repairs only")
    ap.add_argument("--plan-only", action="store_true", help="analyze and print, do not execute")
    ap.add_argument("--lint-strict", action="store_true",
                    help="exit non-zero if the plan contradicts its own evidence")
    ap.add_argument("--emit", metavar="DIR", help="write Dockerfile/compose/lock here")
    a = ap.parse_args(argv)

    repo = _clone(a.target) if a.target.startswith(("http://", "https://", "git@")) else a.target
    if not Path(repo).is_dir():
        sys.exit(f"not a directory: {repo}")

    if a.plan_only:
        ev = collect(repo)
        p = make_plan(ev, prefer=a.prefer)
        if a.base:
            p.base = a.base
        print(f"\n\033[1mevidence shape\033[0m {ev.fingerprint()}   files={len(ev.files)}")
        for k in ("language", "pkgmgr", "framework", "runtime", "service"):
            t = ev.tally(k)
            if t:
                print(f"  {k:10} " + ", ".join(f"{v}={w:.2f}" for v, w in t[:4]))
        print(f"\n\033[1mplan\033[0m")
        for n in p.provenance:
            print(f"  · {n}")
        if p.status != "ok":
            print(f"\n  \033[33mstatus    {p.status.upper()}\033[0m")
        print(f"\n  base      {p.base}")
        print(f"  archetype {p.archetype}")
        print(f"  syspkgs   {', '.join(p.system_packages) or '—'}")
        for s in p.steps:
            print(f"  step      {s.name}: {s.cmd}")
        print(f"  run       {p.run}")
        print(f"  ports     {p.ports}")
        print(f"  oracle    {p.oracle}")
        for s in p.services:
            print(f"  sidecar   {s.name} -> {s.image}")
        bad = _print_lint(p, ev)
        if a.emit:
            _emit(a.emit, p, ev.fingerprint(), [], None)
        return 2 if (bad and a.lint_strict) else 0

    eng = Engine(budget=a.budget, mem_mb=a.mem, run_offline=not a.online_run,
                 use_cache=not a.no_cache, llm=None if a.no_llm else _llm_from_env(),
                 base_override=a.base, store_mb=a.store_mb,
                 step_timeout=a.step_timeout, verbose=a.verbose,
                 disk_mb=a.disk_mb)

    out = eng.run(repo, prefer=a.prefer)

    print("\n" + "─" * 68)
    state = ("SUCCESS" if out.ok else
             "EXHAUSTED" if getattr(out, "exhausted", False) else "FAILED")
    if out.plan.status != "ok":
        state += f" [{out.plan.status}]"
    print(f"\033[1m{state}\033[0m  "
          f"{out.detail}   [{out.elapsed}s, {len(out.attempts)} attempt(s), "
          f"{out.steps_skipped} step(s) from cache]")
    for at in out.attempts:
        mark = "✓" if not at.diagnosis else "⟳"
        print(f"  {mark} attempt {at.n} ({at.duration}s) "
              f"{at.failed_step or 'all steps green'}"
              + (f" -> [{at.patch_source}] {at.diagnosis}" if at.diagnosis else ""))
    if out.ledger is not None:
        print("\n\033[1megress ledger\033[0m")
        for line in out.ledger.report():
            print(f"  {line}")
    if a.emit:
        _emit(a.emit, out.plan, out.evidence_fp, out.attempts, out.ledger)
    return 0 if out.ok else 1


def _print_lint(plan, ev) -> int:
    """Check the plan against its own evidence before anything is executed."""
    findings = lint_mod.lint(plan, ev)
    print("\n\033[1mplan lint\033[0m")
    if not findings:
        print("  \033[32mno contradictions\033[0m")
        return 0
    for f in findings:
        print(f"  \033[{'31' if f.severity == 'error' else '33'}m{f}\033[0m")
    return len(lint_mod.errors(findings))


def _emit(dirname: str, plan, efp: str, attempts, ledger=None) -> None:
    d = Path(dirname)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text(materialize.to_dockerfile(plan))
    (d / "crucible.lock.json").write_text(
        materialize.to_lock(plan, efp, attempts, ledger))
    if plan.services:
        (d / "compose.yml").write_text(materialize.to_compose(plan))
    print(f"\n▸ emitted: {', '.join(sorted(p.name for p in d.iterdir()))}  -> {d}")


if __name__ == "__main__":
    sys.exit(main())
