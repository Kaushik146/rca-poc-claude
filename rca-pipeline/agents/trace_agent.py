"""
TraceAgent — Production-grade ReAct agent with deep distributed trace analysis.

Built on react_core.py ReAct engine.

=== TOOLS (14 total) ===

Phase 1 — Graph Construction:
  build_trace_graph()               → DAG-based trace analysis (pure algorithm)
  get_critical_path()               → slowest root-to-leaf path
  get_root_failure()                → origin of failure cascade
  get_cascaded_failures()           → downstream failure list
  get_silent_corruptions()          → OK-status spans with wrong data
  get_service_latency_breakdown()   → per-service latency %
  search_span(service_name)         → filter spans by service

Phase 2 — Deep Analysis (NEW):
  compute_latency_attribution()     → which service added the most delay?
  score_cascade_severity()          → rate each failure by blast radius
  detect_bottlenecks()              → find spans where latency concentrates
  infer_service_dependencies()      → reconstruct the actual service DAG
  simulate_failure_removal()        → what-if: would removing this failure fix the trace?
  compare_span_timings()            → compare latencies across same-service spans

Meta-tools (from react_core):
  update_scratchpad / read_scratchpad / reflect_on_findings / revise_finding
"""
import os, sys, json, threading
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.trace_analyzer import analyze_trace as algo_analyze_trace
from react_core import ReActEngine

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    # ── Phase 1: Graph Construction ──────────────────────────────────────
    {"type": "function", "function": {
        "name": "build_trace_graph",
        "description": "Build the trace graph from raw trace text. Must be called first. Returns the full algorithmic analysis.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_critical_path",
        "description": "Get the critical path (longest duration root → leaf) from the last built graph.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_root_failure",
        "description": "Get the root failure span (deepest error with no error children) — origin of cascade.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_cascaded_failures",
        "description": "Get list of cascaded failures (errors downstream of root).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_silent_corruptions",
        "description": "Get spans that succeeded (OK status) but produced wrong/corrupted data.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_service_latency_breakdown",
        "description": "Get latency contribution of each service as percentage of total.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "search_span",
        "description": "Filter spans by service name from the last graph.",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    }},
    # ── Phase 2: Deep Analysis ───────────────────────────────────────────
    {"type": "function", "function": {
        "name": "compute_latency_attribution",
        "description": (
            "Attribute latency to each service by computing self-time (time spent in service itself "
            "minus time in child calls). Reveals which service is actually slow vs which is just "
            "waiting for a slow dependency. Returns ranked list by self-time."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "score_cascade_severity",
        "description": (
            "Score each failure by its blast radius — how many downstream spans were affected. "
            "Uses BFS from each failure to count impacted spans. Higher scores = more damage."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "detect_bottlenecks",
        "description": (
            "Find spans where latency concentrates disproportionately. A bottleneck is a span "
            "that accounts for >30% of total trace duration or has >2x the average span duration. "
            "Returns bottleneck spans with their concentration ratio."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "infer_service_dependencies",
        "description": (
            "Reconstruct the service dependency graph from the trace. Shows which services "
            "call which, call counts, and average latency per edge. Useful for understanding "
            "the actual topology vs the expected topology."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "simulate_failure_removal",
        "description": (
            "What-if analysis: if a specific failure were removed, would the rest of the trace "
            "succeed? Simulates removing a failure and checks if downstream spans would proceed. "
            "Helps determine which failure is the TRUE root cause vs a symptom."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "span_service": {"type": "string", "description": "Service of the span to simulate removing"},
                "span_operation": {"type": "string", "description": "Operation name (optional)"},
            },
            "required": ["span_service"],
        },
    }},
    {"type": "function", "function": {
        "name": "compare_span_timings",
        "description": (
            "Compare latencies across spans of the same service/operation. Detects outliers — "
            "spans that took much longer than siblings. Useful for identifying intermittent issues."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service to compare (optional, all if omitted)"},
            },
            "required": [],
        },
    }},
]

SYSTEM_PROMPT = """You are TraceAgent, a production-grade ReAct agent in an incident Root Cause Analysis pipeline.

You analyze distributed traces to find root failures, cascading errors, silent data corruption,
and performance bottlenecks.

=== INVESTIGATION PROTOCOL ===

Phase 1 — Build the picture:
  1. build_trace_graph() to initialize the DAG analysis
  2. get_root_failure() and get_cascaded_failures() to map the failure tree
  3. get_critical_path() to find the slowest execution path
  4. get_silent_corruptions() for data integrity issues

Phase 2 — Go deep:
  5. compute_latency_attribution() to find TRUE slow services (self-time, not wait-time)
  6. score_cascade_severity() to rank failures by blast radius
  7. detect_bottlenecks() to find latency concentration points
  8. infer_service_dependencies() to understand the real topology
  9. simulate_failure_removal() to test if a suspected root cause truly explains the incident

Phase 3 — Cross-validate:
  10. search_span() to drill into suspicious services
  11. compare_span_timings() to check for outliers

=== SCRATCHPAD USAGE ===
Store key findings as you go:
  update_scratchpad(key="root_failure", value={...}, confidence=0.9)
  update_scratchpad(key="bottlenecks", value=[...], confidence=0.8)
  update_scratchpad(key="blast_radius", value={...}, confidence=0.85)

=== MANDATORY REFLECTION ===
Before finish_analysis, call reflect_on_findings to verify:
  - Does the root failure actually explain all downstream cascades?
  - Are there silent corruptions that contradict the identified root cause?
  - Does latency attribution agree with bottleneck analysis?
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final trace analysis. MUST call reflect_on_findings first.",
        "parameters": {
            "type": "object",
            "properties": {
                "trace_id":             {"type": "string"},
                "total_duration_ms":    {"type": "integer"},
                "call_chain":           {"type": "array", "items": {"type": "object"}},
                "root_failure":         {"type": "object"},
                "failure_propagation":  {"type": "array", "items": {"type": "object"}},
                "latency_breakdown":    {"type": "object"},
                "silent_corruptions":   {"type": "array", "items": {"type": "object"}},
                "critical_path":        {"type": "array", "items": {"type": "string"}},
                "fix_targets":          {"type": "array", "items": {"type": "object"}},
                "bottlenecks":          {"type": "array", "items": {"type": "object"}},
                "cascade_severity_ranking": {"type": "array", "items": {"type": "object"}},
                "service_dependencies": {"type": "object"},
                "summary":              {"type": "string"},
            },
            "required": ["trace_id", "total_duration_ms", "call_chain", "root_failure"],
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
_local = threading.local()  # Thread-local storage for trace context


def _execute_tool(name: str, args: dict, trace_text: str = "") -> str:

    # ── Phase 1: Graph Construction ──────────────────────────────────────
    if name == "build_trace_graph":
        result = algo_analyze_trace(trace_text)
        _local.trace_context = result
        return json.dumps({
            "status": "graph_built",
            "total_duration_ms": result.get("total_duration_ms", 0),
            "total_spans": result.get("total_spans", 0),
            "failed_spans": result.get("failed_spans", 0),
            "services_involved": result.get("services_involved", []),
            "root_failure": result.get("root_failure"),
            "cascaded_failure_count": result.get("cascaded_failure_count", 0),
            "silent_corruption_count": len(result.get("silent_corruptions", [])),
        })

    elif name == "get_critical_path":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        return json.dumps({"critical_path": _local.trace_context.get("critical_path", [])})

    elif name == "get_root_failure":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        return json.dumps({"root_failure": _local.trace_context.get("root_failure")})

    elif name == "get_cascaded_failures":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        failures = _local.trace_context.get("failure_propagation", [])
        cascaded = [f for f in failures if f.get("type") == "cascaded"]
        return json.dumps({"cascaded_failures": cascaded, "count": len(cascaded)})

    elif name == "get_silent_corruptions":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        return json.dumps({
            "silent_corruptions": _local.trace_context.get("silent_corruptions", []),
            "count": len(_local.trace_context.get("silent_corruptions", []))
        })

    elif name == "get_service_latency_breakdown":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        return json.dumps({"latency_breakdown": _local.trace_context.get("latency_breakdown", {})})

    elif name == "search_span":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})
        service = args.get("service_name", "").lower()
        chain = _local.trace_context.get("call_chain", [])
        matching = [s for s in chain if service in s.get("service", "").lower()]
        return json.dumps({"service_name": service, "matching_spans": matching, "count": len(matching)})

    # ── Phase 2: Deep Analysis ───────────────────────────────────────────
    elif name == "compute_latency_attribution":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        chain = _local.trace_context.get("call_chain", [])
        # Compute self-time for each span: duration - sum(child durations)
        span_by_id = {s.get("span_id", i): s for i, s in enumerate(chain)}
        service_self_time = defaultdict(float)
        service_total_time = defaultdict(float)

        for span in chain:
            svc = span.get("service", "unknown")
            dur = span.get("duration_ms", 0)
            service_total_time[svc] += dur

            # Estimate child time (spans that started during this span)
            child_time = 0
            for other in chain:
                if other.get("parent_id") == span.get("span_id"):
                    child_time += other.get("duration_ms", 0)
            self_time = max(0, dur - child_time)
            service_self_time[svc] += self_time

        total = sum(service_self_time.values()) or 1
        attribution = sorted([
            {
                "service": svc,
                "self_time_ms": round(st, 1),
                "total_time_ms": round(service_total_time[svc], 1),
                "self_time_pct": round(st / total * 100, 1),
                "is_bottleneck": st / total > 0.3,
            }
            for svc, st in service_self_time.items()
        ], key=lambda x: x["self_time_ms"], reverse=True)

        return json.dumps({
            "attribution": attribution,
            "top_contributor": attribution[0]["service"] if attribution else "unknown",
        })

    elif name == "score_cascade_severity":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        chain = _local.trace_context.get("call_chain", [])
        failures = [s for s in chain if s.get("status", "").upper() in ("ERROR", "FAIL", "500")]

        # BFS from each failure to count downstream impact
        children = defaultdict(list)
        for s in chain:
            pid = s.get("parent_id")
            if pid:
                children[pid].append(s)

        scored = []
        for fail in failures:
            # Count all descendants
            queue = [fail]
            blast = 0
            visited = set()
            while queue:
                current = queue.pop(0)
                sid = current.get("span_id", "")
                if sid in visited:
                    continue
                visited.add(sid)
                for child in children.get(sid, []):
                    blast += 1
                    queue.append(child)

            scored.append({
                "service": fail.get("service", "?"),
                "operation": fail.get("operation", "?"),
                "error": fail.get("error", ""),
                "blast_radius": blast,
                "severity": "CRITICAL" if blast >= 3 else "HIGH" if blast >= 1 else "MEDIUM",
            })

        scored.sort(key=lambda x: x["blast_radius"], reverse=True)
        return json.dumps({"cascade_scores": scored, "total_failures": len(failures)})

    elif name == "detect_bottlenecks":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        chain = _local.trace_context.get("call_chain", [])
        total_dur = _local.trace_context.get("total_duration_ms", 1) or 1

        durations = [s.get("duration_ms", 0) for s in chain]
        avg_dur = sum(durations) / max(len(durations), 1)

        bottlenecks = []
        for s in chain:
            dur = s.get("duration_ms", 0)
            pct = dur / total_dur * 100
            is_bottleneck = pct > 30 or dur > avg_dur * 2

            if is_bottleneck:
                bottlenecks.append({
                    "service": s.get("service", "?"),
                    "operation": s.get("operation", "?"),
                    "duration_ms": dur,
                    "pct_of_total": round(pct, 1),
                    "ratio_to_avg": round(dur / max(avg_dur, 0.1), 1),
                    "reason": "high_pct" if pct > 30 else "outlier_duration",
                })

        bottlenecks.sort(key=lambda x: x["pct_of_total"], reverse=True)
        return json.dumps({
            "bottlenecks": bottlenecks,
            "count": len(bottlenecks),
            "avg_span_duration_ms": round(avg_dur, 1),
            "total_trace_duration_ms": total_dur,
        })

    elif name == "infer_service_dependencies":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        chain = _local.trace_context.get("call_chain", [])
        span_by_id = {s.get("span_id", i): s for i, s in enumerate(chain)}

        edges = defaultdict(lambda: {"count": 0, "total_latency_ms": 0, "errors": 0})
        for s in chain:
            pid = s.get("parent_id")
            if pid and pid in span_by_id:
                parent_svc = span_by_id[pid].get("service", "?")
                child_svc = s.get("service", "?")
                if parent_svc != child_svc:
                    key = f"{parent_svc} → {child_svc}"
                    edges[key]["count"] += 1
                    edges[key]["total_latency_ms"] += s.get("duration_ms", 0)
                    if s.get("status", "").upper() in ("ERROR", "FAIL", "500"):
                        edges[key]["errors"] += 1

        dep_graph = {}
        for edge, stats in edges.items():
            dep_graph[edge] = {
                "call_count": stats["count"],
                "avg_latency_ms": round(stats["total_latency_ms"] / max(stats["count"], 1), 1),
                "error_count": stats["errors"],
                "error_rate": round(stats["errors"] / max(stats["count"], 1), 2),
            }

        return json.dumps({
            "service_dependencies": dep_graph,
            "edge_count": len(dep_graph),
            "services": list(set(
                svc for edge in edges.keys() for svc in edge.split(" → ")
            )),
        })

    elif name == "simulate_failure_removal":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        target_service = args.get("span_service", "").lower()
        chain = _local.trace_context.get("call_chain", [])

        # Find the target failure span
        target_spans = [s for s in chain
                       if target_service in s.get("service", "").lower()
                       and s.get("status", "").upper() in ("ERROR", "FAIL", "500")]

        if not target_spans:
            return json.dumps({"error": f"No failed span found for service: {target_service}"})

        # Simulate: remove this failure and check downstream
        target_ids = {s.get("span_id") for s in target_spans}
        remaining_failures = [s for s in chain
                            if s.get("status", "").upper() in ("ERROR", "FAIL", "500")
                            and s.get("span_id") not in target_ids
                            and s.get("parent_id") not in target_ids]

        would_fix = len(remaining_failures) == 0
        return json.dumps({
            "simulated_removal": target_service,
            "removed_failures": len(target_spans),
            "remaining_failures": len(remaining_failures),
            "remaining_failure_services": [s.get("service") for s in remaining_failures],
            "would_fix_trace": would_fix,
            "conclusion": (
                f"Removing {target_service} failures WOULD fix the trace — this is the true root cause."
                if would_fix else
                f"Removing {target_service} failures would NOT fix the trace — "
                f"{len(remaining_failures)} other failure(s) remain from: "
                f"{', '.join(set(s.get('service','?') for s in remaining_failures))}"
            ),
        })

    elif name == "compare_span_timings":
        if not getattr(_local, 'trace_context', None):
            return json.dumps({"error": "Must call build_trace_graph first"})

        chain = _local.trace_context.get("call_chain", [])
        target = args.get("service_name", "").lower()

        # Group by service+operation
        groups = defaultdict(list)
        for s in chain:
            svc = s.get("service", "?")
            if target and target not in svc.lower():
                continue
            key = f"{svc}/{s.get('operation', '?')}"
            groups[key].append(s.get("duration_ms", 0))

        comparisons = []
        for key, durations in groups.items():
            if len(durations) < 1:
                continue
            avg = sum(durations) / len(durations)
            outliers = [d for d in durations if avg > 0 and d > avg * 2]
            comparisons.append({
                "service_operation": key,
                "span_count": len(durations),
                "avg_duration_ms": round(avg, 1),
                "min_ms": round(min(durations), 1),
                "max_ms": round(max(durations), 1),
                "outlier_count": len(outliers),
                "has_outliers": len(outliers) > 0,
            })

        return json.dumps({"comparisons": comparisons, "groups_analyzed": len(comparisons)})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — uses ReActEngine
# ─────────────────────────────────────────────────────────────────────────────
def analyze_trace(trace_text: str) -> dict:
    _local.trace_context = {}

    engine = ReActEngine(
        agent_name="TraceAgent",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        finish_tool=FINISH_TOOL,
        tool_executor=lambda name, args, **kw: _execute_tool(name, args, trace_text),
        max_iterations=10,
        confidence_threshold=0.7,
        reflection_required=True,
    )

    result = engine.run(
        user_message=(
            f"Analyze this distributed trace:\n\n--- TRACE START ---\n{trace_text[:4000]}\n--- TRACE END ---\n\n"
            f"Use your tools to investigate thoroughly. Store findings in scratchpad. Reflect before finishing."
        ),
    )

    final_result = result.findings
    if not final_result:
        if not getattr(_local, 'trace_context', None):
            _local.trace_context = algo_analyze_trace(trace_text)
        final_result = {
            "trace_id": "UNKNOWN",
            "total_duration_ms": _local.trace_context.get("total_duration_ms", 0),
            "call_chain": _local.trace_context.get("call_chain", []),
            "root_failure": _local.trace_context.get("root_failure"),
            "failure_propagation": _local.trace_context.get("failure_propagation", []),
            "latency_breakdown": _local.trace_context.get("latency_breakdown", {}),
            "silent_corruptions": _local.trace_context.get("silent_corruptions", []),
            "critical_path": _local.trace_context.get("critical_path", []),
            "fix_targets": _local.trace_context.get("fix_targets", []),
            "summary": "Fallback to algorithm — agent did not complete analysis",
        }

    return final_result
