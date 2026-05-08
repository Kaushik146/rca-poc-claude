#!/usr/bin/env python3
"""
Mid-phase checkpoint store for the RCA orchestrator.

Persists validated phase output to `.rca/checkpoints/<incident_id>.json`
between phase transitions so the orchestrator can resume from the last
completed phase if it crashes mid-run.

Contract (called by the orchestrator agent via Bash):

  python3 scripts/checkpoint.py --read  --incident-id INC-1234
      Print the checkpoint as JSON on stdout. Exit 0 on hit, 2 on miss.

  python3 scripts/checkpoint.py --write --incident-id INC-1234 \
      --phase signals --output @-
      Read validated phase output JSON from stdin. Atomically merge it
      into the checkpoint and record `last_completed_phase = signals`.

  python3 scripts/checkpoint.py --clear --incident-id INC-1234
      Remove the checkpoint after fix-and-test completes successfully.

Atomicity: writes go to `.tmp` first, then `os.replace()` (POSIX atomic
rename) replaces the live file. A crash mid-write leaves the live file
untouched. Regression tests in `scripts/tests/test_checkpoint.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
PHASE_ORDER = ["intake", "signals", "prior_incident", "fix_and_test"]


def _checkpoint_path(incident_id, root=None):
    base = (root or Path.cwd()) / ".rca" / "checkpoints"
    return base / f"{incident_id}.json"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_checkpoint(incident_id, root=None):
    p = _checkpoint_path(incident_id, root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format_version") != FORMAT_VERSION:
        return None
    if data.get("incident_id") != incident_id:
        return None
    return data


def write_checkpoint(incident_id, phase, phase_output, rca_stage=None, root=None):
    if phase not in PHASE_ORDER:
        raise ValueError(f"unknown phase: {phase!r} (valid: {PHASE_ORDER})")
    p = _checkpoint_path(incident_id, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = read_checkpoint(incident_id, root) or {
        "format_version": FORMAT_VERSION,
        "incident_id": incident_id,
        "started_at": _now_iso(),
        "rca_stage": rca_stage,
        "last_completed_phase": None,
        "phase_outputs": {},
    }
    existing["phase_outputs"][phase] = phase_output
    existing["last_completed_phase"] = phase
    existing["updated_at"] = _now_iso()
    if rca_stage and not existing.get("rca_stage"):
        existing["rca_stage"] = rca_stage
    fd, tmp_path = tempfile.mkstemp(
        dir=str(p.parent), prefix=f"{incident_id}.", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return p


def clear_checkpoint(incident_id, root=None):
    p = _checkpoint_path(incident_id, root)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _read_output(arg):
    if arg == "@-":
        return json.load(sys.stdin)
    return json.loads(Path(arg).read_text())


def main(argv=None):
    ap = argparse.ArgumentParser(description="RCA orchestrator checkpoint store")
    ap.add_argument("--incident-id", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--read", action="store_true")
    g.add_argument("--write", action="store_true")
    g.add_argument("--clear", action="store_true")
    ap.add_argument("--phase", choices=PHASE_ORDER)
    ap.add_argument("--output", help="@- for stdin, or a file path")
    ap.add_argument("--rca-stage", default=None)
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else None
    if args.read:
        cp = read_checkpoint(args.incident_id, root)
        if cp is None:
            return 2
        json.dump(cp, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.write:
        if not args.phase or not args.output:
            ap.error("--write requires --phase and --output")
        out = _read_output(args.output)
        p = write_checkpoint(args.incident_id, args.phase, out, args.rca_stage, root)
        sys.stderr.write(f"checkpoint written: {p}\n")
        return 0
    if args.clear:
        removed = clear_checkpoint(args.incident_id, root)
        sys.stderr.write(f"checkpoint {'cleared' if removed else 'absent'}\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
