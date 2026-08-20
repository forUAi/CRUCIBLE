"""
The layer chain key has to distinguish repos that share a command string.

`pip install -r requirements.txt` under `python:3.12-slim` is byte-identical
in every python repo, so before `manifest_digest` every component of the chain
key matched between two unrelated repos of the same shape and the second one
adopted the first one's install layer.
"""

import tempfile
import unittest
from pathlib import Path

from crucible.engine import manifest_digest


def repo(files: dict) -> Path:
    d = Path(tempfile.mkdtemp(prefix="crucible-test-"))
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d


class ManifestDigestTest(unittest.TestCase):

    def test_identical_manifests_collide_on_purpose(self):
        """Sharing a layer between repos that declare the same dependencies is
        the feature. Only wrong sharing is the bug."""
        a = repo({"requirements.txt": "fastapi\nuvicorn\n"})
        b = repo({"requirements.txt": "fastapi\nuvicorn\n"})
        self.assertEqual(manifest_digest(a), manifest_digest(b))

    def test_different_manifests_must_not_collide(self):
        a = repo({"requirements.txt": "fastapi\nuvicorn\npsycopg2-binary\n"})
        b = repo({"requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\nredis\n"})
        self.assertNotEqual(manifest_digest(a), manifest_digest(b))

    def test_source_edits_do_not_bust_the_install_layer(self):
        a = repo({"requirements.txt": "flask\n", "main.py": "print(1)\n"})
        b = repo({"requirements.txt": "flask\n", "main.py": "print(2)\n"})
        self.assertEqual(manifest_digest(a), manifest_digest(b))

    def test_nested_manifests_are_counted(self):
        a = repo({"requirements.txt": "flask\n"})
        b = repo({"requirements.txt": "flask\n", "svc/package.json": "{}\n"})
        self.assertNotEqual(manifest_digest(a), manifest_digest(b))

    def test_vendor_dirs_are_ignored(self):
        """node_modules is an *output* of an install step. Hashing it would
        make the key depend on the layer it is supposed to identify."""
        a = repo({"package.json": '{"name":"x"}\n'})
        b = repo({"package.json": '{"name":"x"}\n',
                  "node_modules/dep/package.json": '{"name":"dep"}\n'})
        self.assertEqual(manifest_digest(a), manifest_digest(b))

    def test_lockfile_change_is_visible(self):
        a = repo({"package.json": '{"name":"x"}\n', "package-lock.json": '{"v":1}\n'})
        b = repo({"package.json": '{"name":"x"}\n', "package-lock.json": '{"v":2}\n'})
        self.assertNotEqual(manifest_digest(a), manifest_digest(b))

    def test_missing_repo_is_not_fatal(self):
        self.assertIsInstance(manifest_digest(Path("/nonexistent-crucible")), str)


if __name__ == "__main__":
    unittest.main()


class ComposeAddressingTest(unittest.TestCase):
    """The pod reaches sidecars on loopback; compose reaches them by name."""

    def _plan(self):
        from crucible.schema import RunPlan, Service
        p = RunPlan()
        p.ports = [8000]
        p.services = [Service(name="postgres", image="postgres:16-alpine", ports=[5432]),
                      Service(name="redis", image="redis:7-alpine", ports=[6379])]
        p.env = {
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "SELF": "http://127.0.0.1:8000/callback",
        }
        return p

    def test_sidecar_hosts_become_service_names(self):
        from crucible.materialize import to_compose
        out = to_compose(self._plan())
        self.assertIn("postgresql://u:p@postgres:5432/db", out)
        self.assertIn("redis://redis:6379/0", out)

    def test_app_own_port_stays_loopback(self):
        from crucible.materialize import to_compose
        out = to_compose(self._plan())
        self.assertIn("http://127.0.0.1:8000/callback", out)
