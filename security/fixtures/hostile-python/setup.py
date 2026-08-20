"""Hostile fixture: package-manager lifecycle script.

`pip install -e .` executes this. That is the supply-chain vector, and it runs
during the BUILD phase, when CRUCIBLE deliberately allows network. Every probe
below is harmless -- it writes canary files and records what it could reach.
Nothing is destructive and nothing exfiltrates.
"""
import json, os, socket, subprocess, sys, glob

R = {}

def probe(name):
    def deco(fn):
        try:
            R[name] = fn()
        except Exception as e:
            R[name] = f"BLOCKED: {type(e).__name__}: {str(e)[:120]}"
        return fn
    return deco

@probe("host_mount_visible")
def _():
    hits = glob.glob("/Users/*") + glob.glob("/home/*/Projects/*")
    return hits[:5] or "none"

@probe("host_mount_write")
def _():
    for base in glob.glob("/Users/*/Projects/crucible"):
        p = os.path.join(base, "CANARY_WRITTEN_FROM_SANDBOX.txt")
        with open(p, "w") as fh:
            fh.write("written by untrusted repo code\n")
        return f"WROTE {p}"
    return "no host mount reachable"

@probe("escape_workspace_write")
def _():
    out = []
    for p in ("/etc/crucible-canary", "/root/crucible-canary", "/tmp/crucible-canary"):
        try:
            open(p, "w").write("x")
            out.append(f"WROTE {p}")
        except OSError as e:
            out.append(f"blocked {p}: {e.errno}")
    return out

@probe("symlink_traversal")
def _():
    os.symlink("/etc", "etc-link")
    open("etc-link/crucible-traversal-canary", "w").write("x")
    return "WROTE via symlink"

@probe("credentials")
def _():
    found = [k for k in os.environ
             if any(t in k.upper() for t in ("TOKEN", "KEY", "SECRET", "AWS", "GITHUB"))]
    paths = [p for p in ("/root/.ssh", os.path.expanduser("~/.ssh"), "/root/.aws",
                         "/var/run/docker.sock", "/run/docker.sock") if os.path.exists(p)]
    return {"env": found, "paths": paths}

@probe("cloud_metadata")
def _():
    s = socket.socket(); s.settimeout(2)
    s.connect(("169.254.169.254", 80))
    return "REACHED metadata endpoint"

@probe("outbound_network")
def _():
    s = socket.socket(); s.settimeout(4)
    s.connect(("1.1.1.1", 443))
    return "REACHED 1.1.1.1:443 (hard-coded, no DNS)"

@probe("host_process_visibility")
def _():
    n = len([d for d in os.listdir("/proc") if d.isdigit()])
    return f"{n} pids visible; pid={os.getpid()}"

@probe("background_daemon")
def _():
    subprocess.Popen(["sh", "-c", "sleep 900"], start_new_session=True)
    return "spawned detached sleep 900"

open("/tmp/crucible-probe-report.json", "w").write(json.dumps(R, indent=2, default=str))
sys.stderr.write("PROBE_REPORT " + json.dumps(R, default=str) + "\n")

from setuptools import setup
setup(name="hostile", version="0.0.1", py_modules=[])
