"""
CodeAgent — Production-grade ReAct agent with deep code analysis.

Built on react_core.py ReAct engine. Uses scratchpad for working memory,
self-reflection before finishing, and backtracking when findings contradict.

=== TOOLS (12 total) ===

Phase 1 — Core Code Navigation (6 existing tools):
  list_files(service, extension)         → lists files in service
  read_file(service, relative_path)      → reads file content
  search_code(pattern, service, extension) → regex grep across service
  find_api_calls(service)                → all HTTP client calls
  find_field_usage(field_name)           → cross-service field search
  check_imports(service, relative_path)  → extract imports from file

Phase 2 — Deep Code Analysis (6 new tools):
  analyze_ast_structure(service, file_path)        → parse AST, extract signatures
  detect_type_coercions(service)                   → find dangerous type conversions
  validate_cross_service_contracts(sender, receiver) → compare HTTP field contracts
  compute_change_impact_score(service, file_path)  → risk scoring for changes
  find_error_handling_gaps(service)                → missing error handlers
  detect_serialization_mismatches(field_name)      → camelCase vs snake_case issues

Meta-tools (from react_core):
  update_scratchpad / read_scratchpad / reflect_on_findings / revise_finding
"""
import os, sys, re, json, subprocess, ast, pathlib
from collections import defaultdict, Counter
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from react_core import ReActEngine

# ─────────────────────────────────────────────────────────────────────────────
# Path validation utility
# ─────────────────────────────────────────────────────────────────────────────
def _validate_path(path_str: str, allowed_root: str) -> str:
    """
    Validate that a path is within the allowed root directory.
    Prevents path traversal attacks.

    Args:
        path_str: The path to validate
        allowed_root: The root directory that path_str must be under

    Returns:
        The validated absolute path

    Raises:
        ValueError: If path traversal is attempted
    """
    resolved = pathlib.Path(path_str).resolve()
    allowed = pathlib.Path(allowed_root).resolve()
    if not str(resolved).startswith(str(allowed)):
        raise ValueError(f"Path traversal blocked: {path_str}")
    return str(resolved)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))

