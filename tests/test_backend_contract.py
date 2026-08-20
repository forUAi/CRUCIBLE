"""
The backend is the one component the planning tests never touch, and it is
where a structural mistake hides best.

Inserting a module-level `def` in the middle of the class body once swallowed
every method below it into that function -- `_umount`, `_mount`, `_pump`,
`exec`, `spawn` all silently left the class. It compiled, the 41 planning
tests passed, and the failure only appeared as an AttributeError in the
teardown path of a real Lima run, after a two-minute image pull.

These tests are cheap and structural. They run anywhere, including macOS,
because they never mount anything.
"""

from __future__ import annotations

import inspect
import unittest

from crucible.backends.base import SandboxBackend
from crucible.backends.namespace import (NamespaceBackend, default_store_mb,
                                         reap_abandoned)


class TestBackendContract(unittest.TestCase):

    def test_implements_every_interface_method(self):
        for name, _ in inspect.getmembers(SandboxBackend, inspect.isfunction):
            if name.startswith("__"):
                continue
            with self.subTest(method=name):
                self.assertTrue(hasattr(NamespaceBackend, name),
                                f"NamespaceBackend lost {name}()")
                self.assertTrue(callable(getattr(NamespaceBackend, name)))

    def test_internal_methods_are_on_the_class(self):
        """Everything the engine and exec path reach for."""
        for name in ("_mount", "_umount", "_fresh_live", "_lower_chain", "_pump",
                     "_ns_argv", "_cgroup_setup", "_cgroup_attach",
                     "_cgroup_teardown", "_install_system", "_ca_env", "_claim"):
            with self.subTest(method=name):
                self.assertTrue(hasattr(NamespaceBackend, name),
                                f"NamespaceBackend lost {name}()")

    def test_module_level_helpers_are_module_level(self):
        self.assertTrue(callable(reap_abandoned))
        self.assertFalse(hasattr(NamespaceBackend, "reap_abandoned"))

    def test_declared_capabilities(self):
        self.assertTrue(NamespaceBackend.supports_snapshots)
        self.assertEqual("namespace", NamespaceBackend.name)

    def test_store_sizing_is_bounded(self):
        mb = default_store_mb(floor=4096, ceiling=65536)
        self.assertGreaterEqual(mb, 4096)
        self.assertLessEqual(mb, 65536)
