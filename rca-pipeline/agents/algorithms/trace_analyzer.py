"""
TraceAnalyzer — graph-based distributed trace analysis.
NO LLM. Pure graph algorithms.

Builds a DAG (Directed Acyclic Graph) from trace spans, then:
  - Reconstructs the call tree with parent-child relationships
  - Finds the critical path (longest duration path root → leaf)
  - DFS to identify failure propagation: root failure vs cascaded failures
  - Detects silent corruptions (spans that succeed but produce wrong data)
  - Calculates latency contribution of each service

Data model:
  Span: (id, parent_id, service, operation, duration_ms, status, error, tags)
  DAG: nodes=spans, edges=parent→child
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Span:
    id: str
    parent_id: Optional[str]
    service: str
    operation: str
    duration_ms: int
    start_offset_ms: int
    status: str                # ok | error | timeout
    http_status: Optional[int]
    error_detail: Optional[str]
    tags: dict = field(default_factory=dict)

    @property
    def is_error(self): return self.status in ("error", "timeout")
    @property
    def is_ok(self):    return self.status == "ok"


@dataclass
class TraceGraph:
    spans: list[Span]
    root: Optional[Span]
    children: dict                # span_id → [child spans]
    total_duration_ms: int
    failure_path: list[Span]      # root → failure
    critical_path: list[Span]     # root → slowest leaf
    service_latency: dict         # service → total duration ms
    service_pct: dict             # service → % of total
    root_failure: Optional[Span]  # deepest error that caused cascade
    silent_corruptions: list[Span]
    cascaded_failures: list[Span]
    ok_but_wrong: list[dict]      # spans that returned 200 but produced bad data


# ── Span text parsers ─────────────────────────────────────────────────────────

_SPAN_LINE = re.compile(
    r'[Ss]pan\s*\d+\s*\|'             # "Span 1 |"
    r'\s*([^|]+?)\s*\|'               # service
    r'\s*([^|]+?)\s*\|'               # operation
    r'\s*(\d+)\s*ms?\s*\|'            # duration
    r'\s*(OK|ERROR|TIMEOUT)'          # status
    r'(?:\s*→\s*(.+))?$',            # optional error detail
    re.IGNORECASE
)

_HTTP_STATUS = re.compile(r'HTTP\s+(\d{3})')
_ARROW_INDENT = re.compile(r'^(\s*└─\s*)')


def parse_trace_text(trace_text: str) -> list[Span]:
    """
    Parse raw trace text into Span objects.
    Infers parent-child relationships from indentation (└─ arrows).
    """
    spans = []
    span_id = 0
    indent_stack: list[tuple[int, str]] = []   # (indent_level, span_id)

    lines = [l for l in trace_text.strip().split("\n") if l.strip()]

    for line in lines:
        m = _SPAN_LINE.search(line)
        if not m:
            continue

        service   = m.group(1).strip()
        operation = m.group(2).strip()
        duration  = int(m.group(3))
        status    = m.group(4).upper()
        error_det = m.group(5).strip() if m.group(5) else None

        # HTTP status from error detail
        http_st = None
        if error_det:
            hs = _HTTP_STATUS.search(error_det)
            if hs:
                http_st = int(hs.group(1))

        # Infer indent level from leading spaces / └─
        indent_match = _ARROW_INDENT.match(line)
        raw_indent = len(indent_match.group(1)) if indent_match else 0
        # Normalise: count 2-space levels
        level = raw_indent // 2

        # Pop stack until we find the right parent level
        while indent_stack and indent_stack[-1][0] >= level:
            indent_stack.pop()
        parent_id = indent_stack[-1][1] if indent_stack else None

        sid = str(span_id)
        span = Span(
            id=sid,
            parent_id=parent_id,
            service=service,
            operation=operation.split("(")[0].strip(),
            duration_ms=duration,
            start_offset_ms=0,   # estimated below
            status=status.lower() if status.lower() in ("ok","error","timeout") else
                   ("error" if status == "ERROR" else "ok"),
            http_status=http_st,
            error_detail=error_det,
        )
        spans.append(span)
        indent_stack.append((level, sid))
        span_id += 1

    return spans


def build_trace_graph(spans: list[Span]) -> TraceGraph:
    """
    Build a full TraceGraph from a list of Span objects.
    Runs all graph algorithms: critical path, failure propagation, etc.
    """
    # Build adjacency: parent → children
    children: dict[str, list[Span]] = {s.id: [] for s in spans}
    children["__root__"] = []
    for span in spans:
        if span.parent_id is None:
            children["__root__"].append(span)
        else:
            children.get(span.parent_id, children["__root__"]).append(span)

    root = children["__root__"][0] if children["__root__"] else (spans[0] if spans else None)
    total_ms = root.duration_ms if root else sum(s.duration_ms for s in spans)

    # ── Critical path (DFS, maximise cumulative duration) ─────────────────
    def critical_path_dfs(span: Span, acc: list[Span]) -> list[Span]:
        path = acc + [span]
        kids = children.get(span.id, [])
        if not kids:
            return path
        return max(
            (critical_path_dfs(child, path) for child in kids),
            key=lambda p: sum(s.duration_ms for s in p)
        )
    critical = critical_path_dfs(root, []) if root else []

    # ── Failure propagation (DFS, find error path) ─────────────────────────
    def failure_dfs(span: Span, path: list[Span]) -> list[Span]:
        if span.is_error:
            for child in children.get(span.id, []):
                child_path = failure_dfs(child, path + [span])
                if child_path:
                    return child_path
            return path + [span]
        for child in children.get(span.id, []):
            result = failure_dfs(child, path)
            if result:
                return result
        return []
    failure_path = failure_dfs(root, []) if root else []

    # ── Root failure: deepest span that is an error AND has no error children
    def find_root_failure(span: Span) -> Optional[Span]:
        """The deepest error span where children are all OK (i.e. the origin)."""
        error_children = [c for c in children.get(span.id, []) if c.is_error]
        if span.is_error and not error_children:
            return span
        for child in children.get(span.id, []):
            result = find_root_failure(child)
            if result:
                return result
        return None
    root_failure = find_root_failure(root) if root else None

    # ── Cascaded failures: errors that are downstream of root failure ──────
    cascaded = []
    if root_failure:
        for span in spans:
            if span.is_error and span.id != root_failure.id:
                cascaded.append(span)

    # ── Silent corruptions: OK spans that indicate wrong data ─────────────
    silent = []
    for span in spans:
        if span.is_ok and span.error_detail:
            corruption_clues = re.search(
                r'wrong|should be|expected|stored\s+\d+.*(?:expected|should)',
                span.error_detail, re.IGNORECASE
            )
            if corruption_clues:
                silent.append(span)

    # ── Service latency breakdown ──────────────────────────────────────────
    svc_ms: dict[str, int] = {}
    for span in spans:
        svc = span.service
        svc_ms[svc] = svc_ms.get(svc, 0) + span.duration_ms

    svc_pct = {svc: round(ms / total_ms * 100, 1) for svc, ms in svc_ms.items()} if total_ms > 0 else {}

    return TraceGraph(
        spans=spans,
        root=root,
        children=children,
        total_duration_ms=total_ms,
        failure_path=failure_path,
        critical_path=critical,
        service_latency=svc_ms,
        service_pct=svc_pct,
        root_failure=root_failure,
        silent_corruptions=silent,
        cascaded_failures=cascaded,
        ok_but_wrong=[],
    )


def analyze_trace(trace_text: str) -> dict:
    """
    Full trace analysis pipeline. Returns structured dict for downstream agents.
    """
    spans = parse_trace_text(trace_text)
    graph = build_trace_graph(spans)

    root_f = graph.root_failure
    return {
        "total_duration_ms": graph.total_duration_ms,
        "total_spans": len(spans),
        "failed_spans": sum(1 for s in spans if s.is_error),
        "services_involved": list(graph.service_latency.keys()),
        "call_chain": [
            {
                "step": i + 1,
                "service": s.service,
                "operation": s.operation,
                "duration_ms": s.duration_ms,
                "status": s.status,
                "http_status": s.http_status,
                "error_detail": s.error_detail,
            }
            for i, s in enumerate(graph.spans)
        ],
        "root_failure": {
            "service": root_f.service,
            "operation": root_f.operation,
            "error": root_f.error_detail,
            "is_root_cause": True,
            "reasoning": f"Deepest error span with no error children — origin of failure cascade"
        } if root_f else None,
        "failure_propagation": [
            {"step": i+1, "service": s.service,
             "type": "root" if s is root_f else "cascaded",
             "description": f"{s.operation} → {s.error_detail or s.status}"}
            for i, s in enumerate(graph.failure_path)
        ],
        "silent_corruptions": [
            {"service": s.service, "operation": s.operation,
             "description": s.error_detail}
            for s in graph.silent_corruptions
        ],
        "critical_path": [
            f"{s.service}/{s.operation} ({s.duration_ms}ms)"
            for s in graph.critical_path
        ],
        "latency_breakdown": {
            svc: {"duration_ms": ms, "pct_of_total": f"{pct}%"}
            for svc, ms in graph.service_latency.items()
            for pct in [graph.service_pct.get(svc, 0)]
        },
        "cascaded_failure_count": len(graph.cascaded_failures),
        "fix_targets": [
            {
                "service": root_f.service,
                "operation": root_f.operation,
                "what_to_fix": root_f.error_detail or "error"
            }
        ] if root_f else []
    }


if __name__ == "__main__":
    import json

    sample = """
