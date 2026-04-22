#!/usr/bin/env python3
"""
benchmark_window_selector — offline accuracy benchmark for time-window-selector.

Why this exists
---------------
The `time-window-selector` skill is the headline custom IP of the pipeline.
Its job is to pick the right 30-minute window from a vague incident ticket
fused with vendor AIOps, CUSUM, deploys, pages, and ticket text. The
fixture harness proves it works on one hand-crafted scenario. This
benchmark proves (or disproves) that it beats simple baselines on a
corpus of real, labeled postmortems — that's the evidence that converts
"passes CI" into "earns its keep in production."

This script is the **plumbing only**. It does not ship with a real
corpus. You bring your own labeled postmortems (format below) and run:

    python3 scripts/benchmark_window_selector.py --corpus path/to/postmortems

It reports, per-strategy:
  - hit_rate: fraction of postmortems where the predicted window contains
    the ground-truth incident timestamp.
  - iou: intersection-over-union between predicted and ground-truth windows.
  - offset_min: distance in minutes between predicted and ground-truth
    window midpoints.

Strategies compared:
  - naive_page       : 30-min window centered on the first PagerDuty page.
  - naive_deploy     : 30-min window starting 10 min after the last deploy.
  - vendor_only      : window = span of the first vendor_anomaly.
  - selector         : the full `select_window.py` — all five priors fused.
  - selector_no_vendor: ablation — selector without vendor_anomalies, to
                       measure how much vendor AIOps is actually worth.

Expected corpus format
----------------------
Each file `postmortems/INC-*.json`:

    {
      "incident_id": "INC-REAL-042",
      "ground_truth": {
        "start": "2026-03-14T14:05:00Z",
        "end":   "2026-03-14T14:35:00Z"
      },
      "ticket": { "summary": "...", "description": "..." },
      "candidate_services": ["checkout-service"],
      "lookback_hours": 24,
      "metrics": { ... same shape as fixture ... },
      "deploys": [ ... ],
      "pages":   [ ... ],
      "vendor_anomalies": [ ... optional; omit to test without vendor input ... ]
    }

The `ground_truth` field is what you as the operator labeled from the
human-written postmortem. If you don't have labeled postmortems yet, the
most productive first ~20 take about half a day each to label by hand
from Confluence history.

Usage
-----
    python3 scripts/benchmark_window_selector.py --corpus path/to/dir
    python3 scripts/benchmark_window_selector.py --corpus path/to/dir --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SELECTOR = ROOT / ".claude" / "skills" / "time-window-selector" / "scripts" / "select_window.py"


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────
def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iou_minutes(a_start: datetime, a_end: datetime,
                b_start: datetime, b_end: datetime) -> float:
    """Intersection-over-union of two time intervals (in minutes)."""
    inter = max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds() / 60.0)
    union = (max(a_end, b_end) - min(a_start, b_start)).total_seconds() / 60.0
    return (inter / union) if union > 0 else 0.0


def contains(start: datetime, end: datetime, t: datetime) -> bool:
    return start <= t <= end


# ─────────────────────────────────────────────────────────────────────────────
# Strategies
# ─────────────────────────────────────────────────────────────────────────────
def strategy_naive_page(pm: dict) -> tuple[datetime, datetime] | None:
    pages = pm.get("pages") or []
    if not pages:
        return None
    first = parse_ts(pages[0]["paged_at"])
    return first - timedelta(minutes=15), first + timedelta(minutes=15)


def strategy_naive_deploy(pm: dict) -> tuple[datetime, datetime] | None:
    deploys = pm.get("deploys") or []
    if not deploys:
        return None
    last_deploy = max(parse_ts(d["merged_at"]) for d in deploys)
    return last_deploy + timedelta(minutes=10), last_deploy + timedelta(minutes=40)


def strategy_vendor_only(pm: dict) -> tuple[datetime, datetime] | None:
    vas = pm.get("vendor_anomalies") or []
    if not vas:
        return None
    va = vas[0]
    return parse_ts(va["start"]), parse_ts(va["end"])


def strategy_selector(pm: dict, include_vendor: bool = True) -> tuple[datetime, datetime] | None:
    """Run the actual selector. Returns top window, or None on failure."""
    payload = {
        "ticket":   pm["ticket"],
        "lookback_hours": pm.get("lookback_hours", 24),
        "metrics":  pm.get("metrics", {}),
        "deploys":  pm.get("deploys", []),
        "pages":    pm.get("pages", []),
    }
    if include_vendor and pm.get("vendor_anomalies"):
        payload["vendor_anomalies"] = pm["vendor_anomalies"]
    try:
        proc = subprocess.run(
            ["python3", str(SELECTOR)],
            input=json.dumps(payload).encode(),
            capture_output=True, timeout=180,
        )
        if proc.returncode != 0:
            return None
        out = json.loads(proc.stdout or b"{}")
        windows = out.get("windows") or []
        if not windows:
            return None
        top = windows[0]
        return parse_ts(top["start"]), parse_ts(top["end"])
    except Exception:
        return None


STRATEGIES: dict[str, Any] = {
    "naive_page":          strategy_naive_page,
    "naive_deploy":        strategy_naive_deploy,
    "vendor_only":         strategy_vendor_only,
    "selector":            lambda pm: strategy_selector(pm, include_vendor=True),
    "selector_no_vendor":  lambda pm: strategy_selector(pm, include_vendor=False),
}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
def score_one(pred: tuple[datetime, datetime] | None,
              gt_start: datetime, gt_end: datetime) -> dict:
    if pred is None:
        return {"hit": False, "iou": 0.0, "offset_min": None, "applicable": False}
    p_start, p_end = pred
    gt_mid  = gt_start + (gt_end - gt_start) / 2
    p_mid   = p_start + (p_end - p_start) / 2
    return {
        "hit":        contains(p_start, p_end, gt_mid),
        "iou":        iou_minutes(p_start, p_end, gt_start, gt_end),
        "offset_min": abs((p_mid - gt_mid).total_seconds()) / 60.0,
        "applicable": True,
    }


def aggregate(scores: list[dict]) -> dict:
    applicable = [s for s in scores if s["applicable"]]
    if not applicable:
        return {"n": 0, "applicable": 0, "hit_rate": None, "iou_median": None,
                "offset_median_min": None}
    hits  = sum(1 for s in applicable if s["hit"])
    ious  = [s["iou"] for s in applicable]
    offs  = [s["offset_min"] for s in applicable if s["offset_min"] is not None]
    return {
        "n":                len(scores),
        "applicable":       len(applicable),
        "hit_rate":         hits / len(applicable),
        "iou_median":       statistics.median(ious) if ious else None,
        "iou_mean":         statistics.mean(ious) if ious else None,
        "offset_median_min": statistics.median(offs) if offs else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Corpus loader
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_FIELDS = ("incident_id", "ground_truth", "ticket")


def load_corpus(path: Path) -> list[dict]:
    """Load all *.json files in `path` that conform to the expected schema."""
    if not path.is_dir():
        raise SystemExit(f"corpus directory not found: {path}")
    postmortems = []
    for f in sorted(path.glob("*.json")):
        try:
            pm = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            print(f"  [SKIP] {f.name}: invalid JSON ({e})", file=sys.stderr)
            continue
        missing = [k for k in REQUIRED_FIELDS if k not in pm]
        if missing:
            print(f"  [SKIP] {f.name}: missing fields {missing}", file=sys.stderr)
            continue
        gt = pm["ground_truth"]
        if "start" not in gt or "end" not in gt:
            print(f"  [SKIP] {f.name}: ground_truth missing start/end", file=sys.stderr)
            continue
        postmortems.append(pm)
    return postmortems


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path,
                    help="Directory of labeled postmortem JSONs")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of the table")
    ap.add_argument("--scaffold", action="store_true",
                    help="Write an example postmortem JSON to show the expected format")
    args = ap.parse_args()

    if args.scaffold:
        example = {
            "incident_id": "INC-EXAMPLE-001",
            "ground_truth": {
                "start": "2026-03-14T14:05:00Z",
                "end":   "2026-03-14T14:35:00Z",
                "_label_source": "manually labeled from Confluence postmortem",
            },
            "ticket": {
                "id": "INC-EXAMPLE-001",
                "summary": "checkout failing for some users",
                "description": "saw errors this afternoon",
                "reported_at": "2026-03-14T14:50:00Z",
            },
            "candidate_services": ["checkout-service"],
            "lookback_hours": 24,
            "metrics": {
                "checkout-service": {
                    "error_rate": [{"t": "2026-03-14T14:00:00Z", "v": 0.01}],
                    "p99_latency": [{"t": "2026-03-14T14:00:00Z", "v": 120.0}],
                }
            },
            "deploys": [
                {"repo": "checkout-service", "sha": "abc123",
                 "merged_at": "2026-03-14T13:58:00Z"}
            ],
            "pages": [
                {"service": "checkout-service", "paged_at": "2026-03-14T14:12:00Z"}
            ],
            "vendor_anomalies": [
                {"service": "checkout-service", "metric": "error_rate",
                 "severity": "HIGH",
                 "start": "2026-03-14T14:05:00Z",
                 "end":   "2026-03-14T14:35:00Z",
                 "source": "datadog_watchdog"}
            ],
        }
        print(json.dumps(example, indent=2))
        return 0

    if not args.corpus:
        print(__doc__, file=sys.stderr)
        print("\nERROR: --corpus is required (or use --scaffold to see the format)",
              file=sys.stderr)
        return 2

    pms = load_corpus(args.corpus)
    if not pms:
        print(f"No valid postmortems found in {args.corpus}", file=sys.stderr)
        return 2

    print(f"Loaded {len(pms)} postmortem(s) from {args.corpus}", file=sys.stderr)

    results: dict[str, list[dict]] = {name: [] for name in STRATEGIES}
    for pm in pms:
        gt = pm["ground_truth"]
        gt_start, gt_end = parse_ts(gt["start"]), parse_ts(gt["end"])
        for name, fn in STRATEGIES.items():
            pred = fn(pm)
            results[name].append(score_one(pred, gt_start, gt_end))

    summary = {name: aggregate(scores) for name, scores in results.items()}

    if args.json:
        print(json.dumps({"corpus_size": len(pms), "results": summary}, indent=2))
        return 0

    # Human-readable table
    print()
    print(f"{'strategy':<22s} {'n':>4s} {'appl':>5s} {'hit%':>8s} "
          f"{'iou_med':>9s} {'offset_med_min':>15s}")
    print("-" * 70)
    for name, s in summary.items():
        hit = f"{s['hit_rate']*100:6.1f}%" if s["hit_rate"] is not None else "   n/a"
        iou = f"{s['iou_median']:7.3f}"   if s["iou_median"] is not None else "    n/a"
        off = f"{s['offset_median_min']:13.1f}" if s["offset_median_min"] is not None else "          n/a"
        print(f"{name:<22s} {s['n']:>4d} {s['applicable']:>5d} {hit:>8s} {iou:>9s} {off:>15s}")
    print()
    print(f"(hit% = window contains ground-truth midpoint; "
          f"iou_med = median intersection-over-union; "
          f"offset_med_min = median distance between window midpoints)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