# Service directory mapping
SERVICE_DIRS = {
    "java":   os.path.join(ROOT, "java-order-service", "src", "main", "java", "com", "aspire"),
    "python": os.path.join(ROOT, "python-inventory-service"),
    "node":   os.path.join(ROOT, "node-notification-service"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    # ── Phase 1: Core Code Navigation ────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a service directory filtered by extension. Returns relative paths only (from service root).",
            "parameters": {
                "type": "object",
                "properties": {
                    "service":   {"type": "string", "enum": ["java", "python", "node"], "description": "Which service to list"},
                    "extension": {"type": "string", "description": "File extension to filter (.java, .py, .js, or '' for all)"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the actual content of a file. Use relative paths from list_files output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service":       {"type": "string", "enum": ["java", "python", "node"]},
                    "relative_path": {"type": "string", "description": "Path relative to service root (from list_files)"},
                },
                "required": ["service", "relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Regex search across all files in a service. Returns matching lines with file + line number context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":   {"type": "string", "description": "Regex pattern to search for"},
                    "service":   {"type": "string", "enum": ["java", "python", "node"]},
                    "extension": {"type": "string", "description": "Optional: filter by extension (.java, .py, .js)"},
                },
                "required": ["pattern", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_api_calls",
            "description": "Find all HTTP client calls (requests.get/post, HttpClient, fetch, axios) in a service. Returns method, URL patterns, and file locations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["java", "python", "node"], "description": "Which service to search"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_field_usage",
            "description": "Search for a specific field name across ALL services. Useful for finding where a field is sent vs expected. Returns all occurrences with file/line context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {"type": "string", "description": "Field name to search for (e.g. 'quantity', 'qty', 'orderId', 'order_id')"},
                },
                "required": ["field_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_imports",
            "description": "Extract all import/require statements from a specific file. Helps understand dependencies and APIs used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service":       {"type": "string", "enum": ["java", "python", "node"]},
                    "relative_path": {"type": "string", "description": "Path relative to service root"},
                },
                "required": ["service", "relative_path"],
            },
        },
    },
    # ── Phase 2: Deep Code Analysis ──────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "analyze_ast_structure",
            "description": (
                "Parse Python/Java/JS code and extract function signatures, class hierarchies, "
                "return types, and method dependencies. Helps understand the structure of code "
                "and what functions are responsible for critical operations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service":   {"type": "string", "enum": ["java", "python", "node"]},
                    "file_path": {"type": "string", "description": "Relative path to file (from list_files)"},
                },
                "required": ["service", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_type_coercions",
            "description": (
                "Find dangerous type coercions across all files in a service: int→float truncation, "
                "string→number implicit conversions, BigDecimal→int loss of precision, boolean→string. "
                "Returns patterns and locations of unsafe type conversions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["java", "python", "node"]},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_cross_service_contracts",
            "description": (
                "Compare field names in HTTP client calls of sender service vs expected fields "
                "in receiver service endpoints. Identifies exact field naming mismatches by "
                "analyzing JSON payloads and endpoint definitions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sender_service":   {"type": "string", "enum": ["java", "python", "node"]},
                    "receiver_service": {"type": "string", "enum": ["java", "python", "node"]},
                },
                "required": ["sender_service", "receiver_service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_change_impact_score",
            "description": (
                "Score how impactful changes to a file would be: counts how many other files "
                "import it, how many API endpoints it defines, how many DB queries it makes. "
                "Higher score = higher risk of breaking changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service":   {"type": "string", "enum": ["java", "python", "node"]},
                    "file_path": {"type": "string", "description": "Relative path to file"},
                },
                "required": ["service", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_error_handling_gaps",
            "description": (
                "Find missing error handling: try/catch blocks that swallow exceptions, "
                "missing null checks before field access, unchecked HTTP responses, unhandled "
                "promise rejections. Returns locations and severity of gaps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["java", "python", "node"]},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_serialization_mismatches",
            "description": (
                "Find serialization format mismatches: how a field is serialized in sender "
                "(camelCase? snake_case? different name?) vs how it's deserialized in receiver. "
                "Returns field names, formats, and services involved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_name": {"type": "string", "description": "Field name to analyze (e.g. 'orderId', 'order_id', 'orderID')"},
                },
                "required": ["field_name"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are CodeAgent, a production-grade ReAct agent in an incident Root Cause Analysis pipeline.

You have 12 domain tools + 4 meta-tools (scratchpad, read_scratchpad, reflect, revise).

=== INVESTIGATION PROTOCOL ===

Phase 1 — Strategic Code Navigation (tools 1-6):
  1. list_files to identify candidate files
  2. search_code to find relevant patterns
  3. find_field_usage to locate cross-service field usage
  4. find_api_calls to see what data is being sent between services
  5. read_file to examine actual code for problem areas
  6. check_imports to understand dependencies

Phase 2 — Deep Code Analysis (tools 7-12):
  7. analyze_ast_structure on files that define APIs or critical functions
  8. detect_type_coercions to find unsafe type conversions
  9. validate_cross_service_contracts to compare sender/receiver field contracts
  10. compute_change_impact_score on files that are frequently used
  11. find_error_handling_gaps to identify missing error handlers
  12. detect_serialization_mismatches on suspicious field names

=== SCRATCHPAD USAGE ===
After EACH major finding, store it in your scratchpad with confidence:
  update_scratchpad(key="field_mismatch_qty", value={...}, confidence=0.95)
  update_scratchpad(key="type_coercion_issue", value=[...], confidence=0.8)
  update_scratchpad(key="contract_violation", value={...}, confidence=0.9)

=== MANDATORY REFLECTION ===
Before finish_analysis, call reflect_on_findings to check:
  - Are there unexplained mismatches?
  - Do the sender and receiver field definitions agree?
  - Is every issue backed by actual code evidence?

Each issue must have:
  id, type (field_mismatch, type_error, boundary, string_constant, etc),
  severity (CRITICAL, HIGH, MEDIUM, LOW), services, files involved, code evidence,
  why_it_breaks, suggested_fix.
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final structured code analysis. MUST call reflect_on_findings first.",
        "parameters": {
            "type": "object",
            "properties": {
                "code_issues": {
                    "type": "array",
                    "description": "List of code issues found",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":                  {"type": "string"},
                            "type":                {"type": "string"},
                            "severity":            {"type": "string"},
                            "description":         {"type": "string"},
                            "sender_service":      {"type": "string"},
                            "receiver_service":    {"type": "string"},
                            "sender_file":         {"type": "string"},
                            "sender_code_snippet": {"type": "string"},
                            "receiver_file":       {"type": "string"},
                            "receiver_code_snippet": {"type": "string"},
                            "why_it_breaks":       {"type": "string"},
                            "suggested_fix":       {"type": "string"},
                        },
                    },
                },
                "files_analyzed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of files actually examined",
                },
                "services_checked": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Services that were investigated",
                },
                "summary": {"type": "string", "description": "1-2 sentence summary of findings"},
            },
            "required": ["code_issues", "services_checked"],
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — pure filesystem/algorithmic operations
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    # ── Phase 1: Core Code Navigation ────────────────────────────────────────
    if name == "list_files":
        service = args.get("service")
        extension = args.get("extension", "")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        if not os.path.isdir(service_root):
            return json.dumps({"error": f"Service directory not found: {service_root}"})

        files = []
        try:
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    if not extension or fn.endswith(extension):
                        full_path = os.path.join(root, fn)
                        rel_path = os.path.relpath(full_path, service_root)
                        files.append(rel_path)
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"service": service, "files": sorted(files), "total": len(files)})

    elif name == "read_file":
        service = args.get("service")
        relative_path = args.get("relative_path")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        full_path = os.path.join(service_root, relative_path)

        if not os.path.abspath(full_path).startswith(os.path.abspath(service_root)):
            return json.dumps({"error": "Path traversal attempted"})

        try:
            with open(full_path, 'r') as f:
                content = f.read()
            return json.dumps({
                "service": service,
                "file": relative_path,
                "content": content,
                "lines": len(content.splitlines())
            })
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {relative_path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "search_code":
        pattern = args.get("pattern")
        service = args.get("service")
        extension = args.get("extension", "")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        if not os.path.isdir(service_root):
            return json.dumps({"error": f"Service directory not found: {service_root}"})

        matches = []
        try:
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    if not extension or fn.endswith(extension):
                        full_path = os.path.join(root, fn)
                        try:
                            with open(full_path, 'r') as f:
                                for line_num, line in enumerate(f, 1):
                                    if re.search(pattern, line):
                                        rel_path = os.path.relpath(full_path, service_root)
                                        matches.append({
                                            "file": rel_path,
                                            "line": line_num,
                                            "text": line.strip()[:200]
                                        })
                        except (OSError, IOError):
                            pass
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({
            "pattern": pattern,
            "service": service,
            "matches": matches[:50],
            "total": len(matches)
        })

    elif name == "find_api_calls":
        service = args.get("service")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]

        patterns = {
            "java": [
                r'(HttpClient|RestTemplate|OkHttpClient)\s*\.\s*\w+\(',
                r'new\s+HttpPost|new\s+HttpGet|new\s+HttpPut|new\s+HttpDelete',
            ],
            "python": [
                r'requests\.(get|post|put|delete|patch)',
                r'(Session|Client)\s*\.\s*(get|post|put|delete)',
            ],
            "node": [
                r'(fetch|axios|http\.(get|post|put|delete))',
                r'require\([\'"].*http',
            ]
        }

        calls = []
        try:
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    full_path = os.path.join(root, fn)
                    try:
                        with open(full_path, 'r') as f:
                            content = f.read()
                            rel_path = os.path.relpath(full_path, service_root)
                            for pattern in patterns.get(service, []):
                                for match in re.finditer(pattern, content, re.IGNORECASE):
                                    calls.append({
                                        "file": rel_path,
                                        "snippet": match.group(0)
                                    })
                    except (OSError, IOError, re.error):
                        pass
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({
            "service": service,
            "api_calls": calls[:30],
            "total": len(calls)
        })

    elif name == "find_field_usage":
        field_name = args.get("field_name")

        results = {field_name: {}}
        try:
            for service, service_root in SERVICE_DIRS.items():
                if not os.path.isdir(service_root):
                    continue

                occurrences = []
                for root, dirs, filenames in os.walk(service_root):
                    for fn in filenames:
                        full_path = os.path.join(root, fn)
                        try:
                            with open(full_path, 'r') as f:
                                for line_num, line in enumerate(f, 1):
                                    if re.search(rf'["\']?{re.escape(field_name)}["\']?\s*[:=]|\.{field_name}|get{field_name.capitalize()}', line, re.IGNORECASE):
                                        rel_path = os.path.relpath(full_path, service_root)
                                        occurrences.append({
                                            "file": rel_path,
                                            "line": line_num,
                                            "text": line.strip()[:150]
                                        })
                        except (OSError, IOError):
                            pass

                if occurrences:
                    results[field_name][service] = occurrences[:20]
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps(results)

    elif name == "check_imports":
        service = args.get("service")
        relative_path = args.get("relative_path")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        full_path = os.path.join(service_root, relative_path)

        if not os.path.abspath(full_path).startswith(os.path.abspath(service_root)):
            return json.dumps({"error": "Path traversal attempted"})

        try:
            with open(full_path, 'r') as f:
                content = f.read()

            imports = []
            if service == "java":
                imports = re.findall(r'^\s*import\s+([^;]+);', content, re.MULTILINE)
            elif service == "python":
                imports = re.findall(r'^\s*(?:from|import)\s+([^\n]+)', content, re.MULTILINE)
            elif service == "node":
                imports = re.findall(r"(?:require|import)\s*\(['\"]([^'\"]+)['\"]\)|import\s+.*from\s+['\"]([^'\"]+)['\"]", content)
                imports = [m[0] or m[1] for m in imports]

            return json.dumps({"file": relative_path, "imports": imports})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Phase 2: Deep Code Analysis ──────────────────────────────────────────
    elif name == "analyze_ast_structure":
        service = args.get("service")
        file_path = args.get("file_path")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        full_path = os.path.join(service_root, file_path)

        if not os.path.abspath(full_path).startswith(os.path.abspath(service_root)):
            return json.dumps({"error": "Path traversal attempted"})

        try:
            with open(full_path, 'r') as f:
                content = f.read()

            result = {
                "service": service,
                "file": file_path,
                "functions": [],
                "classes": [],
                "imports": [],
            }

            if service == "python":
                try:
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef):
                            args = [arg.arg for arg in node.args.args]
                            result["functions"].append({
                                "name": node.name,
                                "args": args,
                                "line": node.lineno,
                                "returns": "Any"
                            })
                        elif isinstance(node, ast.ClassDef):
                            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                            result["classes"].append({
                                "name": node.name,
                                "methods": methods,
                                "line": node.lineno,
                            })
                except SyntaxError:
                    result["error"] = "Could not parse Python AST"

            elif service == "java":
                # Regex-based extraction for Java
                classes = re.findall(r'(?:public\s+)?(?:class|interface)\s+(\w+)', content)
                methods = re.findall(r'(?:public|private|protected)\s+(\w+)\s+(\w+)\s*\(', content)
                result["classes"] = [{"name": c} for c in classes]
                result["functions"] = [{"name": m[1], "return_type": m[0]} for m in methods[:20]]

            elif service == "node":
                # Regex-based extraction for JavaScript
                functions = re.findall(r'(?:function|const|let)\s+(\w+)\s*(?:=\s*)?(?:function|\(|async)', content)
                classes = re.findall(r'class\s+(\w+)', content)
                result["functions"] = [{"name": f} for f in functions[:20]]
                result["classes"] = [{"name": c} for c in classes]

            return json.dumps(result)

        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {file_path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "detect_type_coercions":
        service = args.get("service")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        coercions = []

        patterns = {
            "java": [
                (r'int\s+\w+\s*=\s*\(int\)\s*\w+\s*[/.]', "int cast from float/double (truncation)"),
                (r'new\s+Integer\s*\(\s*String', "String to Integer conversion"),
                (r'Integer\.parseInt\s*\(\s*\w+\s*[+*/-]', "parseInt on computed string"),
                (r'BigDecimal\s+\w+\s*=\s*new\s+Integer', "BigDecimal from Integer (wrong type)"),
            ],
            "python": [
                (r'int\s*\(\s*\w+\s*[+*/\-]\s*\w*\)', "int() on computed value (loses precision)"),
                (r'float\s*\(\s*\w+\)', "float() conversion (truncation risk)"),
                (r"int\s*\(\s*str\s*\(", "int(str(...)) double conversion"),
                (r'bool\s*\(\s*str', "bool from string (always True unless empty)"),
            ],
            "node": [
                (r'parseInt\s*\(\s*\w+\s*[+*/\-]', "parseInt on computed value"),
                (r'Number\s*\(\s*\w+\)', "Number() type coercion"),
                (r'\w+\s*\|\s*0(?:\s|;)', "Bitwise OR for int coercion"),
                (r'\+\s*\w+\s*[+*/\-]', "Unary + for number coercion"),
            ]
        }

        try:
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    full_path = os.path.join(root, fn)
                    try:
                        with open(full_path, 'r') as f:
                            content = f.read()
                            rel_path = os.path.relpath(full_path, service_root)

                            for pattern, desc in patterns.get(service, []):
                                for match in re.finditer(pattern, content):
                                    # Count line number
                                    line_num = content[:match.start()].count('\n') + 1
                                    coercions.append({
                                        "file": rel_path,
                                        "line": line_num,
                                        "pattern": desc,
                                        "code": match.group(0)[:80],
                                        "severity": "HIGH"
                                    })
                    except re.error:
                        pass
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({
            "service": service,
            "type_coercions": coercions[:20],
            "total": len(coercions)
        })

    elif name == "validate_cross_service_contracts":
        sender_service = args.get("sender_service")
        receiver_service = args.get("receiver_service")

        if sender_service not in SERVICE_DIRS or receiver_service not in SERVICE_DIRS:
            return json.dumps({"error": "Unknown service"})

        # Extract HTTP calls from sender
        sender_root = SERVICE_DIRS[sender_service]
        receiver_root = SERVICE_DIRS[receiver_service]

        sender_fields = set()
        receiver_fields = set()

        try:
            # Find JSON payloads in sender
            for root, dirs, filenames in os.walk(sender_root):
                for fn in filenames:
                    full_path = os.path.join(root, fn)
                    try:
                        with open(full_path, 'r') as f:
                            content = f.read()
                            # Look for field assignments
                            for match in re.finditer(r'["\'](\w+)["\']?\s*:\s*', content):
                                sender_fields.add(match.group(1))
                    except (OSError, IOError):
                        pass

            # Find endpoint expectations in receiver
            for root, dirs, filenames in os.walk(receiver_root):
                for fn in filenames:
                    full_path = os.path.join(root, fn)
                    try:
                        with open(full_path, 'r') as f:
                            content = f.read()
                            # Look for parameter/field definitions
                            for match in re.finditer(r'(?:@RequestParam|@PathVariable|@RequestBody|\w+\s+(\w+))\s*(?:[,;)])', content):
                                if match.group(1):
                                    receiver_fields.add(match.group(1))
                                else:
                                    # Extract from RequestParam/PathVariable annotations
                                    match_param = re.search(r'(?:param|value)\s*=\s*["\'](\w+)["\']', content[max(0, match.start()-50):match.end()])
                                    if match_param:
                                        receiver_fields.add(match_param.group(1))
                    except re.error:
                        pass

            mismatches = []
            for field in sender_fields:
                # Check for exact match or naming variants
                variants = [
                    field,
                    field.replace('_', ''),
                    re.sub(r'_([a-z])', lambda m: m.group(1).upper(), field),  # snake_to_camel
                    re.sub(r'([A-Z])', r'_\1', field).lower(),  # camel_to_snake
                ]
                if not any(v in receiver_fields for v in variants):
                    mismatches.append({
                        "sender_field": field,
                        "receiver_expects": sorted(receiver_fields)[:5],
                        "severity": "HIGH"
                    })

            return json.dumps({
                "sender": sender_service,
                "receiver": receiver_service,
                "sender_fields": list(sender_fields)[:20],
                "receiver_fields": list(receiver_fields)[:20],
                "mismatches": mismatches[:10],
                "total_mismatches": len(mismatches)
            })

        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "compute_change_impact_score":
        service = args.get("service")
        file_path = args.get("file_path")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        full_path = os.path.join(service_root, file_path)

        if not os.path.abspath(full_path).startswith(os.path.abspath(service_root)):
            return json.dumps({"error": "Path traversal attempted"})

        try:
            with open(full_path, 'r') as f:
                content = f.read()

            # Count various impact factors
            import_count = 0
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    fpath = os.path.join(root, fn)
                    try:
                        with open(fpath, 'r') as fx:
                            fContent = fx.read()
                            if re.search(re.escape(file_path.replace('\\', '/')), fContent):
                                import_count += 1
                    except (OSError, IOError):
                        pass

            # Count API endpoints in this file
            endpoint_patterns = [
                (r'@(?:Get|Post|Put|Delete|Patch)Mapping', "java"),
                (r'@app\.(?:get|post|put|delete|patch)', "python"),
                (r'(?:router|app)\.(?:get|post|put|delete|patch)\s*\(', "node"),
            ]
            api_count = 0
            for pattern, lang in endpoint_patterns:
                api_count += len(re.findall(pattern, content))

            # Count database operations
            db_patterns = [
                r'(?:repository|dao|session)\.\w+',
                r'(?:select|insert|update|delete|query)\s*\(',
                r'(?:save|delete|find|update)\s*\(',
            ]
            db_count = 0
            for pattern in db_patterns:
                db_count += len(re.findall(pattern, content, re.IGNORECASE))

            # Compute impact score
            impact_score = (import_count * 10) + (api_count * 25) + (db_count * 15)

            severity = "CRITICAL" if impact_score > 500 else "HIGH" if impact_score > 200 else "MEDIUM" if impact_score > 50 else "LOW"

            return json.dumps({
                "service": service,
                "file": file_path,
                "import_count": import_count,
                "api_endpoints": api_count,
                "db_operations": db_count,
                "impact_score": impact_score,
                "severity": severity,
                "description": f"Changes to this file would affect {import_count} other files, {api_count} endpoints, and {db_count} DB operations"
            })

        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {file_path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "find_error_handling_gaps":
        service = args.get("service")

        if service not in SERVICE_DIRS:
            return json.dumps({"error": f"Unknown service: {service}"})

        service_root = SERVICE_DIRS[service]
        gaps = []

        # Patterns for error handling issues
        error_patterns = {
            "java": [
                (r'catch\s*\([^)]*\)\s*\{\s*\}', "Empty catch block (swallows exception)"),
                (r'\.get\(\)\s*\.\w+', "Calling .get() on Optional without checking"),
                (r'response\.\w+\(\)(?!.*;)', "HTTP response not checked for errors"),
                (r'catch\s*\([^)]*\)\s*\{[^}]*// ?ignore', "Intentionally ignored exception"),
            ],
            "python": [
                (r'except:\s*(?:pass|continue)', "Bare except or catch-all without logging"),
                (r'\w+\s*=\s*\w+\.get\(\)', "dict.get() without default or check"),
                (r'if\s+\w+\s*==\s*None', "None check (should use 'is None')"),
                (r'requests\.\w+\(\)(?!.*;)', "HTTP request not checked for status"),
            ],
            "node": [
                (r'\.catch\s*\(\s*\)\s*\{?\}', "Empty catch handler"),
                (r'\.catch\s*\(\s*\w+\s*\)\s*\{\s*\}', "catch handler that does nothing"),
                (r'if\s*\(\s*!\s*response\s*\)', "response not checked"),
                (r'async\s+function[^{]*\{(?!.*try)', "async function without try/catch"),
            ]
        }

        try:
            for root, dirs, filenames in os.walk(service_root):
                for fn in filenames:
                    full_path = os.path.join(root, fn)
                    try:
                        with open(full_path, 'r') as f:
                            content = f.read()
                            rel_path = os.path.relpath(full_path, service_root)

                            for pattern, desc in error_patterns.get(service, []):
                                for match in re.finditer(pattern, content):
                                    line_num = content[:match.start()].count('\n') + 1
                                    gaps.append({
                                        "file": rel_path,
                                        "line": line_num,
                                        "issue": desc,
                                        "code": match.group(0)[:80],
                                        "severity": "HIGH"
                                    })
                    except re.error:
                        pass
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({
            "service": service,
            "error_handling_gaps": gaps[:20],
            "total": len(gaps)
        })

    elif name == "detect_serialization_mismatches":
        field_name = args.get("field_name")

        # Find all variants of this field name across services
        variants_found = defaultdict(list)

        try:
            for service, service_root in SERVICE_DIRS.items():
                if not os.path.isdir(service_root):
                    continue

                for root, dirs, filenames in os.walk(service_root):
                    for fn in filenames:
                        full_path = os.path.join(root, fn)
                        try:
                            with open(full_path, 'r') as f:
                                content = f.read()
                                rel_path = os.path.relpath(full_path, service_root)

                                # Look for different naming conventions
                                patterns = [
                                    (field_name, field_name),
                                    (re.sub(r'_([a-z])', lambda m: m.group(1).upper(), field_name), "camelCase"),
                                    (re.sub(r'([A-Z])', r'_\1', field_name).lower().lstrip('_'), "snake_case"),
                                ]

                                for pattern, format_name in patterns:
                                    if re.search(rf'["\']?{re.escape(pattern)}["\']?', content):
                                        variants_found[service].append({
                                            "variant": pattern,
                                            "format": format_name,
                                            "file": rel_path,
                                        })
                        except (OSError, IOError):
                            pass

            # Detect mismatches
            mismatches = []
            services_list = list(variants_found.keys())
            for i, svc1 in enumerate(services_list):
                for svc2 in services_list[i+1:]:
                    vars1 = set(v["variant"] for v in variants_found[svc1])
                    vars2 = set(v["variant"] for v in variants_found[svc2])
                    if vars1 and vars2 and vars1 != vars2:
                        mismatches.append({
                            "service1": svc1,
                            "service1_variants": sorted(vars1),
                            "service2": svc2,
                            "service2_variants": sorted(vars2),
                            "severity": "HIGH"
                        })

            return json.dumps({
                "field_name": field_name,
                "variants_by_service": dict(variants_found),
                "mismatches": mismatches,
                "total_mismatches": len(mismatches)
            })

        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — uses ReActEngine
# ─────────────────────────────────────────────────────────────────────────────
def analyze_code(code_context: str, keywords: list = None) -> dict:
    """Production-grade ReAct agent for code analysis with deep cross-service investigation.

    Uses scratchpad for working memory and mandatory self-reflection before finishing.
    Identifies field mismatches, type errors, and boundary condition issues.

    Args:
        code_context: The code and context information to analyze as a string.
        keywords: Optional list of incident-related keywords to guide analysis.

    Returns:
        dict: Code analysis results with identified issues, fixes, and risk assessments.

    Note:
        Max 12 iterations with confidence threshold 0.7.
    """
    prompt_keywords = ""
    if keywords:
        prompt_keywords = f"\n\nContext keywords from incident: {', '.join(keywords)}"

    engine = ReActEngine(
        agent_name="CodeAgent",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        finish_tool=FINISH_TOOL,
        tool_executor=lambda name, args, **kw: _execute_tool(name, args),
        max_iterations=12,
        confidence_threshold=0.7,
        reflection_required=True,
    )

    result = engine.run(
        user_message=(
            f"Investigate the code for cross-service issues.\n\n"
            f"Code context:\n{code_context[:2000]}{prompt_keywords}\n\n"
            f"Use your tools to strategically examine code. Focus on cross-service field mismatches, "
            f"type errors, boundary conditions, and hardcoded values. Store findings in your scratchpad. "
            f"Reflect before finishing."
        ),
    )

    final_issues = result.findings.get("code_issues", [])

    # Ensure all fields are present
    for issue in final_issues:
        issue.setdefault("id", f"issue_{len(final_issues)}")
        issue.setdefault("severity", "MEDIUM")
        issue.setdefault("type", "code_issue")

    # Fallback: if agent never finished
    if not final_issues:
        final_issues = []

    return {
        "code_issues": final_issues,
        "services_checked": result.findings.get("services_checked", ["java", "python", "node"]),
        "files_analyzed": result.findings.get("files_analyzed", []),
        "summary": result.findings.get("summary", "Code analysis completed"),
    }


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_context = """
    Incident: Orders failing with 400 errors at 2024-01-15 10:23 UTC
    Error messages mention "Missing field: quantity" and "Missing field: orderId"
    Services involved: java-order-service, python-inventory-service, node-notification-service
    """
    result = analyze_code(sample_context, keywords=["quantity", "qty", "orderId", "order_id"])
    print(json.dumps(result, indent=2))
