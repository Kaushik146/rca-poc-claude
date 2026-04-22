#!/usr/bin/env python3
"""
fixture_harness — offline regression test for the RCA pipeline.

Runs each skill's script with canned MCP-shaped input from
.claude/fixtures/<ID>/ and asserts the structural invariants declared in
expected.json. Exits non-zero on any failure.

Why this exists
---------------
The pipeline's integration surface is big (6 MCPs × 4 agent phases) and we
can't dry-run it without real enterprise credentials. But the *skill
scripts* are the deterministic seams — if they break, nothing downstream
works. This harness exercises those seams with no network calls, so CI can
regress the IP on every PR. It is NOT a replacement for a live-MCP
dry-run; it's the cheap continuous check that catches schema drift before
the expensive live-MCP run.

Usage
-----
  python3 scripts/fixture_harness.py                    # run default fixture
  python3 scripts/fixture_harness.py INC-DEMO-001       # explicit
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / ".claude" / "fixtures"
SKILLS = ROOT / ".claude" / "skills"


class HarnessFail(Exception):
    pass


def run_skill(script: Path, payload: dict, timeout: int = 60) -> dict:
    """Invoke a skill script, pass JSON on stdin, parse JSON on stdout."""
    proc = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise HarnessFail(
            f"{script.name} exited {proc.returncode}\n"
            f"stderr:\n{proc.stderr.decode(errors='replace')}"
        )
    try:
        return json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as e:
        raise HarnessFail(f"{script.name} stdout is not JSON: {e}\n{proc.stdout!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-skill checks
# ─────────────────────────────────────────────────────────────────────────────
def check_module_router(fix: Path, exp: dict) -> str:
    payload = {
        "ticket_text": exp["ticket_text_used"],
        "known_services": [
            "inventory-service", "checkout-service",
            "payment-service", "notification-service", "java-order-service",
        ],
    }
    out = run_skill(SKILLS / "module-router" / "scripts" / "route.py", payload)
    if out.get("needs_disambiguation") and not exp.get("must_not_disambiguate", False):
        raise HarnessFail(f"module-router flagged disambiguation: {out}")
    routed = out.get("routed") or []
    if not routed:
        raise HarnessFail(f"module-router returned no routes: {out}")
    if routed[0]["service"] != exp["top_service"]:
        raise HarnessFail(
            f"module-router: expected top={exp['top_service']}, got {routed[0]['service']}"
        )
    return f"top={routed[0]['service']} (score={routed[0]['score']})"


def _tws_payload(fix: Path, *, include_vendor: bool) -> dict:
    """Build a time-window-selector payload from the fixture. Optionally
    include vendor_anomalies so we can compare with/without the Watchdog
    prior and verify it's first-class."""
    ticket = json.loads((fix / "ticket.json").read_text())
    metrics = json.loads((fix / "metrics.json").read_text())
    deploys = json.loads((fix / "deploys.json").read_text())["deploys"]
    pages = json.loads((fix / "pages.json").read_text())["pages"]
    payload = {
        "ticket": ticket,
        "lookback_hours": 12,
        "metrics": metrics,
        "deploys": deploys,
        "pages": pages,
    }
    if include_vendor:
        vendor_path = fix / "vendor_anomalies.json"
        if vendor_path.exists():
            payload["vendor_anomalies"] = json.loads(vendor_path.read_text())["vendor_anomalies"]
    return payload


