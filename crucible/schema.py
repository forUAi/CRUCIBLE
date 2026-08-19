"""
CRUCIBLE :: schema.py

The three nouns of the system:

    Evidence   -- what we OBSERVED about the repo (deterministic, no guessing)
    RunPlan    -- what we BELIEVE will run it (a hypothesis, revisable)
    ExecResult -- what ACTUALLY happened (the ground truth that refutes plans)

The whole engine is a loop that mutates RunPlan until ExecResult stops
refuting it. Everything else is plumbing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

@dataclass
class Signal:
    """One observed fact about the repo. Signals are never inferred -- they
    are read off disk. Weight is how strongly it implies its `kind`."""
    kind: str            # "language" | "pkgmgr" | "service" | "runtime" | ...
    value: str           # "python" | "pnpm" | "postgres" | "3.11" | ...
    weight: float        # 0.0 - 1.0
    source: str          # relative path that produced it -- for provenance

    def __str__(self) -> str:
        return f"{self.kind}={self.value} ({self.weight:.2f} from {self.source})"


@dataclass
class Evidence:
    root: str
    files: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    entrypoints: list[dict] = field(default_factory=list)   # {path, kind, score}
    ports: list[int] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)       # from .env.example etc
    scripts: dict[str, str] = field(default_factory=dict)   # npm/make/just targets
    workspaces: list[str] = field(default_factory=list)     # monorepo members
    native_hints: list[str] = field(default_factory=list)   # needs gcc, libpq, etc
    declared: dict[str, str] = field(default_factory=dict)  # Dockerfile/compose/procfile paths

    # ---- reducers ----

    def top(self, kind: str) -> Optional[str]:
        vals = self.tally(kind)
        return vals[0][0] if vals else None

    def tally(self, kind: str) -> list[tuple[str, float]]:
        acc: dict[str, float] = {}
        for s in self.signals:
            if s.kind == kind:
                # saturating accumulation: many weak signals never beat one strong
                acc[s.value] = 1.0 - (1.0 - acc.get(s.value, 0.0)) * (1.0 - s.weight)
        return sorted(acc.items(), key=lambda kv: -kv[1])

    def has(self, kind: str, value: str) -> bool:
        return any(s.kind == kind and s.value == value for s in self.signals)

    def why(self, kind: str, value: str) -> list[str]:
        return [s.source for s in self.signals if s.kind == kind and s.value == value]

    def fingerprint(self) -> str:
        """Stable hash of the repo's SHAPE, not its contents.

        This is the cache key that lets a plan learned on one repo transfer to
        a structurally identical one. Two different Flask apps with the same
        dependency shape hash identically -- so the second one starts from a
        plan that is already known to work.
        """
        shape = {
            "lang": self.tally("language")[:3],
            "pm": self.tally("pkgmgr")[:3],
            "rt": self.tally("runtime")[:2],
            "svc": sorted({s.value for s in self.signals if s.kind == "service"}),
            "native": sorted(set(self.native_hints)),
            "manifests": sorted(
                f for f in self.files
                if f.count("/") <= 1 and any(
                    f.endswith(m) for m in (
                        "package.json", "pyproject.toml", "requirements.txt", "go.mod",
                        "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "Makefile",
                        "composer.json", "mix.exs", "Dockerfile", "deno.json",
                    )
                )
            ),
        }
        blob = json.dumps(shape, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# RunPlan
# --------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    cmd: str
    network: bool = True          # build steps need it; run steps usually don't
    cwd: str = "."
    timeout: int = 900
    allow_fail: bool = False
    env: dict[str, str] = field(default_factory=dict)

    def key(self) -> str:
        """Content address of this step -- the snapshot cache key."""
        return hashlib.sha256(
            f"{self.cmd}|{self.cwd}|{sorted(self.env.items())}".encode()
        ).hexdigest()[:16]


@dataclass
class Service:
    """A sidecar the app needs (postgres, redis...). Booted before `run`."""
    name: str
    image: str
    ports: list[int] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    command: str = ""          # override; empty = use the image's own entrypoint
    ready_probe: str = ""


@dataclass
class RunPlan:
    archetype: str = "unknown"     # web | cli | library | worker | notebook | static
    base: str = "ubuntu:24.04"     # rootfs identity
    system_packages: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    run: str = ""
    ports: list[int] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    workdir: str = "."
    oracle: dict[str, Any] = field(default_factory=dict)
    provenance: list[str] = field(default_factory=list)
    generation: int = 0            # how many repairs deep we are

    def note(self, msg: str) -> None:
        self.provenance.append(msg)

    def fingerprint(self) -> str:
        core = {
            "base": self.base,
            "pkgs": sorted(self.system_packages),
            "steps": [s.cmd for s in self.steps],
            "run": self.run,
            "env": sorted(self.env.items()),
        }
        return hashlib.sha256(
            json.dumps(core, sort_keys=True).encode()
        ).hexdigest()[:16]

    def clone(self) -> "RunPlan":
        return RunPlan(**json.loads(json.dumps(asdict(self))) | {
            "steps": [Step(**asdict(s)) for s in self.steps],
            "services": [Service(**asdict(s)) for s in self.services],
        })

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# --------------------------------------------------------------------------
# ExecResult
# --------------------------------------------------------------------------

@dataclass
class ExecResult:
    ok: bool
    code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def log(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()

    def tail(self, n: int = 60) -> str:
        return "\n".join(self.log.splitlines()[-n:])
