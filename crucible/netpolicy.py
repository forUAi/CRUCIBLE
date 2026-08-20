"""
CRUCIBLE :: netpolicy.py

Which network a run is allowed, stated once and enforced at the boundary.

Build-time connectivity is a supported capability, not a failure: an
enterprise needs its dependencies to resolve through its own proxy, and a
laboratory needs a machine that can reach the internet. What is not
acceptable is that the answer be implicit, or that it drift.

Three modes:

    hermetic   no egress at any phase. Dependencies come from the layer
               cache or the run fails, clearly, saying so.
    proxy      the operator's proxy environment is passed through to build
               steps. Runtime stays cut unless separately asked for.
    open       broad build connectivity, chosen explicitly and printed on
               every result so nobody mistakes it for the default.

The enforcement point is `_ns_argv` in the backend, not the caller. A repair
rule can set `Step.network = True` -- `_r_dns` literally does it to every
step in the plan -- and under a restrictive policy the sandbox simply does
not hand it a route. Asking every caller to be careful is how a policy
becomes advisory; refusing at the one place that creates the namespace is how
it becomes a property.

On secrets: proxy mode passes an ALLOWLIST of proxy variables, never the
host environment. A sandbox that inherits `os.environ` inherits whatever
token the operator happened to have exported, and the repository is hostile.

On direct-IP bypass: CRUCIBLE does not attempt to stop an application from
dialling an address instead of using the proxy. That is the enterprise
network's control, and implementing a token version here would advertise an
enforcement that does not exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The only host variables that may cross into a sandbox in proxy mode.
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "FTP_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "no_proxy", "ftp_proxy", "all_proxy")
CA_VARS = ("CRUCIBLE_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")

MODES = ("hermetic", "proxy", "open")


class PolicyError(RuntimeError):
    """The requested network mode cannot be established."""


@dataclass(frozen=True)
class NetworkPolicy:
    name: str
    build_egress: str            # none | proxy | open
    runtime_egress: str          # none | proxy | open
    proxy_env: dict = field(default_factory=dict)
    ca_bundle: str = ""

    # -- questions the sandbox asks -----------------------------------

    def allows(self, phase: str) -> bool:
        return (self.build_egress if phase == "build"
                else self.runtime_egress) != "none"

    def env_for(self, phase: str) -> dict:
        if phase != "build" or self.build_egress != "proxy":
            return {}
        env = dict(self.proxy_env)
        if self.ca_bundle:
            env["SSL_CERT_FILE"] = self.ca_bundle
            env["REQUESTS_CA_BUNDLE"] = self.ca_bundle
            env["CURL_CA_BUNDLE"] = self.ca_bundle
            env["NODE_EXTRA_CA_CERTS"] = self.ca_bundle
        return env

    # -- how it is reported -------------------------------------------

    def describe(self) -> str:
        bits = [f"build={self.build_egress}", f"runtime={self.runtime_egress}"]
        if self.proxy_env:
            via = self.proxy_env.get("HTTPS_PROXY") or self.proxy_env.get("HTTP_PROXY")
            bits.append(f"via {via}")
        if self.ca_bundle:
            bits.append(f"ca={self.ca_bundle}")
        return f"{self.name} ({', '.join(bits)})"

    def as_dict(self) -> dict:
        return {"name": self.name, "build_egress": self.build_egress,
                "runtime_egress": self.runtime_egress,
                # The proxy URL is configuration, not a secret; a credential
                # embedded in it would be, so only the host is reported.
                "proxy": _host_only(self.proxy_env.get("HTTPS_PROXY")
                                    or self.proxy_env.get("HTTP_PROXY") or ""),
                "ca_bundle": self.ca_bundle}

    def rank(self) -> tuple[int, int]:
        order = {"none": 0, "proxy": 1, "open": 2}
        return order[self.build_egress], order[self.runtime_egress]

    def more_permissive_than(self, other: "NetworkPolicy") -> bool:
        a, b = self.rank(), other.rank()
        return a[0] > b[0] or a[1] > b[1]


def _host_only(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url)
        return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    except ValueError:
        return "<unparseable>"


def resolve(mode: str, runtime_online: bool = False, env=None) -> NetworkPolicy:
    """Build a policy, or refuse if the requested mode cannot be established.

    Refusing is the point. A proxy mode that quietly falls back to direct
    connections when no proxy is configured is the failure this whole module
    exists to prevent -- the run would succeed, the evidence would say
    `proxy`, and the traffic would have gone straight out.
    """
    env = os.environ if env is None else env
    if mode not in MODES:
        raise PolicyError(f"unknown network mode {mode!r}; expected one of "
                          f"{', '.join(MODES)}")

    if mode == "hermetic":
        if runtime_online:
            raise PolicyError(
                "hermetic and --online-run contradict each other; a policy "
                "must not be quietly widened by another flag")
        return NetworkPolicy("hermetic", "none", "none")

    if mode == "proxy":
        proxy_env = {k: env[k] for k in PROXY_VARS if env.get(k)}
        if not any(k.lower().startswith(("http_proxy", "https_proxy"))
                   for k in proxy_env):
            raise PolicyError(
                "network mode `proxy` requires HTTP_PROXY or HTTPS_PROXY in "
                "the environment; none was set. Refusing rather than falling "
                "back to a direct connection, which would report `proxy` for "
                "traffic that bypassed it")
        ca = next((env[k] for k in CA_VARS if env.get(k)), "")
        if ca and not os.path.exists(ca):
            raise PolicyError(f"CA bundle {ca} does not exist")
        return NetworkPolicy("proxy", "proxy",
                             "proxy" if runtime_online else "none",
                             proxy_env, ca)

    return NetworkPolicy("open", "open", "open" if runtime_online else "none")


DEFAULT = NetworkPolicy("open", "open", "none")
