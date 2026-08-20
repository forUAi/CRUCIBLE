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


def main() -> int:
    text = TM.read_text()
    claims = CLAIM.findall(text)
    blocks = CLAIM.split(text)[1:]

    verified, unverified, broken = [], [], []
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
        verified.append((cid, title))

    print(f"{len(claims)} claims: {len(verified)} with evidence, "
          f"{len(unverified)} UNVERIFIED, {len(broken)} broken")
    for cid, title in unverified:
        print(f"  \033[33m? {cid}\033[0m {title}")
    for cid, title, why in broken:
        print(f"  \033[31m✗ {cid}\033[0m {title} — {why}")

    if not claims:
        print("  ✗ no claims parsed; the format changed")
        return 1
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
