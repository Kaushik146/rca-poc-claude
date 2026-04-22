# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
ImpactAssessmentAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

This agent quantifies business impact from root cause hypotheses using pure algorithmic
calculation tools (no LLM in the calculations).

Tools available:
  - calculate_revenue_impact(error_rate_pct, throughput_per_min, avg_order_value_usd, incident_duration_minutes)
  - check_sla_breach(current_availability_pct, sla_target_pct, incident_duration_minutes)
  - estimate_affected_users(error_rate_pct, total_daily_active_users, incident_duration_minutes)
  - determine_severity_tier(revenue_per_min, affected_users_estimate, is_sla_breached)
  - generate_customer_comms_template(severity_tier, affected_feature, estimated_resolution_minutes)
  - finish_analysis(severity, urgency, revenue_impact, affected_users, sla_status, escalation_plan, rollback_decision, customer_comms_template, immediate_actions)
"""
import os, sys, json, math
from llm_client import get_client, get_model
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_revenue_impact",
            "description": (
                "Pure math: calculate revenue impact from error rate, throughput, and order value. "
                "Returns affected_orders_per_min, revenue_at_risk_per_min, total_lost_revenue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "error_rate_pct": {"type": "number", "description": "Error rate as percentage (0-100)"},
                    "throughput_per_min": {"type": "number", "description": "Throughput in orders/requests per minute"},
                    "avg_order_value_usd": {"type": "number", "description": "Average order value in USD"},
                    "incident_duration_minutes": {"type": "number", "description": "How long incident has been ongoing"},
                },
                "required": ["error_rate_pct", "throughput_per_min", "avg_order_value_usd", "incident_duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_sla_breach",
            "description": (
                "Pure math: check if SLA is breached. Calculates monthly allowable downtime and current usage. "
                "Returns is_breached, allowable_minutes, used_minutes, remaining_minutes, penalty_risk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "current_availability_pct": {"type": "number", "description": "Current availability percentage (0-100)"},
                    "sla_target_pct": {"type": "number", "description": "SLA target availability (e.g. 99.9)"},
                    "incident_duration_minutes": {"type": "number", "description": "Duration of current incident"},
                },
                "required": ["current_availability_pct", "sla_target_pct", "incident_duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_affected_users",
            "description": (
                "Pure math: estimate how many users are affected. "
                "Returns user_count estimate based on DAU, error rate, and incident duration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "error_rate_pct": {"type": "number", "description": "Error rate as percentage"},
                    "total_daily_active_users": {"type": "number", "description": "Total DAU (default 5000)"},
                    "incident_duration_minutes": {"type": "number", "description": "Duration in minutes"},
                },
                "required": ["error_rate_pct", "total_daily_active_users", "incident_duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "determine_severity_tier",
            "description": (
                "Rules-based: determine P0/P1/P2 severity based on revenue impact, affected users, and SLA. "
                "Returns tier, escalation_required, page_oncall, rollback_recommended."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "revenue_per_min": {"type": "number", "description": "Revenue at risk per minute"},
                    "affected_users_estimate": {"type": "number", "description": "Estimated affected users"},
                    "is_sla_breached": {"type": "boolean", "description": "Whether SLA is breached"},
                },
                "required": ["revenue_per_min", "affected_users_estimate", "is_sla_breached"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_customer_comms_template",
            "description": (
                "Returns a template string for customer communication based on severity tier. "
                "Ready to be filled in with specifics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "severity_tier": {"type": "string", "description": "P0|P1|P2|P3"},
                    "affected_feature": {"type": "string", "description": "e.g. 'checkout', 'order processing'"},
                    "estimated_resolution_minutes": {"type": "number", "description": "ETA in minutes"},
                },
                "required": ["severity_tier", "affected_feature", "estimated_resolution_minutes"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit final impact assessment. Call when analysis is complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "description": "P0|P1|P2|P3"},
                "urgency": {"type": "string", "description": "IMMEDIATE|HIGH|MEDIUM|LOW"},
                "revenue_impact": {"type": "object", "description": "Revenue impact breakdown"},
                "affected_users": {"type": "object", "description": "User impact estimate"},
                "sla_status": {"type": "object", "description": "SLA breach info"},
                "escalation_plan": {"type": "array", "description": "List of escalation actions"},
                "rollback_decision": {"type": "object", "description": "Rollback recommendation"},
                "customer_comms_template": {"type": "string", "description": "Template for customer communication"},
                "immediate_actions": {"type": "array", "description": "Immediate mitigation steps"},
            },
            "required": ["severity", "urgency", "revenue_impact", "affected_users"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are ImpactAssessmentAgent, an AI agent in an incident Root Cause Analysis pipeline.

Given root cause hypotheses and APM/ticket data, your job is to:
1. Calculate revenue impact using pure math tools
2. Check SLA breach status
3. Estimate affected users
4. Determine severity tier (P0/P1/P2)
5. Generate customer communication template
6. Submit final assessment via finish_analysis()

Every number must be calculated using tools (no made-up estimates). Use safe defaults
if values are missing (avg_order_value=$250, total_dau=5000, etc.).
"""

