#!/usr/bin/env python3
"""
time-window-selector — pick the most likely incident window from a ticket.

Input (stdin JSON):
{
  "ticket": {...},
  "candidate_services": [...],
  "lookback_hours": 24,
  "metrics": {  # pre-pulled by the signals agent via MCP, passed in
    "<service>": {
      "error_rate":   [{"t": "2026-04-18T08:00Z", "v": 0.01}, ...],
      "p99_latency":  [...],
      "cpu":          [...],
      "memory":       [...],
      "throughput":   [...]
    }
  },
  "deploys": [{"repo": "...", "merged_at": "2026-04-18T08:42Z", "sha": "..."}],
  "pages":   [{"service": "...", "paged_at": "2026-04-18T08:51Z"}],
  "vendor_anomalies": [   # NEW — Datadog Watchdog / Dynatrace Davis output
    {"service": "inventory-service",
     "metric":  "error_rate",
     "severity": "HIGH",          # CRITICAL | HIGH | MEDIUM | LOW
     "start":   "2026-04-18T08:30:00Z",
     "end":     "2026-04-18T08:50:00Z",
     "source":  "datadog_watchdog"}
  ]
}

Vendor anomalies are the highest-weight prior. Jothi's explicit direction
("Datadog / Dynatrace have an AIOps module — use it"): when Watchdog or
Davis flags a window, the selector must honor it. This is the wiring that
lets the fallback anomaly-ensemble skill stay a fallback — if the vendor
already spoke, we trust them.

Output (stdout JSON): see SKILL.md output schema.

Wraps agents/algorithms/cusum.py. Runs entirely offline — no MCP calls here.
The MCP data is fetched by the signals agent / orchestrator and passed in.
"""
from __future__ import annotations
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The project's own agents/*.py modules import via `from algorithms.X` — so
# `agents/` (not the repo root) must be on sys.path. Match that convention.
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]                # .claude/skills/.../scripts -> repo root
sys.path.insert(0, str(REPO_ROOT / "agents"))

import numpy as np  # required by CUSUMDetector's fit()/detect()

from algorithms.cusum import CUSUMDetector  # type: ignore


# CUSUM reports magnitude as `(x - mean) / std`. When the baseline is nearly
# flat, upstream cusum.py clamps std to 1e-10 to avoid div-by-zero, which then
# blows magnitude up to ~1e9. We don't alter the upstream algorithm; we just
# floor std against the baseline's dynamic range and clip the reported value
# to a sane ceiling so downstream JSON stays readable.
_MAGNITUDE_CEILING = 20.0  # 20 sigma is already absurd; anything more is noise


