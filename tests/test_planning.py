"""
Golden expectations for the analysis half: evidence -> plan -> lint.

Stdlib unittest, no network, no sandbox, so it runs anywhere the package
imports -- including macOS, where the execution half cannot run at all.

    python3 -m unittest discover -s tests -v

The load-bearing test is `test_no_plan_lints_with_an_error`. Every specific
expectation below is a fact about one ecosystem; that one is the invariant.
It exists because a real bug shipped here: FRAMEWORKS carries an empty run
hint for the frameworks whose start command isn't a one-liner, and an empty
hint used to mean "fall through", which discarded the archetype and port the
framework had just supplied. Spring Boot, Rails, Laravel and Phoenix web
services were all filed as `library` and verified by running their own test
suites -- a green check on a server that never started. Nothing about that
required a sandbox to detect; the plan disagreed with its own evidence.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from crucible.evidence import collect
from crucible.lint import errors, lint
from crucible.planner import plan as make_plan

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def analyze(name: str):
    ev = collect(str(EXAMPLES / name))
    return ev, make_plan(ev)


# name -> expectations. Only assert what the repo actually states; anything
# else is a guess being frozen into a test.
CASES = {
    "py-fastapi": dict(
        language="python", pkgmgr="pip", archetype="web", port=8000,
        oracle="http", run_has="uvicorn main:app", services={"postgres"}),
    "node-express": dict(
        language="node", pkgmgr="pnpm", archetype="web", port=3000,
        oracle="http", run_has="npm run start", services=set()),
    "go-gin": dict(
        language="go", pkgmgr="gomod", archetype="web", port=8080,
        oracle="http", run_has="/tmp/app", services=set()),
    "rust-lib": dict(
        language="rust", pkgmgr="cargo", archetype="library", port=None,
        oracle="exit0", run_has="cargo test", services=set()),
    "java-maven-spring": dict(
        language="java", pkgmgr="maven", archetype="web", port=9090,
        oracle="http", run_has="java -jar target/orders-2.1.0.jar",
        services={"postgres"}),
    "java-gradle-spring": dict(
        language="java", pkgmgr="gradle", archetype="web", port=8080,
        oracle="http", run_has="build/libs", services={"redis"}),
    "ruby-rails": dict(
        language="ruby", pkgmgr="bundler", archetype="web", port=3000,
        oracle="http", run_has="rails server", services=set()),
    "php-laravel": dict(
        language="php", pkgmgr="composer", archetype="web", port=8000,
        oracle="http", run_has="artisan serve", services=set()),
    "elixir-phoenix": dict(
        language="elixir", pkgmgr="mix", archetype="web", port=4000,
        oracle="http", run_has="mix phx.server", services=set()),
    "dockerfile-go": dict(
        language=None, pkgmgr=None, archetype="web", port=9090,
        oracle="http", run_has="/app", services=set()),
}


class TestPlanning(unittest.TestCase):

    def test_every_example_is_present(self):
        for name in CASES:
            self.assertTrue((EXAMPLES / name).is_dir(), f"missing example {name}")

    def test_expectations(self):
        for name, want in CASES.items():
            with self.subTest(example=name):
                ev, p = analyze(name)
                if want["language"]:
                    self.assertEqual(ev.top("language"), want["language"])
                if want["pkgmgr"]:
                    self.assertEqual(ev.top("pkgmgr"), want["pkgmgr"])
                self.assertEqual(p.archetype, want["archetype"])
                self.assertEqual(p.oracle.get("kind"), want["oracle"])
                self.assertIn(want["run_has"], p.run)
                if want["port"] is None:
                    self.assertEqual(p.ports, [])
                else:
                    self.assertIn(want["port"], p.ports)
                    self.assertEqual(p.oracle.get("port"), want["port"])
                self.assertEqual({s.name for s in p.services}, want["services"])

    # -- the invariant --------------------------------------------------

    def test_no_plan_lints_with_an_error(self):
        """No example may produce a plan that contradicts its own evidence."""
        for name in CASES:
            with self.subTest(example=name):
                ev, p = analyze(name)
                errs = errors(lint(p, ev))
                self.assertEqual([], errs, "\n".join(str(e) for e in errs))

    def test_a_web_framework_never_yields_a_library_archetype(self):
        """The specific regression: web evidence, non-web plan."""
        for name, want in CASES.items():
            if want["archetype"] != "web":
                continue
            with self.subTest(example=name):
                _, p = analyze(name)
                self.assertNotEqual("library", p.archetype)
                self.assertNotEqual("exit0", p.oracle.get("kind"),
                                    "an exit0 oracle on a web app passes without "
                                    "ever starting the server")

    def test_no_oracle_probes_a_dependency_port(self):
        """A verifier aimed at the sidecar reports the sidecar's health."""
        from crucible.planner import SERVICE_PORTS
        for name in CASES:
            with self.subTest(example=name):
                _, p = analyze(name)
                self.assertNotIn(p.oracle.get("port"), SERVICE_PORTS)


