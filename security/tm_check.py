"""
CRUCIBLE :: security/tm_check.py

Keeps THREAT_MODEL.md honest.

A threat model that names tests which do not exist is worse than none: it
reads like assurance and provides none. This parses the claims, resolves
every `EVIDENCE:` reference to something real on disk, and fails if a claim
points at a file, test class or fixture that is missing.

It deliberately does NOT run the tests -- several need root and a Linux
guest. It checks that the map corresponds to the territory.

    python3 security/tm_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TM = ROOT / "THREAT_MODEL.md"

CLAIM = re.compile(r"^### (C\d+) — (.+)$", re.M)
# Things an EVIDENCE line can name: a path, a path::Class, or a fixture dir.
REF = re.compile(r"`([^`]+)`")


def resolve(ref: str) -> tuple[bool, str]:
    """Is this reference real?"""
    token = ref.split()[0].strip("`")
    path, _, sym = token.partition("::")
    p = ROOT / path
    if not p.exists():
        return False, f"no such path: {path}"
    if sym:
        try:
            body = p.read_text()
        except OSError as e:
            return False, f"unreadable {path}: {e}"
        if f"class {sym}" not in body and f"def {sym}" not in body:
            return False, f"{path} has no {sym}"
    return True, ""


ASSERTS = re.compile(r"^ASSERTS: (.+)$", re.M)


def load_results(path: str) -> tuple[dict, str, str]:
    """Suite -> outcome, plus the artifact identity the run was made against."""
    data = json.loads(Path(path).read_text())
    suites = {s["suite"]: s["outcome"] for s in data.get("suites", [])}
    return suites, data.get("sha256", ""), data.get("version", "")


def main() -> int:
    text = TM.read_text()
    claims = CLAIM.findall(text)
    blocks = CLAIM.split(text)[1:]

    ap = argparse.ArgumentParser(prog="tm_check")
    ap.add_argument("--results", help="release verification JSON")
    a = ap.parse_args()
    suites, sha, ver = load_results(a.results) if a.results else ({}, "", "")
    if a.results:
        print(f"against artifact crucible-{ver} sha256:{sha[:16]}…")

    verified, unverified, broken, unexecuted = [], [], [], []
    for i in range(0, len(blocks), 3):
        cid, title, body = blocks[i], blocks[i + 1], blocks[i + 2]
        if "UNVERIFIED" in body:
            unverified.append((cid, title))
            continue
        line = next((l for l in body.splitlines()
                     if l.startswith("EVIDENCE:")), "")
        refs = REF.findall(line)
        if not refs:
            broken.append((cid, title, "claimed but names no evidence"))
            continue
        for r in refs:
            ok, why = resolve(r)
            if not ok:
                broken.append((cid, title, why))

        # A file that exists proves nothing ran. When a results file is
        # supplied, the named suite must have executed and passed in it.
        am = ASSERTS.search(body)
        if not am:
            broken.append((cid, title, "no ASSERTS: line naming an assertion"))
        elif a.results:
            suite = am.group(1).split("::")[0].strip()
            outcome = suites.get(suite)
            if outcome is None:
                unexecuted.append((cid, title, f"{suite} did not run"))
            elif outcome != "pass":
                unexecuted.append((cid, title, f"{suite} -> {outcome}"))
        verified.append((cid, title))

    proven = len(verified) - len(unexecuted)
    print(f"{len(claims)} claims: "
          + (f"{proven} PROVEN by an executed suite, " if a.results else
             f"{len(verified)} with resolving references, ")
          + f"{len(unverified)} UNVERIFIED, {len(broken)} broken"
          + (f", {len(unexecuted)} unexecuted" if unexecuted else ""))
    for cid, title, why in unexecuted:
        print(f"  \033[33m~ {cid}\033[0m {title} — {why}")
    for cid, title in unverified:
        print(f"  \033[33m? {cid}\033[0m {title}")
    for cid, title, why in broken:
        print(f"  \033[31m✗ {cid}\033[0m {title} — {why}")

    if not claims:
        print("  ✗ no claims parsed; the format changed")
        return 1
    return 1 if (broken or unexecuted) else 0


if __name__ == "__main__":
    sys.exit(main())
