"""
CRUCIBLE :: engine.py

The loop.

    evidence -> plan -> for each step { snapshot-hit? skip : exec, snapshot }
             -> spawn run -> oracle -> success
                          \\-> failure -> diagnose -> patch -> rewind -> retry

Three properties make this affordable, and all three are easy to get wrong:

1. REWIND, DON'T REBUILD. On failure we restore to the last snapshot that
   succeeded, not to zero. A repair that adds `libpq-dev` re-runs only the
   step that needed it. Attempt N costs the delta, not the whole build.

2. CYCLE DETECTION VIA PLAN FINGERPRINT. Every patch must move the plan to a
   fingerprint we have not tried. Without this the loop happily oscillates
   between two wrong plans until the budget runs out, which is the classic
   failure mode of naive retry-with-LLM agents.

3. CACHE ON EVIDENCE SHAPE, NOT REPO IDENTITY. The cache key is the *shape*
   of the repo -- language, package managers, native deps, service topology.
   A plan repaired the hard way on one FastAPI+psycopg service is a warm
   start for the next structurally identical one, even a different repo.
   The system gets faster the more repos it sees.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import evidence as ev_mod
from . import planner as planner_mod
from . import repair as repair_mod
from .backends.namespace import NamespaceBackend
from .oracle import Verdict, verify
from .schema import Evidence, ExecResult, RunPlan, Step

CACHE = Path(os.environ.get("CRUCIBLE_CACHE", Path.home() / ".crucible" / "plans.json"))


@dataclass
class Attempt:
    n: int
    plan_fp: str
    failed_step: str = ""
    diagnosis: str = ""
    patch_source: str = ""
    duration: float = 0.0


@dataclass
class Outcome:
    ok: bool
    plan: RunPlan
    evidence_fp: str
    attempts: list[Attempt] = field(default_factory=list)
    verdict: Optional[Verdict] = None
    ledger: Optional[object] = None
    detail: str = ""
    elapsed: float = 0.0
    cache_hit: bool = False
    steps_skipped: int = 0


class Engine:
    def __init__(self, backend_cls=NamespaceBackend, budget: int = 6,
                 llm: Optional[Callable] = None, log=print, mem_mb: int = 2048,
                 run_offline: bool = True, use_cache: bool = True):
        self.backend_cls = backend_cls
        self.budget = budget
        self.llm = llm
        self.log = log
        self.mem_mb = mem_mb
        self.run_offline = run_offline
        self.use_cache = use_cache

    # ------------------------------------------------------------------

    def run(self, repo: str, prefer: str = "auto") -> Outcome:
        t0 = time.time()
        self.log(f"\n\033[1m▸ evidence\033[0m  {repo}")
        ev = ev_mod.collect(repo)
        efp = ev.fingerprint()
        self._describe(ev, efp)

        from .netlog import DnsLedger, Ledger
        dns = DnsLedger(log=self.log)
        dns.start()

        plan, cache_hit = self._seed_plan(ev, efp, prefer)
        box = self.backend_cls(f"box-{uuid.uuid4().hex[:8]}", log=self.log, mem_mb=self.mem_mb)
        box.dns = dns

        out = Outcome(False, plan, efp, cache_hit=cache_hit)
        tried: set[str] = set()
        diagnosed: set[str] = set()
        current_base = None

        try:
            for n in range(1, self.budget + 1):
                fp = plan.fingerprint()
                if fp in tried:
                    out.detail = "repair loop converged on an already-failed plan; stopping"
                    self.log(f"\033[33m  ! {out.detail}\033[0m")
                    break
                tried.add(fp)
                att = Attempt(n, fp)
                a0 = time.time()

                self.log(f"\n\033[1m▸ attempt {n}/{self.budget}\033[0m  "
                         f"plan={fp} base={plan.base}")

                if plan.base != current_base:
                    if current_base is not None:
                        self.log("  rebase -> rebuilding rootfs")
                        box.down()
                        box = self.backend_cls(f"box-{uuid.uuid4().hex[:8]}",
                                               log=self.log, mem_mb=self.mem_mb)
                        box.dns = dns
                    box.up(plan.base, repo, plan.system_packages)
                    current_base = plan.base
                elif plan.system_packages:
                    box._install_system(plan.system_packages)

                failed = self._run_steps(box, plan, out)

                if failed is not None:
                    step, res = failed
                    att.failed_step = step.name
                    patch = repair_mod.diagnose(res, plan, step, llm=self.llm, seen=diagnosed)
                    att.duration = round(time.time() - a0, 1)
                    if patch is None:
                        att.diagnosis = "no rule matched"
                        out.attempts.append(att)
                        out.detail = f"unrepairable failure in `{step.name}`"
                        self.log(f"\033[31m  ✗ {out.detail} -- no patch available\033[0m")
                        self.log("\033[2m" + res.tail(15) + "\033[0m")
                        break
                    diagnosed.add(patch.reason)
                    att.diagnosis, att.patch_source = patch.reason, patch.source
                    out.attempts.append(att)
                    self.log(f"\033[33m  ⟳ repair [{patch.source} p={patch.confidence:.2f}] "
                             f"{patch.reason}\033[0m")
                    patch.apply(plan)
                    plan.generation += 1
                    plan.note(f"gen{plan.generation}: {patch.reason}")
                    continue

                # --- steps all green: now prove the app actually runs ---
                verdict = self._verify_run(box, plan)
                att.duration = round(time.time() - a0, 1)
                out.verdict = verdict

                if verdict.ok:
                    out.attempts.append(att)
                    out.ok = True
                    out.detail = verdict.detail
                    self.log(f"\033[32m  ✓ {verdict.detail}\033[0m")
                    if self.use_cache:
                        self._cache_put(efp, plan)
                    break

                synthetic = ExecResult(False, 1, verdict.evidence, verdict.detail)
                patch = repair_mod.diagnose(
                    synthetic, plan, Step("run", plan.run), llm=self.llm, seen=diagnosed)
                att.failed_step = "run"
                if patch is None:
                    att.diagnosis = f"run failed: {verdict.detail}"
                    out.attempts.append(att)
                    out.detail = verdict.detail
                    self.log(f"\033[31m  ✗ {verdict.detail}\033[0m")
                    self.log("\033[2m" + verdict.evidence[-800:] + "\033[0m")
                    break
                diagnosed.add(patch.reason)
                att.diagnosis, att.patch_source = patch.reason, patch.source
                out.attempts.append(att)
                self.log(f"\033[33m  ⟳ repair [{patch.source}] {patch.reason}\033[0m")
                patch.apply(plan)
                plan.generation += 1
                plan.note(f"gen{plan.generation}: {patch.reason}")
        finally:
            box.down()
            dns.stop()

        out.ledger = Ledger(
            hostnames=dns.hostnames,
            peers=sorted(box.peers.keys()),
            resolved=dict(dns.resolved),
            runtime_egress_possible=not self.run_offline,
        )
        out.plan = plan
        out.elapsed = round(time.time() - t0, 1)
        return out

    # ------------------------------------------------------------------

    def _run_steps(self, box, plan: RunPlan, out: Outcome):
        import hashlib
        chain = hashlib.sha256(
            (plan.base + "|" + ",".join(sorted(plan.system_packages))).encode()
        ).hexdigest()
        for step in plan.steps:
            chain = hashlib.sha256((chain + "|" + step.key()).encode()).hexdigest()
            key = chain[:24]
            if box.supports_snapshots and box.has_snapshot(key):
                box.adopt(key)
                out.steps_skipped += 1
                self.log(f"  \033[2m⤳ {step.name}  (snapshot hit, skipped)\033[0m")
                continue
            self.log(f"  → {step.name}: \033[2m{step.cmd[:110]}\033[0m")
            res = box.exec(step, plan.env, stream=None)
            if not res.ok:
                self.log(f"    \033[31mfailed in {res.duration}s (exit {res.code})\033[0m")
                return step, res
            self.log(f"    \033[32mok\033[0m {res.duration}s")
            if box.supports_snapshots:
                box.snapshot(key)
        return None

    def _verify_run(self, box, plan: RunPlan) -> Verdict:
        if not plan.run:
            return Verdict(True, "no run command; build steps completed", "")

        # Sidecars come up first, in a shared network namespace the app will
        # join. Readiness is a real port probe, not a sleep -- otherwise the
        # app races the database and the failure gets misdiagnosed as the
        # app's fault, which sends the repair loop down the wrong path.
        pod = None
        if plan.services:
            from .pod import Pod
            pod = Pod(f"pod-{uuid.uuid4().hex[:8]}", log=self.log)
            pod.start()
            for svc in plan.services:
                rs = pod.launch(svc)
                if not pod.wait_ready(rs, timeout=90):
                    pod.stop()
                    return Verdict(False, f"sidecar `{svc.name}` failed to start",
                                   rs.detail)
            box.pod = pod
            plan.env.update(pod.env_for_app())

        try:
            return self._do_run(box, plan)
        finally:
            box.pod = None
            if pod is not None:
                pod.stop()

    def _do_run(self, box, plan: RunPlan) -> Verdict:
        # THE NETWORK SPLIT: build had egress, runtime does not. A repo that
        # needs the internet at *runtime* to start is either misconfigured or
        # interesting, and either way you want to know.
        net = not self.run_offline
        self.log(f"  ▸ run  \033[2m{plan.run[:110]}\033[0m  "
                 f"(network {'on' if net else 'CUT'})")
        step = Step("run", plan.run, network=net, cwd=plan.workdir,
                    timeout=plan.oracle.get("timeout", 900))
        proc = box.spawn(step, plan.env)
        buf: list[str] = []

        import threading

        def pump():
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    buf.append(line.rstrip())
                    if len(buf) > 3000:
                        del buf[:1500]
            except (ValueError, OSError):
                pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            v = verify(plan.oracle, proc, log_tail=lambda: "\n".join(buf[-40:]))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except Exception:
                    proc.kill()

        # A crashed process is diagnosable only if we actually have its output.
        # verify() can return the instant poll() goes non-None, which is often
        # before the reader thread has drained the pipe -- so the traceback we
        # most need would arrive after we had already given up on it. Join the
        # pump first, then build the evidence.
        t.join(timeout=5)
        tail = "\n".join(buf[-60:])
        v.evidence = (v.evidence + "\n" + tail).strip() if v.evidence else tail
        return v

    # ------------------------------------------------------------------

    def _seed_plan(self, ev: Evidence, efp: str, prefer: str) -> tuple[RunPlan, bool]:
        if self.use_cache:
            cached = self._cache_get(efp)
            if cached:
                self.log(f"  \033[36mplan cache HIT for shape {efp} "
                         f"(gen {cached.generation}) -- warm start\033[0m")
                return cached, True
        p = planner_mod.plan(ev, prefer=prefer)
        self.log(f"\n\033[1m▸ plan\033[0m")
        for note in p.provenance:
            self.log(f"  \033[2m· {note}\033[0m")
        return p, False

    def _cache_get(self, efp: str) -> Optional[RunPlan]:
        try:
            db = json.loads(CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        raw = db.get(efp)
        if not raw:
            return None
        try:
            steps = [Step(**s) for s in raw.pop("steps", [])]
            from .schema import Service
            svcs = [Service(**s) for s in raw.pop("services", [])]
            return RunPlan(**raw, steps=steps, services=svcs)
        except (TypeError, ValueError):
            return None

    def _cache_put(self, efp: str, plan: RunPlan) -> None:
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            db = {}
            if CACHE.exists():
                try:
                    db = json.loads(CACHE.read_text())
                except json.JSONDecodeError:
                    db = {}
            db[efp] = asdict(plan)
            CACHE.write_text(json.dumps(db, indent=1))
            self.log(f"  \033[36mplan cached under shape {efp}\033[0m")
        except OSError:
            pass

    def _describe(self, ev: Evidence, efp: str) -> None:
        langs = ", ".join(f"{k}({v:.2f})" for k, v in ev.tally("language")[:3]) or "—"
        pms = ", ".join(k for k, _ in ev.tally("pkgmgr")[:3]) or "—"
        fws = ", ".join(k for k, _ in ev.tally("framework")[:3]) or "—"
        svc = ", ".join(sorted({s.value for s in ev.signals if s.kind == "service"})) or "—"
        self.log(f"  files={len(ev.files)}  shape={efp}")
        self.log(f"  language:  {langs}")
        self.log(f"  pkgmgr:    {pms}")
        self.log(f"  framework: {fws}")
        self.log(f"  services:  {svc}")
        if ev.declared:
            self.log(f"  declared:  {', '.join(f'{k}={v}' for k, v in ev.declared.items())}")
        if ev.native_hints:
            self.log(f"  native:    {', '.join(ev.native_hints)}")
        if ev.entrypoints:
            self.log(f"  entry:     " + ", ".join(
                f"{e['path']}({e['score']})" for e in ev.entrypoints[:3]))
