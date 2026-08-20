"""Write until something stops us. Reports how far it got and why.

Deliberately not a small successful write: a disk-limit test that writes 1 MB
and declares victory proves nothing. This writes in 8 MiB chunks until the
kernel refuses, then says which errno refused it.
"""
import errno, json, os, sys

CEILING_MB = 20_000          # far above any budget; the kernel should stop us first
written = 0
stopped = None
try:
    with open("/workspace/ballast", "wb") as fh:
        while written < CEILING_MB:
            fh.write(b"\0" * (8 * 1024 * 1024))
            fh.flush()
            os.fsync(fh.fileno())
            written += 8
except OSError as e:
    stopped = {"errno": e.errno, "name": errno.errorcode.get(e.errno, "?"),
               "msg": str(e)[:80]}

print("DISK_REPORT " + json.dumps(
    {"written_mb": written, "stopped": stopped,
     "hit_ceiling": written >= CEILING_MB}), flush=True)
