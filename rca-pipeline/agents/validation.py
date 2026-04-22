"""
Inter-agent validation layer — validates agent outputs before passing downstream.

Catches:
  - Missing required fields (e.g. anomalies list, service names)
  - Wrong types (e.g. string where list expected)
  - Empty results that should trigger a warning
  - Timestamp format issues that break DBSCAN
  - Anomaly dicts missing keys that HypothesisRanker needs

Each validator returns (cleaned_result, warnings) — it normalizes the data
and reports issues without crashing the pipeline.
"""
import json
from datetime import datetime
from typing import Tuple, List


def validate_log_anomalies(result: list) -> Tuple[list, List[str]]:
    """Validate LogAgent output before passing to AlertCorrelator / HypothesisRanker."""
    warnings = []
    if not isinstance(result, list):
        warnings.append(f"LogAgent returned {type(result).__name__}, expected list — coercing to empty list")
        return [], warnings

    cleaned = []
    for i, anom in enumerate(result):
        if not isinstance(anom, dict):
            warnings.append(f"LogAgent anomaly[{i}] is {type(anom).__name__}, expected dict — skipping")
            continue

        # Ensure required fields exist with defaults
        entry = dict(anom)
        if "service" not in entry or not entry["service"]:
            entry["service"] = "unknown"
            warnings.append(f"LogAgent anomaly[{i}] missing 'service', set to 'unknown'")
        if "description" not in entry:
            entry["description"] = entry.get("anomaly", entry.get("message", "No description"))

        # Ensure timestamp exists and is valid ISO format for DBSCAN
        if "timestamp" not in entry or not entry["timestamp"]:
            entry["timestamp"] = datetime.utcnow().isoformat() + "Z"
            warnings.append(f"LogAgent anomaly[{i}] missing 'timestamp', using current time")

        # Ensure severity exists
        if "severity" not in entry:
            entry["severity"] = entry.get("level", "WARN")

        # Ensure anomaly_type exists (AlertCorrelator uses this)
        if "anomaly_type" not in entry:
            desc = entry.get("description", "").lower()
            if "400" in desc or "404" in desc or "500" in desc:
                entry["anomaly_type"] = "http_error"
            elif "timeout" in desc:
                entry["anomaly_type"] = "timeout"
            elif "latency" in desc or "slow" in desc:
                entry["anomaly_type"] = "latency"
            elif "memory" in desc or "cpu" in desc:
                entry["anomaly_type"] = "resource"
            else:
                entry["anomaly_type"] = "unknown"

        cleaned.append(entry)

    return cleaned, warnings


def validate_apm_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate APMAgent output before passing to AnomalyCorrelator / HypothesisRanker."""
    warnings = []
    if not isinstance(result, dict):
        warnings.append(f"APMAgent returned {type(result).__name__}, expected dict — coercing to empty dict")
        return {"anomalies": [], "summary": "APM analysis unavailable"}, warnings

    if "anomalies" not in result:
        result["anomalies"] = []
        warnings.append("APMAgent result missing 'anomalies' key, set to empty list")

    if not isinstance(result["anomalies"], list):
        warnings.append(f"APMAgent anomalies is {type(result['anomalies']).__name__}, expected list — coercing")
        result["anomalies"] = []

    # Validate each anomaly has required fields
    for i, anom in enumerate(result["anomalies"]):
        if not isinstance(anom, dict):
            continue
        if "service" not in anom:
            anom["service"] = "unknown"
        if "severity" not in anom:
            anom["severity"] = "MEDIUM"

    return result, warnings


def validate_trace_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate TraceAgent output before passing to DependencyGraph / HypothesisRanker."""
    warnings = []
    if not isinstance(result, dict):
        warnings.append(f"TraceAgent returned {type(result).__name__}, expected dict — coercing")
        return {}, warnings

    # Ensure root_failure is a dict if present
    rf = result.get("root_failure")
    if rf and not isinstance(rf, dict):
        warnings.append(f"TraceAgent root_failure is {type(rf).__name__}, expected dict — clearing")
        result["root_failure"] = {}

    return result, warnings


