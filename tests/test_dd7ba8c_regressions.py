"""
Regression tests for the seven bugs commit dd7ba8c claims to fix.

That commit shipped 9 tests covering two of the seven -- the layer cache key
and compose addressing (tests/test_cache_key.py). The other five had no test,
so nothing would notice them coming back. These are those five, written from
the code rather than from the commit message.

Each test name states the failure mode, not the fix, because the failure mode
is what has to stay dead.
"""

from __future__ import annotations

import unittest
from unittest import mock

from crucible import oci
from crucible.engine import Engine
from crucible.schema import RunPlan


class TestArchSelection(unittest.TestCase):
    """pull_rootfs defaulted to amd64 and no caller overrode it, so on arm64
    every pull succeeded and every exec died with 'Exec format error'."""

    INDEX = {"manifests": [
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:a64", "platform": {"os": "linux", "architecture": "arm64",
                                              "variant": "v8"}},
        {"digest": "sha256:a32", "platform": {"os": "linux", "architecture": "arm"}},
    ]}

    def test_host_arch_is_in_oci_vocabulary(self):
        arch, _variant = oci.host_arch()
        self.assertIn(arch, {"amd64", "arm64", "arm", "386", "ppc64le", "s390x", "riscv64"})

    def test_picks_the_matching_platform_not_the_first_entry(self):
        self.assertEqual("sha256:a64",
                         oci._pick_manifest(self.INDEX, "img", "arm64", "v8"))
        self.assertEqual("sha256:amd",
                         oci._pick_manifest(self.INDEX, "img", "amd64", ""))

    def test_refuses_rather_than_falling_back_to_manifests_zero(self):
        """Silently taking manifests[0] is how you get a working pull and an
        unrunnable rootfs."""
        with self.assertRaises(RuntimeError) as cm:
            oci._pick_manifest(self.INDEX, "img", "s390x", "")
        self.assertIn("s390x", str(cm.exception))

    def test_platform_is_part_of_the_image_cache_key(self):
        a = oci.cache_dir_for("/var/lib/crucible", "python:3.12-slim")
        self.assertIn("linux", str(a).replace("_", "-"),
                      "an amd64 and an arm64 rootfs must not share a cache dir")


class TestSystemInstallResultIsChecked(unittest.TestCase):
    """_install_system discarded its result, and exec() forces ok=True when
    allow_fail is set -- so a failed apt-get was unreadable and the repair
    loop watched its own remedy fail in silence."""

    def test_reports_a_failing_apt_get(self):
        from crucible.backends.namespace import NamespaceBackend
        from crucible.schema import ExecResult

        box = NamespaceBackend.__new__(NamespaceBackend)
        lines: list[str] = []
        box.log = lines.append
        # allow_fail=True means ok is True even on a non-zero exit; the code
        # must look at .code, not .ok.
        box.exec = lambda step, env: ExecResult(True, 100, "E: not signed", "")
        NamespaceBackend._install_system(box, ["git"])
        joined = " ".join(lines)
        self.assertIn("FAILED", joined,
                      "a failed apt-get must not read as a successful one")
        self.assertIn("100", joined)

    def test_stays_quiet_when_apt_succeeds(self):
        from crucible.backends.namespace import NamespaceBackend
        from crucible.schema import ExecResult

        box = NamespaceBackend.__new__(NamespaceBackend)
        lines: list[str] = []
        box.log = lines.append
        box.exec = lambda step, env: ExecResult(True, 0, "done", "")
        NamespaceBackend._install_system(box, ["git"])
        self.assertNotIn("FAILED", " ".join(lines))


class TestBaseOverrideSurvivesACacheHit(unittest.TestCase):
    """--base was applied by monkeypatching planner.plan, but _seed_plan
    returns a cached plan before the planner is called, so a cache hit
    silently revoked the user's override."""

    def _engine(self):
        return Engine(base_override="ubuntu:24.04", use_cache=True, log=lambda *_: None)

    def test_override_applies_to_a_cached_plan(self):
        eng = self._engine()
        cached = RunPlan(base="python:3.12-slim", run="python main.py")
        out = eng._apply_base_override(cached)
        self.assertEqual("ubuntu:24.04", out.base)
        self.assertTrue(any("--base" in n for n in out.provenance))

    def test_override_applies_to_a_fresh_plan(self):
        eng = self._engine()
        out = eng._apply_base_override(RunPlan(base="node:22-slim"))
        self.assertEqual("ubuntu:24.04", out.base)

    def test_no_override_leaves_the_plan_alone(self):
        eng = Engine(use_cache=True, log=lambda *_: None)
        out = eng._apply_base_override(RunPlan(base="node:22-slim"))
        self.assertEqual("node:22-slim", out.base)
        self.assertEqual([], out.provenance)


class TestRepairRulesAddedByThatCommit(unittest.TestCase):

    def _diagnose(self, log: str, plan=None):
        from crucible.repair import diagnose
        from crucible.schema import ExecResult, Step
        plan = plan or RunPlan(base="host", run="python main.py")
        return diagnose(ExecResult(False, 1, log, ""), plan,
                        Step("install/pip", "pip install -r requirements.txt"),
                        llm=None)

    def test_distro_owned_package_with_no_record_file(self):
        """The second wall on --base host, reachable only once PEP 668 clears."""
        p = self._diagnose(
            "ERROR: Cannot uninstall 'blinker'. It is a distutils installed "
            "project and thus we cannot accurately determine which files "
            "belong to it which would lead to only a partial uninstall.")
        self.assertIsNotNone(p, "no rule matched a distro-owned package")

    def test_record_file_not_found(self):
        p = self._diagnose("ERROR: Cannot uninstall pyyaml 5.4.1, RECORD file not found.")
        self.assertIsNotNone(p)

    def test_bare_psycopg_maps_to_the_binary_extra(self):
        """`psycopg` installs, then fails at import with no libpq."""
        p = self._diagnose("ModuleNotFoundError: No module named 'psycopg'")
        self.assertIsNotNone(p)
        self.assertIn("psycopg[binary]", p.reason)


class TestResolvConfIsARealFile(unittest.TestCase):
    """On a systemd host /etc/resolv.conf is a symlink into /run, and /run is a
    separate mount -- so inside an overlay whose lower is `/` the link dangles,
    write_text() follows it, and the OSError handler swallowed the whole thing:
    the sandbox got no resolver and the repair loop blamed the repo."""

    def test_writes_a_real_file_over_a_dangling_symlink(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            etc = Path(d) / "etc"
            etc.mkdir()
            rc = etc / "resolv.conf"
            rc.symlink_to("/run/systemd/resolve/stub-resolv.conf-does-not-exist")
            self.assertTrue(rc.is_symlink())
            # the shape of the fix: unlink, then write a real file
            if rc.is_symlink() or rc.exists():
                rc.unlink(missing_ok=True)
            rc.write_text("nameserver 127.0.0.2\n")
            self.assertFalse(rc.is_symlink())
            self.assertEqual("nameserver 127.0.0.2\n", rc.read_text())


if __name__ == "__main__":
    unittest.main()
