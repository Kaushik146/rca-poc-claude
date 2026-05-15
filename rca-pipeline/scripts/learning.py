#!/usr/bin/env python3
"""
Structured learning store — one record per incident, ties a signal
pattern to the fix kind that resolved it and whether the fix stuck.

Today the chassis treats prior incidents narratively (BM25 over
postmortem text). That works for retrieval but doesn't let the pipeline
*learn* (e.g. "for `last_unit_rejected + recent_deploy` signal patterns,
a code fix has worked 12/15 times historically"). This store is the
first step: capture structured `{signal_pattern, fix_kind, outcome}`
triples after every fix-and-test completes.

Record shape:
  {
    "incident_id": "INC-1234", "recorded_at": "...",
    "signal_pattern": "high_error_rate + recent_deploy",
    "fix_kind": "code|config|data|infra|spec_gap|no_fix_needed",
    "fix_outcome": "worked|partial|rolled_back|unknown",
    "prior_incident_id": "PM-42",
    "files_changed": [...], "diff_sha256": "...",
    "confidence": "high|medium|low"
  }

Records live at .rca/learnings/<incident_id>.json. CLI:
  record  --incident-id --signal-pattern --fix-kind [--fix-outcome ...]
  update  --incident-id --fix-outcome
  query   [--signal-pattern X] [--fix-kind X] [--fix-outcome X]
  summary
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


VALID_FIX_KINDS = {"code", "config", "data", "infra", "spec_gap", "no_fix_needed"}
VALID_OUTCOMES = {"worked", "partial", "rolled_back", "unknown"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _learnings_dir(root=None):
    return (root or Path.cwd()) / ".rca" / "learnings"


def _record_path(incident_id, root=None):
    return _learnings_dir(root) / f"{incident_id}.json"


def record(incident_id, signal_pattern, fix_kind, fix_outcome="unknown",
           prior_incident_id=None, files_changed=None, diff_sha256=None,
           confidence="medium", root=None):
    if fix_kind not in VALID_FIX_KINDS:
        raise ValueError(f"fix_kind must be one of {sorted(VALID_FIX_KINDS)}, got {fix_kind!r}")
    if fix_outcome not in VALID_OUTCOMES:
        raise ValueError(f"fix_outcome must be one of {sorted(VALID_OUTCOMES)}, got {fix_outcome!r}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got {confidence!r}")
    if not signal_pattern or not signal_pattern.strip():
        raise ValueError("signal_pattern cannot be empty")

    rec = {
        "incident_id": incident_id, "recorded_at": _now_iso(),
        "signal_pattern": signal_pattern, "fix_kind": fix_kind,
        "fix_outcome": fix_outcome, "prior_incident_id": prior_incident_id,
        "files_changed": files_changed or [], "diff_sha256": diff_sha256,
        "confidence": confidence,
    }
    p = _record_path(incident_id, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return p


def update_outcome(incident_id, fix_outcome, root=None):
    if fix_outcome not in VALID_OUTCOMES:
        raise ValueError(f"fix_outcome must be one of {sorted(VALID_OUTCOMES)}, got {fix_outcome!r}")
    p = _record_path(incident_id, root)
    if not p.is_file():
        raise FileNotFoundError(f"no learning record for {incident_id}")
    rec = json.loads(p.read_text())
    rec["fix_outcome"] = fix_outcome
    rec["updated_at"] = _now_iso()
    p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return p


def read_record(incident_id, root=None):
    p = _record_path(incident_id, root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def all_records(root=None):
    d = _learnings_dir(root)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "incident_id" in rec:
            out.append(rec)
    return out


def _ts_sort_key(ts):
    try:
        return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").timestamp())
    except ValueError:
        return 0


def query(signal_pattern=None, fix_kind=None, fix_outcome=None, root=None):
    records = all_records(root)
    matches = []
    for rec in records:
        if signal_pattern and signal_pattern.lower() not in rec.get("signal_pattern", "").lower():
            continue
        if fix_kind and rec.get("fix_kind") != fix_kind:
            continue
        if fix_outcome and rec.get("fix_outcome") != fix_outcome:
            continue
        matches.append(rec)
    outcome_rank = {"worked": 0, "partial": 1, "unknown": 2, "rolled_back": 3}
    matches.sort(key=lambda r: (
        outcome_rank.get(r.get("fix_outcome", "unknown"), 9),
        -1 * _ts_sort_key(r.get("recorded_at", "")),
    ))
    return matches


def summary(root=None):
    records = all_records(root)
    by_kind, by_outcome = {}, {}
    pattern_success = {}
    for rec in records:
        k = rec.get("fix_kind", "?")
        o = rec.get("fix_outcome", "?")
        by_kind[k] = by_kind.get(k, 0) + 1
        by_outcome[o] = by_outcome.get(o, 0) + 1
        sp = rec.get("signal_pattern", "?")
        pattern_success.setdefault(sp, {"total": 0, "worked": 0})
        pattern_success[sp]["total"] += 1
        if o == "worked":
            pattern_success[sp]["worked"] += 1
    return {
        "total_records": len(records),
        "by_fix_kind": by_kind,
        "by_fix_outcome": by_outcome,
        "pattern_success_rate": {
            sp: {**counts, "rate": (counts["worked"] / counts["total"]) if counts["total"] else 0.0}
            for sp, counts in pattern_success.items()
        },
    }


def _cmd_record(args):
    root = Path(args.root) if args.root else None
    files = [f.strip() for f in args.files_changed.split(",")] if args.files_changed else []
    p = record(args.incident_id, args.signal_pattern, args.fix_kind,
               args.fix_outcome, args.prior_incident_id,
               files, args.diff_sha256, args.confidence, root)
    sys.stderr.write(f"recorded learning for {args.incident_id} → {p}\n")
    return 0


def _cmd_update(args):
    root = Path(args.root) if args.root else None
    p = update_outcome(args.incident_id, args.fix_outcome, root)
    sys.stderr.write(f"updated {args.incident_id} outcome → {args.fix_outcome} ({p})\n")
    return 0


def _cmd_query(args):
    root = Path(args.root) if args.root else None
    results = query(args.signal_pattern, args.fix_kind, args.fix_outcome, root)
    json.dump(results, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_summary(args):
    root = Path(args.root) if args.root else None
    s = summary(root)
    json.dump(s, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="RCA chassis structured learning store")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("record")
    r.add_argument("--incident-id", required=True)
    r.add_argument("--signal-pattern", required=True)
    r.add_argument("--fix-kind", required=True, choices=sorted(VALID_FIX_KINDS))
    r.add_argument("--fix-outcome", default="unknown", choices=sorted(VALID_OUTCOMES))
    r.add_argument("--prior-incident-id", default=None)
    r.add_argument("--files-changed", default=None)
    r.add_argument("--diff-sha256", default=None)
    r.add_argument("--confidence", default="medium", choices=sorted(VALID_CONFIDENCE))
    r.set_defaults(func=_cmd_record)

    u = sub.add_parser("update")
    u.add_argument("--incident-id", required=True)
    u.add_argument("--fix-outcome", required=True, choices=sorted(VALID_OUTCOMES))
    u.set_defaults(func=_cmd_update)

    q = sub.add_parser("query")
    q.add_argument("--signal-pattern", default=None)
    q.add_argument("--fix-kind", default=None, choices=sorted(VALID_FIX_KINDS))
    q.add_argument("--fix-outcome", default=None, choices=sorted(VALID_OUTCOMES))
    q.set_defaults(func=_cmd_query)

    s = sub.add_parser("summary")
    s.set_defaults(func=_cmd_summary)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
