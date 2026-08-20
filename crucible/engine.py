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

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import evidence as ev_mod
from . import lifecycle
from . import lint as lint_mod
from . import planner as planner_mod
from . import repair as repair_mod
from .backends.namespace import STATE_ROOT, NamespaceBackend
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
    exhausted: bool = False     # ran out of a sandbox resource, not a repo defect


_MANIFESTS = (
    "requirements.txt", "requirements-dev.txt", "constraints.txt", "pyproject.toml",
    "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
    "Gemfile", "Gemfile.lock", "composer.json", "composer.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile",
    "mix.exs", "mix.lock", "Dockerfile",
)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist",
              "build", "__pycache__", ".tox", "vendor"}


def manifest_digest(repo, depth: int = 3) -> str:
    """Content hash of every dependency manifest in the repo.

    The layer chain was keyed on base + system packages + step *command*, and
    a command string does not identify what the command reads. `pip install
    -r requirements.txt` under `python:3.12-slim` is byte-identical in every
    python repo alive, so two repos of the same shape produced the same chain
    key and the second silently adopted the first's site-packages -- with the
    wrong dependency set installed and nothing anywhere saying so. Because
    layers are deliberately shared across runs and across repos, that
    collision is the *normal* case, not an exotic one, and it can return a
    green run whose emitted Dockerfile never installs what the repo declares.

    The README already caught the neighbouring case -- "`pip install -r
    requirements.txt` under python:3.11 and under node:22 are different
    layers that happen to share a command string". This is the same argument
    one step further in: the same command against a different manifest is a
    different layer too.

    Over-invalidating is the safe direction here. A missed cache hit costs
    seconds; a false hit costs a wrong answer that looks right.
    """
    root = Path(repo)
    h = hashlib.sha256()
    found: list[tuple[str, bytes]] = []

    def walk(d: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.is_dir() and not e.is_symlink():
                if e.name not in _SKIP_DIRS and not e.name.startswith("."):
                    walk(e, level + 1)
            elif e.name in _MANIFESTS:
                try:
                    found.append((str(e.relative_to(root)), e.read_bytes()))
                except OSError:
                    pass

    walk(root, 0)
    for rel, data in sorted(found):
        h.update(rel.encode())
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()[:16]


class Engine:
    def __init__(self, backend_cls=NamespaceBackend, budget: int = 6,
                 llm: Optional[Callable] = None, log=print, mem_mb: int = 2048,
                 run_offline: bool = True, use_cache: bool = True,
                 base_override: Optional[str] = None,
                 store_mb: Optional[int] = None,
                 step_timeout: Optional[int] = None,
                 verbose: bool = False, disk_mb: int = 4096,
                 policy=None):
        self.backend_cls = backend_cls
        self.budget = budget
        self.llm = llm
        self.log = log
        self.mem_mb = mem_mb
        self.run_offline = run_offline
        self.use_cache = use_cache
        self.base_override = base_override
        from .backends.namespace import default_store_mb
        self.store_mb = store_mb or default_store_mb()
        self.step_timeout = step_timeout
        self.verbose = verbose
        # A default budget, not an opt-in. An untrusted repository with no
        # ceiling can fill the store and take every other job with it.
        self.disk_mb = disk_mb
        from .netpolicy import DEFAULT
        self.policy = policy or DEFAULT

    # ------------------------------------------------------------------

    def _own(self, path: str) -> None:
        """Declare a directory as this run's, before anything is put in it."""
        rec, reg = getattr(self, "_record", None), getattr(self, "_registry", None)
        if rec is None or reg is None or path in rec.dirs:
            return
        rec.dirs.append(path)
        reg.write(rec)

    # 128+SIGXFSZ from a shell, or the raw signal from a direct exec.
    _FSIZE_CODES = {153, -25}
    _QUOTA_TEXT = ("Disk quota exceeded", "EDQUOT", "No space left on device",
                   "File size limit exceeded")

    def _hit_disk_budget(self, box, res) -> bool:
        b = getattr(box, "budget", None)
        if b is None or not b.enforced:
            return False
        if res.code in self._FSIZE_CODES:
            return True
        if any(s in res.log for s in self._QUOTA_TEXT):
            return True
        from .diskbudget import usage_mb
        used = usage_mb(b.project, STATE_ROOT)
        return used is not None and used >= b.limit_mb * 0.95

    def _require_cgroup(self, box) -> str:
        """A backend without an ownership boundary cannot be cleaned up after.

        This was `getattr(box, "cgroup", "")`, and when the property went
        missing during an edit the default silently turned ownership tracking
        off: the pod's pause container joined nothing, the crash suite found
        it orphaned, and the only symptom was a leak. A missing boundary is a
        defect, not a degraded mode.
        """
        cg = getattr(box, "cgroup", None)
        if not cg:
            if not getattr(box, "supports_snapshots", True):
                return ""            # a stub backend that owns nothing
            raise RuntimeError(
                f"{type(box).__name__} exposes no cgroup; processes it starts "
                f"could not be reclaimed after a crash")
        return cg

    def _lint(self, plan: RunPlan, ev: Evidence) -> None:
        """Check the plan against its own evidence before building a sandbox.

        Costs microseconds; the attempt it can save costs tens of seconds. An
        `error` here means the plan cannot verify what it claims to verify, so
        executing it would produce a result that is evidence of nothing.
        Recorded on the plan so it survives into crucible.lock.json.
        """
        findings = lint_mod.lint(plan, ev)
        if not findings:
            return
        self.log("\n\033[1m\u25b8 plan lint\033[0m")
        for f in findings:
            self.log(f"  \033[{'31' if f.severity == 'error' else '33'}m{f}\033[0m")
            plan.note(f"lint/{f.severity}: {f.code} -- {f.detail}")

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
        self._lint(plan, ev)

        # Cleanup after a crash cannot be the crashing process's job, so a
        # later run does it -- but only for runs it can prove are gone.
        from .backends.namespace import reap_abandoned
        try:
            lifecycle.reap(STATE_ROOT, self.log)
            reap_abandoned(self.log)
        except OSError as e:
            self.log(f"  ! reaper: {e}")
        box = self.backend_cls(f"box-{uuid.uuid4().hex[:8]}", log=self.log, mem_mb=self.mem_mb,
                               store_mb=self.store_mb,
                               disk_mb=self.disk_mb)
        box.policy = self.policy
        box.dns = dns

        # Declare ownership before creating anything. A record written after
        # the fact is a record the crash happens before.
        registry = lifecycle.Registry(STATE_ROOT)
        run_id = box.id
        record = registry.open(run_id, cgroup=self._require_cgroup(box))
        record.dirs = [str(box.dir), str(box.log_dir)]
        registry.write(record)
        # _verify_run creates the pod later; it must be able to declare that
        # directory as owned too. The pod dir was the one resource the crash
        # suite still found surviving: its mounts and its pause container were
        # reclaimed via the cgroup, but nothing knew the directory was ours.
        self._registry, self._record = registry, record

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
                        box.destroy()
                        box = self.backend_cls(f"box-{uuid.uuid4().hex[:8]}",
                                               log=self.log, mem_mb=self.mem_mb,
                                               store_mb=self.store_mb,
                                               disk_mb=self.disk_mb)
                        box.policy = self.policy
                        box.dns = dns
                    box.up(plan.base, repo, plan.system_packages)
                    current_base = plan.base
                elif plan.system_packages:
                    box._install_system(plan.system_packages)

                failed = self._run_steps(box, plan, out)

                if failed is not None:
                    step, res = failed
                    # Distinguish "this repository is broken" from "this
                    # sandbox ran out of its allowance". They are not the same
                    # result and must not be reported as the same one. A disk
                    # bomb is killed by SIGXFSZ or refused with EDQUOT, and
                    # either way the previous message was just "unrepairable
                    # failure", which tells an operator nothing actionable.
                    # A step that was refused egress and then failed did not
                    # fail for the reason its output suggests. Under hermetic,
                    # pip's "no matching distribution" is a symptom of the
                    # policy, and the repair loop was diagnosing it as a
                    # missing wheel and rebasing the image to chase it.
                    if getattr(box, "_denied", {}).get(step.name):
                        out.detail = (
                            f"step `{step.name}` failed after the "
                            f"`{self.policy.name}` policy denied it egress; "
                            f"this is the policy, not the repository. Use "
                            f"--network proxy or open, or pre-populate the "
                            f"layer cache")
                        att.failed_step = step.name
                        att.diagnosis = f"denied by network policy {self.policy.name}"
                        out.attempts.append(att)
                        self.log(f"\033[31m  ✗ {out.detail}\033[0m")
                        break
                    if self._hit_disk_budget(box, res):
                        b = box.budget
                        out.detail = (f"disk budget exceeded: the sandbox was "
                                      f"capped at {b.limit_mb} MB and the build "
                                      f"tried to write past it")
                        out.exhausted = True
                        att.failed_step = step.name
                        att.diagnosis = "disk budget exceeded"
                        out.attempts.append(att)
                        self.log(f"\033[31m  ✗ {out.detail}\033[0m")
                        break
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
        except OSError as e:
            # Resource exhaustion is a classified outcome, not a traceback.
            # ENOSPC in the layer store crashed the run with a raw stack after
            # a 76-second Maven build had already succeeded.
            import errno
            if e.errno not in (errno.ENOSPC, errno.EDQUOT, errno.EMFILE, errno.ENFILE):
                raise
            name = errno.errorcode.get(e.errno, str(e.errno))
            hint = ("--disk-mb (this sandbox's budget)" if e.errno == errno.EDQUOT
                    else "--store-mb (the shared layer store)")
            out.detail = (f"sandbox resource exhausted ({name}): {e}. "
                          f"Raise {hint}.")
            out.exhausted = True
            self.log(f"\033[31m  ✗ {out.detail}\033[0m")
        finally:
            box.destroy()
            dns.stop()
            # Belt and braces: the graceful teardown above should already have
            # released everything, so this normally kills zero processes. It
            # exists for the paths where it does not -- a sidecar that ignored
            # SIGKILL, a mount that was busy -- and it is exact, because
            # membership came from the kernel.
            left = lifecycle.cgroup_kill(record.cgroup) if record.cgroup else 0
            if left:
                self.log(f"  \033[33m! {left} process(es) survived teardown; "
                         f"cgroup.kill released them\033[0m")
            if record.cgroup:
                lifecycle.cgroup_remove(record.cgroup)
            b = getattr(box, "budget", None)
            if b is not None and b.enforced:
                from .diskbudget import release
                release(b.project, STATE_ROOT)
            registry.close(run_id)

        out.ledger = Ledger(
            hostnames=dns.hostnames,
            peers=sorted(box.peers.keys()),
            resolved=dict(dns.resolved),
            runtime_egress_possible=self.policy.allows("runtime"),
        )
        out.plan = plan
        out.elapsed = round(time.time() - t0, 1)
        return out

    # ------------------------------------------------------------------

    def _run_steps(self, box, plan: RunPlan, out: Outcome):
        chain = hashlib.sha256(
            (plan.base + "|" + ",".join(sorted(plan.system_packages))
             + "|" + manifest_digest(box.repo)).encode()
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
            if self.step_timeout:
                step.timeout = min(step.timeout, self.step_timeout)
            # A sandbox that discards the output of a *successful* build step
            # has no record of what that step did -- and a build step is
            # arbitrary code from an untrusted repository. On failure the tail
            # is printed; on success it went nowhere.
            sink = (lambda line, n=step.name: self.log(f"    \033[2m[{n}] {line}\033[0m")
                    ) if self.verbose else None
            res = box.exec(step, plan.env, stream=sink)
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
            pod = Pod(f"pod-{uuid.uuid4().hex[:8]}", log=self.log,
                      cgroup=self._require_cgroup(box))
            self._own(str(pod.dir))
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
        net = self.policy.allows("runtime")
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
                return self._apply_base_override(cached), True
        p = planner_mod.plan(ev, prefer=prefer)
        self.log(f"\n\033[1m▸ plan\033[0m")
        for note in p.provenance:
            self.log(f"  \033[2m· {note}\033[0m")
        return self._apply_base_override(p), False

    def _apply_base_override(self, p: RunPlan) -> RunPlan:
        """`--base` is an instruction, not a suggestion, so it is applied to
        whatever plan we seeded -- cached or freshly planned.

        It used to be wired up by monkeypatching `planner.plan` from the CLI,
        which meant a plan-cache hit (which returns before the planner is ever
        called) silently discarded it: the user asked for `--base host`, the
        run reported `base=python:3.12-slim`, and nothing said why. An
        override that a cache can quietly revoke is worse than no override.
        """
        if self.base_override and p.base != self.base_override:
            p.base = self.base_override
            p.note(f"base overridden by --base {self.base_override}")
            self.log(f"  \033[2m· base overridden by --base {self.base_override}\033[0m")
        return p

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
