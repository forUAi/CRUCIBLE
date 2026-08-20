"""Build-phase probes. Harmless: writes canaries, records what it reached.

Run as a Dockerfile RUN step so the output goes straight to CRUCIBLE's step
log. Routing it through `pip install -e .` hid every result: pip captures
build output and only forwards it on completion, and this fixture is designed
to stop pip completing.
"""
import glob, json, os, socket, subprocess, sys

R = {}

def probe(name):
    def deco(fn):
        try:
            R[name] = fn()
        except Exception as e:
            R[name] = f"BLOCKED: {type(e).__name__}: {str(e)[:110]}"
        return fn
    return deco

@probe("host_mount_visible")
def _():
    return (glob.glob("/Users/*") + glob.glob("/home/*/Projects/*"))[:4] or "none"

@probe("host_mount_write")
def _():
    for base in glob.glob("/Users/*/Projects/crucible"):
        p = os.path.join(base, "CANARY_WRITTEN_FROM_SANDBOX.txt")
        open(p, "w").write("written by untrusted repo code\n")
        return f"WROTE {p}"
    return "no host mount reachable"

@probe("escape_workspace_write")
def _():
    out = []
    for p in ("/etc/crucible-canary", "/root/crucible-canary"):
        try:
            open(p, "w").write("x"); out.append(f"WROTE {p}")
        except OSError as e:
            out.append(f"blocked {p}: errno {e.errno}")
    return out

@probe("symlink_traversal")
def _():
    os.symlink("/etc", "etc-link")
    open("etc-link/crucible-traversal-canary", "w").write("x")
    return "WROTE via symlink into /etc"

@probe("credentials")
def _():
    env = [k for k in os.environ
           if any(t in k.upper() for t in ("TOKEN", "KEY", "SECRET", "AWS", "GITHUB", "NPM"))]
    paths = [p for p in ("/root/.ssh", "/root/.aws", "/root/.docker",
                         "/var/run/docker.sock", "/run/docker.sock",
                         os.path.expanduser("~/.ssh")) if os.path.exists(p)]
    return {"env": env, "paths": paths}

@probe("cloud_metadata")
def _():
    s = socket.socket(); s.settimeout(3)
    s.connect(("169.254.169.254", 80))
    return "REACHED cloud metadata endpoint"

@probe("outbound_hardcoded_ip")
def _():
    s = socket.socket(); s.settimeout(5)
    s.connect(("1.1.1.1", 443))
    return "REACHED 1.1.1.1:443 without DNS"

@probe("pid_namespace")
def _():
    return f"pid={os.getpid()} visible_pids={len([d for d in os.listdir('/proc') if d.isdigit()])}"

@probe("fork_pressure")
def _():
    """Bounded on purpose: enough to hit a pid cap, not enough to wedge a host."""
    kids, err = [], None
    try:
        for _ in range(400):
            kids.append(subprocess.Popen(["sleep", "30"]))
    except OSError as e:
        err = f"capped after {len(kids)} at errno {e.errno}"
    for k in kids:
        k.kill()
    return err or f"spawned {len(kids)} processes with no cap hit"

@probe("disk_pressure")
def _():
    try:
        with open("/tmp/fill", "wb") as fh:
            for _ in range(64):
                fh.write(b"\0" * (16 * 1024 * 1024))     # 1 GiB ceiling
        n = os.path.getsize("/tmp/fill"); os.unlink("/tmp/fill")
        return f"wrote {n // (1024*1024)} MiB inside the box"
    except OSError as e:
        return f"stopped at errno {e.errno}"

# Report BEFORE spawning anything that could hold a descriptor open.
sys.stdout.write("PROBE_REPORT " + json.dumps(R, default=str) + "\n")
sys.stdout.flush()

@probe("background_daemon")
def _():
    subprocess.Popen(["sh", "-c", "sleep 900"], start_new_session=True)
    return "spawned detached sleep 900"

sys.stdout.write("PROBE_REPORT2 " + json.dumps(
    {"background_daemon": R["background_daemon"]}) + "\n")
sys.stdout.flush()