# ─────────────────────────────────────────────────────────────────────────────
# Pure algorithmic tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def _calculate_revenue_impact(error_rate_pct: float, throughput_per_min: float,
                              avg_order_value_usd: float, incident_duration_minutes: float) -> dict:
    """
    Pure math: affected_orders_per_min = throughput * (error_rate/100)
    revenue_at_risk_per_min = affected_orders * avg_order_value
    total_lost = revenue_at_risk * incident_duration
    """
    affected_orders_per_min = throughput_per_min * (error_rate_pct / 100.0)
    revenue_at_risk_per_min = affected_orders_per_min * avg_order_value_usd
    total_lost_revenue = revenue_at_risk_per_min * incident_duration_minutes

    return {
        "affected_orders_per_min": round(affected_orders_per_min, 2),
        "revenue_at_risk_per_min_usd": round(revenue_at_risk_per_min, 2),
        "total_lost_revenue_usd": round(total_lost_revenue, 2),
        "incident_duration_minutes": incident_duration_minutes,
    }


def _check_sla_breach(current_availability_pct: float, sla_target_pct: float,
                      incident_duration_minutes: float) -> dict:
    """
    Pure math: is_breached = current_availability < sla_target
    Monthly allowable_downtime_minutes = (1 - sla_target/100) * 43200
    """
    is_breached = current_availability_pct < sla_target_pct

    # Monthly minutes: 30 days * 24 hrs * 60 min = 43200
    monthly_minutes = 30 * 24 * 60
    allowable_downtime = monthly_minutes * (1.0 - sla_target_pct / 100.0)
    used_downtime = incident_duration_minutes
    remaining = allowable_downtime - used_downtime

    penalty_risk = "HIGH" if remaining <= 0 else ("MEDIUM" if remaining < allowable_downtime * 0.2 else "LOW")

    return {
        "is_breached": is_breached,
        "current_availability_pct": round(current_availability_pct, 2),
        "sla_target_pct": sla_target_pct,
        "allowable_downtime_minutes": round(allowable_downtime, 1),
        "used_downtime_minutes": round(used_downtime, 1),
        "remaining_downtime_minutes": round(remaining, 1),
        "penalty_risk": penalty_risk,
    }


def _estimate_affected_users(error_rate_pct: float, total_daily_active_users: float,
                             incident_duration_minutes: float) -> dict:
    """
    Math: users_per_minute = total_dau / 1440
    affected = users_per_minute * incident_duration * (error_rate/100)
    """
    minutes_per_day = 24 * 60
    users_per_minute = total_daily_active_users / minutes_per_day
    affected_users = users_per_minute * incident_duration_minutes * (error_rate_pct / 100.0)

    return {
        "total_daily_active_users": int(total_daily_active_users),
        "users_per_minute": round(users_per_minute, 2),
        "affected_users_estimate": int(affected_users),
        "error_rate_pct": error_rate_pct,
        "incident_duration_minutes": incident_duration_minutes,
    }


