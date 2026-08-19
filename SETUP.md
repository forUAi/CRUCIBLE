# Setup notes

The zip (`~/Downloads/files (2).zip`) was a flat drop of 13 files. This tree is
that drop rearranged into the package layout the code's own imports expect
(`crucible/`, `crucible/backends/`), plus the pieces the zip did not contain.

## What came from the zip

`README.md` and the 12 modules, unmodified — `schema.py`, `evidence.py`,
`planner.py`, `repair.py`, `oracle.py`, `engine.py`, `pod.py`, `netlog.py`,
`materialize.py`, `oci.py`, `cli.py`, and `backends/namespace.py`.

## What was added during setup

| File | Why |
|---|---|
| `crucible/backends/base.py` | **Missing from the zip.** `namespace.py:47` does `from .base import SandboxBackend` — nothing imports without it. Reconstructed from the call sites in `engine.py`/`namespace.py` and the README's description ("four verbs", `fork()` reserved for speculative plans). Interface only; no behavior. |
| `crucible/__init__.py`, `crucible/backends/__init__.py` | package markers |
| `crucible/__main__.py` | lets `python3 -m crucible` work alongside `python3 -m crucible.cli` |
| `pyproject.toml` | stdlib-only, `requires-python >=3.11`, `crucible` console script |
| `.gitignore` | |

`examples/` (8 synthetic repos, per the README's layout section) was not in the
zip and was not invented here.

## Running it

The system Python is 3.9; this needs 3.11+. A venv on Homebrew's 3.13 is at
`.venv/`:

```bash
/Users/vishalchandupatla/Projects/crucible/.venv/bin/crucible ./some-repo --plan-only
```

Or without the venv, from the project root:

```bash
/opt/homebrew/bin/python3.13 -m crucible.cli ./some-repo --plan-only
```

## What works on this Mac, and what doesn't

**Analysis works.** `--plan-only` and `--emit` are pure Python and were verified
against three repo shapes: pip/FastAPI (detected postgres sidecar, synthesized
env, emitted Dockerfile + compose.yml + lock), pnpm/express, and a Go repo whose
Dockerfile was interpreted rather than built.

**Execution does not.** `Engine.run()` needs Linux — overlayfs, mount/pid/net
namespaces, cgroups, and root. On macOS it gets as far as the plan and then dies
at the first overlay mount:

```
RuntimeError: rootfs overlay failed: usage: mount [-dfFrkuvw] ...
```

That's the documented substrate requirement, not a broken install. The DNS
ledger also can't bind `127.0.0.2:53` here and disables itself with a warning,
which is its intended degraded path.

To actually run the sandbox you need a Linux host (or VM/CI container) with root
or `CAP_SYS_ADMIN`. Nothing here is macOS-specific, so a Linux box only needs
Python 3.11+.