def check_time_window_selector(fix: Path, exp: dict) -> str:
    out = run_skill(
        SKILLS / "time-window-selector" / "scripts" / "select_window.py",
        _tws_payload(fix, include_vendor=False), timeout=120,
    )
    windows = out.get("windows") or []
    if not windows:
        raise HarnessFail(f"time-window-selector produced no windows: {out}")
    top = windows[0]
    if top["confidence"] < exp["min_confidence"]:
        raise HarnessFail(
            f"time-window-selector: top confidence {top['confidence']} "
            f"< threshold {exp['min_confidence']}"
        )
    target = exp["top_window_contains_utc"]
    if not (top["start"] <= target <= top["end"]):
        raise HarnessFail(
            f"time-window-selector: {target} not within top window "
            f"[{top['start']}, {top['end']}]"
        )
    for req in exp.get("supporting_evidence_required", []):
        evidence = top.get("supporting_evidence", {}).get(req) or []
        if not evidence:
            raise HarnessFail(
                f"time-window-selector: missing supporting evidence '{req}' in top window"
            )
    # Magnitude clamp regression
    for w in windows:
        for cp in w.get("supporting_evidence", {}).get("change_points", []):
            if abs(cp.get("magnitude", 0)) > 20.000001:
                raise HarnessFail(
                    f"magnitude clamp broken: {cp['magnitude']} > 20 (cusum near-zero-var bug)"
                )
    return f"top=[{top['start']}, {top['end']}] conf={top['confidence']}"


def check_time_window_selector_vendor(fix: Path, exp: dict) -> str:
    """
    Rerun the selector WITH vendor_anomalies and verify Watchdog is a
    first-class prior: the top window still contains the target timestamp,
    vendor evidence shows up in supporting_evidence, and confidence does
    not regress vs the no-vendor baseline. This is the check that proves
    Datadog Watchdog / Dynatrace Davis output is actually fused in, not
    just logged alongside.
    """
    if not (fix / "vendor_anomalies.json").exists():
        raise HarnessFail("vendor_anomalies.json missing from fixture")

    baseline_out = run_skill(
        SKILLS / "time-window-selector" / "scripts" / "select_window.py",
        _tws_payload(fix, include_vendor=False), timeout=120,
    )
    vendor_out = run_skill(
        SKILLS / "time-window-selector" / "scripts" / "select_window.py",
        _tws_payload(fix, include_vendor=True), timeout=120,
    )
    base_windows = baseline_out.get("windows") or []
    vend_windows = vendor_out.get("windows") or []
    if not base_windows or not vend_windows:
        raise HarnessFail(
            f"selector produced no windows (baseline={len(base_windows)}, "
            f"vendor={len(vend_windows)})"
        )
    top = vend_windows[0]
    target = exp["top_window_contains_utc"]
    if not (top["start"] <= target <= top["end"]):
        raise HarnessFail(
            f"with-vendor: {target} not within top window "
            f"[{top['start']}, {top['end']}]"
        )
    if exp.get("must_surface_vendor_evidence"):
        va = top.get("supporting_evidence", {}).get("vendor_anomalies") or []
        if not va:
            raise HarnessFail(
                "with-vendor: top window carries no vendor_anomalies in "
                "supporting_evidence — Watchdog is not first-class"
            )
    if exp.get("confidence_must_not_regress_vs_baseline"):
        base_conf = base_windows[0]["confidence"]
        vend_conf = top["confidence"]
        if vend_conf + 1e-6 < base_conf:
            raise HarnessFail(
                f"with-vendor confidence regressed: baseline={base_conf} "
                f"vendor={vend_conf}"
            )
    return (
        f"baseline_conf={base_windows[0]['confidence']} "
        f"vendor_conf={top['confidence']} "
        f"vendor_hits={len(top.get('supporting_evidence', {}).get('vendor_anomalies') or [])}"
    )


def check_bm25_rerank(fix: Path, exp: dict) -> str:
    candidates = json.loads((fix / "candidates.json").read_text())["candidates"]
    payload = {
        "query": exp["query_used"],
        "candidates": candidates,
        "top_k": 3,
    }
    out = run_skill(SKILLS / "bm25-rerank" / "scripts" / "rerank.py", payload)
    reranked = out.get("reranked") or []
    if not reranked:
        raise HarnessFail(f"bm25-rerank returned nothing: {out}")
    top = reranked[0]
    if top["id"] != exp["top_candidate_id"]:
        raise HarnessFail(
            f"bm25-rerank: expected top={exp['top_candidate_id']}, got {top['id']}"
        )
    if top["score"] < exp["top_candidate_min_score"]:
        raise HarnessFail(
            f"bm25-rerank: top score {top['score']} < threshold {exp['top_candidate_min_score']}"
        )
    return f"top={top['id']} (score={top['score']})"


