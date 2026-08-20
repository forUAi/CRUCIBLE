"""
Properties that must hold in the SHIPPED tree, not just in a checkout.

Twice now a fix was reported as applied and was not: a patch script printed
success while its file variable had already been reassigned, so `box.cgroup`
vanished and every sandbox ran untracked; and `bench.execbench.fetch` was
described in two commit messages while `TARGETS` still pointed at a developer
home. Both survived review because "the code says so" was checked by reading
the intent rather than the artifact.

These tests read the tree they are running inside. Under `release/verify.py`
that tree is the extracted artifact, so passing here means the capability is
in the thing that ships.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Paths that only exist on the machine that wrote them. A release that needs
# one of these is not a release.
DEVELOPER_PATHS = re.compile(
    r"""(~/audit|/home/[a-z][\w.-]*/(?!\.)|/Users/[\w.-]+/)""")

SCANNED = ["crucible", "bench", "security", "release"]


def _sources() -> list[Path]:
    out = []
    for d in SCANNED:
        base = ROOT / d
        if base.is_dir():
            out += [f for f in sorted(base.rglob("*.py"))
                    if "__pycache__" not in f.parts]
    return out


class TestNoDeveloperMachineState(unittest.TestCase):

    def test_no_source_file_hardcodes_a_home_directory(self):
        offenders = []
        for f in _sources():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if DEVELOPER_PATHS.search(code):
                    offenders.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
        self.assertEqual([], offenders,
                         "a release cannot depend on one machine's home:\n"
                         + "\n".join(offenders))


class TestExecbenchIsSelfSufficient(unittest.TestCase):
    """The execution benchmark must obtain its own targets."""

    def setUp(self):
        self.src = (ROOT / "bench" / "execbench.py").read_text()
        self.tree = ast.parse(self.src)

    def test_fetch_exists(self):
        names = {n.name for n in ast.walk(self.tree)
                 if isinstance(n, ast.FunctionDef)}
        self.assertIn("fetch", names,
                      "external targets must be fetched, not assumed present")

    def test_every_external_target_is_a_pinned_url(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from bench.execbench import ROOT as BROOT, TARGETS
        self.assertGreaterEqual(len(TARGETS), 4)
        for label, path, _expect in TARGETS:
            with self.subTest(target=label):
                if path.startswith(str(BROOT)):
                    continue                      # ships inside the artifact
                self.assertTrue(path.startswith("git:"),
                                f"{label} is neither shipped nor fetchable: {path}")
                url, _, sha = path[4:].partition("@")
                self.assertTrue(url.startswith("https://"), url)
                self.assertRegex(sha, r"^[0-9a-f]{40}$",
                                 f"{label} must pin a full commit sha; an "
                                 f"unpinned benchmark changes what it measures "
                                 f"whenever upstream moves")

    def test_a_fetch_failure_keeps_its_cause(self):
        """`harness_error` with no detail is what made three targets
        unexplainable across two release gates."""
        fn = next((n for n in ast.walk(self.tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "run_one"), None)
        self.assertIsNotNone(fn, "run_one must exist")
        body = ast.unparse(fn)
        self.assertIn("harness_error", body)
        # The real exception type has to reach the result. `harness_error`
        # with an empty detail is what left three targets unexplainable
        # across two release gates.
        self.assertIn("type(e).__name__", body)
        self.assertIn("detail=", body)


class TestOwnershipBoundaryShipped(unittest.TestCase):
    """The cgroup property went missing once and nothing noticed for hours."""

    def test_backend_declares_a_cgroup(self):
        src = (ROOT / "crucible" / "backends" / "namespace.py").read_text()
        self.assertIn("def cgroup", src)

    def test_the_child_joins_the_cgroup_before_exec(self):
        """Attaching after Popen is too late: unshare --fork has already
        forked into the root cgroup, and every limit is decorative."""
        src = (ROOT / "crucible" / "backends" / "namespace.py").read_text()
        self.assertIn("_join_cgroup", src)
        self.assertTrue(
            re.search(r"def _child_limits.*?_join_cgroup", src, re.S),
            "_child_limits must join the cgroup in the forked child")

    def test_store_teardown_ships(self):
        self.assertIn("def teardown", (ROOT / "release" / "verify.py").read_text())
        self.assertIn("orphaned_store_mounts",
                      (ROOT / "crucible" / "lifecycle.py").read_text())


if __name__ == "__main__":
    unittest.main()
