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

---

# Benchmark: 18 real repositories

`examples/` only proves the planner does what I think it does on repos I wrote.
`bench/bench.py` runs the same analysis against upstream code nobody shaped for
it — Spring Petclinic, Flask, Express, gin, axum, ripgrep, Laravel, Nest,
Rich, Requests, and Docker's sample apps.

```bash
python3 bench/bench.py --clone
python3 bench/bench.py --run --out /tmp/after.json
git worktree add /tmp/pre <pre-fix-sha>
CRUCIBLE_ROOT=/tmp/pre python3 bench/bench.py --run --out /tmp/before.json
python3 bench/bench.py --compare /tmp/before.json /tmp/after.json
```

| | before | after |
|---|---|---|
| archetype correct (unambiguous labels) | 5/12 | **11/12** |
| plans that contradict their own evidence | 8/18 | **0/18** |
| language correct | 14/16 | 14/16 |
| crashes | 0 | 0 |

`better 10, worse 0, unchanged 8`. The lint column is the one to trust: it
needs no ground truth, because "this plan cannot verify what it claims to
verify" is true or false regardless of what anyone thinks the repo is.

## What the benchmark found that I did not

Fixing the archetype fall-through created the mirror-image bug, and only real
repos exposed it. `express`, `gin`, `axum` and `rich` started planning as web
apps — a framework's own repository detects its own framework. Four
independent causes, each needing its own rule:

- **Provenance.** Evidence records the file behind every signal, and a name
  that only ever appeared in the merged corpus grep is a mention. Rich was
  planned as a Django app because a doc mentions Django.
- **Identity.** The repo *is* the framework — unless a root manifest also
  declares it as a dependency, which is exactly what separates
  `laravel/laravel` (an app that requires `laravel/framework`) from
  `pallets/flask` (whose only flask dependency lives under `examples/`).
- **Structure.** No `main.go`, no app. The go/rust branches already reasoned
  this way; the framework branch had been bypassing them.
- **Monorepo roots.** A workspace root with nothing to start is not a service.

Three more real bugs surfaced on the way:

- **A devcontainer could never produce a run command.** `_from_devcontainer`
  set base, env, steps, ports and archetype but never `run`, so any repo with a
  `.devcontainer/` and a `postCreateCommand` planned as a web app with nothing
  to launch. It describes a place to develop, not a service.
- **`_parse_go` scanned the `module` line**, so `module github.com/gin-gonic/gin`
  read as a dependency on gin.
- **`_parse_python` grepped instead of parsing.** Flask's own pyproject says
  "flask" four times — name, trove classifier, console script, coverage source
  — and depends on it zero times. Now parsed with `tomllib`.
- **Nested `package.json` scripts were merged into the root**, so
  `@nestjs/core` appeared to have a start script belonging to a sample project.

The linter and the planner now share one predicate (`_framework_implies_app`).
A linter that asks a different question than the planner answers reports a
contradiction every time the planner is correctly cautious.

## Known misses, unfixed

- **ripgrep** → `library`, should be `cli`. Cargo workspace whose binary lives
  in a member crate; the root has no `src/main.rs`. This is the monorepo
  limitation the README already documents, not a new one.
- **fastapi-template** → `node`, should be `python`. The frontend outweighs the
  backend by file count. Same limitation.
- **micronaut-examples** → no language. A repo of independent example projects
  with nothing at the root.

All three are the same underlying gap: per-workspace planning. None produce a
plan that contradicts itself — they produce an honest plan for the wrong
subtree.

---

# Session 2 — executing it

The note above says "**Execution does not [work]**… you need a Linux host". That
is true of Darwin and only of Darwin. It is a statement about the substrate, not
about the code, and it is cheap to fix: `brew install lima` and an Ubuntu 24.04
arm64 VM on Apple's Virtualization.framework. The VM config lives in
`.lima/crucible.yaml`.

```bash
limactl start --tty=false --name=crucible .lima/crucible.yaml
limactl shell crucible -- bash -lc '
  rsync -a --exclude .venv --exclude __pycache__ ~/Projects/crucible/ ~/crucible/
  cd ~/crucible && sudo python3 -m crucible.cli examples/py-fastapi'
```

Everything below was found by running it there. All seven are in the README's
"bugs only running it could have found" genre; none is a typo.

## 1. `oci.py` pulled the wrong architecture, then hid it

`pull_rootfs(ref, dest, arch="amd64")` — and no caller ever passed `arch`. On
this arm64 host the pull succeeded, the extract succeeded, and the first exec
died with `chroot: failed to run command '/bin/sh': Exec format error`, which
names neither the image nor the architecture.

The multi-arch selector made it worse: when no manifest matched it fell back to
`manifests[0]` rather than failing, converting "this image is not published for
your platform" into a silent wrong answer.

Fixed: `host_arch()` maps uname spellings to OCI ones, selection is
variant-aware, a miss raises and lists the platforms that *are* published, and
`cache_dir_for()` puts the architecture in the image cache key — a cache that
ignores it will hand an amd64 tree to an arm64 kernel and call it a hit.
`CRUCIBLE_ARCH` still allows a deliberate cross-pull.

## 2. A failed `apt-get` was invisible to the repair loop

`_install_system` threw its result away. Worse, `exec()` computes
`ok = (code == 0 and not timed_out) or step.allow_fail`, so the flag that makes
the step non-fatal also makes it *unreadable* — `res.ok` is True by
construction. Checking `res.code` is the only way to see the truth.

