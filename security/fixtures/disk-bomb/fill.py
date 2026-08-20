"""Write until something stops us. Reports how far it got and why.

Deliberately not a small successful write: a disk-limit test that writes 1 MB
and declares victory proves nothing. This writes in 8 MiB chunks until the
kernel refuses, then says which errno refused it.
"""
import errno, json, os, signal, sys

# RLIMIT_FSIZE raises SIGXFSZ, whose default action is to kill the process
# before it can say anything. Ignore it so the write returns EFBIG as an
# ordinary error and this fixture can report which control actually bound --
# a probe that dies silently proves nothing about the limit that killed it.
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)

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