Span 1  | java-order-service       | OrderService.checkout()           | 4187ms | ERROR
  └─ Span 2 | java-order-service   | HttpInventoryClient.reserve()     |   52ms | ERROR → HTTP 400
       └─ Span 3 | python-inv-svc  | Flask POST /reserve               |   48ms | ERROR → KeyError:'quantity'
  └─ Span 4 | java-order-service   | SqliteOrderRepository.insert()    |  180ms | OK    → stored total=99 (should be 99.99)
  └─ Span 5 | java-order-service   | HttpNotificationClient.notify()   |   31ms | ERROR → HTTP 400
       └─ Span 6 | node-notif-svc  | Node POST /notify                 |   28ms | ERROR → Missing: orderId
    """

    result = analyze_trace(sample)
    print(f"Total duration: {result['total_duration_ms']}ms")
    print(f"Spans: {result['total_spans']} total, {result['failed_spans']} failed")
    print(f"Root failure: {result['root_failure']}")
    print(f"Silent corruptions: {result['silent_corruptions']}")
    print(f"Critical path: {' → '.join(result['critical_path'])}")
    print(f"\nLatency breakdown:")
    for svc, data in result['latency_breakdown'].items():
        print(f"  {svc}: {data['duration_ms']}ms ({data['pct_of_total']})")