class TestJvmEvidence(unittest.TestCase):
    """The pom is XML with the answers in it; assert we read them, not grep them."""

    def setUp(self):
        self.ev, self.plan = analyze("java-maven-spring")

    def test_reads_artifact_coordinates(self):
        self.assertEqual("orders", self.ev.top("jvm.artifact"))
        self.assertEqual("2.1.0", self.ev.top("jvm.version"))

    def test_jdk_from_pom_properties(self):
        self.assertEqual("21", self.ev.top("runtime").split(":")[1])
        self.assertIn("21", self.plan.base)

    def test_author_stated_port_beats_convention(self):
        self.assertEqual("9090", self.ev.top("jvm.port"))
        self.assertEqual([9090], self.plan.ports)   # not spring's default 8080

    def test_jdbc_url_implies_a_sidecar(self):
        self.assertTrue(self.ev.has("service", "postgres"))

    def test_prefers_the_repos_own_wrapper(self):
        # eclipse-temurin ships a JDK and no build tool, so plain `mvn` is
        # `not found` on the base we just chose.
        self.assertTrue(self.ev.has("trait", "maven-wrapper"))
        for s in self.plan.steps:
            self.assertTrue(s.cmd.startswith("./mvnw"), s.cmd)

    def test_sidecar_is_wired_into_the_app(self):
        self.assertIn("SPRING_DATASOURCE_URL", self.plan.env)
        self.assertIn("127.0.0.1", self.plan.env["SPRING_DATASOURCE_URL"])


class TestYamlFlattening(unittest.TestCase):
    """Spring settings are equally valid dotted or nested; both must land."""

    def test_nesting_becomes_dotted_paths(self):
        from crucible.evidence import _flatten_yaml
        flat = _flatten_yaml(
            "spring:\n"
            "  data:\n"
            "    redis:\n"
            "      host: localhost\n"
            "      port: 6379\n"
            "server:\n"
            "  port: 9443\n")
        self.assertIn("spring.data.redis.host=localhost", flat)
        self.assertIn("spring.data.redis.port=6379", flat)
        self.assertIn("server.port=9443", flat)

    def test_dedents_back_out(self):
        from crucible.evidence import _flatten_yaml
        flat = _flatten_yaml("a:\n  b:\n    c: 1\nd: 2\n")
        self.assertIn("a.b.c=1", flat)
        self.assertIn("d=2", flat)
        self.assertNotIn("a.b.d", flat)

    def test_nested_yaml_yields_a_sidecar(self):
        ev, p = analyze("java-gradle-spring")
        self.assertTrue(ev.has("service", "redis"))
        self.assertIn("redis", {s.name for s in p.services})