def check_anomaly_ensemble(fix: Path, exp: dict) -> str:
    # Minimal smoke check — AnomalyDetector is heavy (training on first run,
    # cached thereafter), so we send a tiny batch and assert we get JSON back.
    payload = {
        "features_by_service": {
            "inventory-service": [
                {"cpu_pct": 0.31, "error_rate": 0.005, "latency_p99": 120.0,
                 "throughput": 850.0, "mem_pct": 0.52, "db_query_ms": 40.0},
                {"cpu_pct": 0.82, "error_rate": 0.12, "latency_p99": 340.0,
                 "throughput": 300.0, "mem_pct": 0.91, "db_query_ms": 220.0},
            ]
        }
    }
    out = run_skill(
        SKILLS / "anomaly-ensemble" / "scripts" / "detect.py", payload, timeout=180
    )
    if "anomalies" not in out:
        raise HarnessFail(f"anomaly-ensemble missing 'anomalies' key: {out}")
    return f"{len(out['anomalies'])} anomalies"


def check_cross_agent_validator(fix: Path, exp: dict) -> str:
    """
    Contrive a contradiction: fix_and_test claims files under
    'payment-service/' but signals only covered 'inventory-service'. The
    validator should flag this as a cross-phase contradiction.

    Payload shape matches the validator's actual contract (see
    .claude/skills/cross-agent-validator/scripts/validate.py): phase uses
    snake_case, `output`+`prior_outputs` keys, files_changed list.
    """
    payload = {
        "phase": "fix_and_test",
        "output": {
            "pr_url": "https://example/pr/1",
            "fix_applied": True,
            "files_changed": ["payment-service/src/main/java/Gateway.java"],
            "test_results": {"passed": 12, "failed": 0},
        },
        "prior_outputs": {
            "intake":  {"affected_components": ["inventory-service"]},
            "signals": {"logs": {"inventory-service": []},
                        "metrics": {"inventory-service": {}}},
        },
    }
    out = run_skill(
        SKILLS / "cross-agent-validator" / "scripts" / "validate.py", payload
    )
    blocking = out.get("blocking_warnings", []) or []
    if exp.get("contradiction_warning_expected") and not any("contradiction" in b for b in blocking):
        raise HarnessFail(
            f"validator missed the contradiction "
            f"(files touch payment-service, signals covered inventory-service): {out}"
        )
    return f"{len(blocking)} blocking warning(s) (expected contradiction flagged)"


# (name, fn, fatal_on_failure)
CHECKS = [
    ("module_router",               check_module_router,               True),
    ("time_window_selector",        check_time_window_selector,        True),
    ("time_window_selector_vendor", check_time_window_selector_vendor, True),
    ("bm25_rerank",                 check_bm25_rerank,                 True),
    ("anomaly_ensemble",            check_anomaly_ensemble,            True),
    ("cross_agent_validator",       check_cross_agent_validator,       True),
]


def main() -> int:
    fixture_id = sys.argv[1] if len(sys.argv) > 1 else "INC-DEMO-001"
    fix = FIXTURES / fixture_id
    if not fix.is_dir():
        print(f"ERROR: fixture {fix} not found", file=sys.stderr)
        return 2
    expected = json.loads((fix / "expected.json").read_text())

    print(f"=== Fixture harness: {fixture_id} ===\n")
    failures = 0
    warns = 0
    for key, fn, fatal in CHECKS:
        exp = expected.get(key, {})
        try:
            summary = fn(fix, exp)
            print(f"  [OK]   {key:<24s}  {summary}")
        except (HarnessFail, Exception) as e:  # noqa: BLE001
            label = "FAIL" if fatal else "WARN"
            if fatal:
                failures += 1
            else:
                warns += 1
            cause = e if isinstance(e, HarnessFail) else f"unexpected: {type(e).__name__}: {e}"
            print(f"  [{label}] {key:<24s}  {cause}")

    print()
    if failures:
        print(f"FAILED: {failures} fatal, {warns} warning(s) across {len(CHECKS)} checks")
        return 1
    print(f"PASSED: {len(CHECKS) - warns} / {len(CHECKS)} fatal checks"
          + (f" ({warns} non-fatal warning(s))" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
