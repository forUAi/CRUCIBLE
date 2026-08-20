"""
Workspace graph discovery.

Synthetic fixtures, so these run anywhere with no network. Each one encodes a
shape taken from a real repository, named in the docstring, because every
rule here exists because a real monorepo broke the previous rule.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crucible.workspaces import discover


def build(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="ws-"))
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d


def pkg(**kw) -> str:
    return json.dumps(kw)


class TestDeclarationBeatsGuessing(unittest.TestCase):

    def test_npm_workspace_members_are_discovered(self):
        g = discover(build({
            "package.json": pkg(name="root", private=True,
                                workspaces=["apps/*", "packages/*"]),
            "apps/web/package.json": pkg(name="web", scripts={"start": "node s.js"}),
            "apps/api/package.json": pkg(name="api", scripts={"start": "node a.js"}),
            "packages/ui/package.json": pkg(name="ui", main="src/index.ts"),
        }))
        root = g.by_path(".")
        self.assertTrue(root.is_root)
        self.assertEqual(["apps/api", "apps/web", "packages/ui"], root.members)
        self.assertEqual({"apps/web", "apps/api"},
                         {w.path for w in g.deployable()})
        self.assertEqual("library", g.by_path("packages/ui").role)

    def test_pnpm_negation_is_honoured(self):
        """turborepo's own workspace excludes `!packages/turbo`."""
        g = discover(build({
            "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n  - 'packages/*'\n  - '!packages/excluded'\n",
            "apps/web/package.json": pkg(name="web", scripts={"start": "x"}),
            "packages/kept/package.json": pkg(name="kept", main="i.js"),
            "packages/excluded/package.json": pkg(name="ex", scripts={"start": "x"}),
        }))
        root = g.by_path(".")
        self.assertIn("apps/web", root.members)
        self.assertIn("packages/kept", root.members)
        self.assertNotIn("packages/excluded", root.members,
                         "a member the author excluded must not be reported")

    POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0">'
           "<modelVersion>4.0.0</modelVersion>{parent}<groupId>g</groupId>"
           "<artifactId>{a}</artifactId><version>1</version>{extra}</project>")
    BOOT_PARENT = ("<parent><groupId>org.springframework.boot</groupId>"
                   "<artifactId>spring-boot-starter-parent</artifactId>"
                   "<version>3.3.2</version></parent>")
    BOOT_DEP = ("<dependencies><dependency>"
                "<groupId>org.springframework.boot</groupId>"
                "<artifactId>spring-boot-starter-web</artifactId>"
                "</dependency></dependencies>")

    def test_maven_reactor_modules(self):
        """A reactor root is a container; a service module is deployable and a
        plain library module is not. Both directions matter -- the planner
        adjudicates, so a bare pom with no application code stays a library."""
        g = discover(build({
            "pom.xml": self.POM.format(
                a="root", parent="",
                extra="<modules><module>svc</module><module>lib</module></modules>"),
            "svc/pom.xml": self.POM.format(a="svc", parent=self.BOOT_PARENT,
                                           extra=self.BOOT_DEP),
            "svc/src/main/java/App.java": "class App {}",
            "lib/pom.xml": self.POM.format(a="lib", parent="", extra=""),
        }))
        self.assertEqual(["lib", "svc"], sorted(g.by_path(".").members))
        self.assertEqual("workspace-root", g.by_path(".").role)
        deploy = {w.path for w in g.deployable()}
        self.assertIn("svc", deploy, "a Spring Boot module is deployable")
        self.assertNotIn("lib", deploy, "a bare pom with no app code is a library")
        self.assertIn("archetype", " ".join(g.by_path("lib").why()))

    def test_go_work_members(self):
        g = discover(build({
            "go.work": "go 1.22\n\nuse (\n    ./svc\n    ./lib\n)\n",
            "svc/go.mod": "module x/svc\n\ngo 1.22\n",
            "svc/main.go": "package main\n\nfunc main() {}\n",
            "lib/go.mod": "module x/lib\n\ngo 1.22\n",
            "lib/lib.go": "package lib\n",
        }))
        self.assertEqual({"svc", "lib"}, set(g.by_path(".").members))
        self.assertEqual(["svc"], [w.path for w in g.deployable()])


