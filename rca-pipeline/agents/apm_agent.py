# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
APMAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

The agent has a suite of tools that call real algorithms autonomously:
  - run_ensemble_detector(metrics_json)   → autoencoder + isolation forest
  - compute_statistics(metric, values)    → mean/std/min/max/percentiles
  - detect_spike(metric, values)          → statistical spike detection (z-score)
  - find_incident_start(snapshots_json)   → sliding window over time series
  - compare_services(service_a, service_b)→ correlation between two services
  - parse_apm_text(section)               → extract structured metrics from text

The LLM calls these tools in whatever sequence it needs, then submits a final
structured report via finish_analysis(). Every numeric finding is backed by
an algorithm, not just GPT intuition.
"""
import os, sys, json, re
import numpy as np
from llm_client import get_client, get_model
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.anomaly_detector import AnomalyDetector, parse_apm_text_to_snapshots, FEATURE_NAMES
from algorithms.cusum import CUSUMDetector

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client   = get_client()
_detector = AnomalyDetector()   # singleton — trained once at import

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_ensemble_detector",
            "description": (
                "Run the ensemble anomaly detector (autoencoder neural network + isolation forest) "
                "on a snapshot of APM metrics. Returns ensemble score, severity, and which features "
                "are most anomalous. Inputs are normalised 0-1."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cpu_pct":     {"type": "number", "description": "CPU usage fraction 0-1"},
                    "error_rate":  {"type": "number", "description": "Error rate fraction 0-1"},
                    "latency_p99": {"type": "number", "description": "p99 latency fraction of 5000ms max"},
                    "throughput":  {"type": "number", "description": "Throughput fraction of 2000 req/s max"},
                    "mem_pct":     {"type": "number", "description": "Memory usage fraction 0-1"},
                    "db_query_ms": {"type": "number", "description": "DB query time fraction of 3000ms max"},
                    "service":     {"type": "string", "description": "Service name for labelling"},
                },
                "required": ["cpu_pct", "error_rate", "latency_p99", "throughput", "mem_pct", "db_query_ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_statistics",
            "description": "Compute descriptive statistics (mean, std, min, max, p95, p99, coefficient of variation) for a metric's time series values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string"},
                    "values":      {"type": "array", "items": {"type": "number"},
                                   "description": "List of numeric metric observations"},
                },
                "required": ["metric_name", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_spike",
            "description": (
                "Statistical spike detection using z-score and IQR. "
                "Pass a time series and get back which values are spikes, "
                "the spike magnitude, and when the spike started."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string"},
                    "values":      {"type": "array", "items": {"type": "number"}},
                    "threshold_z": {"type": "number", "description": "Z-score threshold for spike (default 2.5)", "default": 2.5},
                },
                "required": ["metric_name", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_incident_start",
            "description": (
                "Run the sliding-window incident-start detector over a sequence of metric snapshots. "
                "Returns the index of the first snapshot where anomaly scores become consistently high."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "snapshots": {
                        "type": "array",
                        "description": "List of metric snapshot dicts, each with keys: cpu_pct, error_rate, latency_p99, throughput, mem_pct, db_query_ms (all 0-1)",
                        "items": {"type": "object"},
                    },
                    "window": {"type": "integer", "description": "Consecutive anomalous snapshots required (default 2)", "default": 2},
                },
                "required": ["snapshots"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_services",
            "description": "Compute Pearson correlation between two services' metric time series. High positive correlation suggests one is causing the other to fail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_a":     {"type": "string"},
                    "metric_a":      {"type": "array", "items": {"type": "number"}},
                    "service_b":     {"type": "string"},
                    "metric_b":      {"type": "array", "items": {"type": "number"}},
                    "metric_name":   {"type": "string"},
                },
                "required": ["service_a", "metric_a", "service_b", "metric_b", "metric_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_change_point",
            "description": (
                "CUSUM (Cumulative Sum) change point detection. Detects regime shifts in a metric "
                "time series — e.g. when error rate jumped from baseline to incident level. "
                "Returns detected change points with timestamps, directions, and magnitudes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string"},
                    "values":      {"type": "array", "items": {"type": "number"},
                                   "description": "Time series values"},
                    "baseline_values": {"type": "array", "items": {"type": "number"},
                                       "description": "Optional baseline values for fitting. If omitted, first 30% of values used."},
                },
                "required": ["metric_name", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_apm_text",
            "description": "Parse a section of free-form APM text and extract structured metric snapshots (normalised 0-1). Useful for turning raw metric text into inputs for run_ensemble_detector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Raw APM metrics text"},
                },
                "required": ["text"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit the final structured APM analysis. Call this when you've run enough tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_window":    {"type": "object"},
                "anomalies":          {"type": "array", "items": {"type": "object"}},
                "healthy_services":   {"type": "array", "items": {"type": "string"}},
                "neural_network_analysis": {"type": "object"},
                "timeline":           {"type": "array", "items": {"type": "object"}},
                "root_signal":        {"type": "object"},
                "cascading_effects":  {"type": "array", "items": {"type": "object"}},
                "error_rate_cause":   {"type": "string"},
                "latency_cause":      {"type": "string"},
                "summary":            {"type": "string"},
                "metrics_analyzed":   {"type": "integer"},
            },
            "required": ["anomalies", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are APMAgent, an AI agent in an incident Root Cause Analysis pipeline.

You have algorithmic tools to analyse APM metrics. Use them:

1. Parse the raw APM text with parse_apm_text to get structured snapshots.
2. Run run_ensemble_detector on key snapshots (especially at incident time vs baseline).
3. Use detect_spike to find exactly when error rate, latency, or CPU spiked.
4. Use detect_change_point (CUSUM) to detect regime shifts — when metrics permanently changed level.
5. Use compute_statistics to quantify the deviation from baseline.
6. Use find_incident_start to pinpoint when the anomaly window opened.
7. Use compare_services to check if one service's errors are causing another's.

Every anomaly you report should have a numeric confidence backed by the algorithms.
When done, call finish_analysis() with structured results.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict, _metrics_text: str) -> str:
    try:
        if name == "run_ensemble_detector":
            svc = args.pop("service", "unknown")
            score = _detector.score(**{k: float(args.get(k, 0.0)) for k in FEATURE_NAMES})
            return json.dumps({
                "service":          svc,
                "ensemble_score":   round(score.ensemble_score, 4),
                "is_anomaly":       score.is_anomaly,
                "severity":         score.severity,
                "confidence":       round(score.confidence, 3),
                "ae_msre":          round(score.ae_msre, 5),
                "ae_threshold":     round(score.ae_threshold, 5),
                "if_score":         round(score.if_score, 4),
                "if_avg_path":      round(score.if_avg_path, 2),
                "anomalous_features": score.anomalous_features,
                "source": "ensemble:autoencoder+isolation_forest",
            })

        elif name == "compute_statistics":
            vals = np.array(args["values"], dtype=float)
            if len(vals) == 0:
                return json.dumps({"error": "empty values array"})
            return json.dumps({
                "metric": args["metric_name"],
                "n":      int(len(vals)),
                "mean":   round(float(vals.mean()), 4),
                "std":    round(float(vals.std()),  4),
                "min":    round(float(vals.min()),  4),
                "max":    round(float(vals.max()),  4),
                "p50":    round(float(np.percentile(vals, 50)), 4),
                "p95":    round(float(np.percentile(vals, 95)), 4),
                "p99":    round(float(np.percentile(vals, 99)), 4),
                "cv":     round(float(vals.std() / vals.mean()) if vals.mean() != 0 else 0, 4),
            })

        elif name == "detect_spike":
            vals = np.array(args["values"], dtype=float)
            z_thr = float(args.get("threshold_z", 2.5))
            if len(vals) < 4:
                return json.dumps({"error": "need at least 4 values for spike detection"})
            mean, std = vals.mean(), vals.std()
            z_scores  = (vals - mean) / (std + 1e-9)
            spikes    = [{"index": int(i), "value": float(v), "z_score": round(float(z_scores[i]), 2)}
                         for i, v in enumerate(vals) if abs(z_scores[i]) >= z_thr]
            # IQR method
            q1, q3 = np.percentile(vals, 25), np.percentile(vals, 75)
            iqr    = q3 - q1
            iqr_outliers = [int(i) for i, v in enumerate(vals) if v > q3 + 1.5 * iqr or v < q1 - 1.5 * iqr]

            first_spike = spikes[0]["index"] if spikes else None
            return json.dumps({
                "metric":         args["metric_name"],
                "spike_count":    len(spikes),
                "spikes":         spikes[:10],
                "first_spike_at": first_spike,
                "iqr_outliers":   iqr_outliers[:10],
                "baseline_mean":  round(float(mean), 4),
                "baseline_std":   round(float(std), 4),
                "max_z_score":    round(float(np.abs(z_scores).max()), 2),
            })

        elif name == "find_incident_start":
            snaps  = args["snapshots"]
            window = int(args.get("window", 2))
            idx    = _detector.find_incident_start(snaps, window=window)
            scores = _detector.score_time_series(snaps)
            return json.dumps({
                "incident_start_index": idx,
                "snapshot_scores": [
                    {"index": i, "ensemble": round(s.ensemble_score, 3),
                     "is_anomaly": s.is_anomaly, "severity": s.severity}
                    for i, s in enumerate(scores)
                ],
            })

        elif name == "compare_services":
            a = np.array(args["metric_a"], dtype=float)
            b = np.array(args["metric_b"], dtype=float)
            n = min(len(a), len(b))
            if n < 3:
                return json.dumps({"error": "need at least 3 points to correlate"})
            corr = float(np.corrcoef(a[:n], b[:n])[0, 1])
            lag1 = float(np.corrcoef(a[1:n], b[:n-1])[0, 1]) if n > 3 else 0.0
            return json.dumps({
                "metric":           args["metric_name"],
                "service_a":        args["service_a"],
                "service_b":        args["service_b"],
                "pearson_r":        round(corr, 4),
                "lag1_correlation": round(lag1, 4),
                "interpretation":   (
                    "strong positive — likely causal" if corr > 0.8 else
                    "moderate positive — possible causation" if corr > 0.5 else
                    "weak / no correlation"
                ),
            })

        elif name == "detect_change_point":
            vals = np.array(args["values"], dtype=float)
            baseline = args.get("baseline_values")
            detector = CUSUMDetector()
            if baseline:
                detector.fit(np.array(baseline, dtype=float))
            else:
                # Use first 30% as baseline
                split = max(3, int(len(vals) * 0.3))
                detector.fit(vals[:split])
            result = detector.detect(vals)
            return json.dumps({
                "metric": args["metric_name"],
                "is_changed": result.is_changed,
                "first_change_index": result.first_change_index,
                "change_points": [
                    {
                        "index": cp.index,
                        "direction": cp.direction,
                        "magnitude": round(cp.magnitude, 4),
                        "cusum_value": round(cp.cusum_value, 4),
                        "severity": cp.severity,
                    }
                    for cp in result.change_points
                ],
                "total_detected": len(result.change_points),
                "description": result.description,
                "algorithm": "CUSUM (Cumulative Sum) change point detection",
            })

        elif name == "parse_apm_text":
            snaps = parse_apm_text_to_snapshots(args["text"])
            return json.dumps({"snapshots": snaps, "count": len(snaps)})

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def analyze_metrics(metrics_text: str) -> dict:
    """Real tool-using ReAct agent. The LLM calls algorithms autonomously
    and submits a structured result via finish_analysis().

    Args:
        metrics_text: Raw APM metrics data as a string.

    Returns:
        dict: Analysis results with anomalies, summary, and optional neural network insights.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Investigate these APM metrics. Use your tools to detect anomalies algorithmically.\n\n"
         f"--- METRICS ---\n{metrics_text[:6000]}\n--- END ---\n\n"
         f"Call finish_analysis() when done."},
    ]

    final_result = {}
    max_iterations = 10

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=ALL_TOOLS,
            tool_choice="auto",
            temperature=0,
            timeout=60,
        )

        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            break

        done = False
        for tc in msg.tool_calls:
            fn   = tc.function.name
            args = json.loads(tc.function.arguments or "{}")

            if fn == "finish_analysis":
                final_result = args
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps({"status": "accepted"}),
                })
                done = True
            else:
                result = _execute_tool(fn, args, metrics_text)
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": result,
                })

        if done:
            break

    # Ensure anomalies key always exists
    if not final_result:
        final_result = {"anomalies": [], "summary": "Analysis incomplete (max iterations reached)"}
    final_result.setdefault("anomalies", [])
    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample = """
    Time window: 2024-01-15 09:45 – 11:00 UTC

    java-order-service:
      CPU: baseline 15% → spike to 78% at 10:23
      Error rate: 0.1% → 31.4% at 10:23
      Latency p99: 200ms → 4200ms
      Throughput: 850 req/min → 590 req/min

    python-inventory-service:
      CPU: stable 8%
      Error rate: 0.0% → 29.1% at 10:23 (all HTTP 400s)
      Latency p99: 45ms → 50ms (stable)

    sqlite (order-db):
      Query time avg: 2ms → 180ms at 10:23
    """
    result = analyze_metrics(sample)
    import pprint
    pprint.pprint(result)
