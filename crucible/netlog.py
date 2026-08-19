"""
CRUCIBLE :: netlog.py

The build/run network split already exists. This records what crossed it.

Motivation: a repo runner that executes untrusted build scripts with network
access is, whether it admits it or not, a dynamic analysis harness. The only
question is whether it throws the telemetry away. Two cheap sensors turn it
into a supply-chain instrument:

    DnsLedger       what hostnames did the build resolve?
    SocketSampler   what IPs did it actually connect to?

Neither alone is sufficient, and the gap between them is the interesting part:

  * DNS-only misses connections to hard-coded IPs -- precisely the technique
    used to dodge DNS-based egress monitoring.
  * Socket-only gives you 104.16.x.x and no idea whose it is.
  * A peer that appears in the socket table but never in the DNS log resolved
    its address somewhere we cannot see, or skipped resolution entirely. That
    delta is a finding, not noise.

Implementation notes, since the usual tools are unavailable here (no iproute2,
no strace, no eBPF, no netfilter userland):

  DNS      A UDP forwarder on 127.0.0.2:53. The sandbox's /etc/resolv.conf is
           rewritten to point at it, so every resolution is logged and then
           relayed upstream. Pure stdlib, no privileges beyond binding a
           loopback address.

  Sockets  /proc/net/tcp lists every connection on the host, which is far too
           noisy. Instead we walk the sandboxed process's descendants, collect
           the socket inodes they hold open in /proc/<pid>/fd, and keep only
           /proc/net/tcp rows whose inode is in that set. That attributes each
           peer to our process tree specifically, rather than to the machine.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOOPBACK_PREFIXES = ("127.", "0.0.0.0", "::1")


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def _qname(pkt: bytes) -> str:
    """Extract the QNAME from a DNS query. Labels are length-prefixed."""
    try:
        i, parts = 12, []
        while i < len(pkt):
            n = pkt[i]
            if n == 0:
                break
            if n >= 0xC0:            # compression pointer; not valid in a query
                break
            # NB: ASCII, not idna. The idna codec rejects a non-strict errors
            # argument and raises UnicodeError, which silently emptied every
            # parse. DNS labels are ASCII on the wire regardless -- IDNs arrive
            # already punycoded.
            parts.append(pkt[i + 1:i + 1 + n].decode("ascii", "replace"))
            i += n + 1
        return ".".join(parts)
    except (IndexError, UnicodeError):
        return ""


def _answer_ips(pkt: bytes) -> list[str]:
    """Pull A/AAAA rdata out of a DNS reply.

    Needed for the delta that makes this worth running: an IP the build
    connected to that never appeared in any DNS answer was not resolved
    through us. Either the address was hard-coded -- the standard way to
    evade DNS-based egress monitoring -- or resolution happened somewhere
    we cannot see. Both are worth surfacing.
    """
    try:
        qd = int.from_bytes(pkt[4:6], "big")
        an = int.from_bytes(pkt[6:8], "big")
        i = 12
        for _ in range(qd):                       # skip questions
            while i < len(pkt) and pkt[i]:
                if pkt[i] >= 0xC0:
                    i += 1
                    break
                i += pkt[i] + 1
            i += 5                                # null byte + QTYPE + QCLASS
        out = []
        for _ in range(an):
            if pkt[i] >= 0xC0:
                i += 2
            else:
                while i < len(pkt) and pkt[i]:
                    i += pkt[i] + 1
                i += 1
            rtype = int.from_bytes(pkt[i:i + 2], "big")
            rdlen = int.from_bytes(pkt[i + 8:i + 10], "big")
            rdata = pkt[i + 10:i + 10 + rdlen]
            if rtype == 1 and rdlen == 4:
                out.append(socket.inet_ntoa(rdata))
            elif rtype == 28 and rdlen == 16:
                out.append(socket.inet_ntop(socket.AF_INET6, rdata))
            i += 10 + rdlen
        return out
    except (IndexError, ValueError, OSError):
        return []


class DnsLedger:
    """Logging DNS forwarder. Binds a loopback address; relays upstream."""

    def __init__(self, bind: str = "127.0.0.2", port: int = 53,
                 upstream: str = "", log=print):
        self.bind, self.port = bind, port
        self.upstream = upstream or self._host_resolver()
        self.log = log
        self.queries: list[tuple[float, str]] = []
        self.resolved: dict[str, str] = {}     # ip -> the name that produced it
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active = False

    @staticmethod
    def _host_resolver() -> str:
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                if line.startswith("nameserver"):
                    ns = line.split()[1]
                    if not ns.startswith("127."):
                        return ns
        except OSError:
            pass
        return "8.8.8.8"

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind, self.port))
            s.settimeout(0.5)
        except OSError as e:
            self.log(f"  ! DNS ledger unavailable ({e}); hostname capture off")
            return False
        self._sock = s
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.active = True
        return True

    def _serve(self) -> None:
        """Log the question, relay it upstream, relay the answer back.

        Deliberately a forwarder and not a resolver: we must not change what
        the build can reach, only observe it. A ledger that also breaks DNS
        would get switched off, and a sensor nobody runs measures nothing.
        """
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(4)
        while not self._stop.is_set():
            try:
                pkt, client = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            name = _qname(pkt)
            if name:
                self.queries.append((time.time(), name.lower().rstrip(".")))
            try:
                up.sendto(pkt, (self.upstream, 53))
                reply, _ = up.recvfrom(4096)
                for ip in _answer_ips(reply):
                    self.resolved.setdefault(ip, name)
                self._sock.sendto(reply, client)
            except OSError:
                # SERVFAIL rather than a hang, so callers fail fast
                if len(pkt) >= 4:
                    try:
                        self._sock.sendto(pkt[:2] + b"\x81\x82" + pkt[4:12], client)
                    except OSError:
                        pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
        self.active = False

    @property
    def hostnames(self) -> list[str]:
        seen, out = set(), []
        for _, q in self.queries:
            if q and q not in seen:
                seen.add(q)
                out.append(q)
        return out


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

def _hex_to_addr(h: str) -> tuple[str, int]:
    """/proc/net/tcp encodes IPv4 little-endian hex: '0100007F:1F90'."""
    a, _, p = h.partition(":")
    port = int(p, 16)
    if len(a) == 8:
        ip = socket.inet_ntoa(struct.pack("<I", int(a, 16)))
    else:
        raw = bytes.fromhex(a)
        ip = socket.inet_ntop(socket.AF_INET6,
                              b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    return ip, port


def _descendants(root_pid: int) -> set[int]:
    """All pids under root_pid, read from /proc. Cheap enough to poll."""
    children: dict[int, list[int]] = {}
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return {root_pid}
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(pid)
    out, stack = {root_pid}, [root_pid]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _inodes_of(pids: set[int]) -> set[str]:
    ino = set()
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    tgt = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if tgt.startswith("socket:["):
                    ino.add(tgt[8:-1])
        except OSError:
            continue
    return ino


@dataclass
class Peer:
    ip: str
    port: int
    first_seen: float


class SocketSampler:
    """Poll the TCP table, keeping only sockets owned by our process tree."""

    def __init__(self, root_pid: int, interval: float = 0.015):
        self.root_pid = root_pid
        self.interval = interval
        self.peers: dict[tuple[str, int], Peer] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._sample_safe()          # last look before the fds disappear
        self._stop.set()
        self._thread.join(timeout=2)

    def _loop(self) -> None:
        # Sample immediately rather than after the first interval: build steps
        # routinely open and close a connection inside 100ms, so a poller that
        # sleeps first sees an empty table and reports no egress at all. This
        # is sampling, not tracing -- it can still miss a connection shorter
        # than the interval. Only kernel-side capture (eBPF, netfilter, or a
        # conntrack hook) closes that gap properly.
        self._sample_safe()
        while not self._stop.is_set():
            self._sample_safe()
            self._stop.wait(self.interval)

    def _sample_safe(self) -> None:
        try:
            self._sample()
        except Exception:
            pass

    def _sample(self) -> None:
        ino = _inodes_of(_descendants(self.root_pid))
        if not ino:
            return
        for tbl in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(tbl).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                f = line.split()
                if len(f) < 10 or f[9] not in ino:
                    continue
                ip, port = _hex_to_addr(f[2])           # rem_address
                if port == 0 or ip.startswith(LOOPBACK_PREFIXES):
                    continue
                key = (ip, port)
                if key not in self.peers:
                    self.peers[key] = Peer(ip, port, time.time())


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------

@dataclass
class Ledger:
    hostnames: list[str] = field(default_factory=list)
    peers: list[tuple[str, int]] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    runtime_egress_possible: bool = False

    @property
    def unresolved(self) -> list[tuple[str, int]]:
        """Peers contacted that no DNS answer ever named."""
        return [(ip, port) for ip, port in self.peers if ip not in self.resolved]

    def report(self) -> list[str]:
        out = []
        if self.hostnames:
            out.append(f"build resolved {len(self.hostnames)} host(s): "
                       + ", ".join(self.hostnames[:8])
                       + (" …" if len(self.hostnames) > 8 else ""))
        if self.peers:
            out.append(f"build connected to {len(self.peers)} peer(s): "
                       + ", ".join(f"{i}:{p}" for i, p in self.peers[:6])
                       + (" …" if len(self.peers) > 6 else ""))
        if self.unresolved:
            out.append("\033[33m! " + f"{len(self.unresolved)} peer(s) never named by DNS: "
                       + ", ".join(f"{i}:{p}" for i, p in self.unresolved[:6])
                       + " — hard-coded address or out-of-band resolution\033[0m")
        out.append("runtime egress: "
                   + ("POSSIBLE (--online-run)" if self.runtime_egress_possible
                      else "none — namespace has no route out"))
        return out
