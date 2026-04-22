#!/usr/bin/env python3
"""
seed_bug — reversibly seed INC-DEMO-001's off-by-one bug in inventory-service.

Why: fix-and-test needs a concrete failing test to target. The working code
already has the correct boundary check (`stock >= quantity`). This script
flips that line to the bug form (`stock > quantity`) so the unit tests in
tests/test_reserve_boundary.py fail — exactly the state a real incident
would present to the RCA pipeline.

Usage:
  python3 scripts/seed_bug.py          # seed
  python3 scripts/seed_bug.py --revert # revert

Idempotent: re-seeding or re-reverting when already in that state is a no-op.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "python-inventory-service" / "app.py"

FIXED = "if stock >= quantity:"
BUGGY = "if stock > quantity:"


def seed() -> int:
    text = TARGET.read_text()
    if BUGGY in text:
        print(f"already seeded: {TARGET.relative_to(ROOT)}")
        return 0
    if FIXED not in text:
        print(f"ERROR: could not find fixed line in {TARGET}", file=sys.stderr)
        print(f"  looking for: {FIXED!r}", file=sys.stderr)
        return 2
    # Replace exactly once to avoid accidental duplicates
    new = text.replace(FIXED, BUGGY, 1)
    TARGET.write_text(new)
    print(f"seeded INC-DEMO-001 off-by-one bug in {TARGET.relative_to(ROOT)}")
    return 0


def revert() -> int:
    text = TARGET.read_text()
    if FIXED in text and BUGGY not in text:
        print(f"already reverted: {TARGET.relative_to(ROOT)}")
        return 0
    if BUGGY not in text:
        print(f"ERROR: nothing to revert in {TARGET}", file=sys.stderr)
        return 2
    new = text.replace(BUGGY, FIXED, 1)
    TARGET.write_text(new)
    print(f"reverted INC-DEMO-001 off-by-one bug in {TARGET.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revert", action="store_true", help="Revert the seeded bug")
    args = ap.parse_args()
    return revert() if args.revert else seed()


if __name__ == "__main__":
    sys.exit(main())
