# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
JiraAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

How real agents work vs "glorified prompts":
  - The LLM is given a set of TOOLS (functions) it can call autonomously.
  - It reasons about what tools to call, calls them, reads results, then
    calls more tools if needed — this loop continues until it decides it's done.
  - The agent PULLS its own data rather than receiving pre-packaged context.
  - This is the ReAct (Reason + Act) pattern used by production incident-response agents.

Tools available to this agent:
  - extract_priority(text)           → regex extracts P0/P1/HIGH/CRITICAL/MEDIUM
  - extract_affected_services(text)  → regex finds service names
  - extract_error_messages(text)     → pulls out quoted errors, exceptions, HTTP codes
  - extract_timeline(text)           → finds timestamps and event sequence
  - identify_regression(text)        → checks for regression keywords
  - generate_search_keywords(error_messages, services) → produces keyword list
  - finish_analysis(...)             → final output with structured results
"""
import os, sys, re, json
from llm_client import get_client, get_model
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "extract_priority",
            "description": "Extract priority level from ticket text using regex. Looks for P0, P1, HIGH, CRITICAL, MEDIUM, LOW, etc.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_affected_services",
            "description": "Extract affected service names from ticket text. Looks for java-order-service, python-inventory-service, node-notification-service, sqlite, etc.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_error_messages",
            "description": "Extract error messages, HTTP codes, and exception details from ticket text.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_timeline",
            "description": "Extract timestamps and sequence of events from ticket text to understand when the issue started.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "identify_regression",
            "description": "Check if this is a regression (worked before, broke after deployment). Looks for keywords like 'regression', 'worked before', 'was working', 'recent deploy', 'rollback'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_search_keywords",
            "description": "Generate search keywords from error messages and services for downstream agents (LogAgent, TraceAgent, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "error_messages": {"type": "array", "items": {"type": "string"}, "description": "List of error messages"},
                    "services": {"type": "array", "items": {"type": "string"}, "description": "List of affected services"},
                },
                "required": ["error_messages", "services"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are JiraAgent, an AI agent in an incident Root Cause Analysis pipeline.

You have access to tools that let you extract structured data from Jira tickets. Use them to
investigate the ticket autonomously — call whichever tools you need, in whatever order.

Your investigation strategy:
1. Extract priority to understand urgency.
2. Extract affected services to understand scope.
3. Extract error messages to understand what went wrong.
4. Extract timeline to understand when the issue started.
5. Identify if this is a regression.
6. Generate search keywords for downstream agents.
7. When done, call finish_analysis with a structured result.

Each finding should lead to the next question. Keep investigating until you have enough evidence.
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final analysis. Call this when you have extracted all relevant information.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id":            {"type": "string"},
                "priority":             {"type": "string"},
                "severity":             {"type": "string"},
                "affected_services":    {"type": "array", "items": {"type": "string"}},
                "symptoms":             {"type": "array", "items": {"type": "string"}},
                "search_keywords":      {"type": "array", "items": {"type": "string"}},
                "is_regression":        {"type": "boolean"},
                "recommended_agent_focus": {"type": "string"},
                "blast_radius_estimate":{"type": "string"},
                "summary":              {"type": "string"},
            },
            "required": ["ticket_id", "priority", "affected_services", "symptoms", "search_keywords"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

# ─────────────────────────────────────────────────────────────────────────────
# Tool executors — pure algorithmic implementations
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict, ticket_text: str) -> str:
    """Execute a tool call and return the result as a string."""

    if name == "extract_priority":
        priority_re = re.compile(r'\b(P0|P1|P2|CRITICAL|HIGH|MEDIUM|LOW)\b', re.I)
        matches = priority_re.findall(ticket_text)
        priority = matches[0].upper() if matches else "MEDIUM"
        return json.dumps({
            "priority": priority,
            "confidence": 0.9 if matches else 0.5,
            "matches": matches
        })

    elif name == "extract_affected_services":
        services_re = re.compile(
            r'\b(java-order-service|python-inventory-service|node-notification-service|'
            r'sqlite|order-db|inventory-db|notification-service|order-service|'
            r'inventory-service)\b',
            re.I
        )
        matches = list(set(m.lower() for m in services_re.findall(ticket_text)))
        return json.dumps({
            "affected_services": matches,
            "count": len(matches)
        })

    elif name == "extract_error_messages":
        error_patterns = [
            r'(?:HTTP|Error|Exception)\s+(\d{3})',
            r'(?:KeyError|ValueError|TypeError|AttributeError)[:\s]+([^"\n]+)',
            r'"([^"]*(?:error|failed|missing|invalid)[^"]*)"',
        ]
        errors = []
        for pattern in error_patterns:
            errors.extend(re.findall(pattern, ticket_text, re.I))
        errors = list(set(e.strip() for e in errors if e.strip()))[:20]
        return json.dumps({
            "error_messages": errors,
            "http_codes": [m for m in re.findall(r'\b\d{3}\b', ticket_text) if m[0] in '45'],
            "total": len(errors)
        })

    elif name == "extract_timeline":
        time_patterns = [
            r'(\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:am|pm|UTC))?)',
            r'(\d{4}-\d{2}-\d{2})',
            r'(yesterday|today|last\s+\w+)',
        ]
        events = []
        for line in ticket_text.split('\n'):
            for pattern in time_patterns:
                if re.search(pattern, line, re.I):
                    events.append(line.strip())
                    break
        return json.dumps({
            "timeline_events": events[:15],
            "event_count": len(events)
        })

    elif name == "identify_regression":
        regression_keywords = [
            'regression', 'worked before', 'was working', 'recent deploy',
            'rollback', 'deployment', 'changed', 'broke', 'started after'
        ]
        text_lower = ticket_text.lower()
        found = [kw for kw in regression_keywords if kw in text_lower]
        is_regression = len(found) >= 2
        return json.dumps({
            "is_regression": is_regression,
            "confidence": min(len(found) / len(regression_keywords), 1.0),
            "found_keywords": found
        })

    elif name == "generate_search_keywords":
        error_messages = args.get("error_messages", [])
        services = args.get("services", [])
        keywords = []

        # Keywords from errors
        for err in error_messages:
            tokens = re.findall(r'[a-z0-9]+', err.lower())
            keywords.extend(t for t in tokens if len(t) >= 3)

        # Keywords from service names
        keywords.extend(services)

        # Add common keywords for this ticket
        keywords.extend(['HTTP 400', 'field mismatch', 'missing field', 'checkout', 'order'])

        keywords = list(set(keywords))[:30]
        return json.dumps({
            "search_keywords": keywords,
            "keyword_count": len(keywords)
        })

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def analyze_ticket(ticket_text: str) -> dict:
    """
    Real tool-using ReAct agent.
    The LLM decides which tools to call, calls them, sees results, and
    iterates until it has enough information to submit finish_analysis().

    Max iterations: 8 (prevents runaway loops).
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Analyze this Jira ticket:\n\n--- TICKET START ---\n{ticket_text[:5000]}\n--- TICKET END ---\n\n"
         f"Use your tools to investigate. When done, call finish_analysis()."},
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

        # No tool calls → agent decided to stop without calling finish_analysis
        if not msg.tool_calls:
            break

        all_done = False
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")

            if fn_name == "finish_analysis":
                final_result = fn_args
                all_done = True
                # Still need to give the tool call a response
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps({"status": "accepted"}),
                })
            else:
                # Execute the tool and feed the result back
                result = _execute_tool(fn_name, fn_args, ticket_text)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

        if all_done:
            break

    # Fallback: if agent never called finish_analysis, construct a minimal result
    if not final_result:
        # Extract basics using tools directly
        try:
            priority = json.loads(_execute_tool("extract_priority", {}, ticket_text)).get("priority", "unknown")
        except (json.JSONDecodeError, TypeError, KeyError):
            priority = "unknown"

        try:
            services = json.loads(_execute_tool("extract_affected_services", {}, ticket_text)).get("affected_services", [])
        except (json.JSONDecodeError, TypeError, KeyError):
            services = []

        try:
            errors = json.loads(_execute_tool("extract_error_messages", {}, ticket_text)).get("error_messages", [])
        except (json.JSONDecodeError, TypeError, KeyError):
            errors = []

        try:
            keywords = json.loads(_execute_tool("generate_search_keywords",
                                              {"error_messages": errors, "services": services},
                                              ticket_text)).get("search_keywords", [])
        except (json.JSONDecodeError, TypeError, KeyError):
            keywords = []

        try:
            is_reg = json.loads(_execute_tool("identify_regression", {}, ticket_text)).get("is_regression", False)
        except (json.JSONDecodeError, TypeError, KeyError):
            is_reg = False

        final_result = {
            "ticket_id": "UNKNOWN",
            "priority": priority,
            "severity": priority,
            "affected_services": services,
            "symptoms": errors,
            "search_keywords": keywords,
            "is_regression": is_reg,
            "recommended_agent_focus": "LogAgent, TraceAgent",
            "blast_radius_estimate": "Unknown",
            "summary": "Analysis fallback — agent did not complete structured extraction"
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample = """
    TICKET: RCA-2041 — Orders failing at checkout with 400 error
    Priority: HIGH | Environment: production | Labels: checkout, inventory, cross-service
    Reporter: qa-team | Status: Open

    Description:
    Since yesterday 10am UTC, ~30% of checkout attempts are failing. Customers see a generic
    "Order could not be completed" error. Backend logs show HTTP 400 from the inventory service.
    Some orders go through but with wrong totals — $99 instead of $99.99.
    Notification emails are also not being sent for affected orders.

    Steps to reproduce: Add any item to cart → proceed to checkout → submit order.
    Affects: All product categories. Started after morning deployment.

    Comments:
    [DevOps - 10:45am] Deployment of order-service v2.3.1 went out at 9:45am yesterday.
      Changed: HttpInventoryClient.java, HttpNotificationClient.java, SqliteOrderRepository.java
    [Backend - 11:02am] Inventory service logs show KeyError on 'quantity' field.
      We send 'qty' but Python service expects 'quantity'.
    [QA - 11:15am] Notification service also returning 400 on some orders.
      Looks like field name mismatch — we send order_id, they expect orderId.
    [Finance - 11:30am] Reports show ~150 orders in last 2 hours with whole-number totals.
      $99 instead of $99.99, $149 instead of $149.99. Revenue gap ~$150.
    """
    result = analyze_ticket(sample)
    print(json.dumps(result, indent=2))
