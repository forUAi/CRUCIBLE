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


class TestOwnershipBoundary(unittest.TestCase):
    """The cgroup is how a later run proves which processes were abandoned.

    It went missing once during an edit and `getattr(box, "cgroup", "")`
    turned that into silence: the pod's pause container joined nothing and
    orphaned to init. A missing boundary must fail loudly.
    """

    def test_backend_exposes_a_cgroup_name(self):
        box = NamespaceBackend.__new__(NamespaceBackend)
        box.id = "box-deadbeef"
        self.assertEqual("crucible-box-deadbeef", box.cgroup)

    def test_engine_refuses_a_backend_without_one(self):
        from crucible.engine import Engine

        class NoOwnership:
            supports_snapshots = True

        eng = Engine(log=lambda *_: None)
        with self.assertRaises(RuntimeError) as cm:
            eng._require_cgroup(NoOwnership())
        self.assertIn("cgroup", str(cm.exception))

    def test_pod_adopts_into_the_run_cgroup(self):
        from crucible.pod import Pod
        seen = []
        pod = Pod("pod-test", log=lambda *_: None, cgroup="crucible-box-test")
        import crucible.lifecycle as L
        real = L.cgroup_attach
        try:
            L.cgroup_attach = lambda name, pid: seen.append((name, pid)) or True
            pod._adopt(4242)
        finally:
            L.cgroup_attach = real
        self.assertEqual([("crucible-box-test", 4242)], seen)

    def test_pod_without_a_cgroup_adopts_nothing(self):
        from crucible.pod import Pod
        Pod("pod-test", log=lambda *_: None, cgroup="")._adopt(1)
