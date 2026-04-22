# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
DeploymentAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

Correlates incidents with recent deployments using real git commands
and deployment history analysis.

Tools available:
  - get_git_log(n_commits, since_hours)        → runs git log via subprocess
  - get_commit_diff(commit_hash)               → runs git show on specific commit
  - get_changed_files(since_hours)             → git diff for recent changes
  - check_timing_correlation(incident_time_str, commit_timestamps) → pure Python timing analysis
  - parse_deployment_text(text)                → regex parser for deployment manifests
  - finish_analysis(deployments, suspicious_deployments, timing_correlation, risk_assessment, changed_files, recommendation)

Real git operations via subprocess. Falls back to text parsing if git unavailable.
"""
import os, sys, json, subprocess, re, datetime
from llm_client import get_client, get_model
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))
client = get_client()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_git_log",
            "description": "Retrieve git log from the project repo. Returns recent commits with hash, message, files changed, author, and timestamp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n_commits":  {"type": "integer", "description": "Number of commits to retrieve (default 20)", "default": 20},
                    "since_hours":{"type": "integer", "description": "Only commits from last N hours (optional)", "default": 48},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_commit_diff",
            "description": "Get detailed diff and stats for a specific commit. Shows which files changed and how much.",
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_hash": {"type": "string", "description": "Git commit hash (short or long form)"},
                },
                "required": ["commit_hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_changed_files",
            "description": "Get list of files that changed between HEAD and HEAD~1, or within last N hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since_hours": {"type": "integer", "description": "Files changed in last N hours (default 24)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_timing_correlation",
            "description": "Pure Python analysis: given incident timestamp and list of commit timestamps, find commits within 2 hours before incident (suspicious).",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_time_str": {"type": "string", "description": "ISO 8601 or common datetime format (e.g. '2024-01-15 10:23 UTC')"},
                    "commit_timestamps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of commit timestamps to check"
                    },
                },
                "required": ["incident_time_str", "commit_timestamps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_deployment_text",
            "description": "Regex parser: extract deployment events, versions, timestamps, and changed files from free-form deployment log text. Use when git is unavailable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Raw deployment manifest or log text"},
                },
                "required": ["text"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are DeploymentAgent, an AI agent in an incident Root Cause Analysis pipeline.

You have tools to investigate git history and deployment records. Use them to correlate
incidents with recent deployments.

Your investigation strategy:
1. First, retrieve recent git history with get_git_log.
2. Check the timing of commits vs incident time using check_timing_correlation.
3. For suspicious commits, use get_commit_diff to see what files changed.
4. Identify high-risk changes: field name changes, type changes, hardcoded values.
5. If git is unavailable, fall back to parse_deployment_text on the provided manifest.
6. Build a risk assessment for each deployment (HIGH/MEDIUM/LOW).
7. Rank deployments by likelihood of causing the incident.

When done, call finish_analysis with structured deployment analysis and verdict.
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final deployment analysis with risk assessment and verdict.",
        "parameters": {
            "type": "object",
            "properties": {
                "deployments": {
                    "type": "array",
                    "description": "All deployments analyzed",
                    "items": {
                        "type": "object",
                        "properties": {
                            "version":       {"type": "string"},
                            "service":       {"type": "string"},
                            "deployed_at":   {"type": "string"},
                            "commit_hash":   {"type": "string"},
                            "changed_files": {"type": "array", "items": {"type": "string"}},
                            "commit_message": {"type": "string"},
                        },
                    },
                },
                "suspicious_deployments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "version":            {"type": "string"},
                            "service":            {"type": "string"},
                            "deployed_at":        {"type": "string"},
                            "time_before_incident": {"type": "string"},
                            "changed_files":      {"type": "array", "items": {"type": "string"}},
                            "commit_message":     {"type": "string"},
                            "risk_level":         {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                            "reason":             {"type": "string"},
                        },
                    },
                },
                "timing_correlation": {
                    "type": "object",
                    "description": "Analysis of timing between deployments and incident",
                    "properties": {
                        "incident_time":        {"type": "string"},
                        "suspicious_window_hours": {"type": "integer"},
                        "deployments_in_window": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "risk_assessment": {"type": "string"},
                "changed_files":    {"type": "array", "items": {"type": "string"}},
                "recommendation":   {"type": "string"},
                "summary":          {"type": "string"},
            },
            "required": ["suspicious_deployments", "summary"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — real git subprocess calls + algorithmic operations
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    if name == "get_git_log":
        n_commits = int(args.get("n_commits", 20))
        since_hours = int(args.get("since_hours", 48))

        try:
            result = subprocess.run(
                [
                    "git", "log",
                    "--oneline",
                    "--name-only",
                    "--pretty=format:%H|%s|%ci|%an",
                    f"--since={since_hours} hours ago",
                    f"-{n_commits}"
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return json.dumps({"error": f"git log failed: {result.stderr}"})

            commits = []
            lines = result.stdout.strip().split('\n')
            i = 0
            while i < len(lines) and lines[i].strip():
                parts = lines[i].split('|')
                if len(parts) >= 4:
                    commit = {
                        "hash":    parts[0],
                        "message": parts[1],
                        "timestamp": parts[2],
                        "author":  parts[3],
                        "files":   []
                    }
                    i += 1
                    # Collect file names until next commit or end
                    while i < len(lines) and lines[i].strip() and '|' not in lines[i]:
                        commit["files"].append(lines[i].strip())
                        i += 1
                    commits.append(commit)
                else:
                    i += 1

            return json.dumps({"commits": commits, "total": len(commits)})

        except subprocess.TimeoutExpired:
            return json.dumps({"error": "git log timeout"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "get_commit_diff":
        commit_hash = args.get("commit_hash", "")

        try:
            result = subprocess.run(
                ["git", "show", "--stat", commit_hash],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return json.dumps({"error": f"git show failed: {result.stderr}"})

            return json.dumps({
                "commit": commit_hash,
                "diff_stat": result.stdout[:2000]
            })

        except subprocess.TimeoutExpired:
            return json.dumps({"error": "git show timeout"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "get_changed_files":
        since_hours = int(args.get("since_hours", 24))

        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"--since={since_hours} hours ago", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                # Fallback: try simple diff
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10
                )

            if result.returncode == 0:
                files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
                return json.dumps({"changed_files": files, "total": len(files)})
            else:
                return json.dumps({"error": "git diff failed"})

        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "check_timing_correlation":
        incident_time_str = args.get("incident_time_str", "")
        commit_timestamps = args.get("commit_timestamps", [])

        try:
            # Parse incident time
            incident_dt = _parse_datetime(incident_time_str)
            if incident_dt is None:
                return json.dumps({"error": f"Could not parse incident time: {incident_time_str}"})

            suspicious = []
            for ts in commit_timestamps:
                commit_dt = _parse_datetime(ts)
                if commit_dt is None:
                    continue

                # Check if commit is within 2 hours BEFORE incident
                time_diff = (incident_dt - commit_dt).total_seconds() / 3600.0
                if 0 <= time_diff <= 2:
                    suspicious.append({
                        "timestamp": ts,
                        "hours_before_incident": round(time_diff, 2)
                    })

            return json.dumps({
                "incident_time": incident_time_str,
                "suspicious_window_hours": 2,
                "suspicious_commits": suspicious,
                "total": len(suspicious)
            })

        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "parse_deployment_text":
        text = args.get("text", "")

        # Regex patterns for deployment info
        patterns = {
            "deployment": r'(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?)\s+[–-]\s+(\w+[-\w]*)\s+(v[\d.]+)',
            "service":    r'(java-order-service|python-inventory-service|node-notification-service)',
            "file":       r'[-\*]\s+([a-zA-Z0-9._/]+\.(?:java|py|js))',
            "commit":     r'Commit:\s*["\']?([^"\']+)["\']?',
            "changed":    r'Changed files?:',
        }

        deployments = []

        # Find deployment entries
        for m in re.finditer(patterns["deployment"], text):
            deployment = {
                "timestamp": m.group(1),
                "service":   m.group(2),
                "version":   m.group(3),
                "files":     []
            }

            # Find associated changed files and commit message
            start_pos = m.end()
            # Look ahead for files and commit until next deployment or end
            section = text[start_pos:m.end() + 500]

            for file_m in re.finditer(patterns["file"], section):
                deployment["files"].append(file_m.group(1))

            for commit_m in re.finditer(patterns["commit"], section):
                deployment["commit_message"] = commit_m.group(1)
                break

            deployments.append(deployment)

        return json.dumps({
            "deployments": deployments,
            "total": len(deployments)
        })

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


def _parse_datetime(ts_str: str):
    """Parse various datetime formats."""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M %Z",
    ]

    for fmt in formats:
        try:
            return datetime.datetime.strptime(ts_str.replace("UTC", "").strip(), fmt)
        except ValueError:
            pass

    # Try ISO format with timezone
    try:
        ts_clean = ts_str.replace("UTC", "").replace("Z", "").strip()
        # Remove timezone offset if present
        ts_clean = re.sub(r'[+-]\d{2}:?\d{2}$', '', ts_clean)
        return datetime.datetime.fromisoformat(ts_clean)
    except (ValueError, TypeError):
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def analyze_deployments(deployment_text: str, incident_time: str) -> dict:
    """
    Real tool-using ReAct agent.
    The LLM calls git subprocess tools and analyzes timing correlation.

    Max iterations: 8.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Incident time: {incident_time}\n\n"
         f"Deployment context:\n{deployment_text[:2000]}\n\n"
         f"Use your tools to investigate recent deployments and correlate with the incident. "
         f"When done, call finish_analysis()."},
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
                result = _execute_tool(fn_name, fn_args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

        if all_done:
            break

    # Fallback
    if not final_result:
        final_result = {
            "suspicious_deployments": [],
            "summary": "Deployment analysis incomplete (max iterations reached)"
        }

    final_result.setdefault("suspicious_deployments", [])
    final_result.setdefault("summary", "Deployment analysis completed")

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_deployments = """
    2024-01-15 09:45 – java-order-service v2.3.1
      Changed files:
        - HttpInventoryClient.java
        - HttpNotificationClient.java
      Commit: "feat: refactor HTTP clients"

    2024-01-14 16:30 – python-inventory-service v1.1.2
      Changed files:
        - app.py
      Commit: "chore: improve logging"
    """
    result = analyze_deployments(sample_deployments, "2024-01-15 10:23 UTC")
    print(json.dumps(result, indent=2))
