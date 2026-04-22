# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
DependencyGraphAgent — Service dependency graph analysis with PageRank blame scoring.

Tools build and analyze a directed graph of service dependencies, detecting critical
paths and single points of failure using graph algorithms:
  - build_graph_from_traces(trace_data_json)    → construct graph from trace spans
  - build_graph_from_code(code_issues_json)     → infer dependencies from code issues
  - run_pagerank(error_bias_json)               → weighted PageRank blame attribution
  - find_blast_radius(root_service)             → BFS to find all downstream effects
  - find_critical_dependencies()                → articulation point detection
  - get_graph_stats()                           → node/edge metrics
  - finish_analysis(...)                        → submit final graph analysis
"""
import os, sys, json, re
from llm_client import get_client, get_model
from dotenv import load_dotenv
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.pagerank import ServiceGraph, PageRankScorer

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# Module-level graph state (persists across tool calls within one invocation)
_graph = None

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "build_graph_from_traces",
            "description": "Build service dependency graph from distributed trace spans. Parses service names and parent-child relationships.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trace_data_json": {
                        "type": "string",
                        "description": "JSON string of trace data: [{span_id, parent_id, service_name, operation, timestamp}, ...]"
                    }
                },
                "required": ["trace_data_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_graph_from_code",
            "description": "Build dependency graph from code agent's cross-service issues. Infers edges from HTTP calls, imports, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code_issues_json": {
                        "type": "string",
                        "description": "JSON string of code issues: [{service, issue_type, calls_service, ...}, ...]"
                    }
                },
                "required": ["code_issues_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pagerank",
            "description": "Run error-weighted PageRank on the built graph to rank services by blame. Services with more incoming errors get higher scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "error_bias_json": {
                        "type": "string",
                        "description": "JSON string of {service_name: error_count} to bias PageRank towards error sources"
                    }
                },
                "required": ["error_bias_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_blast_radius",
            "description": "Find all services downstream from a root service using BFS. Shows what fails if root service fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_service": {"type": "string", "description": "Service name to start BFS from"}
                },
                "required": ["root_service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_critical_dependencies",
            "description": "Detect articulation points: services that are single points of failure. If they fail, the system fragments.",
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
            "name": "get_graph_stats",
            "description": "Return graph topology metrics: node count, edge count, most connected service, average degree.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit final graph analysis with blame ranking, blast radius, critical services, and topology metrics.",
        "parameters": {
            "type": "object",
            "properties": {
                "blame_ranking": {
                    "type": "array",
                    "description": "Services ranked by PageRank blame score (highest = most central to failure)",
                    "items": {"type": "object"}
                },
                "blast_radius": {
                    "type": "object",
                    "description": "For each suspected root, downstream services affected"
                },
                "critical_dependencies": {
                    "type": "array",
                    "description": "Services that are articulation points (single points of failure)",
                    "items": {"type": "string"}
                },
                "graph_stats": {
                    "type": "object",
                    "description": "Topology metrics"
                },
                "summary": {"type": "string", "description": "Summary of dependency analysis"}
            },
            "required": ["blame_ranking", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are DependencyGraphAgent, an AI agent analyzing service dependencies.

You have tools to build a directed graph of services, detect critical paths, and rank
services by their likely responsibility for the incident (using PageRank weighted by errors).

Investigation strategy:
1. Build the graph from trace data and code issues to get topology.
2. Run PageRank with error bias to rank services by centrality and error correlation.
3. Find the blast radius from suspected root causes.
4. Identify critical dependencies (articulation points).
5. Get graph statistics to assess complexity and resilience.

Submit your final analysis with blame ranking, critical dependencies, and summary.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call. Pure algorithmic, no LLM."""
    global _graph

    try:
        if name == "build_graph_from_traces":
            trace_data = json.loads(args.get("trace_data_json", "[]"))
            _graph = ServiceGraph()

            for span in trace_data:
                service = span.get("service_name", "unknown")
                parent_service = None

                # Infer parent service from parent_id if available
                if span.get("parent_id"):
                    for other in trace_data:
                        if other.get("span_id") == span.get("parent_id"):
                            parent_service = other.get("service_name")
                            break

                _graph.add_node(service)
                if parent_service and parent_service != service:
                    _graph.add_edge(parent_service, service)

            return json.dumps({
                "status": "built from traces",
                "nodes": len(_graph.nodes()),
                "edges": len(_graph.edges()),
            })

        elif name == "build_graph_from_code":
            code_issues = json.loads(args.get("code_issues_json", "[]"))
            if _graph is None:
                _graph = ServiceGraph()

            for issue in code_issues:
                service = issue.get("service", "unknown")
                calls = issue.get("calls_service")
                if calls:
                    _graph.add_node(service)
                    _graph.add_node(calls)
                    _graph.add_edge(service, calls)

            return json.dumps({
                "status": "built from code",
                "nodes": len(_graph.nodes()),
                "edges": len(_graph.edges()),
            })

        elif name == "run_pagerank":
            if _graph is None:
                return json.dumps({"error": "Graph not built. Call build_graph_from_traces first."})

            error_bias = json.loads(args.get("error_bias_json", "{}"))
            scorer = PageRankScorer(_graph, error_bias=error_bias)
            scores = scorer.rank()

            ranking = [
                {"service": svc, "score": round(score, 4), "rank": i+1}
                for i, (svc, score) in enumerate(scores)
            ]

            return json.dumps({"ranking": ranking, "total_services": len(scores)})

        elif name == "find_blast_radius":
            if _graph is None:
                return json.dumps({"error": "Graph not built."})

            root = args.get("root_service", "unknown")
            downstream = set()
            queue = deque([root])
            visited = {root}

            while queue:
                current = queue.popleft()
                for neighbor in _graph.successors(current):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        downstream.add(neighbor)
                        queue.append(neighbor)

            return json.dumps({
                "root_service": root,
                "downstream_services": sorted(list(downstream)),
                "blast_radius_count": len(downstream)
            })

        elif name == "find_critical_dependencies":
            if _graph is None:
                return json.dumps({"error": "Graph not built."})

            # Tarjan's algorithm for articulation points
            critical = _graph.find_articulation_points()

            return json.dumps({
                "articulation_points": sorted(list(critical)),
                "count": len(critical),
                "interpretation": "Services that are single points of failure"
            })

        elif name == "get_graph_stats":
            if _graph is None:
                return json.dumps({"error": "Graph not built."})

            nodes = list(_graph.nodes())
            edges = list(_graph.edges())

            in_degrees = {n: _graph.in_degree(n) for n in nodes}
            out_degrees = {n: _graph.out_degree(n) for n in nodes}

            most_connected = max(nodes, key=lambda n: in_degrees[n] + out_degrees[n]) if nodes else None
            avg_degree = sum(in_degrees.values()) / len(nodes) if nodes else 0

            return json.dumps({
                "node_count": len(nodes),
                "edge_count": len(edges),
                "most_connected_service": most_connected,
                "average_degree": round(avg_degree, 2),
                "services": sorted(nodes),
            })

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e), "traceback": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def analyze_dependencies(trace_data: dict, code_issues: list, anomalies: list = None) -> dict:
    """
    Analyze service dependencies using PageRank and graph algorithms.

    Args:
        trace_data: dict with span list
        code_issues: list of code issues
        anomalies: list of detected anomalies (for error bias)
    """
    # Prepare error bias from anomalies
    error_bias = defaultdict(int)
    if anomalies:
        for anom in anomalies:
            service = anom.get("service", "unknown")
            error_bias[service] += 1

    # Build context for LLM
    trace_json = json.dumps(trace_data.get("spans", []))
    code_json = json.dumps(code_issues)
    error_bias_json = json.dumps(dict(error_bias))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
         f"Analyze service dependencies to find root causes and single points of failure.\n\n"
         f"Trace data: {trace_json[:1000]}\n"
         f"Code issues: {code_json[:500]}\n"
         f"Error bias: {error_bias_json}\n\n"
         f"Use your tools to build graphs, run PageRank, find blast radius, identify critical paths, and submit analysis."},
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
        final_result = {"blame_ranking": [], "summary": "Analysis incomplete"}

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_trace = {
        "spans": [
            {"span_id": "1", "parent_id": None, "service_name": "java-order-service", "operation": "checkout"},
            {"span_id": "2", "parent_id": "1", "service_name": "python-inventory-service", "operation": "reserve"},
            {"span_id": "3", "parent_id": "1", "service_name": "node-notification-service", "operation": "notify"},
        ]
    }

    sample_code_issues = [
        {"service": "java-order-service", "issue_type": "http_call", "calls_service": "python-inventory-service"},
        {"service": "java-order-service", "issue_type": "http_call", "calls_service": "node-notification-service"},
    ]

    sample_anomalies = [
        {"service": "python-inventory-service", "severity": "ERROR"},
        {"service": "python-inventory-service", "severity": "ERROR"},
        {"service": "node-notification-service", "severity": "ERROR"},
    ]

    result = analyze_dependencies(sample_trace, sample_code_issues, sample_anomalies)
    print(json.dumps(result, indent=2))