The visible symptom was a loop that diagnosed correctly and then gave up:

```
attempt 1  → [rule p=0.90] `pip` not on PATH -> apt python3-pip
attempt 2  → ✗ unrepairable failure in `install/pip` -- no patch available
```

The rule was right. The remedy failed and said nothing, so the next attempt met
the identical error, found no new rule, and blamed the repo. **A repair loop
that cannot observe its own remedy fail will misattribute every time.**

## 3. The sandbox had no resolver at all on `--base host`

With the apt failure visible, the cause showed up immediately:
`Temporary failure resolving 'ports.ubuntu.com'`.

On a systemd host `/etc/resolv.conf` is a symlink into `/run` — and `/run` is a
separate mount, so it is *not* part of an overlay whose lower is `/`. Inside the
sandbox that symlink dangles. `Path.write_text()` follows it to a directory that
does not exist, raises `OSError`, and lands in a bare `except OSError: pass`.

Two silences stacked: the symlink deref failed, and the handler swallowed it.
Fixed by unlinking whatever is there and writing a real file, and by logging the
failure instead of passing.

## 4. `--base` was silently revoked by the plan cache

The CLI implemented `--base` by monkeypatching `planner.plan`. `_seed_plan`
returns a cached plan *before* the planner is ever called, so on a cache hit the
user's explicit override vanished — `--base host` ran on `python:3.12-slim` and
nothing said why. Moved to `Engine.base_override`, applied to whichever plan was
seeded. An override a cache can quietly revoke is worse than no override.

## 5. New rule: distro-owned packages (34 rules, not 29)

`Cannot uninstall typing_extensions … RECORD file not found` — apt-installed
modules carry no RECORD, so pip can see them and refuses to replace them. This
is the *second* wall on `--base host`, reachable only after PEP 668 is cleared,
so the loop previously stalled one step past its own success. Sets
`PIP_IGNORE_INSTALLED`, the same env-var mechanism the PEP 668 rule uses.

The full chain now runs to green with no LLM:

```
attempt 1  install/pip  → [rule] `pip` not on PATH -> apt python3-pip
attempt 2  install/pip  → [rule] PEP 668 externally-managed env
attempt 3  install/pip  → [rule] `typing_extensions` distro-owned -> --ignore-installed
attempt 4  ✓ port 8000 answered                                    [22.7s]
```

## 6. The layer cache could return the wrong filesystem

The worst of the seven, because its failure mode is a *pass*.

The chain key was `base + system_packages + step.key()`, and `Step.key()` is
`sha256(cmd | cwd | env)`. `pip install --no-cache-dir -r requirements.txt`
under `python:3.12-slim` is byte-identical in every python repo alive. Layers
are shared across runs and across repos deliberately — that is the "shape
transfer" feature — so the collision is the *normal* case, not an exotic one.

Observed: a brand-new repo reported `⤳ install/pip (snapshot hit, skipped)` for
a `requirements.txt` the system had never read, and ran against a different
repo's site-packages. Here it surfaced as a confusing `ImportError`; had the
first repo's dependencies been a superset of the second's it would have gone
green and emitted a Dockerfile that never installs what the repo declares.

The README already caught the neighbouring case — "`pip install -r
requirements.txt` under `python:3.11` and under `node:22` are different layers
that happen to share a command string". This is the same argument one step
further in: the same command against a different manifest is a different layer
too.

`manifest_digest()` hashes every dependency manifest in the repo (28 filenames,
depth 3, vendor dirs skipped) into the chain seed. Identical manifests still
share layers — that is the feature — and source edits still do not bust the
install layer. `tests/test_cache_key.py` pins all three properties.

## 7. The emitted `compose.yml` could not connect

The pod model is the reason `DATABASE_URL` says `127.0.0.1:5432`: app and
sidecars share one network namespace. Compose gives every service its own and
resolves peers by name. `to_compose` copied the verified env verbatim, so the
generated file declared a postgres it could never reach — a bad failure for an
artifact whose whole claim is that it was validated by execution.
`compose_host_rewrite()` translates sidecar `host:port` to `service:port` and
leaves the app's own port alone.

## Footgun, documented rather than fixed

`--emit` into the analyzed repo makes the *next* run read CRUCIBLE's own output
as author intent — the planner prefers a declared Dockerfile, so a repair gets
frozen in permanently and re-inference never happens again. Emit to a separate
directory, or pass `--prefer infer`. This bit during this session and the run it
produced was discarded.

## Verified on this machine

Ubuntu 24.04 aarch64, kernel 6.8, cgroup v2, Lima/vz on an M-series Mac.

```
✓ daemonless OCI pull          python:3.12-slim, postgres:16-alpine, redis:7-alpine (arm64)
✓ pod netns + loopback up      net:[4026532420], egress cut, 127.0.0.1 live
✓ postgres sidecar             real initdb, ready on 127.0.0.1:5432
✓ redis sidecar                ready on 127.0.0.1:6379
✓ two sidecars in one pod      both ready before the app starts
✓ oracle                       port 8000 answered, network CUT
✓ deterministic repair chain   3 rules -> green, no LLM
✓ plan cache warm start        35.7s -> 10.1s
✓ cross-run snapshot reuse     install/pip restored from disk
✓ egress ledger                pypi.org + files.pythonhosted.org + peer IPs
✓ 27 unit tests
```
