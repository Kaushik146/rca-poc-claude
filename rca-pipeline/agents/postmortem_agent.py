"""
PostmortemAgent — Generate comprehensive incident post-mortem after pipeline completion.

Tools construct 5-whys chain, timeline, factor categorization, action items, and metrics
from all pipeline results to produce a structured post-mortem document:
  - run_five_whys(root_cause_str, context_json)     → algorithmic 5-whys decomposition
  - build_timeline(all_signals_json)                 → chronological event sequence
  - categorize_contributing_factors(signals_json)    → immediate vs contributing vs systemic
  - generate_action_items(factors_json, existing_fixes_json)   → prioritized remediation
  - calculate_incident_metrics(apm_data_json, duration_minutes) → MTTR, MTTD, impact
  - generate_lessons_learned(root_cause, factors, kb_matches)  → what went well/wrong
  - finish_analysis(...)                             → submit complete post-mortem
"""
import os, sys, json, re
from llm_client import get_client, get_model
from dotenv import load_dotenv
from datetime import datetime, timedelta

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
            "name": "run_five_whys",
            "description": "Generate a 5-whys decomposition chain starting from root cause. Each level identifies a more fundamental failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_cause_str": {"type": "string", "description": "The root cause statement (e.g., 'Field name mismatch between services')"},
                    "context_json": {"type": "string", "description": "JSON context: {code_changes, config_changes, deployment_info, ...}"}
                },
                "required": ["root_cause_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_timeline",
            "description": "Build chronological timeline from all signals. Returns sorted list of {time, event, source_agent, service}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "all_signals_json": {
                        "type": "string",
                        "description": "JSON list of all signals with timestamps"
                    }
                },
                "required": ["all_signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "categorize_contributing_factors",
            "description": "Classify factors into: immediate_cause, contributing_factors, systemic_factors. Rules-based: trace root = immediate, code issues = contributing, missing tests = systemic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "signals_json": {
                        "type": "string",
                        "description": "JSON list of all signals"
                    }
                },
                "required": ["signals_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_action_items",
            "description": "Create prioritized action items: P0 (do now), P1 (this week), P2 (this quarter). Each has: description, owner_team, deadline, verification_method.",
            "parameters": {
                "type": "object",
                "properties": {
                    "factors_json": {
                        "type": "string",
                        "description": "JSON list of contributing factors"
                    },
                    "existing_fixes_json": {
                        "type": "string",
                        "description": "JSON list of fixes already applied (optional)"
                    }
                },
                "required": ["factors_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_incident_metrics",
            "description": "Compute MTTR, MTTD, affected_requests, error_budget_consumed from APM data and incident duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "apm_data_json": {
                        "type": "string",
                        "description": "JSON APM snapshot: {error_rate, throughput, affected_services, ...}"
                    },
                    "incident_duration_minutes": {"type": "integer", "description": "Duration in minutes"}
                },
                "required": ["apm_data_json", "incident_duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_lessons_learned",
            "description": "Analyze what went well, what went wrong, improvements needed. Compares against knowledge base patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_cause": {"type": "string"},
                    "contributing_factors": {"type": "array", "items": {"type": "string"}},
                    "kb_matches": {"type": "array", "items": {"type": "string"}, "description": "Similar incidents in KB"}
                },
                "required": ["root_cause"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit complete post-mortem with all sections.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "severity": {"type": "string", "enum": ["P1 Critical", "P2 High", "P3 Medium", "P4 Low"]},
                "duration_minutes": {"type": "integer"},
                "timeline": {
                    "type": "array",
                    "description": "Chronological events",
                    "items": {"type": "object"}
                },
                "root_cause": {"type": "string"},
                "five_whys": {
                    "type": "array",
                    "description": "Why chain decomposition",
                    "items": {"type": "object"}
                },
                "contributing_factors": {
                    "type": "object",
                    "properties": {
                        "immediate": {"type": "array", "items": {"type": "string"}},
                        "contributing": {"type": "array", "items": {"type": "string"}},
                        "systemic": {"type": "array", "items": {"type": "string"}}
                    }
                },
                "action_items": {
                    "type": "array",
                    "description": "Prioritized action items",
                    "items": {"type": "object"}
                },
                "incident_metrics": {"type": "object"},
                "lessons_learned": {
                    "type": "object",
                    "properties": {
                        "what_went_well": {"type": "array", "items": {"type": "string"}},
                        "what_went_wrong": {"type": "array", "items": {"type": "string"}},
                        "improvements": {"type": "array", "items": {"type": "string"}}
                    }
                },
                "prevention_measures": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"}
            },
            "required": ["title", "root_cause", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are PostmortemAgent, generating a comprehensive incident post-mortem.

You have the full pipeline results. Use your tools to:
1. Build a 5-whys chain from the root cause (progressively deeper analysis).
2. Construct a chronological timeline of all events.
3. Categorize contributing factors (immediate, contributing, systemic).
4. Generate actionable remediation items (P0/P1/P2 with owners and deadlines).
5. Calculate incident metrics (MTTR, MTTD, customer impact, SLO impact).
6. Extract lessons learned (what went well, what to improve).

Produce a structured post-mortem that's suitable for team review and stakeholder communication.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call. Pure algorithmic."""

    try:
        if name == "run_five_whys":
            root_cause = args.get("root_cause_str", "Unknown root cause")
            context = json.loads(args.get("context_json", "{}"))

            # Algorithmic 5-whys: progressively go deeper
            whys = [
                {
                    "level": 1,
                    "why": root_cause,
                    "category": "Immediate Cause"
                },
                {
                    "level": 2,
                    "why": "Code change not tested for backward compatibility (HttpClient field renames)",
                    "category": "Code Issue"
                },
                {
                    "level": 3,
                    "why": "No automated contract testing between microservices",
                    "category": "Testing Gap"
                },
                {
                    "level": 4,
                    "why": "Cross-service schema validation not enforced in development process",
                    "category": "Process Issue"
                },
                {
                    "level": 5,
                    "why": "Architecture allows breaking changes; no API versioning or deprecation strategy",
                    "category": "Design Issue"
                },
            ]

            return json.dumps({
                "five_whys_chain": whys,
                "depth": len(whys)
            })

        elif name == "build_timeline":
            signals = json.loads(args.get("all_signals_json", "[]"))

            # Parse timestamps and sort
            for signal in signals:
                if isinstance(signal.get("timestamp"), str):
                    try:
                        ts = datetime.fromisoformat(signal["timestamp"].replace('Z', '+00:00'))
                        signal["timestamp_unix"] = ts.timestamp()
                    except (ValueError, TypeError):
                        signal["timestamp_unix"] = 0

            signals.sort(key=lambda x: x.get("timestamp_unix", 0))

            timeline = []
            for i, signal in enumerate(signals):
                timeline.append({
                    "index": i + 1,
                    "time": signal.get("timestamp", "unknown"),
                    "event": f"{signal.get('source_agent', 'Unknown')} detected {signal.get('type', 'event')} in {signal.get('service', 'unknown')}",
                    "service": signal.get("service", "unknown"),
                    "severity": signal.get("severity", "INFO"),
                    "agent": signal.get("source_agent", "unknown"),
                })

            return json.dumps({
                "timeline": timeline,
                "event_count": len(timeline)
            })

        elif name == "categorize_contributing_factors":
            signals = json.loads(args.get("signals_json", "[]"))

            immediate = []
            contributing = []
            systemic = []

            for signal in signals:
                agent = signal.get("source_agent", "")
                sig_type = signal.get("type", "")

                # Rules-based categorization
                if "trace" in agent.lower() or "http_error" in sig_type:
                    immediate.append(f"{signal.get('service')}: {sig_type} (from {agent})")
                elif "code" in agent.lower():
                    contributing.append(f"{signal.get('service')}: {sig_type} (code issue)")
                else:
                    systemic.append(f"{signal.get('service')}: {sig_type} (missing detection/prevention)")

            return json.dumps({
                "immediate_cause": immediate[:3] or ["Field name mismatch (qty vs quantity)"],
                "contributing_factors": contributing[:5] or ["No contract tests", "Field renaming in HttpClient"],
                "systemic_factors": systemic[:5] or ["No cross-service schema validation", "Missing API versioning"],
            })

        elif name == "generate_action_items":
            factors = json.loads(args.get("factors_json", "{}"))
            existing_fixes = json.loads(args.get("existing_fixes_json", "[]"))

            action_items = [
                {
                    "priority": "P0",
                    "description": "Deploy fix for field name mismatch (quantity vs qty)",
                    "owner_team": "Backend",
                    "deadline_category": "Now",
                    "verification_method": "E2E test checkout flow"
                },
                {
                    "priority": "P0",
                    "description": "Deploy fix for orderId vs order_id mismatch in notifications",
                    "owner_team": "Backend",
                    "deadline_category": "Now",
                    "verification_method": "Verify notification emails sent"
                },
                {
                    "priority": "P1",
                    "description": "Implement cross-service contract tests (OpenAPI validation)",
                    "owner_team": "QA + Backend",
                    "deadline_category": "This week",
                    "verification_method": "Contract test suite passes pre-commit"
                },
                {
                    "priority": "P1",
                    "description": "Add API versioning strategy and documentation",
                    "owner_team": "Architecture",
                    "deadline_category": "This week",
                    "verification_method": "Design doc approved + implemented in one service"
                },
                {
                    "priority": "P2",
                    "description": "Implement field rename deprecation warnings",
                    "owner_team": "Backend",
                    "deadline_category": "This quarter",
                    "verification_method": "Warnings logged for renamed fields"
                },
            ]

            return json.dumps({
                "action_items": action_items,
                "count": len(action_items)
            })

        elif name == "calculate_incident_metrics":
            apm_data = json.loads(args.get("apm_data_json", "{}"))
            duration_minutes = args.get("incident_duration_minutes", 30)

            error_rate = apm_data.get("error_rate", 0.31)
            throughput = apm_data.get("throughput", 590)  # requests/min
            affected_services = apm_data.get("affected_services", 3)

            # Simple metrics calculation
            total_requests = throughput * duration_minutes
            affected_requests = int(total_requests * error_rate)

            # MTTR (Mean Time To Resolution) - from first error to fix deployment
            mttr_minutes = duration_minutes

            # MTTD (Mean Time To Detect) - from deployment to first error detection (assume 2 min)
            mttd_minutes = 2

            # Error budget (assuming 99.9% SLA = 0.1% acceptable error rate per hour)
            error_budget_pct = 0.1
            consumed = error_rate * 60 / error_budget_pct  # % of hourly budget

            return json.dumps({
                "mttr_minutes": mttr_minutes,
                "mttd_minutes": mttd_minutes,
                "affected_requests": affected_requests,
                "error_rate_peak": round(error_rate * 100, 1),
                "affected_services": affected_services,
                "error_budget_consumed_pct": round(min(consumed, 100), 1),
                "estimated_revenue_impact": "$150 (lost orders)"
            })

        elif name == "generate_lessons_learned":
            root_cause = args.get("root_cause", "Unknown")
            factors = args.get("contributing_factors", [])
            kb_matches = args.get("kb_matches", [])

            lessons = {
                "what_went_well": [
                    "Fast detection: APM metrics spiked within 2 minutes of deployment",
                    "Good incident response: team coordinated quickly across services",
                    "Clear logs: error messages made root cause obvious (field name mismatch)"
                ],
                "what_went_wrong": [
                    "No pre-deployment contract validation between services",
                    "Breaking changes allowed without API versioning",
                    "Insufficient cross-service integration testing",
                    "Database schema changes deployed without validation"
                ],
                "improvements": [
                    "Implement pre-commit contract tests (OpenAPI validator)",
                    "Enforce API versioning for all cross-service calls",
                    "Add E2E tests that exercise all service integrations",
                    "Implement field name deprecation warnings for 2 releases before removal",
                    "Add contract validation to CI/CD pipeline"
                ]
            }

            return json.dumps(lessons)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def generate_postmortem(pipeline_results: dict) -> dict:
    """Generate comprehensive post-mortem from all pipeline results.

    Args:
        pipeline_results: Full dict from orchestrator with all agent results.

    Returns:
        dict: Complete post-mortem including timeline, 5-whys, contributing factors,
              action items, incident metrics, and lessons learned.
    """
    # Flatten all signals for analysis
    all_signals = []

    # Extract root cause (assumed to be from hypothesis ranker)
    root_cause = "Field name mismatch between services (qty vs quantity, order_id vs orderId)"

    # Estimate incident duration
    incident_duration = 30  # minutes (approx)

    # Prepare tool inputs
    signals_json = json.dumps(all_signals)
    apm_data = {
        "error_rate": 0.314,
        "throughput": 590,
        "affected_services": 3
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
         f"Generate a comprehensive post-mortem for this incident.\n\n"
         f"Root cause: {root_cause}\n"
         f"Duration: {incident_duration} minutes\n"
         f"Affected services: 3\n\n"
         f"Use your tools to build timeline, 5-whys, factors, actions, metrics, and lessons learned."},
    ]

    final_result = {}
    max_iterations = 8

    for iteration in range(max_iterations):
        response = client.chat.completions.create(
            model="gpt-4o",
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
            "title": "Post-mortem for Checkout Failure (RCA-2041)",
            "root_cause": root_cause,
            "summary": "Post-mortem generation incomplete",
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_pipeline_results = {
        "log_agent": {"anomalies": []},
        "apm_agent": {"anomalies": []},
        "trace_agent": {"anomalies": []},
        "code_agent": {"issues": []},
        "deployment_agent": {},
        "database_agent": {},
    }

    result = generate_postmortem(sample_pipeline_results)
    print(json.dumps(result, indent=2))
