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

---

# Changes after setup

## The bug

`FRAMEWORKS` (evidence.py) maps a framework to `(archetype, port, run hint)`.
The hint is a one-liner for Django/Flask/FastAPI/Streamlit and `""` for
everything whose start command isn't a one-liner. `_pick_run` read it as:

```python
if hint:
    return arch, port, hint
# ...falls through, discarding arch and port
```

An empty hint therefore threw away the archetype and port the framework had
just supplied, and execution reached the final `return "library", 0,
_test_cmd(...)`. Every **Spring Boot, Rails, Laravel and Phoenix** web service
was planned as:

```
archetype library    run  mvn -B test    ports []    oracle {'kind': 'exit0'}
```

The oracle passes when the tests exit 0. The server is never started, and the
emitted Dockerfile says `CMD ["sh","-c","mvn -B test"]`. This is the exact
failure the README calls "the worst possible failure mode for a verifier" —
just arriving through the archetype rather than through the port.

The same rule had a second hole: `SERVICE_PORTS` filtering lived inside
`_app_ports()`, which only the *inference* path calls. A Dockerfile with
`EXPOSE 6379` went straight through, so the oracle probed the redis sidecar and
reported it as the app's health.

## What changed

**`planner.py`** — an empty hint keeps the framework's archetype and port and
synthesizes a start command (`_synth_start`) for JVM, Ruby, PHP, Elixir, Node,
Python, Go and Rust. `_finish`/`_deconflict_ports` now applies the
dependency-port rule to *all three* plan sources; when no app port survives, the
oracle degrades to `alive` rather than probing a sidecar. Maven/Gradle wrapper
preference (`./mvnw`, `./gradlew`) — the JDK images ship no build tool, so the
plain command is `not found` on the base the planner just chose. Spring sidecars
are wired in through `SPRING_*` env vars.

**`evidence.py`** — `_parse_jvm`, the structured manifest parser the JVM was
missing (every other ecosystem had one). Reads `pom.xml` as XML: artifact,
version, `finalName`, JDK release, parent, dependencies, reactor modules.
Reads Gradle for the boot plugin, toolchain, `mainClass`. Reads
`application.properties`/`.yml` for `server.port`. JDBC URLs and Spring
property names now imply sidecars. `_flatten_yaml` turns nested Spring YAML
into dotted keys — a redis dependency written as nested YAML previously
produced no sidecar at all.

**`repair.py`** — `mvn`/`gradle` added to `CMD_TO_APT` (their absence made a
guaranteed failure unrepairable), plus rules for plain-jar `no main manifest
attribute`, missing main class, unconfigured Spring DataSource, Spring's own
port-in-use message, dependency resolution failure, and JVM heap exhaustion.

**`lint.py` (new)** — a verifier for the plan, not the run. The planner was the
one component whose output nothing checked, which is how this shipped. Nine
checks; `error` means the plan cannot verify what it claims to verify. Runs in
the engine before the first sandbox is built (microseconds against tens of
seconds) and in `--plan-only`; findings land in `crucible.lock.json`.
`--lint-strict` exits non-zero.

**`examples/` + `tests/`** — 10 synthetic repos and 18 stdlib-unittest tests.
The load-bearing one is `test_no_plan_lints_with_an_error`: no example may
produce a plan that contradicts its own evidence.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Before / after, same repo

```
archetype library                    →  archetype web
run       mvn -B test                →  run       java -jar target/orders-2.1.0.jar --server.port=9090
ports     []                         →  ports     [9090]
oracle    {'kind': 'exit0'}          →  oracle    {'kind': 'http', 'port': 9090}
step      mvn -B -q package          →  step      ./mvnw -B -q package
sidecars  none                       →  sidecar   postgres (from the JDBC url), wired via SPRING_DATASOURCE_URL
```

All of this is the analysis half, so it is verified on macOS. The execution
half is unchanged and still needs Linux.
