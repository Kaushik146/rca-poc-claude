"""
LogAgent — Production-grade ReAct agent with self-reflection and deep analysis.

Built on react_core.py ReAct engine. Uses scratchpad for working memory,
self-reflection before finishing, and backtracking when findings contradict.

=== TOOLS (15 total) ===

Phase 1 — Raw Extraction:
  search_logs(pattern)            → grep-style regex search
  count_by_level(level)           → count ERROR/WARN/INFO/DEBUG
  extract_http_calls()            → all HTTP method/URL/status pairs
  get_stack_traces()              → exception stack traces
  get_json_payloads(direction)    → request/response bodies

Phase 2 — Algorithmic Analysis:
  run_algo_parser()               → full regex algorithmic parse
  detect_field_mismatches()       → cross-service contract violations

Phase 3 — Deep Analysis (NEW):
  build_timeline()                → reconstruct chronological event timeline
  cluster_errors()                → group related errors into incident clusters
  correlate_temporal_patterns()   → find errors that co-occur in time windows
  extract_error_chains()          → trace error propagation across log lines
  compute_error_rate_windows()    → sliding-window error rate for anomaly detection
  diff_request_response_fields()  → compare field names in request vs response JSON

Meta-tools (from react_core):
  update_scratchpad / read_scratchpad / reflect_on_findings / revise_finding
"""
import os, sys, re, json, math
from collections import defaultdict, Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.log_parser import parse_logs as algo_parse_logs, extract_field_contract_violations
from react_core import ReActEngine

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    # ── Phase 1: Raw Extraction ──────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_logs",
            "description": "Search the log text for lines matching a regex pattern. Returns matching lines with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":        {"type": "string", "description": "Regex pattern to search for"},
                    "case_sensitive": {"type": "boolean", "description": "Whether to match case-sensitively", "default": False},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_by_level",
            "description": "Count log lines by severity level (ERROR, WARN, INFO, DEBUG). Returns counts per level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "description": "Log level to count: ERROR | WARN | INFO | DEBUG | ALL"},
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_http_calls",
            "description": "Extract all HTTP calls from the logs: method, URL, status code.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stack_traces",
            "description": "Extract exception stack traces and error messages from the logs.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_json_payloads",
            "description": "Extract JSON request/response bodies from logs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["request", "response", "both"]},
                },
                "required": ["direction"],
            },
        },
    },
    # ── Phase 2: Algorithmic Analysis ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "run_algo_parser",
            "description": "Run the full algorithmic regex log parser. Returns structured anomalies, field mismatches, type errors, and HTTP errors.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_field_mismatches",
            "description": "Cross-service field contract analysis. Detects where one service sends a field under one name (e.g. 'qty') while the receiving service expects another name (e.g. 'quantity').",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Phase 3: Deep Analysis ───────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "build_timeline",
            "description": (
                "Reconstruct a chronological event timeline from log timestamps. "
                "Groups events into time buckets, identifies the incident window, "
                "and shows the sequence of failures. Essential for understanding causality."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bucket_seconds": {"type": "integer", "description": "Time bucket size in seconds (default 60)", "default": 60},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cluster_errors",
            "description": (
                "Group related errors into incident clusters using message similarity "
                "and temporal proximity. Errors that share keywords and occur within "
                "the same time window are grouped together. Helps distinguish multiple "
                "simultaneous issues from a single cascading failure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "time_window_seconds": {"type": "integer", "description": "Max time gap between related errors (default 120)", "default": 120},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_temporal_patterns",
            "description": (
                "Find errors that co-occur in the same time windows. Uses sliding window "
                "analysis to detect patterns like 'Error A always appears within 5s of Error B'. "
                "This reveals causal chains not obvious from individual error messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_seconds": {"type": "integer", "description": "Co-occurrence window in seconds (default 10)", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_error_chains",
            "description": (
                "Trace error propagation across consecutive log lines. Finds sequences "
                "where one error leads to another (e.g. HTTP 400 → reservation failed → "
                "notification failed). Returns ordered chains showing how failures cascade."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_error_rate_windows",
            "description": (
                "Compute error rates in sliding time windows. Detects sudden spikes "
                "in error rate that indicate the start of an incident. Returns rates "
                "per window plus the detected spike point."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_seconds": {"type": "integer", "description": "Window size in seconds", "default": 60},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_request_response_fields",
            "description": (
                "Compare field names between JSON request bodies and expected fields in "
                "response error messages. Reveals exact field naming mismatches by pairing "
                "each request with its error response."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

SYSTEM_PROMPT = """You are LogAgent, a production-grade ReAct agent in an incident Root Cause Analysis pipeline.

You have 13 domain tools + 4 meta-tools (scratchpad, read_scratchpad, reflect, revise).

=== INVESTIGATION PROTOCOL ===

Phase 1 — Broad sweep:
  1. run_algo_parser() for baseline anomaly detection
  2. count_by_level(ALL) to understand error distribution
  3. extract_http_calls() to see cross-service failures

Phase 2 — Deep dive:
  4. build_timeline() to reconstruct the incident timeline
  5. extract_error_chains() to trace failure cascading
  6. cluster_errors() to separate distinct issues from cascade noise
  7. correlate_temporal_patterns() to find causal co-occurrence
  8. diff_request_response_fields() to pinpoint exact field mismatches

Phase 3 — Confirm and cross-validate:
  9. Use search_logs with targeted patterns to confirm findings
  10. Use get_json_payloads to verify field names in actual payloads
  11. Use get_stack_traces for root cause details

=== SCRATCHPAD USAGE ===
After EACH major finding, store it in your scratchpad:
  update_scratchpad(key="timeline", value={...}, confidence=0.8)
  update_scratchpad(key="root_errors", value=[...], confidence=0.9)
  update_scratchpad(key="field_mismatches", value=[...], confidence=0.95)

=== MANDATORY REFLECTION ===
Before finish_analysis, call reflect_on_findings to check:
  - Are there unexplained errors?
  - Do temporal correlations support the causal chain?
  - Is every anomaly backed by at least 2 evidence sources?

Each anomaly must have:
  service, severity (ERROR|WARN), anomaly_type, description, affected_field,
  raw_log_line, source, confidence (0-1), evidence_count (how many tools confirmed it)
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final structured anomaly list. MUST call reflect_on_findings first.",
        "parameters": {
            "type": "object",
            "properties": {
                "anomalies": {
                    "type": "array",
                    "description": "List of anomalies found",
                    "items": {
                        "type": "object",
                        "properties": {
                            "service":        {"type": "string"},
                            "severity":       {"type": "string"},
                            "anomaly_type":   {"type": "string"},
                            "description":    {"type": "string"},
                            "affected_field": {"type": "string"},
                            "raw_log_line":   {"type": "string"},
                            "source":         {"type": "string"},
                            "confidence":     {"type": "number"},
                            "evidence_count": {"type": "integer", "description": "Number of tools that confirmed this"},
                            "causal_chain":   {"type": "array", "items": {"type": "string"}, "description": "Ordered list of events leading to this anomaly"},
                        },
                        "required": ["service", "severity", "anomaly_type", "description"],
                    },
                },
                "timeline": {
                    "type": "array",
                    "description": "Ordered incident timeline events",
                    "items": {"type": "object"},
                },
                "error_clusters": {
                    "type": "array",
                    "description": "Groups of related errors",
                    "items": {"type": "object"},
                },
                "summary": {"type": "string"},
            },
            "required": ["anomalies"],
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — pure algorithmic implementations
# ─────────────────────────────────────────────────────────────────────────────

# Timestamp regex for parsing log lines
_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})')
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_timestamp(line: str):
    """Extract datetime from a log line."""
    m = _TS_RE.search(line)
    if m:
        try:
            return datetime.strptime(m.group(1), _TS_FMT)
        except ValueError:
            pass
    return None


def _execute_tool(name: str, args: dict, log_text: str = "", service_name: str = "") -> str:
    """Execute a tool call and return the result as a string."""

    # ── Phase 1: Raw Extraction ──────────────────────────────────────────
    if name == "search_logs":
        pattern = args.get("pattern", "")
        flags   = 0 if args.get("case_sensitive", False) else re.IGNORECASE
        try:
            matches = []
            for i, ln in enumerate(log_text.splitlines(), 1):
                if re.search(pattern, ln, flags):
                    matches.append({"line_num": i, "text": ln.strip()[:200]})
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})
        return json.dumps({"pattern": pattern, "matches": matches[:30], "total": len(matches)})

    elif name == "count_by_level":
        level  = args.get("level", "ALL").upper()
        lines  = log_text.splitlines()
        levels = ["ERROR", "WARN", "WARNING", "INFO", "DEBUG"]
        if level == "ALL":
            counts = {}
            for lv in levels:
                counts[lv] = sum(1 for l in lines if lv in l.upper())
            return json.dumps({"counts": counts, "total_lines": len(lines)})
        else:
            n = sum(1 for l in lines if level in l.upper())
            return json.dumps({"level": level, "count": n, "total_lines": len(lines)})

    elif name == "extract_http_calls":
        http_re = re.compile(
            r"(GET|POST|PUT|DELETE|PATCH)\s+(https?://[^\s]+|/[^\s]*)\s*(?:returned|→|->|status)?\s*(\d{3})?",
            re.I
        )
        calls = []
        for i, ln in enumerate(log_text.splitlines(), 1):
            m = http_re.search(ln)
            if m:
                calls.append({
                    "line_num": i,
                    "method": m.group(1).upper(),
                    "url":    m.group(2),
                    "status": int(m.group(3)) if m.group(3) else None,
                    "is_error": int(m.group(3)) >= 400 if m.group(3) else False,
                    "line":   ln.strip()[:150],
                })
        failed = [c for c in calls if c.get("is_error")]
        return json.dumps({
            "http_calls": calls, "total": len(calls),
            "failed_calls": failed, "failure_rate": f"{len(failed)/max(len(calls),1)*100:.0f}%",
        })

    elif name == "get_stack_traces":
        trace_re  = re.compile(r"(?:Exception|Error|Traceback|at com\.|at java\.)[^\n]*", re.I)
        traces    = [ln.strip() for ln in log_text.splitlines() if trace_re.search(ln)]
        return json.dumps({"stack_traces": traces[:20], "total": len(traces)})

    elif name == "get_json_payloads":
        direction = args.get("direction", "both")
        json_re   = re.compile(r'\{[^{}]{3,}\}')
        req_kw    = re.compile(r'(?:request|body|payload|sent|sending)', re.I)
        res_kw    = re.compile(r'(?:response|returned|received|error)', re.I)
        results   = {"requests": [], "responses": []}
        for ln in log_text.splitlines():
            for m in json_re.finditer(ln):
                blob = m.group(0)
                if direction in ("request", "both") and req_kw.search(ln):
                    results["requests"].append({"line": ln.strip()[:100], "json": blob})
                if direction in ("response", "both") and res_kw.search(ln):
                    results["responses"].append({"line": ln.strip()[:100], "json": blob})
        return json.dumps({
            "requests":  results["requests"][:10],
            "responses": results["responses"][:10],
        })

    # ── Phase 2: Algorithmic Analysis ────────────────────────────────────
    elif name == "run_algo_parser":
        parsed = algo_parse_logs(log_text, service_name)
        return json.dumps({
            "anomalies":       parsed.anomalies[:20],
            "http_errors":     parsed.http_errors[:20],
            "field_mismatches":parsed.field_mismatches[:20],
            "type_errors":     parsed.type_errors[:10],
            "total_lines":     parsed.total_lines,
        }, default=str)

    elif name == "detect_field_mismatches":
        parsed    = algo_parse_logs(log_text, service_name)
        violations = extract_field_contract_violations([parsed])
        return json.dumps({"field_contract_violations": violations}, default=str)

    # ── Phase 3: Deep Analysis ───────────────────────────────────────────
    elif name == "build_timeline":
        bucket_secs = int(args.get("bucket_seconds", 60))
        events = []
        for i, ln in enumerate(log_text.splitlines(), 1):
            ts = _parse_timestamp(ln)
            if ts:
                level = "INFO"
                for lv in ["ERROR", "WARN", "DEBUG"]:
                    if lv in ln.upper():
                        level = lv
                        break
                events.append({"timestamp": ts.isoformat(), "line_num": i, "level": level, "text": ln.strip()[:120]})

        if not events:
            return json.dumps({"timeline": [], "error": "No timestamps found in logs"})

        # Group into time buckets
        first_ts = datetime.fromisoformat(events[0]["timestamp"])
        buckets = defaultdict(lambda: {"errors": 0, "warns": 0, "infos": 0, "events": []})
        for e in events:
            ts = datetime.fromisoformat(e["timestamp"])
            bucket_key = int((ts - first_ts).total_seconds() // bucket_secs) * bucket_secs
            bucket_label = f"+{bucket_key}s"
            buckets[bucket_label]["events"].append(e)
            if e["level"] == "ERROR": buckets[bucket_label]["errors"] += 1
            elif e["level"] == "WARN": buckets[bucket_label]["warns"] += 1
            else: buckets[bucket_label]["infos"] += 1

        # Find incident window (first bucket with errors)
        incident_start = None
        for k, v in sorted(buckets.items(), key=lambda x: int(''.join(filter(str.isdigit, x[0])) or '0')):
            if v["errors"] > 0 and incident_start is None:
                incident_start = k

        # Build error-only timeline
        error_timeline = [e for e in events if e["level"] == "ERROR"]

        return json.dumps({
            "total_events": len(events),
            "time_range": {"first": events[0]["timestamp"], "last": events[-1]["timestamp"]},
            "incident_start_bucket": incident_start,
            "buckets": dict(sorted(
                {k: {"errors": v["errors"], "warns": v["warns"], "infos": v["infos"]}
                 for k, v in buckets.items()}.items()
            )),
            "error_timeline": error_timeline[:30],
        })

    elif name == "cluster_errors":
        window_secs = int(args.get("time_window_seconds", 120))
        error_lines = []
        for i, ln in enumerate(log_text.splitlines(), 1):
            if any(lv in ln.upper() for lv in ["ERROR", "EXCEPTION", "FAILED"]):
                ts = _parse_timestamp(ln)
                error_lines.append({"line_num": i, "timestamp": ts, "text": ln.strip()[:150]})

        if not error_lines:
            return json.dumps({"clusters": [], "total_errors": 0})

        # Simple clustering: group by keyword similarity + time proximity
        clusters = []
        used = set()
        for i, err in enumerate(error_lines):
            if i in used:
                continue
            cluster = [err]
            used.add(i)
            words_i = set(re.findall(r'\b\w{4,}\b', err["text"].lower()))
            for j, other in enumerate(error_lines):
                if j in used:
                    continue
                words_j = set(re.findall(r'\b\w{4,}\b', other["text"].lower()))
                overlap = len(words_i & words_j) / max(len(words_i | words_j), 1)
                time_ok = True
                if err["timestamp"] and other["timestamp"]:
                    delta = abs((err["timestamp"] - other["timestamp"]).total_seconds())
                    time_ok = delta <= window_secs
                if overlap > 0.3 and time_ok:
                    cluster.append(other)
                    used.add(j)
            clusters.append({
                "cluster_id": len(clusters) + 1,
                "size": len(cluster),
                "representative": cluster[0]["text"][:100],
                "members": [c["text"][:80] for c in cluster[:5]],
                "time_span": {
                    "first": cluster[0]["timestamp"].isoformat() if cluster[0]["timestamp"] else None,
                    "last": cluster[-1]["timestamp"].isoformat() if cluster[-1]["timestamp"] else None,
                },
            })

        return json.dumps({
            "clusters": clusters,
            "total_errors": len(error_lines),
            "cluster_count": len(clusters),
            "largest_cluster_size": max(c["size"] for c in clusters) if clusters else 0,
        })

    elif name == "correlate_temporal_patterns":
        window_secs = int(args.get("window_seconds", 10))
        events = []
        for i, ln in enumerate(log_text.splitlines(), 1):
            if any(lv in ln.upper() for lv in ["ERROR", "WARN"]):
                ts = _parse_timestamp(ln)
                if ts:
                    # Extract a "signature" for each error type
                    sig_parts = re.findall(r'(?:ERROR|WARN)\s+(\S+)', ln)
                    sig = sig_parts[0] if sig_parts else f"line_{i}"
                    events.append({"timestamp": ts, "signature": sig, "line": ln.strip()[:100]})

        if len(events) < 2:
            return json.dumps({"correlations": [], "note": "Not enough events for correlation"})

        # Find co-occurring pairs within window
        co_occur = Counter()
        for i, a in enumerate(events):
            for j, b in enumerate(events):
                if i >= j: continue
                if a["signature"] == b["signature"]: continue
                delta = abs((a["timestamp"] - b["timestamp"]).total_seconds())
                if delta <= window_secs:
                    pair = tuple(sorted([a["signature"], b["signature"]]))
                    co_occur[pair] += 1

        correlations = []
        for (sig_a, sig_b), count in co_occur.most_common(10):
            correlations.append({
                "event_a": sig_a, "event_b": sig_b,
                "co_occurrence_count": count,
                "strength": "STRONG" if count >= 3 else "MODERATE" if count >= 2 else "WEAK",
            })

        return json.dumps({
            "correlations": correlations,
            "window_seconds": window_secs,
            "total_event_pairs_checked": len(events) * (len(events) - 1) // 2,
        })

    elif name == "extract_error_chains":
        lines = log_text.splitlines()
        error_lines = []
        for i, ln in enumerate(lines):
            if any(lv in ln.upper() for lv in ["ERROR", "EXCEPTION", "FAILED"]):
                error_lines.append({"index": i, "text": ln.strip()[:150]})

        # Find chains: consecutive error lines within 3 lines of each other
        chains = []
        current_chain = []
        for i, err in enumerate(error_lines):
            if not current_chain:
                current_chain = [err]
            else:
                gap = err["index"] - current_chain[-1]["index"]
                if gap <= 3:  # within 3 lines
                    current_chain.append(err)
                else:
                    if len(current_chain) >= 2:
                        chains.append({
                            "chain_id": len(chains) + 1,
                            "length": len(current_chain),
                            "events": [e["text"][:100] for e in current_chain],
                            "propagation": " → ".join(
                                (re.findall(r'(\S+Service|\S+Client|\S+Error)', e["text"])[:1] or ["?"])[0]
                                for e in current_chain
                            ),
                        })
                    current_chain = [err]

        # Don't forget the last chain
        if len(current_chain) >= 2:
            chains.append({
                "chain_id": len(chains) + 1,
                "length": len(current_chain),
                "events": [e["text"][:100] for e in current_chain],
                "propagation": " → ".join(
                    (re.findall(r'(\S+Service|\S+Client|\S+Error)', e["text"])[:1] or ["?"])[0]
                    for e in current_chain
                ),
            })

        return json.dumps({
            "error_chains": chains,
            "total_chains": len(chains),
            "longest_chain": max(c["length"] for c in chains) if chains else 0,
        })

    elif name == "compute_error_rate_windows":
        window_secs = int(args.get("window_seconds", 60))
        events = []
        for ln in log_text.splitlines():
            ts = _parse_timestamp(ln)
            if ts:
                is_error = any(lv in ln.upper() for lv in ["ERROR", "EXCEPTION"])
                events.append({"timestamp": ts, "is_error": is_error})

        if not events:
            return json.dumps({"windows": [], "error": "No timestamped events"})

        first_ts = events[0]["timestamp"]
        windows = defaultdict(lambda: {"total": 0, "errors": 0})
        for e in events:
            bucket = int((e["timestamp"] - first_ts).total_seconds() // window_secs)
            windows[bucket]["total"] += 1
            if e["is_error"]:
                windows[bucket]["errors"] += 1

        window_list = []
        spike_window = None
        max_rate = 0
        for bucket_idx in sorted(windows.keys()):
            w = windows[bucket_idx]
            rate = w["errors"] / max(w["total"], 1)
            window_list.append({
                "window": f"+{bucket_idx * window_secs}s",
                "total_events": w["total"],
                "errors": w["errors"],
                "error_rate": round(rate, 3),
            })
            if rate > max_rate:
                max_rate = rate
                spike_window = f"+{bucket_idx * window_secs}s"

        return json.dumps({
            "windows": window_list,
            "spike_window": spike_window,
            "max_error_rate": round(max_rate, 3),
            "window_size_seconds": window_secs,
        })

    elif name == "diff_request_response_fields":
        json_re = re.compile(r'\{[^{}]{3,}\}')
        req_kw  = re.compile(r'(?:request|body|payload|sent|sending|was)', re.I)
        res_kw  = re.compile(r'(?:error|missing|required|expected|invalid)', re.I)

        pairs = []
        lines = log_text.splitlines()
        for i, ln in enumerate(lines):
            # Look for request body lines
            if req_kw.search(ln):
                for m in json_re.finditer(ln):
                    try:
                        req_fields = set(json.loads(m.group(0)).keys())
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    # Look for error response in nearby lines (within 3)
                    for j in range(max(0, i-3), min(len(lines), i+3)):
                        if res_kw.search(lines[j]):
                            # Extract mentioned field name from error
                            field_match = re.search(r'(?:field|key|parameter)[:\s]+["\']?(\w+)', lines[j], re.I)
                            if field_match:
                                expected_field = field_match.group(1)
                                pairs.append({
                                    "request_fields": sorted(req_fields),
                                    "expected_field": expected_field,
                                    "field_present": expected_field in req_fields,
                                    "possible_typo": [f for f in req_fields
                                                     if f != expected_field and
                                                     (f in expected_field or expected_field in f or
                                                      len(set(f) & set(expected_field)) > len(f) * 0.5)],
                                    "request_line": ln.strip()[:100],
                                    "error_line": lines[j].strip()[:100],
                                })

        return json.dumps({
            "field_diffs": pairs,
            "total_mismatches": len(pairs),
            "unique_missing_fields": list(set(p["expected_field"] for p in pairs)),
        })

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — uses ReActEngine
# ─────────────────────────────────────────────────────────────────────────────
def analyze_logs(log_text: str, service_name: str = "unknown") -> list:
    """
    Production-grade ReAct agent with self-reflection.
    Uses scratchpad for working memory, mandatory reflection before finishing.
    Max 12 iterations (up from 8).
    """
    engine = ReActEngine(
        agent_name="LogAgent",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        finish_tool=FINISH_TOOL,
        tool_executor=lambda name, args, **kw: _execute_tool(name, args, log_text, service_name),
        max_iterations=12,
        confidence_threshold=0.7,
        reflection_required=True,
    )

    result = engine.run(
        user_message=(
            f"Investigate the logs for service: {service_name}\n\n"
            f"--- LOG START ---\n{log_text[:8000]}\n--- LOG END ---\n\n"
            f"Use your tools to investigate thoroughly. Store findings in your scratchpad. "
            f"Reflect before finishing."
        ),
    )

    final_anomalies = result.findings.get("anomalies", [])
    for a in final_anomalies:
        a.setdefault("source", "react_agent_v2")
        a.setdefault("confidence", result.confidence)

    # Fallback: if agent never finished, run algo parser directly
    if not final_anomalies:
        parsed = algo_parse_logs(log_text, service_name)
        final_anomalies = parsed.anomalies
        for a in final_anomalies:
            a["source"] = "algo_fallback"

    return final_anomalies


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_log = """
2024-01-15 10:23:41 ERROR HttpInventoryClient - POST http://localhost:5003/reserve returned 400
Response body: {"error": "Missing required field: quantity"}
Request body was: {"qty": 2, "product_id": "PROD-001"}
2024-01-15 10:23:41 ERROR OrderService - Inventory reservation failed for order ORD-8821
2024-01-15 10:23:42 ERROR HttpNotificationClient - POST http://localhost:5004/notify returned 400
Response body: {"error": "Missing required field: orderId"}
Request body was: {"order_id": "ORD-8821", "customer_id": "CUST-001"}
"""
    results = analyze_logs(sample_log, "java-order-service")
    print(json.dumps(results, indent=2))
