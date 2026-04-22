# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
AnomalyCorrelatorAgent — Cross-correlates findings from ALL Phase 2 agents.

Fuses signals from multiple agents (APM, logs, traces, code, deployments, database)
to find patterns and contradictions that individual agents miss:
  - correlate_by_service()                   → group signals by service
  - correlate_by_time()                      → find temporal clusters
  - compute_signal_agreement()               → multi-agent voting strength
  - find_contradictions()                    → conflicts between agent findings
  - compute_evidence_matrix()                → services × agents heatmap
  - identify_correlated_failures()           → services that always fail together
  - finish_analysis(...)                     → submit correlated findings
"""
import os, sys, json
from llm_client import get_client, get_model
from dotenv import load_dotenv
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "correlate_by_service",
            "description": "Group all signals (anomalies, code issues, traces, deployments, DB issues) by service. Returns signal counts and severity per service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "all_signals_json": {
                        "type": "string",
                        "description": "JSON list of all signals: [{service, source_agent, severity, type, timestamp}, ...]"
                    }
                },
                "required": ["all_signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_by_time",
            "description": "Sort signals by timestamp and find temporal clusters (events within 60s of each other).",
            "parameters": {
                "type": "object",
                "properties": {
                    "all_signals_json": {
                        "type": "string",
                        "description": "JSON list of signals with timestamps"
                    }
                },
                "required": ["all_signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_signal_agreement",
            "description": "Count how many independent agents flagged the same service. Agreement score = n_agents_flagging / total_agents. >0.5 = high confidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string"},
                    "signals_json": {"type": "string", "description": "JSON list of all signals"}
                },
                "required": ["service_name", "signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_contradictions",
            "description": "Find cases where one agent says a service is healthy but another says it's failing. Returns list of contradictions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "signals_json": {
                        "type": "string",
                        "description": "JSON list of signals from all agents"
                    }
                },
                "required": ["signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_evidence_matrix",
            "description": "Build services × agents boolean matrix: did agent X flag service Y? Returns matrix plus row/column totals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "services_list": {"type": "array", "items": {"type": "string"}},
                    "agents_list": {"type": "array", "items": {"type": "string"}},
                    "signals_json": {"type": "string", "description": "JSON list of all signals"}
                },
                "required": ["services_list", "agents_list", "signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "identify_correlated_failures",
            "description": "Find services that always fail together (co-occurrence counting). If A fails, B also fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "signals_json": {
                        "type": "string",
                        "description": "JSON list of signals"
                    }
                },
                "required": ["signals_json"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit multi-agent fusion analysis and correlations.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_ranking": {
                    "type": "array",
                    "description": "Services ranked by signal count and agreement",
                    "items": {"type": "object"}
                },
                "temporal_clusters": {
                    "type": "array",
                    "description": "Clusters of events within 60-second windows",
                    "items": {"type": "object"}
                },
                "signal_agreement": {
                    "type": "object",
                    "description": "Per-service agreement scores across agents"
                },
                "contradictions": {
                    "type": "array",
                    "description": "Conflicting findings between agents",
                    "items": {"type": "object"}
                },
                "evidence_matrix": {
                    "type": "object",
                    "description": "services × agents heatmap"
                },
                "correlated_failure_groups": {
                    "type": "array",
                    "description": "Services that fail together",
                    "items": {"type": "object"}
                },
                "summary": {"type": "string"}
            },
            "required": ["service_ranking", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are AnomalyCorrelatorAgent, fusing signals from ALL Phase 2 agents.

Use your tools to:
1. Group signals by service (detect most affected services).
2. Find temporal clusters (events happening together).
3. Compute multi-agent agreement (voting strength for each service).
4. Find contradictions (conflicting findings).
5. Build evidence matrix (which agents flagged which services).
6. Identify correlated failures (services that fail together).

Your goal: synthesize all signals to build consensus and detect patterns no single agent sees.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call. Pure algorithmic."""

    try:
        if name == "correlate_by_service":
            signals = json.loads(args.get("all_signals_json", "[]"))

            by_service = defaultdict(lambda: {"signals": [], "severity_counts": {}, "agent_sources": set()})

            for signal in signals:
                service = signal.get("service", "unknown")
                severity = signal.get("severity", "WARN")
                agent = signal.get("source_agent", "unknown")

                by_service[service]["signals"].append(signal)
                by_service[service]["severity_counts"][severity] = by_service[service]["severity_counts"].get(severity, 0) + 1
                by_service[service]["agent_sources"].add(agent)

            result = []
            for service, data in by_service.items():
                result.append({
                    "service": service,
                    "signal_count": len(data["signals"]),
                    "severity_summary": data["severity_counts"],
                    "n_agents_flagging": len(data["agent_sources"]),
                    "agents": sorted(list(data["agent_sources"])),
                })

            # Sort by signal count
            result.sort(key=lambda x: x["signal_count"], reverse=True)

            return json.dumps({
                "by_service": result,
                "total_services": len(result),
                "total_signals": len(signals),
            })

        elif name == "correlate_by_time":
            signals = json.loads(args.get("all_signals_json", "[]"))

            # Parse timestamps
            for signal in signals:
                if isinstance(signal.get("timestamp"), str):
                    try:
                        ts = datetime.fromisoformat(signal["timestamp"].replace('Z', '+00:00'))
                        signal["timestamp_unix"] = ts.timestamp()
                    except (ValueError, TypeError):
                        signal["timestamp_unix"] = 0

            # Sort by time
            signals.sort(key=lambda x: x.get("timestamp_unix", 0))

            # Find clusters within 60-second windows
            clusters = []
            if signals:
                current_cluster = [signals[0]]
                for signal in signals[1:]:
                    if signal.get("timestamp_unix", 0) - current_cluster[0].get("timestamp_unix", 0) <= 60:
                        current_cluster.append(signal)
                    else:
                        if len(current_cluster) > 1:
                            clusters.append(current_cluster)
                        current_cluster = [signal]

                if len(current_cluster) > 1:
                    clusters.append(current_cluster)

            result = []
            for i, cluster in enumerate(clusters):
                services = set(s.get("service", "unknown") for s in cluster)
                result.append({
                    "cluster_id": i,
                    "signal_count": len(cluster),
                    "services": sorted(list(services)),
                    "time_range_start": cluster[0].get("timestamp", "unknown") if cluster else "unknown",
                    "time_range_end": cluster[-1].get("timestamp", "unknown") if cluster else "unknown",
                })

            return json.dumps({
                "temporal_clusters": result,
                "cluster_count": len(clusters),
            })

        elif name == "compute_signal_agreement":
            service_name = args.get("service_name", "unknown")
            signals = json.loads(args.get("signals_json", "[]"))

            service_signals = [s for s in signals if s.get("service") == service_name]
            unique_agents = set(s.get("source_agent", "unknown") for s in service_signals)

            # Assume 6 total agents (APM, logs, traces, code, deployment, DB)
            total_agents = 6
            agreement_score = len(unique_agents) / total_agents

            return json.dumps({
                "service": service_name,
                "n_agents_flagging": len(unique_agents),
                "agents": sorted(list(unique_agents)),
                "total_agents": total_agents,
                "agreement_score": round(agreement_score, 3),
                "confidence": "HIGH" if agreement_score > 0.5 else "MEDIUM" if agreement_score > 0.3 else "LOW"
            })

        elif name == "find_contradictions":
            signals = json.loads(args.get("signals_json", "[]"))

            by_service = defaultdict(lambda: {"healthy": 0, "failing": 0, "agents_healthy": [], "agents_failing": []})

            for signal in signals:
                service = signal.get("service", "unknown")
                severity = signal.get("severity", "WARN")
                agent = signal.get("source_agent", "unknown")

                if severity == "ERROR":
                    by_service[service]["failing"] += 1
                    by_service[service]["agents_failing"].append(agent)
                else:
                    by_service[service]["healthy"] += 1
                    by_service[service]["agents_healthy"].append(agent)

            contradictions = []
            for service, data in by_service.items():
                if data["healthy"] > 0 and data["failing"] > 0:
                    contradictions.append({
                        "service": service,
                        "agents_saying_healthy": data["agents_healthy"],
                        "agents_saying_failing": data["agents_failing"],
                        "severity": "MEDIUM"
                    })

            return json.dumps({
                "contradictions": contradictions,
                "contradiction_count": len(contradictions),
            })

        elif name == "compute_evidence_matrix":
            services = args.get("services_list", [])
            agents = args.get("agents_list", [])
            signals = json.loads(args.get("signals_json", "[]"))

            # Build matrix: services × agents
            matrix = {}
            for service in services:
                matrix[service] = {}
                for agent in agents:
                    # Check if this agent flagged this service
                    flagged = any(
                        s.get("service") == service and s.get("source_agent") == agent
                        for s in signals
                    )
                    matrix[service][agent] = 1 if flagged else 0

            # Row totals (per service)
            row_totals = {service: sum(matrix[service].values()) for service in services}

            # Column totals (per agent)
            col_totals = {agent: sum(matrix[s].get(agent, 0) for s in services) for agent in agents}

            return json.dumps({
                "matrix": matrix,
                "row_totals": row_totals,
                "column_totals": col_totals,
            })

        elif name == "identify_correlated_failures":
            signals = json.loads(args.get("signals_json", "[]"))

            # Group signals by temporal windows
            windows = defaultdict(set)
            for signal in signals:
                ts = signal.get("timestamp_unix", 0)
                window = int(ts / 60) * 60  # 60-second buckets
                service = signal.get("service", "unknown")
                windows[window].add(service)

            # Co-occurrence counting
            co_occur = defaultdict(int)
            for window, services in windows.items():
                service_list = sorted(list(services))
                for i, s1 in enumerate(service_list):
                    for s2 in service_list[i+1:]:
                        pair = tuple(sorted([s1, s2]))
                        co_occur[pair] += 1

            # Find pairs that appear together frequently (>50% of windows)
            window_count = len(windows)
            correlated = [
                {"services": list(pair), "co_occurrence_count": count, "frequency": round(count / window_count, 2)}
                for pair, count in co_occur.items()
                if count >= window_count * 0.5
            ]

            correlated.sort(key=lambda x: x["co_occurrence_count"], reverse=True)

            return json.dumps({
                "correlated_failure_groups": correlated,
                "group_count": len(correlated),
            })

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def correlate_anomalies(log_results: list, apm_results: dict, trace_results: dict,
                       code_results: dict, deployment_results: dict, db_results: dict) -> dict:
    """
    Cross-correlate findings from all Phase 2 agents.

    Args:
        log_results: anomalies from LogAgent
        apm_results: anomalies from APMAgent
        trace_results: anomalies from TraceAgent
        code_results: issues from CodeAgent
        deployment_results: info from DeploymentAgent
        db_results: issues from DatabaseAgent
    """
    # Flatten all signals into a unified list
    all_signals = []

    for anom in log_results:
        all_signals.append({
            "service": anom.get("service", "unknown"),
            "source_agent": "LogAgent",
            "severity": anom.get("severity", "WARN"),
            "type": anom.get("anomaly_type", "log_error"),
            "timestamp": anom.get("timestamp", datetime.utcnow().isoformat()),
        })

    for anom in apm_results.get("anomalies", []):
        all_signals.append({
            "service": anom.get("service", "unknown"),
            "source_agent": "APMAgent",
            "severity": anom.get("severity", "WARN"),
            "type": anom.get("metric_type", "metric_anomaly"),
            "timestamp": anom.get("timestamp", datetime.utcnow().isoformat()),
        })

    for anom in trace_results.get("anomalies", []):
        all_signals.append({
            "service": anom.get("service", "unknown"),
            "source_agent": "TraceAgent",
            "severity": anom.get("severity", "WARN"),
            "type": anom.get("type", "trace_error"),
            "timestamp": anom.get("timestamp", datetime.utcnow().isoformat()),
        })

    for issue in code_results.get("issues", []):
        all_signals.append({
            "service": issue.get("service", "unknown"),
            "source_agent": "CodeAgent",
            "severity": issue.get("severity", "WARN"),
            "type": issue.get("issue_type", "code_issue"),
            "timestamp": datetime.utcnow().isoformat(),
        })

    signals_json = json.dumps(all_signals)
    services = list(set(s.get("service", "unknown") for s in all_signals))
    agents = ["LogAgent", "APMAgent", "TraceAgent", "CodeAgent", "DeploymentAgent", "DatabaseAgent"]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
         f"Correlate signals from {len(all_signals)} findings across {len(services)} services and {len(agents)} agents.\n\n"
         f"Services: {', '.join(services)}\n"
         f"Signals JSON: {signals_json[:800]}...\n\n"
         f"Analyze agreement, contradictions, temporal patterns, and correlated failures."},
    ]

    final_result = {}
    max_iterations = 8

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
        final_result = {
            "service_ranking": [],
            "temporal_clusters": [],
            "signal_agreement": {},
            "contradictions": [],
            "evidence_matrix": {},
            "correlated_failure_groups": [],
            "summary": "Correlation incomplete"
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_log = [
        {"service": "python-inventory-service", "severity": "ERROR", "anomaly_type": "http_400"},
        {"service": "python-inventory-service", "severity": "ERROR", "anomaly_type": "http_400"},
    ]

    sample_apm = {
        "anomalies": [
            {"service": "python-inventory-service", "severity": "ERROR", "metric_type": "error_rate"},
            {"service": "java-order-service", "severity": "WARN", "metric_type": "latency"},
        ]
    }

    sample_trace = {"anomalies": []}
    sample_code = {"issues": []}
    sample_deployment = {}
    sample_db = {}

    result = correlate_anomalies(
        sample_log, sample_apm, sample_trace, sample_code, sample_deployment, sample_db
    )
    print(json.dumps(result, indent=2))
