"""
Network policy: resolution, refusal, and refusal to widen.

The failure this guards against is a run that reports one policy and performs
another. A `proxy` mode that falls back to a direct connection when no proxy
is configured would succeed, be labelled `proxy`, and have sent the traffic
straight out.
"""

from __future__ import annotations

import unittest

from crucible import netpolicy as N


class TestResolution(unittest.TestCase):

    def test_hermetic_cuts_both_phases(self):
        p = N.resolve("hermetic", env={})
        self.assertFalse(p.allows("build"))
        self.assertFalse(p.allows("runtime"))

    def test_hermetic_refuses_to_be_widened_by_another_flag(self):
        with self.assertRaises(N.PolicyError) as cm:
            N.resolve("hermetic", runtime_online=True, env={})
        self.assertIn("contradict", str(cm.exception))

    def test_proxy_requires_a_proxy(self):
        with self.assertRaises(N.PolicyError) as cm:
            N.resolve("proxy", env={})
        self.assertIn("HTTP_PROXY", str(cm.exception))

    def test_proxy_passes_only_allowlisted_variables(self):
        env = {"HTTPS_PROXY": "http://p:3128", "NO_PROXY": "localhost",
               "AWS_SECRET_ACCESS_KEY": "shhh", "GITHUB_TOKEN": "ghp_x",
               "PATH": "/usr/bin"}
        p = N.resolve("proxy", env=env)
        passed = p.env_for("build")
        self.assertEqual("http://p:3128", passed["HTTPS_PROXY"])
        self.assertEqual("localhost", passed["NO_PROXY"])
        for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "PATH"):
            self.assertNotIn(leaked, passed,
                             "the sandbox must not inherit host secrets")

    def test_proxy_leaves_runtime_cut_by_default(self):
        p = N.resolve("proxy", env={"HTTP_PROXY": "http://p:3128"})
        self.assertTrue(p.allows("build"))
        self.assertFalse(p.allows("runtime"))

    def test_open_is_build_only_unless_asked(self):
        self.assertFalse(N.resolve("open", env={}).allows("runtime"))
        self.assertTrue(N.resolve("open", runtime_online=True, env={}).allows("runtime"))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(N.PolicyError):
            N.resolve("permissive", env={})

    def test_a_missing_ca_bundle_is_refused(self):
        with self.assertRaises(N.PolicyError) as cm:
            N.resolve("proxy", env={"HTTP_PROXY": "http://p:3128",
                                    "CRUCIBLE_CA_BUNDLE": "/nope/ca.pem"})
        self.assertIn("does not exist", str(cm.exception))


class TestReporting(unittest.TestCase):

    def test_credentials_in_a_proxy_url_are_not_reported(self):
        p = N.resolve("proxy", env={"HTTPS_PROXY": "http://user:pw@proxy:3128"})
        blob = str(p.as_dict())
        self.assertNotIn("pw", blob)
        self.assertIn("proxy", blob)

    def test_every_policy_describes_both_phases(self):
        for mode, env in (("hermetic", {}), ("open", {}),
                          ("proxy", {"HTTP_PROXY": "http://p:3128"})):
            d = N.resolve(mode, env=env).describe()
            self.assertIn("build=", d)
            self.assertIn("runtime=", d)

    def test_permissiveness_is_ordered(self):
        herm = N.resolve("hermetic", env={})
        opn = N.resolve("open", env={})
        self.assertTrue(opn.more_permissive_than(herm))
        self.assertFalse(herm.more_permissive_than(opn))


class TestEnforcementIsAtTheBoundary(unittest.TestCase):
    """A repair rule can set Step.network; the sandbox decides anyway."""

    def _argv(self, policy, step):
        from crucible.backends.namespace import NamespaceBackend
        box = NamespaceBackend.__new__(NamespaceBackend)
        box.policy, box._denied, box.pod = policy, {}, None
        box.log = lambda *_: None
        return NamespaceBackend._ns_argv(box, step)

    def test_hermetic_denies_a_step_that_asks_for_network(self):
        from crucible.schema import Step
        argv = self._argv(N.resolve("hermetic", env={}),
                          Step("install/pip", "pip install x", network=True))
        self.assertIn("--net", argv,
                      "an isolated netns must be created despite the request")

    def test_open_grants_a_build_step(self):
        from crucible.schema import Step
        argv = self._argv(N.resolve("open", env={}),
                          Step("install/pip", "pip install x", network=True))
        self.assertNotIn("--net", argv)

    def test_the_dns_repair_cannot_widen_a_hermetic_run(self):
        """_r_dns sets network=True on every step in the plan."""
        from crucible.repair import diagnose
        from crucible.schema import ExecResult, RunPlan, Step
        plan = RunPlan(base="host", run="python main.py")
        plan.steps = [Step("install/pip", "pip install -r r.txt", network=False)]
        patch = diagnose(ExecResult(False, 1, "Temporary failure resolving 'pypi.org'", ""),
                         plan, plan.steps[0], llm=None)
        self.assertIsNotNone(patch)
        patch.apply(plan)
        self.assertTrue(plan.steps[0].network, "the repair did widen the plan")
        argv = self._argv(N.resolve("hermetic", env={}), plan.steps[0])
        self.assertIn("--net", argv,
                      "and the boundary refused it anyway")


if __name__ == "__main__":
    unittest.main()