def _determine_severity_tier(revenue_per_min: float, affected_users_estimate: float,
                             is_sla_breached: bool) -> dict:
    """
    Rules-based:
    P0 if revenue_per_min > 100 OR is_sla_breached OR affected_users > 1000
    P1 if > 10/min OR > 100 users
    P2 otherwise
    """
    if revenue_per_min > 100 or is_sla_breached or affected_users_estimate > 1000:
        tier = "P0"
        escalation_required = True
        page_oncall = True
        rollback_recommended = True
    elif revenue_per_min > 10 or affected_users_estimate > 100:
        tier = "P1"
        escalation_required = True
        page_oncall = False
        rollback_recommended = False
    else:
        tier = "P2"
        escalation_required = False
        page_oncall = False
        rollback_recommended = False

    return {
        "severity_tier": tier,
        "escalation_required": escalation_required,
        "page_oncall": page_oncall,
        "rollback_recommended": rollback_recommended,
    }


def _generate_customer_comms_template(severity_tier: str, affected_feature: str,
                                      estimated_resolution_minutes: float) -> str:
    """
    Returns a template string for customer communication based on severity.
    """
    eta_str = f"{int(estimated_resolution_minutes)} minutes" if estimated_resolution_minutes < 60 else \
              f"{estimated_resolution_minutes / 60:.1f} hours"

    if severity_tier == "P0":
        return f"""
[URGENT] Service Degradation - {affected_feature}

We are currently experiencing a critical issue affecting {affected_feature}.
Estimated resolution time: {eta_str}

Our team is actively working on a fix. We will provide updates every 15 minutes.

For questions, please contact support@company.com
"""
    elif severity_tier == "P1":
        return f"""
[HIGH PRIORITY] Service Issue - {affected_feature}

We have identified an issue affecting {affected_feature}.
Our engineering team is working on a fix.
Estimated resolution time: {eta_str}

We apologize for the inconvenience and will keep you updated.

For questions, please contact support@company.com
"""
    else:
        return f"""
[UPDATE] Scheduled Maintenance - {affected_feature}

We are performing scheduled maintenance on {affected_feature}.
Expected completion time: {eta_str}

Thank you for your patience.

For questions, please contact support@company.com
"""


# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — runs the actual tool logic
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    try:
        if name == "calculate_revenue_impact":
            result = _calculate_revenue_impact(
                args.get("error_rate_pct", 0.0),
                args.get("throughput_per_min", 100.0),
                args.get("avg_order_value_usd", 250.0),
                args.get("incident_duration_minutes", 0.0),
            )
            return json.dumps(result)

        elif name == "check_sla_breach":
            result = _check_sla_breach(
                args.get("current_availability_pct", 99.0),
                args.get("sla_target_pct", 99.9),
                args.get("incident_duration_minutes", 0.0),
            )
            return json.dumps(result)

        elif name == "estimate_affected_users":
            result = _estimate_affected_users(
                args.get("error_rate_pct", 0.0),
                args.get("total_daily_active_users", 5000.0),
                args.get("incident_duration_minutes", 0.0),
            )
            return json.dumps(result)

        elif name == "determine_severity_tier":
            result = _determine_severity_tier(
                args.get("revenue_per_min", 0.0),
                args.get("affected_users_estimate", 0.0),
                args.get("is_sla_breached", False),
            )
            return json.dumps(result)

        elif name == "generate_customer_comms_template":
            result = _generate_customer_comms_template(
                args.get("severity_tier", "P2"),
                args.get("affected_feature", "service"),
                args.get("estimated_resolution_minutes", 30),
            )
            return json.dumps({"template": result})

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def assess_impact(hypotheses: list, apm_data: dict = None, jira_data: dict = None) -> dict:
    """
    Real tool-using ReAct agent. The LLM calls calculation tools autonomously
    and submits a final impact assessment via finish_analysis().

    Max iterations: 6
    """
    apm_data = apm_data or {}
    jira_data = jira_data or {}

    # Extract impact metrics from inputs (with safe defaults)
    error_rate_pct = float(apm_data.get("error_rate_pct", 0.0))
    throughput_per_min = float(apm_data.get("throughput_per_min", 100.0))
    avg_order_value_usd = float(jira_data.get("avg_order_value_usd", 250.0))
    incident_duration_minutes = float(apm_data.get("incident_duration_minutes", 10.0))
    sla_target_pct = float(apm_data.get("sla_target_pct", 99.9))
    current_availability_pct = float(apm_data.get("current_availability_pct", 99.0))
    total_dau = float(apm_data.get("total_daily_active_users", 5000.0))
    affected_feature = jira_data.get("affected_feature", "order processing")

    context_msg = f"""
Hypotheses: {len(hypotheses)} ranked
Error rate: {error_rate_pct}%
Throughput: {throughput_per_min} orders/min
Incident duration: {incident_duration_minutes} minutes
Avg order value: ${avg_order_value_usd}
Current availability: {current_availability_pct}%
Affected feature: {affected_feature}

Top hypothesis: {hypotheses[0].get('hypothesis', 'N/A') if hypotheses else 'N/A'}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Assess the business impact using these metrics:\n\n{context_msg}\n\n"
         f"Use your tools to calculate revenue impact, check SLA, estimate users, "
         f"determine severity, and generate customer comms. Then call finish_analysis()."},
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

        all_done = False
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")

            if fn_name == "finish_analysis":
                final_result = fn_args
                all_done = True
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps({"status": "accepted"}),
                })
            else:
                result  = _execute_tool(fn_name, fn_args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

        if all_done:
            break

    # Fallback: if agent never called finish_analysis, compute ourselves
    if not final_result:
        revenue_impact = _calculate_revenue_impact(error_rate_pct, throughput_per_min, avg_order_value_usd, incident_duration_minutes)
        sla_status = _check_sla_breach(current_availability_pct, sla_target_pct, incident_duration_minutes)
        affected_users = _estimate_affected_users(error_rate_pct, total_dau, incident_duration_minutes)
        severity = _determine_severity_tier(revenue_impact["revenue_at_risk_per_min_usd"], affected_users["affected_users_estimate"], sla_status["is_breached"])
        comms_template = _generate_customer_comms_template(severity["severity_tier"], affected_feature, incident_duration_minutes)

        final_result = {
            "severity": severity["severity_tier"],
            "urgency": "IMMEDIATE" if severity["page_oncall"] else "HIGH" if severity["escalation_required"] else "MEDIUM",
            "revenue_impact": revenue_impact,
            "affected_users": affected_users,
            "sla_status": sla_status,
            "escalation_plan": ["Page on-call"] if severity["page_oncall"] else ["Notify incident commander"],
            "rollback_decision": {"recommended": severity["rollback_recommended"], "risk": "unknown"},
            "customer_comms_template": comms_template,
            "immediate_actions": [],
            "analysis_notes": "Generated via fallback algorithm (agent max iterations)"
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    hypotheses = [
        {"rank": 1, "hypothesis": "qty/quantity field mismatch — 30% of checkouts failing", "confidence": 0.97},
        {"rank": 2, "hypothesis": "int cast truncates totals — all orders undercharged", "confidence": 0.95}
    ]
    apm = {
        "incident_duration_minutes": 15,
        "error_rate_pct": 31.0,
        "throughput_per_min": 120.0,
        "current_availability_pct": 69.0,
        "sla_target_pct": 99.9,
        "total_daily_active_users": 5000,
    }
    jira = {
        "priority": "HIGH",
        "environment": "production",
        "affected_feature": "checkout",
        "avg_order_value_usd": 85.0,
    }
    result = assess_impact(hypotheses, apm, jira)
    print(json.dumps(result, indent=2))
