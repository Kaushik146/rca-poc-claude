# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
AlertCorrelatorAgent — Cluster related alerts to separate single incidents from multiple issues.

Tools cluster alerts and anomalies using DBSCAN, identify alert storms, and correlate
across services to distinguish "one root cause → many alerts" from multiple independent incidents:
  - cluster_alerts(alerts_json)              → DBSCAN clustering by timestamp/service
  - get_incident_groups()                    → real clusters with metadata
  - get_noise_alerts()                       → unclustered alerts (separate issues)
  - compute_alert_storm_score()              → alert-to-cluster ratio metric
  - correlate_across_services()              → Jaccard similarity across services
  - finish_analysis(...)                     → submit correlated incidents
"""
import os, sys, json, time
from llm_client import get_client, get_model
from dotenv import load_dotenv
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.dbscan import AlertDBSCAN

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# Module-level state
_clusters = None
_alerts = None

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cluster_alerts",
            "description": "Cluster alerts/anomalies using DBSCAN. Groups correlated events into incidents, marks noise as separate issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alerts_json": {
                        "type": "string",
                        "description": "JSON string of alerts: [{timestamp, service, severity, type}, ...]"
                    }
                },
                "required": ["alerts_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident_groups",
            "description": "Return real clusters (not noise): cluster_id, services, time_range, alert_count, dominant_type.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_noise_alerts",
            "description": "Return unclustered alerts (noise points). These are isolated incidents not part of larger clusters.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_alert_storm_score",
            "description": "Compute alert-to-cluster ratio. If > 5 alerts per cluster, it's an 'alert storm'. Returns storm_score and recommendation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n_alerts": {"type": "integer", "description": "Total alert count"},
                    "n_clusters": {"type": "integer", "description": "Real cluster count"},
                    "time_window_seconds": {"type": "integer", "description": "Duration of incident window"}
                },
                "required": ["n_alerts", "n_clusters"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_across_services",
            "description": "For each cluster, compute which services appear together. Computes Jaccard similarity of service sets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cluster_json": {
                        "type": "string",
                        "description": "JSON string of clusters with service lists"
                    }
                },
                "required": ["cluster_json"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit deduplicated incident groups and analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_groups": {
                    "type": "array",
                    "description": "Real clusters representing true incidents",
                    "items": {"type": "object"}
                },
                "noise_alerts": {
                    "type": "array",
                    "description": "Isolated alerts not part of clusters",
                    "items": {"type": "object"}
                },
                "alert_storm_score": {"type": "number"},
                "cross_service_correlation": {"type": "object"},
                "deduplicated_alerts": {"type": "integer"},
                "summary": {"type": "string"}
            },
            "required": ["incident_groups", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are AlertCorrelatorAgent, clustering alerts to identify distinct incidents.

Use your tools to:
1. Cluster alerts by DBSCAN (temporal & service similarity).
2. Extract real incident groups vs noise.
3. Compute alert storm score (alert/cluster ratio).
4. Correlate services within clusters.

Your goal: separate "1 root cause → many alerts" from multiple independent issues.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call. Pure algorithmic."""
    global _clusters, _alerts

    try:
        if name == "cluster_alerts":
            _alerts = json.loads(args.get("alerts_json", "[]"))

            # Convert timestamps to numeric (unix seconds) for clustering
            for alert in _alerts:
                if isinstance(alert.get("timestamp"), str):
                    try:
                        ts = datetime.fromisoformat(alert["timestamp"].replace('Z', '+00:00'))
                        alert["timestamp"] = ts.timestamp()
                    except (ValueError, TypeError):
                        alert["timestamp"] = time.time()

            # Run DBSCAN clustering — eps=0.3 in normalized space, min 2 alerts per cluster
            dbscan = AlertDBSCAN(eps=0.35, min_samples=2)
            dbscan.fit(_alerts)
            _clusters = list(dbscan.predict())

            n_real = len(set(c for c in _clusters if c >= 0))
            n_noise = sum(1 for c in _clusters if c < 0)

            return json.dumps({
                "n_clusters": n_real,
                "n_noise": n_noise,
                "total_alerts": len(_alerts),
                "status": "clustered"
            })

        elif name == "get_incident_groups":
            if _clusters is None or _alerts is None:
                return json.dumps({"error": "Run cluster_alerts first"})

            incidents = {}
            for alert, cluster_id in zip(_alerts, _clusters):
                if cluster_id >= 0:  # Skip noise (cluster_id == -1)
                    if cluster_id not in incidents:
                        incidents[cluster_id] = []
                    incidents[cluster_id].append(alert)

            result = []
            for cluster_id, alerts in incidents.items():
                services = set(a.get("service", "unknown") for a in alerts)
                timestamps = [a.get("timestamp", time.time()) for a in alerts]
                times = sorted(timestamps)
                types_list = [a.get("type", a.get("anomaly_type", "unknown")) for a in alerts]

                result.append({
                    "cluster_id": int(cluster_id),
                    "alert_count": len(alerts),
                    "services": sorted(list(services)),
                    "time_range_start": times[0] if times else None,
                    "time_range_end": times[-1] if times else None,
                    "dominant_type": max(set(types_list), key=lambda x: types_list.count(x)),
                })

            return json.dumps({"incidents": result, "count": len(result)})

        elif name == "get_noise_alerts":
            if _clusters is None or _alerts is None:
                return json.dumps({"error": "Run cluster_alerts first"})

            noise = [alert for alert, cluster_id in zip(_alerts, _clusters) if cluster_id < 0]

            return json.dumps({
                "noise_count": len(noise),
                "noise_alerts": noise[:20],  # First 20 only
            })

        elif name == "compute_alert_storm_score":
            n_alerts = args.get("n_alerts", 0)
            n_clusters = args.get("n_clusters", 1)
            time_window_seconds = args.get("time_window_seconds", 3600)

            if n_clusters == 0:
                n_clusters = 1

            ratio = n_alerts / n_clusters
            is_storm = ratio > 5
            storm_score = min(ratio / 10.0, 1.0)  # Normalize 0-1

            return json.dumps({
                "alert_to_cluster_ratio": round(ratio, 2),
                "is_alert_storm": is_storm,
                "storm_score": round(storm_score, 3),
                "recommendation": "Multiple root causes likely" if is_storm else "Single or few root causes likely"
            })

        elif name == "correlate_across_services":
            clusters = json.loads(args.get("cluster_json", "{}"))

            # Compute Jaccard similarity between service sets of clusters
            cluster_services = {}
            for cluster_id, cluster_data in clusters.items():
                services = set(cluster_data.get("services", []))
                cluster_services[cluster_id] = services

            correlations = {}
            cluster_ids = list(cluster_services.keys())
            for i, c1 in enumerate(cluster_ids):
                for c2 in cluster_ids[i+1:]:
                    s1, s2 = cluster_services[c1], cluster_services[c2]
                    intersection = len(s1 & s2)
                    union = len(s1 | s2)
                    jaccard = intersection / union if union > 0 else 0
                    correlations[f"{c1}-{c2}"] = round(jaccard, 3)

            return json.dumps({
                "cluster_count": len(cluster_services),
                "pairwise_jaccard": correlations,
                "avg_similarity": round(sum(correlations.values()) / len(correlations), 3) if correlations else 0
            })

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def correlate_alerts(all_anomalies: list, apm_anomalies: list = None) -> dict:
    """
    Correlate and cluster alerts to identify distinct incidents.

    Args:
        all_anomalies: list of anomalies from all sources
        apm_anomalies: optional APM-specific anomalies
    """
    # Build alert list
    alerts = []
    for anom in all_anomalies:
        alerts.append({
            "timestamp": anom.get("timestamp", datetime.utcnow().isoformat()),
            "service": anom.get("service", "unknown"),
            "severity": anom.get("severity", "WARN"),
            "type": anom.get("anomaly_type", "unknown"),
        })

    alerts_json = json.dumps(alerts)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
         f"Cluster {len(alerts)} alerts into incident groups.\n\n"
         f"Alerts: {alerts_json[:1000]}\n\n"
         f"Use clustering to separate true incidents from noise and alert storms."},
    ]

    final_result = {}
    max_iterations = 6

    for iteration in range(max_iterations):
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
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")

            if fn_name == "finish_analysis":
                final_result = fn_args
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"status": "accepted"}),
                })
                done = True
            else:
                result = _execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        if done:
            break

    if not final_result:
        final_result = {"incident_groups": [], "summary": "Correlation incomplete"}

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_anomalies = [
        {"timestamp": "2024-01-15T10:23:41Z", "service": "python-inventory-service", "severity": "ERROR", "anomaly_type": "http_400"},
        {"timestamp": "2024-01-15T10:23:42Z", "service": "python-inventory-service", "severity": "ERROR", "anomaly_type": "http_400"},
        {"timestamp": "2024-01-15T10:23:43Z", "service": "node-notification-service", "severity": "ERROR", "anomaly_type": "http_400"},
        {"timestamp": "2024-01-15T10:24:00Z", "service": "java-order-service", "severity": "WARN", "anomaly_type": "latency"},
    ]

    result = correlate_alerts(sample_anomalies)
    print(json.dumps(result, indent=2))
