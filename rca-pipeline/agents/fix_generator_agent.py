"""
FixGeneratorAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

This agent generates code fixes from root cause hypotheses using file manipulation
and analysis tools. All tools are pure algorithmic (file I/O, regex, validation) —
no LLM inside the tool executors.

Tools available:
  - read_source_file(service, relative_path)
  - search_bug_location(pattern, service)
  - list_service_files(service, extension)
  - generate_patch(find_str, replace_str, file_path)
  - check_field_name_variants(field_name)
  - estimate_fix_risk(fix_description, file_path, lines_changed)
  - finish_analysis(fixes, fix_summary, overall_risk, estimated_time_to_deploy_minutes, rollback_instructions)
"""
import os, sys, json, re, glob
from llm_client import get_client, get_model
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(ROOT, '.env'))
client = get_client()

# Service directory map
SERVICE_MAP = {
    "java": os.path.join(ROOT, "java-order-service", "src", "main", "java", "com", "aspire"),
    "python": os.path.join(ROOT, "python-inventory-service"),
    "node": os.path.join(ROOT, "node-notification-service"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_source_file",
            "description": (
                "Read a source file from disk. Service maps to project directory "
                "(java|python|node). Returns file content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["java", "python", "node"], "description": "Service name"},
                    "relative_path": {"type": "string", "description": "Relative path within service (e.g. 'app.py', 'OrderClient.java')"},
                },
                "required": ["service", "relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_bug_location",
            "description": (
                "Regex search across service files. Returns matching lines with "
                "file + line number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "service": {"type": "string", "enum": ["java", "python", "node"], "description": "Service to search in"},
                },
                "required": ["pattern", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_service_files",
            "description": "List files in a service directory by extension.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["java", "python", "node"], "description": "Service name"},
                    "extension": {"type": "string", "description": "File extension (e.g. '.py', '.java', '.js')"},
                },
                "required": ["service", "extension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_patch",
            "description": (
                "Validate that find_str exists in file, return preview of change WITHOUT applying. "
                "Returns context_lines_before, proposed_change, context_lines_after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "find_str": {"type": "string", "description": "Exact string to find (must be unique in file)"},
                    "replace_str": {"type": "string", "description": "Replacement string"},
                    "file_path": {"type": "string", "description": "Absolute path to file"},
                },
                "required": ["find_str", "replace_str", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_field_name_variants",
            "description": (
                "Search ALL services for camelCase, snake_case, and abbreviated variants "
                "of a field name (e.g. 'orderId' → searches 'orderId', 'order_id', 'orderid', 'order-id'). "
                "Returns matches with file locations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {"type": "string", "description": "Field name to search for (e.g. 'orderId')"},
                },
                "required": ["field_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_fix_risk",
            "description": (
                "Rules-based risk assessment. Returns 'high_risk', 'medium_risk', or 'low_risk'. "
                "High if >10 lines or core service file, medium if 3-10 lines, low if 1-2 lines in client/adapter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fix_description": {"type": "string", "description": "What the fix does"},
                    "file_path": {"type": "string", "description": "File being modified"},
                    "lines_changed": {"type": "integer", "description": "Number of lines changed"},
                },
                "required": ["fix_description", "file_path", "lines_changed"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit final fix recommendations. Call when analysis is complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "fixes": {"type": "array", "items": {"type": "object"}, "description": "List of fix dicts"},
                "fix_summary": {"type": "string", "description": "Brief summary of fixes"},
                "overall_risk": {"type": "string", "description": "low_risk|medium_risk|high_risk"},
                "estimated_time_to_deploy_minutes": {"type": "number", "description": "ETA to deploy"},
                "rollback_instructions": {"type": "string", "description": "How to rollback if needed"},
            },
            "required": ["fixes", "fix_summary", "overall_risk"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

SYSTEM_PROMPT = """You are FixGeneratorAgent, an AI agent in an incident Root Cause Analysis pipeline.

Given a root cause hypothesis, your job is to:
1. Locate the exact bug in source code using search and read tools
2. Understand the code context and all files that need to change
3. Generate minimal, targeted fixes (no refactoring unrelated code)
4. Check field name variants across services (e.g. qty vs quantity)
5. Estimate risk for each fix
6. Produce before/after patch previews (via generate_patch)
7. Submit fixes via finish_analysis()

Rules:
- Make MINIMAL changes that fix ONLY the reported bug
- Use generate_patch to preview changes WITHOUT applying them
- For field mismatches, search all service code for variants
- Always include rollback instructions
"""

# ─────────────────────────────────────────────────────────────────────────────
# Pure algorithmic tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def _read_source_file(service: str, relative_path: str) -> dict:
    """Read a source file from disk."""
    if service not in SERVICE_MAP:
        return {"error": f"Unknown service: {service}"}

    base_dir = SERVICE_MAP[service]
    file_path = os.path.join(base_dir, relative_path)

    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    try:
        with open(file_path, 'r') as f:
            content = f.read()
        return {
            "file_path": file_path,
            "content": content,
            "lines": len(content.splitlines()),
        }
    except Exception as e:
        return {"error": str(e)}


def _search_bug_location(pattern: str, service: str) -> dict:
    """Regex search across service files."""
    if service not in SERVICE_MAP:
        return {"error": f"Unknown service: {service}"}

    base_dir = SERVICE_MAP[service]
    if not os.path.exists(base_dir):
        return {"error": f"Service directory not found: {base_dir}"}

    matches = []
    try:
        # Search all Python, Java, JS files
        for ext in ["*.py", "*.java", "*.js"]:
            for file_path in glob.glob(os.path.join(base_dir, "**", ext), recursive=True):
                try:
                    with open(file_path, 'r') as f:
                        for line_num, line in enumerate(f, 1):
                            if re.search(pattern, line, re.IGNORECASE):
                                matches.append({
                                    "file": file_path,
                                    "line_number": line_num,
                                    "line": line.strip()[:120],
                                })
                except (OSError, IOError):
                    pass
    except Exception as e:
        return {"error": str(e)}

    return {
        "pattern": pattern,
        "matches": matches[:20],
        "total": len(matches),
    }


def _list_service_files(service: str, extension: str) -> dict:
    """List files in service directory by extension."""
    if service not in SERVICE_MAP:
        return {"error": f"Unknown service: {service}"}

    base_dir = SERVICE_MAP[service]
    if not os.path.exists(base_dir):
        return {"error": f"Service directory not found: {base_dir}"}

    files = []
    try:
        for file_path in glob.glob(os.path.join(base_dir, "**", f"*{extension}"), recursive=True):
            rel_path = os.path.relpath(file_path, base_dir)
            files.append(rel_path)
    except Exception as e:
        return {"error": str(e)}

    return {
        "service": service,
        "extension": extension,
        "files": sorted(files),
        "count": len(files),
    }


def _generate_patch(find_str: str, replace_str: str, file_path: str) -> dict:
    """Validate find_str exists, return patch preview WITHOUT applying."""
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}", "valid": False}

    try:
        with open(file_path, 'r') as f:
            content = f.read()

        if find_str not in content:
            return {
                "error": f"Pattern not found in {file_path}",
                "valid": False,
                "find_str": find_str[:100],
            }

        # Count occurrences
        count = content.count(find_str)
        if count > 1:
            return {
                "error": f"Pattern found {count} times (must be unique)",
                "valid": False,
            }

        # Get context
        lines = content.splitlines(keepends=True)
        find_lines = find_str.splitlines()
        find_line_count = len(find_lines)

        # Find the starting line index
        start_line_idx = None
        for i, line in enumerate(lines):
            if find_str.split('\n')[0] in line:
                # Check if this is the right match
                test_content = ''.join(lines[i:min(i+find_line_count+1, len(lines))])
                if find_str in test_content:
                    start_line_idx = i
                    break

        context_before = ''.join(lines[max(0, start_line_idx-2):start_line_idx]) if start_line_idx is not None else ""
        context_after = ''.join(lines[min(len(lines), start_line_idx+find_line_count):start_line_idx+find_line_count+2]) if start_line_idx is not None else ""

        return {
            "valid": True,
            "file_path": file_path,
            "context_lines_before": context_before[:200],
            "proposed_change": replace_str[:500],
            "context_lines_after": context_after[:200],
            "lines_to_change": len(find_lines),
        }

    except Exception as e:
        return {"error": str(e), "valid": False}


def _check_field_name_variants(field_name: str) -> dict:
    """Search all services for field name variants."""
    # Generate variants
    variants = [field_name]

    # camelCase to snake_case
    snake = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', field_name)
    snake = re.sub('([a-z0-9])([A-Z])', r'\1_\2', snake).lower()
    variants.append(snake)

    # snake_case to camelCase
    camel = ''.join(word.capitalize() if i > 0 else word for i, word in enumerate(field_name.split('_')))
    variants.append(camel)

    # abbreviated
    variants.append(field_name.replace('_', '').lower())
    variants.append(field_name.replace('_', '-'))

    variants = list(set(variants))

    matches = {}
    for service in SERVICE_MAP:
        service_matches = []
        for variant in variants:
            result = _search_bug_location(f'\\b{variant}\\b', service)
            if result.get("matches"):
                service_matches.extend(result["matches"])

        if service_matches:
            matches[service] = service_matches[:10]

    return {
        "field_name": field_name,
        "variants_searched": variants,
        "matches_by_service": matches,
    }


def _estimate_fix_risk(fix_description: str, file_path: str, lines_changed: int) -> dict:
    """Rules-based risk assessment."""
    risk = "low_risk"

    # Check file type and location
    is_core = any(x in file_path.lower() for x in ["order", "inventory", "notification", "core", "service"])
    is_client_adapter = any(x in file_path.lower() for x in ["client", "adapter", "connector", "http"])

    if lines_changed > 10 or (is_core and lines_changed > 5):
        risk = "high_risk"
    elif lines_changed > 3 or (is_core and lines_changed > 2):
        risk = "medium_risk"
    elif is_client_adapter and lines_changed <= 2:
        risk = "low_risk"

    return {
        "risk_level": risk,
        "lines_changed": lines_changed,
        "is_core_service": is_core,
        "is_client_adapter": is_client_adapter,
        "justification": f"{'High' if risk == 'high_risk' else 'Medium' if risk == 'medium_risk' else 'Low'} risk: {lines_changed} lines in {'core service' if is_core else 'client/adapter' if is_client_adapter else 'support'} file",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — runs the actual tool logic
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    try:
        if name == "read_source_file":
            result = _read_source_file(args.get("service", ""), args.get("relative_path", ""))
            return json.dumps(result)

        elif name == "search_bug_location":
            result = _search_bug_location(args.get("pattern", ""), args.get("service", ""))
            return json.dumps(result)

        elif name == "list_service_files":
            result = _list_service_files(args.get("service", ""), args.get("extension", ""))
            return json.dumps(result)

        elif name == "generate_patch":
            result = _generate_patch(args.get("find_str", ""), args.get("replace_str", ""), args.get("file_path", ""))
            return json.dumps(result)

        elif name == "check_field_name_variants":
            result = _check_field_name_variants(args.get("field_name", ""))
            return json.dumps(result)

        elif name == "estimate_fix_risk":
            result = _estimate_fix_risk(args.get("fix_description", ""), args.get("file_path", ""), args.get("lines_changed", 1))
            return json.dumps(result)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def generate_fix(hypothesis: dict, source_code: str = None, file_path: str = None) -> dict:
    """Generate code fixes from root cause hypotheses using file manipulation tools.

    The LLM calls file manipulation and analysis tools autonomously and submits
    final fixes via finish_analysis(). All tools are pure algorithmic with no LLM calls.

    Args:
        hypothesis: Dict with hypothesis_id, hypothesis, hypothesis_type, affected_services,
                    confidence, and fix_category.
        source_code: Legacy parameter for orchestrator compatibility (unused).
        file_path: Legacy parameter for orchestrator compatibility (unused).

    Returns:
        dict: Code fixes with patch previews, risk assessments, and rollback instructions.

    Note:
        Max iterations: 8. Files are located using tools, not passed as parameters.
    """

    hypothesis_str = f"""
Hypothesis ID: {hypothesis.get('hypothesis_id', hypothesis.get('id', 'unknown'))}
Hypothesis: {hypothesis.get('hypothesis', 'unknown')}
Type: {hypothesis.get('hypothesis_type', 'unknown')}
Services: {', '.join(hypothesis.get('affected_services', []))}
Confidence: {hypothesis.get('confidence', 0.0)}
Fix category: {hypothesis.get('fix_category', 'unknown')}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Generate a code fix for this hypothesis:\n\n{hypothesis_str}\n\n"
         f"Use your tools to locate the bug, understand the code, generate patches, "
         f"and check for related issues. When ready, call finish_analysis() with the fixes."},
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

    # Fallback: if agent never called finish_analysis
    if not final_result:
        final_result = {
            "fixes": [],
            "fix_summary": "Unable to generate fix (max iterations reached)",
            "overall_risk": "unknown",
            "estimated_time_to_deploy_minutes": 0,
            "rollback_instructions": "No fixes generated",
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    hypothesis = {
        "hypothesis_id": "H1",
        "hypothesis": "Java sends 'qty' but Python inventory service expects 'quantity'",
        "hypothesis_type": "field_mismatch",
        "confidence": 0.97,
        "fix_category": "field_rename",
        "affected_services": ["java-order-service", "python-inventory-service"]
    }

    result = generate_fix(hypothesis)
    print(json.dumps(result, indent=2))
