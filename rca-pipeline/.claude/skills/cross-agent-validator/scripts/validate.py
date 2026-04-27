#!/usr/bin/env python3
"""
cross-agent-validator — schema + cross-phase consistency gate.

Input (stdin JSON):
{
  "phase": "intake" | "signals" | "prior_incident" | "fix_and_test",
  "output": { ... },
  "prior_outputs": { "intake": {...}, "signals": {...} }   # optional
}

Output (stdout JSON):
{
  "passed": true/false,
  "cleaned_output": {...},
  "normalizing_warnings": [...],
  "blocking_warnings": [...]
}
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]
sys.path.insert(0, str(REPO_ROOT))

# Reuse the existing validators from the Python project.
from agents import validation as V  # type: ignore


def validate_intake(out: dict) -> tuple[dict, list[str], list[str]]:
    norm, block = [], []
    cleaned = dict(out) if isinstance(out, dict) else {}
    if not cleaned.get("ticket_id"): block.append("intake: ticket_id missing")
    if not cleaned.get("raw_text_for_tws"):
        cleaned["raw_text_for_tws"] = (cleaned.get("title", "") + " " + cleaned.get("description", "")).strip()
        norm.append("intake: raw_text_for_tws derived from title + description")
    if not cleaned.get("affected_components"):
        cleaned["affected_components"] = []
        norm.append("intake: affected_components defaulted to []")
    return cleaned, norm, block


def validate_signals(out: dict) -> tuple[dict, list[str], list[str]]:
    norm, block = [], []
    cleaned = dict(out) if isinstance(out, dict) else {}
    logs = cleaned.get("logs") or {}
    traces = cleaned.get("traces") or []
    metrics = cleaned.get("metrics") or {}
    if not logs and not traces and not metrics:
        block.append("signals: no logs, traces, or metrics returned — coverage=empty")
    # Run the existing log-anomaly validator on whatever logs came back (flattened)
    flat = []
    for svc, entries in logs.items():
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict):
                    flat.append({**e, "service": e.get("service", svc)})
    if flat:
        _, w = V.validate_log_anomalies(flat)
        norm.extend([f"signals.logs: {x}" for x in w])
    return cleaned, norm, block


def validate_prior_incident(out: dict) -> tuple[dict, list[str], list[str]]:
    norm, block = [], []
    cleaned = dict(out) if isinstance(out, dict) else {}
    if "matches" not in cleaned:
        cleaned["matches"] = []
        norm.append("prior_incident: matches defaulted to []")
    if "novelty_flag" not in cleaned:
        cleaned["novelty_flag"] = len(cleaned["matches"]) == 0
        norm.append("prior_incident: novelty_flag derived from empty matches")
    return cleaned, norm, block


def validate_fix_and_test(out: dict) -> tuple[dict, list[str], list[str]]:
    norm, block = [], []
    cleaned = dict(out) if isinstance(out, dict) else {}
    if cleaned.get("fix_applied") and not cleaned.get("pr_url"):
        block.append("fix_and_test: fix_applied=true but no pr_url present")
    if "test_results" not in cleaned:
        block.append("fix_and_test: test_results missing")
    return cleaned, norm, block


PHASE_DISPATCH = {
    "intake":          validate_intake,
    "signals":         validate_signals,
    "prior_incident":  validate_prior_incident,
    "fix_and_test":    validate_fix_and_test,
}


def cross_phase_checks(phase: str, cleaned: dict, prior: dict) -> list[str]:
    """Return blocking contradictions across phases."""
    block: list[str] = []
    if phase == "signals":
        intake = (prior or {}).get("intake") or {}
        affected = set(intake.get("affected_components") or [])
        services_in_signals = set(list(cleaned.get("logs", {}).keys()) + list(cleaned.get("metrics", {}).keys()))
        if affected and services_in_signals and affected.isdisjoint(services_in_signals):
            block.append(
                f"contradiction: intake.affected_components={sorted(affected)} "
                f"but signals only covered {sorted(services_in_signals)}"
            )
    elif phase == "fix_and_test":
        signals = (prior or {}).get("signals") or {}
        services_touched = set()
        for f in cleaned.get("files_changed") or []:
            # Very rough heuristic — service name is the first path segment
            parts = str(f).split("/")
            if parts: services_touched.add(parts[0])
        services_in_signals = set(list(signals.get("logs", {}).keys()) + list(signals.get("metrics", {}).keys()))
        if services_touched and services_in_signals and services_touched.isdisjoint(services_in_signals):
            block.append(
                f"contradiction: fix touches services {sorted(services_touched)} "
                f"but signals implicated {sorted(services_in_signals)}"
            )
    return block


def main() -> None:
    payload = json.loads(sys.stdin.read())
    phase = payload.get("phase")
    out = payload.get("output", {})
    prior = payload.get("prior_outputs", {}) or {}
    fn = PHASE_DISPATCH.get(phase)
    if not fn:
        print(json.dumps({"passed": False, "blocking_warnings": [f"unknown_phase: {phase}"]}))
        return
    cleaned, norm, block = fn(out)
    block.extend(cross_phase_checks(phase, cleaned, prior))
    passed = len(block) == 0
    print(json.dumps({
        "passed": passed,
        "cleaned_output": cleaned,
        "normalizing_warnings": norm,
        "blocking_warnings": block,
    }, indent=2))


if __name__ == "__main__":
    main()