class TestRolesAreEvidenceBacked(unittest.TestCase):

    def test_a_root_with_members_is_a_container_not_a_service(self):
        """backstage's root runs `backstage-cli repo start` and deploys nothing."""
        g = discover(build({
            "package.json": pkg(name="root", private=True,
                                workspaces=["packages/*"],
                                scripts={"start": "backstage-cli repo start"}),
            "packages/app/package.json": pkg(name="app", scripts={"start": "x"}),
        }))
        root = g.by_path(".")
        self.assertEqual("workspace-root", root.role)
        self.assertFalse(root.runnable)
        self.assertEqual("ambiguous", root.status,
                         "a root that also has a start script is ambiguous, "
                         "not silently dropped")
        self.assertIn("member", root.rejected_because)

    def test_library_entry_point_outranks_a_dev_harness_script(self):
        """Every Backstage plugin has main + `backstage-cli package start`."""
        g = discover(build({
            "package.json": pkg(name="root", private=True, workspaces=["plugins/*"]),
            "plugins/cat/package.json": pkg(name="@x/cat", main="src/index.ts",
                                            scripts={"start": "cli package start"}),
        }))
        w = g.by_path("plugins/cat")
        self.assertEqual("library", w.role)
        self.assertIn("main", w.rejected_because)

    def test_bin_makes_it_a_cli(self):
        g = discover(build({"package.json": pkg(name="t", bin={"t": "./b.js"})}))
        self.assertEqual("cli", g.by_path(".").role)
        self.assertTrue(g.by_path(".").runnable)

    def test_examples_and_docs_are_never_deployable(self):
        g = discover(build({
            "package.json": pkg(name="root", private=True,
                                workspaces=["examples/*", "docs/*"]),
            "examples/demo/package.json": pkg(name="d", scripts={"start": "x"}),
            "docs/site/package.json": pkg(name="s", scripts={"start": "x"}),
        }))
        self.assertEqual([], [w.path for w in g.deployable()])
        self.assertEqual("example", g.by_path("examples/demo").role)
        self.assertEqual("docs", g.by_path("docs/site").role)

    def test_go_main_outside_root_and_cmd_is_a_helper(self):
        """grpc-go keeps `package main` in interop/ and benchmark/."""
        g = discover(build({
            "go.mod": "module x\n\ngo 1.22\n",
            "lib.go": "package x\n",
            "interop/server/server.go": "package main\n\nfunc main() {}\n",
            "benchmark/b/main.go": "package main\n\nfunc main() {}\n",
        }))
        w = g.by_path(".")
        self.assertEqual("library", w.role)
        self.assertFalse(w.runnable)

    def test_go_cmd_binary_is_deployable(self):
        g = discover(build({
            "go.mod": "module x\n\ngo 1.22\n",
            "cmd/tool/main.go": "package main\n\nfunc main() {}\n",
        }))
        self.assertEqual("cli", g.by_path(".").role)
        self.assertTrue(g.by_path(".").runnable)

    def test_nested_module_is_not_this_modules_entry_point(self):
        g = discover(build({
            "go.mod": "module x\n\ngo 1.22\n",
            "lib.go": "package x\n",
            "sub/go.mod": "module x/sub\n\ngo 1.22\n",
            "sub/main.go": "package main\n\nfunc main() {}\n",
        }))
        self.assertFalse(g.by_path(".").runnable)
        self.assertTrue(g.by_path("sub").runnable)


class TestProvenance(unittest.TestCase):

    def test_every_workspace_explains_itself(self):
        g = discover(build({
            "package.json": pkg(name="root", private=True, workspaces=["apps/*"]),
            "apps/web/package.json": pkg(name="w", scripts={"start": "node s.js"}),
            "apps/lib/package.json": pkg(name="l", main="i.js"),
        }))
        for w in g.workspaces:
            with self.subTest(ws=w.path):
                self.assertTrue(w.why(), f"{w.path} recorded no reasons")
                for r in w.why():
                    self.assertIn("[", r, "a reason must cite its source")
                if not w.runnable:
                    self.assertTrue(w.rejected_because,
                                    f"{w.path} was rejected without saying why")

    def test_dependency_edges_between_workspaces(self):
        g = discover(build({
            "package.json": pkg(name="root", private=True, workspaces=["*"]),
            "app/package.json": pkg(name="app", scripts={"start": "x"},
                                    dependencies={"ui": "workspace:*"}),
            "ui/package.json": pkg(name="ui", main="i.js"),
        }))
        self.assertIn("ui", g.by_path("app").depends_on)

    def test_declared_member_without_a_manifest_is_reported_ambiguous(self):
        d = build({"package.json": pkg(name="root", private=True,
                                       workspaces=["packages/*"])})
        (d / "packages/empty").mkdir(parents=True)
        g = discover(d)
        w = g.by_path("packages/empty")
        self.assertIsNotNone(w, "a declared member must appear in the graph")
        self.assertEqual("ambiguous", w.status)


class TestMixedLanguage(unittest.TestCase):

    def test_two_ecosystems_in_one_repository(self):
        """turborepo is a pnpm workspace with a Rust CLI beside it."""
        g = discover(build({
            "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
            "apps/web/package.json": pkg(name="web", scripts={"start": "x"}),
            "crates/cli/Cargo.toml": '[package]\nname = "cli"\nversion = "0.1.0"\n',
            "crates/cli/src/main.rs": "fn main() {}\n",
        }))
        self.assertEqual({"node", "rust"}, set(g.languages()))
        self.assertEqual({"apps/web", "crates/cli"},
                         {w.path for w in g.deployable()})

    def test_frontend_backend_pair(self):
        g = discover(build({
            "backend/pyproject.toml": '[project]\nname = "api"\ndependencies = ["fastapi"]\n',
            "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "frontend/package.json": pkg(name="fe", scripts={"start": "vite"}),
        }))
        paths = {w.path for w in g.deployable()}
        self.assertIn("frontend", paths)
        self.assertIn("backend", paths)


if __name__ == "__main__":
    unittest.main()