def validate_code_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate CodeAgent output before passing to HypothesisRanker / FixGenerator."""
    warnings = []
    if not isinstance(result, dict):
        warnings.append(f"CodeAgent returned {type(result).__name__}, expected dict — coercing")
        return {"code_issues": [], "issues": []}, warnings

    # Normalize: some versions use "code_issues", others "issues"
    if "code_issues" not in result and "issues" in result:
        result["code_issues"] = result["issues"]
    elif "code_issues" not in result:
        result["code_issues"] = []

    if not isinstance(result["code_issues"], list):
        warnings.append(f"CodeAgent code_issues is {type(result['code_issues']).__name__}, coercing to list")
        result["code_issues"] = []

    # Ensure each issue has required fields
    for i, issue in enumerate(result["code_issues"]):
        if not isinstance(issue, dict):
            continue
        if "description" not in issue:
            issue["description"] = issue.get("message", issue.get("title", "No description"))
        if "severity" not in issue:
            issue["severity"] = "MEDIUM"
        if "service" not in issue:
            issue["service"] = issue.get("file", "unknown")

    return result, warnings


def validate_deploy_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate DeploymentAgent output."""
    warnings = []
    if not isinstance(result, dict):
        return {"verdict": "unknown", "suspicious_deployments": []}, warnings
    if "verdict" not in result:
        result["verdict"] = "unknown"
    if "suspicious_deployments" not in result:
        result["suspicious_deployments"] = []
    return result, warnings


def validate_kb_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate KnowledgeBaseAgent output before passing to HypothesisRanker."""
    warnings = []
    if not isinstance(result, dict):
        return {"matches": [], "lessons_learned": []}, warnings
    if "matches" not in result:
        result["matches"] = []
    if "lessons_learned" not in result:
        result["lessons_learned"] = []
    return result, warnings


def validate_hypothesis_result(result: dict) -> Tuple[dict, List[str]]:
    """Validate HypothesisRankerAgent output before passing to FixGenerator."""
    warnings = []
    if not isinstance(result, dict):
        return {"ranked_hypotheses": [], "top_hypothesis": None}, warnings

    hyps = result.get("ranked_hypotheses", result.get("hypotheses", []))
    if not isinstance(hyps, list):
        warnings.append("HypothesisRanker hypotheses not a list — coercing")
        hyps = []
    result["ranked_hypotheses"] = hyps

    # Ensure each hypothesis has required fields for FixGenerator
    for i, h in enumerate(hyps):
        if not isinstance(h, dict):
            continue
        if "description" not in h and "hypothesis" not in h:
            h["description"] = f"Hypothesis {i+1}"
        if "confidence" not in h:
            h["confidence"] = 0.5

    return result, warnings


def pipeline_sanity_check(
    all_anomalies: list,
    apm_result: dict,
    trace_result: dict,
    code_issues: list,
    deploy_result: dict,
    db_result: dict,
    change_result: dict,
) -> Tuple[bool, List[str]]:
    """
    Check if the pipeline collected enough signal to produce useful results.
    Returns (is_healthy, issues).
    """
    issues = []
    signal_count = 0

    if all_anomalies:
        signal_count += 1
    else:
        issues.append("LogAgent produced 0 anomalies — check log format or agent health")

    if apm_result.get("anomalies"):
        signal_count += 1
    else:
        issues.append("APMAgent produced 0 anomalies — check metrics format")

    if trace_result.get("root_failure") or trace_result.get("spans"):
        signal_count += 1
    else:
        issues.append("TraceAgent found no root failure — check trace format")

    if code_issues:
        signal_count += 1
    else:
        issues.append("CodeAgent found 0 code issues — check service source dirs")

    if deploy_result.get("verdict") not in (None, "unknown", ""):
        signal_count += 1

    if db_result.get("data_anomalies") or db_result.get("schema_issues"):
        signal_count += 1

    if change_result.get("config_changes") or change_result.get("api_contract_mismatches"):
        signal_count += 1

    is_healthy = signal_count >= 2  # Need at least 2 signals for meaningful RCA
    if not is_healthy:
        issues.insert(0, f"CRITICAL: Only {signal_count}/7 agents produced results — RCA will be weak")

    return is_healthy, issues