def detect_change_points(values):
    """
    Thin wrapper around CUSUMDetector so this script's scoring code can stay
    simple. Uses the first 25% of the series (min 10 points) as baseline and
    runs detection on the full series. Returns a list of ChangePoint objects
    with .index, .direction, .magnitude, .severity. Magnitude is clamped to
    [-_MAGNITUDE_CEILING, _MAGNITUDE_CEILING] to protect against near-zero
    baseline variance blow-up.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size < 10:
        return []
    baseline_end = max(10, arr.size // 4)
    baseline = arr[:baseline_end]
    detector = CUSUMDetector()
    try:
        detector.fit(baseline)
        result = detector.detect(arr)
    except Exception:
        return []
    # Clamp ridiculous magnitudes in place. (ChangePoint is a dataclass; we
    # mutate its field rather than rebuild the list.)
    for cp in (result.change_points or []):
        try:
            m = float(cp.magnitude)
        except (TypeError, ValueError):
            m = 0.0
        if m != m or abs(m) > _MAGNITUDE_CEILING:  # NaN or > ceiling
            cp.magnitude = float(np.sign(m) * _MAGNITUDE_CEILING) if m == m else 0.0
    return result.change_points or []


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerant of trailing 'Z'."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ticket_time_hints(text: str, reported_at: datetime) -> list[tuple[datetime, float]]:
    """
    Very lightweight ticket-text parser. Returns (anchor_time, weight) pairs.
    Examples:
      "since this morning"   -> anchor at 08:00 local, weight 0.3
      "started after lunch"  -> anchor at 13:00 local, weight 0.3
      "after the deploy"     -> no direct anchor; deploy evidence will carry it
      "just now" / "right now" -> anchor at reported_at, weight 0.8
    """
    hints: list[tuple[datetime, float]] = []
    t = text.lower()
    r = reported_at
    day0 = r.replace(hour=0, minute=0, second=0, microsecond=0)
    if "just now" in t or "right now" in t or "happening now" in t:
        hints.append((r, 0.8))
    if "this morning" in t:
        hints.append((day0 + timedelta(hours=8), 0.3))
    if "after lunch" in t or "this afternoon" in t:
        hints.append((day0 + timedelta(hours=13), 0.3))
    if "overnight" in t or "last night" in t:
        hints.append((day0 - timedelta(hours=2), 0.4))
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and hh < 12: hh += 12
        if ap == "am" and hh == 12: hh = 0
        hints.append((day0 + timedelta(hours=hh, minutes=mm), 0.6))
    return hints


def gaussian_bump(center: datetime, width_min: float, grid: list[datetime], weight: float, skew: float = 0.0) -> list[float]:
    """
    Bump centered at `center`, standard deviation `width_min` minutes, optional skew.
    skew > 0 → right-skewed (incident after deploy).
    skew < 0 → left-skewed (incident before page).
    """
    out = []
    for t in grid:
        delta_min = (t - center).total_seconds() / 60.0
        if skew != 0:
            if (skew > 0 and delta_min < 0) or (skew < 0 and delta_min > 0):
                delta_min *= (1.0 + abs(skew) * 1.5)  # squash the "wrong" side
        out.append(weight * math.exp(-0.5 * (delta_min / width_min) ** 2))
    return out


def normalize_series(series: list[dict]) -> tuple[list[datetime], list[float]]:
    """[{t, v}, ...] → (timestamps, values). Returns ([], []) on empty."""
    if not series:
        return [], []
    ts = [parse_ts(p["t"]) for p in series]
    vs = [float(p["v"]) for p in series]
    return ts, vs


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring routine
# ─────────────────────────────────────────────────────────────────────────────
def score_windows(payload: dict) -> dict:
    ticket = payload.get("ticket", {})
    lookback_hours = int(payload.get("lookback_hours", 24))
    reported_at_s = ticket.get("reported_at")
    reported_at = parse_ts(reported_at_s) if reported_at_s else datetime.now(timezone.utc)

    window_end = reported_at
    window_start = window_end - timedelta(hours=lookback_hours)

    # Build a 5-minute grid across the lookback
    grid: list[datetime] = []
    t = window_start
    while t <= window_end:
        grid.append(t)
        t += timedelta(minutes=5)
    if not grid:
        return {"windows": [], "fallback_window": None, "coverage_warning": "empty_grid"}

    # Posterior in log space — start from uniform prior
    log_post = [0.0] * len(grid)

    # ── Evidence 1: CUSUM change points over metric series ──
    change_points_found: list[dict] = []
    metrics = payload.get("metrics") or {}
    for service, series_map in metrics.items():
        for metric_name, series in (series_map or {}).items():
            ts, vs = normalize_series(series)
            if len(vs) < 10:
                continue
            try:
                cps = detect_change_points(vs)
            except Exception:
                continue
            for cp in cps:
                # cp has .index, .direction, .magnitude, .severity
                if cp.index >= len(ts): continue
                cp_time = ts[cp.index]
                if not (window_start <= cp_time <= window_end): continue
                weight = {"CRITICAL": 1.2, "HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3}.get(cp.severity, 0.4)
                bump = gaussian_bump(cp_time, width_min=15, grid=grid, weight=weight)
                for i, b in enumerate(bump):
                    log_post[i] += math.log1p(b)
                change_points_found.append({
                    "service": service, "metric": metric_name,
                    "t": iso(cp_time), "direction": cp.direction,
                    "magnitude": float(cp.magnitude), "severity": cp.severity,
                })

    # ── Evidence 2: Deploys (right-skewed — incident happens AFTER deploy) ──
    deploys = payload.get("deploys") or []
    for d in deploys:
        try: dt = parse_ts(d["merged_at"])
        except Exception: continue
        if not (window_start <= dt <= window_end): continue
        bump = gaussian_bump(dt + timedelta(minutes=10), width_min=30, grid=grid, weight=0.7, skew=+0.6)
        for i, b in enumerate(bump): log_post[i] += math.log1p(b)

    # ── Evidence 3: Pages (left-skewed — incident starts BEFORE page) ──
    pages = payload.get("pages") or []
    for p in pages:
        try: pt = parse_ts(p["paged_at"])
        except Exception: continue
        if not (window_start <= pt <= window_end): continue
        bump = gaussian_bump(pt - timedelta(minutes=5), width_min=20, grid=grid, weight=0.8, skew=-0.5)
        for i, b in enumerate(bump): log_post[i] += math.log1p(b)

    # ── Evidence 4: Vendor AIOps anomalies (Datadog Watchdog, Dynatrace Davis) ──
    #   Highest-weight prior. Jothi was explicit: "Datadog and Dynatrace have
    #   an AIOps module — use it." When the vendor flags a window, we honor
    #   it. Severity scales the weight; anomaly span provides the center
    #   (midpoint), and we left-skew slightly because the flag trails the
    #   actual regime shift.
    vendor_anomalies_used: list[dict] = []
    vendor_anomalies = payload.get("vendor_anomalies") or []
    for va in vendor_anomalies:
        try:
            a_start = parse_ts(va["start"])
            a_end   = parse_ts(va.get("end") or va["start"])
        except Exception:
            continue
        # keep only anomalies whose midpoint lies inside the lookback
        mid = a_start + (a_end - a_start) / 2
        if not (window_start <= mid <= window_end):
            continue
        sev = (va.get("severity") or "HIGH").upper()
        w = {"CRITICAL": 1.3, "HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}.get(sev, 1.0)
        span_min = max(5.0, (a_end - a_start).total_seconds() / 60.0)
        # width scales with the reported anomaly span, capped so a multi-hour
        # vendor flag doesn't smear the posterior flat
        width = min(25.0, max(10.0, span_min / 2.0))
        # small left skew: the vendor flag lags the onset
        bump = gaussian_bump(mid - timedelta(minutes=3), width_min=width,
                             grid=grid, weight=w, skew=-0.3)
        for i, b in enumerate(bump):
            log_post[i] += math.log1p(b)
        vendor_anomalies_used.append({
            "service": va.get("service"),
            "metric":  va.get("metric"),
            "severity": sev,
            "start":   iso(a_start),
            "end":     iso(a_end),
            "source":  va.get("source") or "vendor_aiops",
        })

    # ── Evidence 5: Ticket text anchors ──
    text = ticket.get("raw_text_for_tws") or (ticket.get("title", "") + " " + ticket.get("description", ""))
    for anchor, w in parse_ticket_time_hints(text, reported_at):
        if not (window_start <= anchor <= window_end): continue
        bump = gaussian_bump(anchor, width_min=20, grid=grid, weight=w)
        for i, b in enumerate(bump): log_post[i] += math.log1p(b)

    # Convert back to linear and normalize
    max_lp = max(log_post)
    lin = [math.exp(lp - max_lp) for lp in log_post]
    s = sum(lin) or 1.0
    post = [x / s for x in lin]

    # Find top-3 windows (30-min each) with highest summed posterior
    window_len = 6  # 6 * 5min = 30 minutes
    sums = []
    for i in range(len(post) - window_len + 1):
        sums.append((sum(post[i:i + window_len]), i))
    sums.sort(reverse=True)

    results = []
    seen_centers: list[int] = []
    for sum_val, idx in sums:
        # de-dup — skip windows whose center is within 20 min of an already-chosen center
        center = idx + window_len // 2
        if any(abs(center - c) < 4 for c in seen_centers): continue
        seen_centers.append(center)
        start = grid[idx]; end = grid[min(idx + window_len - 1, len(grid) - 1)]
        conf = min(1.0, float(sum_val * len(post) / window_len))  # normalize against uniform baseline
        # Collect supporting evidence local to this window
        supp_cps = [c for c in change_points_found if start <= parse_ts(c["t"]) <= end]
        supp_deploys = [d for d in deploys if d.get("merged_at") and (start - timedelta(minutes=30)) <= parse_ts(d["merged_at"]) <= end]
        supp_pages = [p for p in pages if p.get("paged_at") and start <= parse_ts(p["paged_at"]) <= (end + timedelta(minutes=30))]
        # Vendor anomaly is "in" this window if its interval overlaps at all.
        supp_vendor = []
        for va in vendor_anomalies_used:
            try:
                va_s = parse_ts(va["start"])
                va_e = parse_ts(va["end"])
            except Exception:
                continue
            if va_e >= start and va_s <= end:
                supp_vendor.append(va)
        rationale_parts = []
        if supp_vendor: rationale_parts.append(
            f"{len(supp_vendor)} vendor AIOps anomaly(s) "
            f"[{', '.join(sorted({v.get('source', 'vendor') for v in supp_vendor}))}]"
        )
        if supp_cps: rationale_parts.append(f"{len(supp_cps)} CUSUM shift(s)")
        if supp_deploys: rationale_parts.append(f"{len(supp_deploys)} deploy(s) nearby")
        if supp_pages: rationale_parts.append(f"{len(supp_pages)} page(s)")
        if not rationale_parts: rationale_parts.append("ticket-text anchor")
        results.append({
            "start": iso(start), "end": iso(end),
            "confidence": round(conf, 3),
            "rationale": "; ".join(rationale_parts),
            "supporting_evidence": {
                "vendor_anomalies": supp_vendor,
                "change_points": supp_cps,
                "deploys": supp_deploys,
                "pages": supp_pages,
            },
        })
        if len(results) >= 3: break

    # Fallback: reported_at ± 30 min
    fallback = {
        "start": iso(reported_at - timedelta(minutes=30)),
        "end":   iso(reported_at + timedelta(minutes=0)),
    }

    coverage_warning = None
    if (not change_points_found and not deploys and not pages
            and not vendor_anomalies_used):
        coverage_warning = "no_evidence_available"

    return {
        "windows": results,
        "fallback_window": fallback,
        "coverage_warning": coverage_warning,
    }


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "no_input_received"}))
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid_json: {e}"}))
        return
    try:
        result = score_windows(payload)
    except Exception as e:
        print(json.dumps({"error": f"scoring_failed: {type(e).__name__}: {e}"}))
        return
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
