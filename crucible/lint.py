"""
CRUCIBLE :: lint.py

A verifier for the plan, not for the run.

The thesis of this system is that verification is cheap and prediction is
expensive, so you are allowed to guess wrong. That holds for everything the
engine executes -- and held for nothing the planner emitted. The planner was
the one component whose output nobody checked, and it showed: a framework
whose FRAMEWORKS entry had no run hint silently lost its archetype and its
port, so Spring Boot, Rails, Laravel and Phoenix services were filed as
`library` and "verified" by running their own test suites. Green check, app
never started.

No sandbox was needed to catch that. The plan contradicts itself on its face:
it carries a `framework=spring-boot` signal that means "web" and an archetype
that says "library". This module reads plans and says where they disagree
with their own evidence.

Cost is a few hundred microseconds against 10-60 seconds for the attempt it
saves, so this runs before the first sandbox is ever built.

Severity means exactly one thing:

    error  the plan cannot verify what it claims to verify. Executing it
           produces a result that is not evidence either way -- the failure
           mode the oracle exists to prevent.
    warn   the plan will probably fail, or will succeed for a reason you
           did not intend. Worth printing, not worth blocking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import FRAMEWORKS
from .planner import SERVICE_PORTS
from .schema import Evidence, RunPlan

# Commands that prove a repo's tests pass, not that its app runs. If the
# archetype is anything but `library`, seeing one of these as `run` means the
# archetype and the run command were decided by different code paths.
_TEST_RUN = re.compile(
    r"\b(pytest|unittest|npm (?:run )?test|go test|cargo test|mix test|"
    r"rspec|rake test|phpunit|mvn\b.*\btest|gradle\b.*\btest)\b")

# Base image -> build tools it does NOT ship. Picking a base and then issuing
# a command that base has never contained is a self-inflicted `not found`.
_BASE_LACKS = {
    "eclipse-temurin": ["mvn", "gradle"],
    "openjdk": ["mvn", "gradle"],
    "python": ["node", "npm", "go", "java", "mvn"],
    "node": ["python", "pip", "go", "java", "mvn"],
    "golang": ["node", "npm", "java", "mvn"],
}


@dataclass
class Finding:
    severity: str        # "error" | "warn"
    code: str            # stable slug, greppable
    detail: str
    hint: str = ""

    def __str__(self) -> str:
        mark = "✗" if self.severity == "error" else "!"
        tail = f"  -> {self.hint}" if self.hint else ""
        return f"{mark} [{self.code}] {self.detail}{tail}"


def lint(plan: RunPlan, ev: Evidence | None = None) -> list[Finding]:
    """Check a plan against itself and against the evidence that produced it."""
    out: list[Finding] = []
    for check in (_c_archetype_lost, _c_web_without_port, _c_oracle_probes_sidecar,
                  _c_run_is_test, _c_no_run, _c_tool_absent_from_base,
                  _c_service_unwired, _c_port_collides_with_sidecar,
                  _c_monorepo_ignored):
        out.extend(check(plan, ev))
    return sorted(out, key=lambda f: (f.severity != "error", f.code))


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _web_frameworks(ev: Evidence | None) -> list[str]:
    if ev is None:
        return []
    return [v for v, w in ev.tally("framework")
            if w >= 0.6 and FRAMEWORKS.get(v, ("", 0, ""))[0] == "web"]


def _c_archetype_lost(plan, ev):
    """The bug this module exists for. Evidence says web, plan says otherwise."""
    fws = _web_frameworks(ev)
    if fws and plan.archetype != "web":
        return [Finding(
            "error", "archetype-lost",
            f"evidence names web framework(s) {', '.join(fws)} but archetype is "
            f"`{plan.archetype}` -- the oracle will be `{plan.oracle.get('kind')}`, "
            f"which passes without ever starting the server",
            f"archetype should be web on port "
            f"{FRAMEWORKS[fws[0]][1]}")]
    return []


def _c_web_without_port(plan, ev):
    if plan.archetype == "web" and not plan.ports:
        return [Finding("error", "web-without-port",
                        f"archetype is web but no app port survived, so the oracle "
                        f"is `{plan.oracle.get('kind')}` and cannot prove the server "
                        f"answers",
                        "declare EXPOSE, a PORT env var, or a framework default")]
    return []


def _c_oracle_probes_sidecar(plan, ev):
    """A verifier that probes the dependency is worse than no verifier."""
    bad = [p for p in plan.ports if p in SERVICE_PORTS]
    if not bad:
        return []
    probed = plan.oracle.get("port")
    sev = "error" if probed in bad else "warn"
    return [Finding(sev, "oracle-probes-sidecar",
                    f"port(s) {bad} belong to a dependency, not the app"
                    + (f"; the oracle probes {probed} and would report the sidecar's "
                       "health as the app's" if sev == "error" else ""),
                    "drop dependency ports before choosing the oracle target")]


def _c_run_is_test(plan, ev):
    if plan.archetype not in ("library", "unknown") and _TEST_RUN.search(plan.run or ""):
        return [Finding("error", "run-is-test",
                        f"archetype is `{plan.archetype}` but the run command "
                        f"(`{plan.run}`) runs the test suite, which proves the tests "
                        f"pass and nothing about the app",
                        "synthesize a start command for this framework")]
    return []


def _c_no_run(plan, ev):
    if not (plan.run or "").strip():
        return [Finding("error", "no-run-command",
                        "plan has no run command; there is nothing for the oracle "
                        "to verify")]
    return []


def _c_tool_absent_from_base(plan, ev):
    base = (plan.base or "").split(":")[0].split("/")[-1]
    lacks = _BASE_LACKS.get(base)
    if not lacks:
        return []
    out = []
    cmds = [(s.name, s.cmd) for s in plan.steps] + [("run", plan.run or "")]
    for name, cmd in cmds:
        head = cmd.strip().split()[0] if cmd.strip() else ""
        if head in lacks and head not in plan.system_packages:
            out.append(Finding(
                "warn", "tool-absent-from-base",
                f"step `{name}` starts with `{head}`, which {plan.base} does not ship",
                f"use the repo's wrapper (./{head}w) or add it to system_packages"))
    return out


def _c_service_unwired(plan, ev):
    """A booted sidecar nothing points at is a slower way to fail."""
    out = []
    blob = " ".join(list(plan.env.values()) + list(plan.env.keys())).lower()
    for svc in plan.services:
        port = str(svc.ports[0]) if svc.ports else ""
        if svc.name.lower() in blob or (port and port in blob):
            continue
        out.append(Finding("warn", "service-unwired",
                           f"sidecar `{svc.name}` is booted but no env var names it, "
                           f"so the app will use its own default host",
                           f"set a connection URL pointing at 127.0.0.1:{port}"))
    return out


def _c_port_collides_with_sidecar(plan, ev):
    taken = {p for s in plan.services for p in s.ports}
    hit = sorted(set(plan.ports) & taken)
    if hit:
        return [Finding("error", "port-collides-with-sidecar",
                        f"the app and a sidecar both claim {hit}; they share one "
                        f"network namespace, so one of them will fail to bind",
                        "shift the app port")]
    return []


def _c_monorepo_ignored(plan, ev):
    if ev is not None and ev.workspaces:
        n = len(ev.workspaces)
        return [Finding("warn", "monorepo-ignored",
                        f"{n} workspace member(s) detected ({', '.join(ev.workspaces[:3])}"
                        f"{'...' if n > 3 else ''}) but the plan targets the repo root only",
                        "plan per-member, or point --target at one member")]
    return []
