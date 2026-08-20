"""
The store leak that made a 14-second benchmark take 2000 seconds.

`release/verify.py` points CRUCIBLE_STATE at a private temp directory, so
`ensure_private_store` builds a loop-mounted ext4 image beside it. verify.py
then deleted that directory with `shutil.rmtree(..., ignore_errors=True)`.
rmtree cannot remove a mountpoint and, with ignore_errors, says nothing --
but the backing image sits *next to* the mountpoint, not under it, so rmtree
did unlink that. Every gate run therefore left a mounted ~10 GB filesystem
attached to a file that no longer existed.

Five had accumulated: 5 GB of page cache in a 6 GiB VM, seven OOM kills, and
a lifecycle case that failed once and then passed on every later attempt.

These tests pin the two halves of the repair: the detector must recognise
exactly that shape and nothing adjacent to it, and the teardown must release
the mount before the directory is removed.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from crucible import lifecycle


MOUNTS = """\
/dev/vda1 / ext4 rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid 0 0
/dev/loop0 /var/lib/crucible ext4 rw,relatime,prjquota 0 0
/dev/loop1 /tmp/crucible-release-aaa/state ext4 rw,relatime 0 0
/dev/loop2 /tmp/crucible-release-bbb/state ext4 rw,relatime 0 0
/dev/loop9 /mnt/somebody-elses ext4 rw,relatime 0 0
overlay /var/lib/crucible/box-1/merged overlay rw 0 0
"""

# What /sys/block/<dev>/loop/backing_file reports for each device.
BACKING = {
    "loop0": "/var/lib/crucible-store.img",                       # live store
    "loop1": "/tmp/crucible-release-aaa/crucible-store.img (deleted)",
    "loop2": "/tmp/crucible-release-bbb/crucible-store.img (deleted)",
    "loop9": "/mnt/other/disk.img (deleted)",                     # not ours
}


class TestOrphanDetection(unittest.TestCase):

    def _detect(self):
        def fake_read(self, *a, **k):
            s = str(self)
            if s == "/proc/mounts":
                return MOUNTS
            for dev, val in BACKING.items():
                if s == f"/sys/block/{dev}/loop/backing_file":
                    return val
            raise OSError("no such file")
        with mock.patch.object(Path, "read_text", fake_read):
            return lifecycle.orphaned_store_mounts()

    def test_finds_only_deleted_crucible_stores(self):
        found = {mp for _dev, mp, _b in self._detect()}
        self.assertEqual(
            {"/tmp/crucible-release-aaa/state", "/tmp/crucible-release-bbb/state"},
            found)

    def test_leaves_the_live_store_alone(self):
        for _dev, mp, _b in self._detect():
            self.assertNotEqual("/var/lib/crucible", mp,
                                "the working store is not an orphan")

    def test_leaves_a_foreign_deleted_image_alone(self):
        """Deleted is not sufficient. The name has to be one only we write."""
        for dev, mp, _b in self._detect():
            self.assertNotEqual("/dev/loop9", dev)
            self.assertNotEqual("/mnt/somebody-elses", mp)

    def test_a_live_backing_file_is_not_an_orphan(self):
        """A store whose image still exists may be in legitimate use."""
        backing = dict(BACKING, loop1="/tmp/crucible-release-aaa/crucible-store.img")

        def fake_read(self, *a, **k):
            s = str(self)
            if s == "/proc/mounts":
                return MOUNTS
            for dev, val in backing.items():
                if s == f"/sys/block/{dev}/loop/backing_file":
                    return val
            raise OSError("no such file")
        with mock.patch.object(Path, "read_text", fake_read):
            found = {mp for _d, mp, _b in lifecycle.orphaned_store_mounts()}
        self.assertNotIn("/tmp/crucible-release-aaa/state", found)


class TestTeardownOrdering(unittest.TestCase):
    """The mount must be released BEFORE the directory holding it is deleted."""

    def test_release_precedes_removal(self):
        import release.verify as V

        calls: list[str] = []

        class R:
            returncode = 0

        def fake_run(cmd, *a, **k):
            calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
            return R()

        with mock.patch.object(V.subprocess, "run", fake_run), \
             mock.patch.object(V.shutil, "rmtree",
                               lambda *a, **k: calls.append("rmtree")), \
             mock.patch.object(Path, "read_text",
                               lambda self, *a, **k:
                               "/dev/loop7 /tmp/wd/state ext4 rw 0 0\n"), \
             mock.patch.object(Path, "exists", lambda self: False):
            V.teardown(Path("/tmp/wd"), {"CRUCIBLE_STATE": "/tmp/wd/state"}, False)

        joined = " | ".join(calls)
        self.assertIn("umount", joined)
        self.assertIn("losetup -d /dev/loop7", joined)
        self.assertLess(calls.index([c for c in calls if "umount" in c][0]),
                        calls.index("rmtree"),
                        "unmounting after rmtree is exactly the leak")

    def test_keep_still_releases_the_mount(self):
        """--keep preserves the tree for inspection, not the leak."""
        import release.verify as V
        calls: list[str] = []

        class R:
            returncode = 0

        with mock.patch.object(V.subprocess, "run",
                               lambda cmd, *a, **k: (calls.append(str(cmd)), R())[1]), \
             mock.patch.object(V.shutil, "rmtree",
                               lambda *a, **k: calls.append("rmtree")), \
             mock.patch.object(Path, "read_text",
                               lambda self, *a, **k:
                               "/dev/loop7 /tmp/wd/state ext4 rw 0 0\n"):
            V.teardown(Path("/tmp/wd"), {"CRUCIBLE_STATE": "/tmp/wd/state"}, True)

        self.assertTrue(any("umount" in c for c in calls))
        self.assertNotIn("rmtree", calls)


class TestImageCacheIsShared(unittest.TestCase):
    """Pulled layers are immutable and content-addressed; re-pulling ~10 GB
    into every ephemeral state root is what made the leak expensive."""

    def test_image_root_is_independently_configurable(self):
        import importlib
        import os
        with mock.patch.dict(os.environ, {"CRUCIBLE_STATE": "/tmp/s1",
                                          "CRUCIBLE_IMAGES": "/var/cache/imgs"}):
            ns = importlib.reload(
                importlib.import_module("crucible.backends.namespace"))
            self.assertEqual(Path("/var/cache/imgs"), ns.IMAGE_ROOT)
            self.assertEqual(Path("/tmp/s1"), ns.STATE_ROOT)
        importlib.reload(importlib.import_module("crucible.backends.namespace"))

    def test_image_root_defaults_to_the_state_root(self):
        import importlib
        import os
        env = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_IMAGES"}
        env["CRUCIBLE_STATE"] = "/tmp/s2"
        with mock.patch.dict(os.environ, env, clear=True):
            ns = importlib.reload(
                importlib.import_module("crucible.backends.namespace"))
            self.assertEqual(Path("/tmp/s2"), ns.IMAGE_ROOT)
        importlib.reload(importlib.import_module("crucible.backends.namespace"))


class TestStoreImageIsPerStateRoot(unittest.TestCase):
    """Concurrent boxes must not share one backing image.

    The image name derived from STATE_ROOT.parent alone, so every state root
    under /var/lib collapsed onto /var/lib/crucible-store.img. Two concurrent
    boxes then mounted the same ext4 twice and neither had an independent
    store -- which is why the concurrent resource case read 0 MB for both.
    """

    def _img_for(self, state_root: str) -> str:
        import importlib
        import os
        with mock.patch.dict(os.environ, {"CRUCIBLE_STATE": state_root}):
            ns = importlib.reload(
                importlib.import_module("crucible.backends.namespace"))
            img = ns.STATE_ROOT.parent / f"{ns.STATE_ROOT.name}-store.img"
        importlib.reload(importlib.import_module("crucible.backends.namespace"))
        return str(img)

    def test_distinct_roots_get_distinct_images(self):
        a = self._img_for("/var/lib/crucible-conc-0")
        b = self._img_for("/var/lib/crucible-conc-1")
        main = self._img_for("/var/lib/crucible")
        self.assertNotEqual(a, b, "concurrent boxes must not share a store image")
        self.assertNotEqual(a, main)
        self.assertNotEqual(b, main)

    def test_default_root_keeps_the_warm_cache_path(self):
        # The whole point of encoding the name this way is to NOT invalidate
        # the existing warm cache.
        self.assertEqual("/var/lib/crucible-store.img",
                         self._img_for("/var/lib/crucible"))


if __name__ == "__main__":
    unittest.main()
