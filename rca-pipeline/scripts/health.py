#!/usr/bin/env python3
"""
Health event log — structured observability for the RCA chassis.

The orchestrator and validator agents call this script (via Bash) to
record events that would otherwise be silent: a blocked validator, an
MCP auth failure, a slow phase, a missing token. Events accumulate as
JSONL at `.rca/health/<incident_id>.jsonl` — append-only so a crash
mid-write loses at most the last record, and JSONL so each event is
independently parseable even when later ones are corrupt.

Event taxonomy (severity → typical event_type):
  info  : phase_started, phase_completed, mcp_call_success
  warn  : phase_slow, validator_normalizing_warning, coverage_gap
  error : phase_failed, validator_blocked, mcp_auth_failure,
          mcp_timeout, token_expired

CLI:
  python3 scripts/health.py record --incident-id INC-X --phase signals \
      --event-type mcp_call_success --severity info --details '{...}'
  python3 scripts/health.py summary --incident-id INC-X
  python3 scripts/health.py check --since 24h [--incident-id INC-X]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


VALID_PHASES = {"intake", "signals", "prior_incident", "fix_and_test",
                "validator", "orchestrator"}
VALID_SEVERITIES = {"info", "warn", "error"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _health_dir(root=None):
    return (root or Path.cwd()) / ".rca" / "health"


def _health_log_path(incident_id, root=None):
    return _health_dir(root) / f"{incident_id}.jsonl"


def record_event(incident_id, phase, event_type, severity, details=None, root=None):
    if phase not in VALID_PHASES:
        raise ValueError(f"phase must be one of {sorted(VALID_PHASES)}, got {phase!r}")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}")
    if not event_type:
        raise ValueError("event_type cannot be empty")

    event = {
        "timestamp": _now_iso(), "incident_id": incident_id,
        "phase": phase, "event_type": event_type,
        "severity": severity, "details": details or {},
    }
    p = _health_log_path(incident_id, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    return p


def read_events(incident_id, root=None):
    p = _health_log_path(incident_id, root)
    if not p.is_file():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def all_recent_events(since, root=None):
    d = _health_dir(root)
    if not d.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - since
    out = []
    for f in d.glob("*.jsonl"):
        for ev in read_events(f.stem, root):
            try:
                ts = datetime.strptime(ev["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                out.append(ev)
    out.sort(key=lambda e: e.get("timestamp", ""))
    return out


_SINCE_RE = re.compile(r"^(\d+)([smhd])$")


def parse_since(s):
    m = _SINCE_RE.match(s.strip())
    if not m:
        raise ValueError(f"invalid --since duration: {s!r} (use NNs/NNm/NNh/NNd)")
    n, unit = int(m.group(1)), m.group(2)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
            "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def summarize(events):
    if not events:
        return "(no events)\n"
    lines = []
    for ev in events:
        sev_tag = {"info": "  ", "warn": "WW", "error": "EE"}.get(ev.get("severity"), "??")
        lines.append(
            f"{sev_tag}  {ev.get('timestamp','?')}  "
            f"{ev.get('incident_id','?'):<15}  "
            f"{ev.get('phase','?'):<14}  "
            f"{ev.get('event_type','?')}  "
            f"{json.dumps(ev.get('details', {}), sort_keys=True)}"
        )
    return "\n".join(lines) + "\n"


def _within(ev, td):
    try:
        ts = datetime.strptime(ev["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return False
    return ts >= (datetime.now(timezone.utc) - td)


def _cmd_record(args):
    details = {}
    if args.details:
        try:
            details = json.loads(args.details)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"--details must be valid JSON: {e}\n")
            return 2
        if not isinstance(details, dict):
            sys.stderr.write("--details must decode to a JSON object\n")
            return 2
    root = Path(args.root) if args.root else None
    p = record_event(args.incident_id, args.phase, args.event_type,
                     args.severity, details, root)
    sys.stderr.write(f"recorded {args.severity} {args.event_type} → {p}\n")
    return 0


def _cmd_summary(args):
    root = Path(args.root) if args.root else None
    sys.stdout.write(summarize(read_events(args.incident_id, root)))
    return 0


def _cmd_check(args):
    root = Path(args.root) if args.root else None
    since_td = parse_since(args.since)
    if args.incident_id:
        events = [ev for ev in read_events(args.incident_id, root) if _within(ev, since_td)]
    else:
        events = all_recent_events(since_td, root)
    errors = [ev for ev in events if ev.get("severity") == "error"]
    warns = [ev for ev in events if ev.get("severity") == "warn"]
    sys.stdout.write(
        f"window: last {args.since}  events: {len(events)}  "
        f"warn: {len(warns)}  error: {len(errors)}\n"
    )
    if errors:
        sys.stdout.write("\nERRORS:\n" + summarize(errors))
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="RCA chassis health event log")
    ap.add_argument("--root", default=None, help="Override repo root (testing)")
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("record", help="Append a health event")
    r.add_argument("--incident-id", required=True)
    r.add_argument("--phase", required=True, choices=sorted(VALID_PHASES))
    r.add_argument("--event-type", required=True)
    r.add_argument("--severity", required=True, choices=sorted(VALID_SEVERITIES))
    r.add_argument("--details", default=None)
    r.set_defaults(func=_cmd_record)

    s = sub.add_parser("summary", help="Print all events for one incident")
    s.add_argument("--incident-id", required=True)
    s.set_defaults(func=_cmd_summary)

    c = sub.add_parser("check", help="Exit non-zero if recent errors exist")
    c.add_argument("--since", default="24h")
    c.add_argument("--incident-id", default=None)
    c.set_defaults(func=_cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