class TestPortDeconfliction(unittest.TestCase):
    """`EXPOSE 6379` must not make the oracle probe the redis sidecar."""

    def _plan_from(self, tmp: Path):
        ev = collect(str(tmp))
        return ev, make_plan(ev)

    def test_declared_dependency_port_never_becomes_the_probe_target(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            (t / "Dockerfile").write_text(
                "FROM node:22-slim\nCOPY . .\nRUN npm ci\n"
                "EXPOSE 6379\nCMD [\"node\", \"server.js\"]\n")
            (t / "package.json").write_text('{"dependencies":{"ioredis":"^5"}}')
            (t / "server.js").write_text("require('ioredis');")
            _, p = self._plan_from(t)
            self.assertIn("redis", {s.name for s in p.services})
            self.assertNotEqual(6379, p.oracle.get("port"))
            self.assertNotIn(6379, p.ports)
            # Honest degradation, not a false pass on the sidecar's health.
            self.assertEqual("alive", p.oracle.get("kind"))

    def test_a_real_app_port_alongside_a_dependency_port_wins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            (t / "Dockerfile").write_text(
                "FROM node:22-slim\nCOPY . .\nRUN npm ci\n"
                "EXPOSE 3000\nEXPOSE 6379\nCMD [\"node\", \"server.js\"]\n")
            (t / "package.json").write_text('{"dependencies":{"ioredis":"^5"}}')
            (t / "server.js").write_text("require('ioredis');")
            _, p = self._plan_from(t)
            self.assertEqual([3000], p.ports)
            self.assertEqual(3000, p.oracle.get("port"))


class TestFrameworkSignalMeansAnApp(unittest.TestCase):
    """Detecting a framework name is not the same as running the framework.

    Every case here was found by the real-repo benchmark, not by reasoning:
    once an empty run hint stopped discarding the archetype, framework repos
    started planning as web apps built on themselves.
    """

    def _plan(self, files: dict[str, str]):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            for rel, body in files.items():
                (t / rel).parent.mkdir(parents=True, exist_ok=True)
                (t / rel).write_text(body)
            ev = collect(str(t))
            return ev, make_plan(ev)

    def test_a_frameworks_own_repo_is_a_library(self):
        """pallets/flask: name = "Flask", and no dependency on flask."""
        ev, p = self._plan({
            "pyproject.toml":
                '[project]\nname = "Flask"\nversion = "3.2.0"\n'
                'classifiers = ["Framework :: Flask"]\n'
                'dependencies = ["werkzeug>=3.0", "jinja2>=3.1"]\n'
                '\n[project.scripts]\nflask = "flask.cli:main"\n',
            "src/flask/__init__.py": "",
        })
        self.assertEqual("library", p.archetype)
        self.assertEqual("exit0", p.oracle.get("kind"))

    def test_an_app_named_after_its_framework_is_still_an_app(self):
        """laravel/laravel is called laravel AND requires laravel/framework."""
        ev, p = self._plan({
            "composer.json":
                '{"name":"laravel/laravel","require":{"laravel/framework":"^11.0"}}',
            "artisan": "<?php\n",
            "public/index.php": "<?php\n",
        })
        self.assertEqual("web", p.archetype)
        self.assertIn("artisan serve", p.run)

    def test_a_doc_mention_is_not_a_dependency(self):
        """Textualize/rich mentions Django in prose; it is not a Django app."""
        ev, p = self._plan({
            "pyproject.toml": '[project]\nname = "rich"\ndependencies = ["pygments"]\n',
            "README.md": "Works great with django and flask projects.\n",
            "rich/__init__.py": "# django integration notes\n",
        })
        self.assertEqual("library", p.archetype)

    def test_pyproject_self_references_are_not_dependencies(self):
        """The four ways flask's own pyproject says "flask" without depending on it."""
        ev, _ = self._plan({
            "pyproject.toml":
                '[project]\nname = "Flask"\n'
                'classifiers = ["Framework :: Flask"]\n'
                'dependencies = ["click"]\n'
                '\n[project.scripts]\nflask = "flask.cli:main"\n'
                '\n[tool.coverage.run]\nsource = ["flask", "tests"]\n',
        })
        # The corpus grep may still mention it -- that signal is deliberately
        # weak and `_framework_implies_app` rejects grep-only evidence. What
        # must not happen is the *manifest* claiming a dependency it declares
        # nowhere, because a manifest signal is the trusted kind.
        self.assertNotIn("pyproject.toml", ev.why("framework", "flask"),
                         "read the dependency table, do not grep the file")
        from crucible.planner import _framework_implies_app
        self.assertFalse(_framework_implies_app(ev, "flask", "python"))

    def test_compiled_language_needs_an_entrypoint(self):
        """gin-gonic/gin has no main.go, so it is the library, not a service."""
        ev, p = self._plan({
            "go.mod": "module github.com/gin-gonic/gin\n\ngo 1.22\n",
            "gin.go": "package gin\n",
        })
        self.assertEqual("library", p.archetype)

    def test_a_go_app_that_imports_gin_is_a_service(self):
        ev, p = self._plan({
            "go.mod": "module demo\n\ngo 1.22\n\n"
                      "require github.com/gin-gonic/gin v1.10.0\n",
            "main.go": "package main\n\nfunc main() {}\n",
        })
        self.assertEqual("web", p.archetype)
        self.assertIn(8080, p.ports)

    def test_nested_package_scripts_do_not_become_root_scripts(self):
        ev, _ = self._plan({
            "package.json": '{"name":"@acme/core","workspaces":["packages/*"]}',
            "packages/sample/package.json":
                '{"name":"sample","scripts":{"start":"node main.js"}}',
        })
        self.assertNotIn("start", ev.scripts)


class TestDevcontainerIsNotAPlan(unittest.TestCase):
    """A devcontainer describes a place to develop, not a service to run."""

    def test_devcontainer_repo_still_gets_a_run_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            (t / ".devcontainer").mkdir()
            (t / ".devcontainer/devcontainer.json").write_text(
                '{"image":"mcr.microsoft.com/devcontainers/python:3.12",'
                '"postCreateCommand":"pip install -e .","forwardPorts":[5000,8080]}')
            (t / "pyproject.toml").write_text(
                '[project]\nname = "widget"\ndependencies = ["click"]\n')
            (t / "widget").mkdir()
            (t / "widget/__init__.py").write_text("")
            ev = collect(str(t))
            p = make_plan(ev)
            self.assertTrue(p.run.strip(), "a plan with no run verifies nothing")
            self.assertEqual([], errors(lint(p, ev)))
            # the author's setup step survives
            self.assertTrue(any("pip install -e ." in s.cmd for s in p.steps))


class TestLintCatchesTheOriginalBug(unittest.TestCase):

    def test_archetype_lost_is_an_error(self):
        from crucible.schema import RunPlan, Step
        ev, _ = analyze("java-maven-spring")
        # Exactly what the planner emitted before the fix.
        old = RunPlan(archetype="library", base="eclipse-temurin:21-jdk",
                      run="mvn -B test", ports=[], oracle={"kind": "exit0"})
        old.steps = [Step("install/maven", "mvn -B -q dependency:go-offline")]
        codes = {f.code for f in lint(old, ev)}
        self.assertIn("archetype-lost", codes)
        self.assertIn("tool-absent-from-base", codes)

    def test_clean_plan_has_no_errors(self):
        ev, p = analyze("java-maven-spring")
        self.assertEqual([], errors(lint(p, ev)))


if __name__ == "__main__":
    unittest.main()


class TestJdkReleaseParsing(unittest.TestCase):
    """`lstrip` takes a character set, not a prefix. 17 -> 7, 11 -> ''."""

    def test_modern_and_legacy_versions(self):
        from crucible.evidence import _jdk_release
        for raw, want in (("17", "17"), ("11", "11"), ("21", "21"),
                          ("1.8", "8"), ("8", "8"), (" 17 ", "17")):
            with self.subTest(raw=raw):
                self.assertEqual(want, _jdk_release(raw))

    def test_pom_java_version_reaches_the_base_image(self):
        import tempfile
        for ver, want in (("17", "17"), ("11", "11"), ("1.8", "8")):
            with self.subTest(ver=ver), tempfile.TemporaryDirectory() as d:
                t = Path(d)
                (t / "pom.xml").write_text(
                    '<project xmlns="http://maven.apache.org/POM/4.0.0">'
                    '<modelVersion>4.0.0</modelVersion>'
                    '<groupId>x</groupId><artifactId>svc</artifactId>'
                    '<version>1.0</version>'
                    f'<properties><java.version>{ver}</java.version></properties>'
                    '</project>')
                (t / "App.java").write_text("class App {}")
                ev = collect(str(t))
                p = make_plan(ev)
                self.assertEqual(f"eclipse-temurin:{want}-jdk", p.base)


class TestStatedPortLeads(unittest.TestCase):
    """The oracle probes ports[0]; a grepped 80 must not outrank 8080."""

    def test_framework_port_outranks_an_incidental_low_port(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            (t / "src/main/resources").mkdir(parents=True)
            (t / "pom.xml").write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0">'
                '<modelVersion>4.0.0</modelVersion>'
                '<parent><groupId>org.springframework.boot</groupId>'
                '<artifactId>spring-boot-starter-parent</artifactId>'
                '<version>3.3.2</version></parent>'
                '<groupId>x</groupId><artifactId>svc</artifactId><version>1.0</version>'
                '<properties><java.version>17</java.version></properties>'
                '<dependencies><dependency>'
                '<groupId>org.springframework.boot</groupId>'
                '<artifactId>spring-boot-starter-web</artifactId>'
                '</dependency></dependencies></project>')
            # a docs URL containing :80, exactly how petclinic acquired it
            (t / "README.md").write_text("visit http://example.com:80/ for docs\n")
            (t / "src/main/resources/application.properties").write_text("")
            (t / "App.java").write_text("class App {}")
            ev = collect(str(t))
            p = make_plan(ev)
            self.assertEqual(8080, p.ports[0])
            self.assertEqual(8080, p.oracle.get("port"))
            self.assertEqual("8080", p.env.get("PORT"))


class TestImageEnvParsing(unittest.TestCase):
    """oci.pull_rootfs normalises the config blob; read the shape it writes."""

    def test_both_config_shapes(self):
        from crucible.backends.namespace import _env_from_config
        want = {"JAVA_HOME": "/opt/java/openjdk", "PATH": "/opt/java/openjdk/bin:/usr/bin"}
        normalised = {"entrypoint": [], "cmd": [], "env": dict(want), "ports": []}
        self.assertEqual(want, _env_from_config(normalised))
        raw_oci = {"config": {"Env": [f"{k}={v}" for k, v in want.items()]}}
        self.assertEqual(want, _env_from_config(raw_oci))
        self.assertEqual({}, _env_from_config({}))
