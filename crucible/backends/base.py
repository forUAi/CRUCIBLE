"""
CRUCIBLE :: backends/base.py

The substrate interface. Four verbs is the whole contract:

    up()        materialize a rootfs with the repo mounted at /workspace
    exec()      run one step to completion, capture the log
    snapshot()  freeze the current filesystem under a content-address
    down()      release everything

Everything the engine does is a sequence of those. `spawn()` (long-running
run command), `restore()`/`adopt()` (rewind, warm start) and `fork()`
(speculative branches) are refinements on top, and a backend that cannot
provide them says so with `supports_snapshots = False` rather than lying.

Why an interface at all, when there is exactly one real implementation:
namespace isolation is not a security boundary for hostile code (shared
kernel, full syscall surface). A gVisor or Firecracker backend is the answer
there, and it differs from NamespaceBackend only in how these four verbs are
implemented -- not in what the engine asks for. Keeping the seam explicit is
what makes that swap a file rather than a rewrite.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from ..schema import ExecResult, Step


class SandboxBackend:
    """Abstract substrate. Subclasses implement the four verbs.

    Attributes the engine reads directly:
        name                human-readable substrate name
        supports_snapshots  False => every attempt re-runs every step
        pod                 optional shared-netns Pod the run step joins
        dns                 optional DnsLedger the sandbox resolves through
        peers               {(ip, port): first_seen} observed egress
    """

    name: str = "abstract"
    supports_snapshots: bool = False

    def __init__(self, box_id: str, log: Callable = print, mem_mb: int = 2048,
                 cpu_pct: int = 100, pid_max: int = 512, store_mb: int = 4096):
        self.id = box_id
        self.log = log
        self.mem_mb, self.cpu_pct, self.pid_max = mem_mb, cpu_pct, pid_max
        self.store_mb = store_mb
        self.pod = None
        self.dns = None
        self.peers: dict = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def up(self, base: str, repo_path: str, system_packages: list[str]) -> None:
        """Build the rootfs from `base`, mount `repo_path` at /workspace
        copy-on-write, and install `system_packages` if any."""
        raise NotImplementedError

    def down(self) -> None:
        """Unmount and release. Must be idempotent -- the engine calls it in
        a `finally`, possibly after a failed `up()`."""
        raise NotImplementedError

    def destroy(self) -> None:
        """down() plus delete this box's private state. Layers are shared and
        deliberately survive."""
        self.down()

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def exec(self, step: Step, env: dict[str, str], stream=None) -> ExecResult:
        """Run one step to completion inside the sandbox.

        Never raises on a failing command -- a non-zero exit is data, and the
        repair loop is the consumer. Raise only when the sandbox itself is
        broken. `stream`, if given, is called with each output line as it
        arrives.
        """
        raise NotImplementedError

    def spawn(self, step: Step, env: dict[str, str]) -> subprocess.Popen:
        """Start a long-running process (the `run` command) without waiting.

        stdout/stderr merged onto a pipe; the oracle probes while it runs.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # snapshots -- optional, but the repair loop is only affordable with them
    # ------------------------------------------------------------------

    def snapshot(self, key: str) -> None:
        """Freeze the current filesystem delta under `key` (a chain hash)."""
        raise NotImplementedError

    def restore(self, key: Optional[str]) -> bool:
        """Rewind to the state just after `key`; None = back to the base."""
        return False

    def has_snapshot(self, key: str) -> bool:
        """True if `key` exists in the layer store, this run or a previous one."""
        return False

    def adopt(self, key: str) -> bool:
        """Attach an on-disk snapshot from a previous run to this box. This is
        what turns a plan-cache hit into an actual time saving."""
        return False

    def fork(self) -> "SandboxBackend":
        """Branch the current state into a second sandbox.

        Reserved for speculative parallel plans (roadmap): when evidence is
        ambiguous, run N candidate plans from a shared prefix and take the
        first that satisfies the oracle. Cheap for a snapshotting backend --
        the branches share every layer beneath the fork point.
        """
        raise NotImplementedError(f"{self.name} backend cannot fork")

    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id} snapshots={self.supports_snapshots}>"
