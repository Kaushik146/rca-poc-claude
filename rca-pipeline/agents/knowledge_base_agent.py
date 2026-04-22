# Model: gpt-4o-mini (tool-calling agent, reasoning done by algorithms)
"""
KnowledgeBaseAgent — REAL tool-using agent (ReAct loop via OpenAI tool_use API).

Pass 0 (TF-IDF algorithm): cosine similarity retrieval — fast, deterministic, no LLM.
                           Ranks incidents by term overlap using TF-IDF vectors.
Pass 1+ (LLM ReAct):      GPT-4o-mini uses tools to match anomalies against past incidents,
                          extracts proven fixes, estimates resolution time.

Tools available:
  - search_incidents(query, top_k)        → calls engine.search(), returns top matches with scores
  - search_incidents_multi(queries_list, top_k) → calls engine.search_multi()
  - get_incident_details(incident_id)    → returns full incident dict
  - search_runbooks(symptom_keyword)     → searches RUNBOOKS list
  - compare_symptoms(current_symptoms_list, incident_id) → compute term overlap
  - finish_analysis(...)                 → final output
"""
import os, sys, json
from llm_client import get_client, get_model
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from algorithms.similarity_engine import TFIDFSimilarityEngine
from algorithms.bm25 import BM25Engine

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# TF-IDF engine built once at import time (no LLM, no network)
# Populated after INCIDENT_LIBRARY is defined below.
_engine: "TFIDFSimilarityEngine | None" = None
_bm25_engine: "BM25Engine | None" = None

INCIDENT_LIBRARY = [
    {
        "id": "INC-1021", "date": "2023-08-14",
        "title": "Field name mismatch between Java client and Python service",
        "symptoms": ["HTTP 400 from Python service", "KeyError in Flask logs", "Missing required field error"],
        "root_cause": "Java sent 'qty', Python expected 'quantity'. JSON contract not enforced by any schema.",
        "fix": "Standardised on 'quantity'. Added JSON schema validation to Flask endpoint.",
        "prevention": "Implement OpenAPI contract testing in CI pipeline.",
        "time_to_detect": "23 minutes", "time_to_fix": "45 minutes", "time_to_verify": "30 minutes",
        "affected_pct": "28%", "services": ["java-order-service", "python-inventory-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1021"
    },
    {
        "id": "INC-0887", "date": "2023-05-22",
        "title": "Integer cast truncating decimal totals in SQLite",
        "symptoms": ["Order totals stored as whole numbers", "Revenue discrepancy", "Customer overcharge complaints"],
        "root_cause": "(int) cast on double before INSERT. SQLite column REAL but Java discarded decimal.",
        "fix": "Removed int cast. Used BigDecimal for all monetary values in Java.",
        "prevention": "Add monetary value tests asserting decimal precision end-to-end.",
        "time_to_detect": "67 minutes", "time_to_fix": "20 minutes", "time_to_verify": "25 minutes",
        "affected_pct": "100% of orders", "services": ["java-order-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0887"
    },
    {
        "id": "INC-0934", "date": "2023-07-03",
        "title": "camelCase vs snake_case mismatch between Java and Node.js",
        "symptoms": ["Notification service 400", "Missing orderId field", "No confirmation emails sent"],
        "root_cause": "Java sent order_id (snake_case), Node.js expected orderId (camelCase).",
        "fix": "Standardised on camelCase for all inter-service JSON.",
        "prevention": "Add contract tests. Generate clients from OpenAPI spec.",
        "time_to_detect": "15 minutes", "time_to_fix": "20 minutes", "time_to_verify": "15 minutes",
        "affected_pct": "12%", "services": ["java-order-service", "node-notification-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0934"
    },
    {
        "id": "INC-1105", "date": "2023-11-08",
        "title": "Currency conversion rate hardcoded incorrectly — decimal shift",
        "symptoms": ["EUR prices 10x lower than expected", "International orders undercharged", "Revenue loss on EUR transactions"],
        "root_cause": "EUR rate 0.108 instead of 1.08. Decimal point off by one position.",
        "fix": "Fixed rate constant. Added unit tests for all currency conversions.",
        "prevention": "Pull currency rates from config service, never hardcode. Alert on rate changes > 5%.",
        "time_to_detect": "3 hours", "time_to_fix": "5 minutes", "time_to_verify": "10 minutes",
        "affected_pct": "~8% (EUR transactions only)", "services": ["java-order-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1105"
    },
    {
        "id": "INC-0756", "date": "2023-02-17",
        "title": "Off-by-one in stock boundary check — last unit never sold",
        "symptoms": ["Items showing in-stock but checkout fails", "Inventory shows 1 but reservation rejected"],
        "root_cause": "Condition `stock > quantity` should be `stock >= quantity`. Last unit always rejected.",
        "fix": "Changed > to >= in reservation check in Python service.",
        "prevention": "Add explicit test for boundary case where stock == requested quantity.",
        "time_to_detect": "2 days", "time_to_fix": "5 minutes", "time_to_verify": "10 minutes",
        "affected_pct": "~2%", "services": ["python-inventory-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0756"
    },
    {
        "id": "INC-0623", "date": "2022-11-30",
        "title": "Multiple cross-service field mismatches introduced in single deployment",
        "symptoms": ["Multiple services returning 400 simultaneously", "Error rate spike across all services", "Deployment correlated exactly"],
        "root_cause": "HTTP client refactor changed field names without updating receiving services. No integration tests caught it.",
        "fix": "Reverted deployment, then applied targeted fixes service by service.",
        "prevention": "Contract tests mandatory before any HTTP client changes land in main.",
        "time_to_detect": "8 minutes", "time_to_fix": "2 hours", "time_to_verify": "1 hour",
        "affected_pct": "35%", "services": ["java-order-service", "python-inventory-service", "node-notification-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0623"
    },
    # ── DEMO2-relevant incidents (timeout cascade / connection pool) ──
    {
        "id": "INC-1203", "date": "2023-12-05",
        "title": "Connection pool exhaustion causing cascading timeout across services",
        "symptoms": ["HTTP 503 Service Unavailable", "Connection pool exhausted", "Thread pool saturated",
                     "Timeout waiting for DB connection", "Cascading failure across services",
                     "All worker threads blocked", "Exponential backoff retries flooding upstream"],
        "root_cause": "Config deployment reduced DB connection pool from 20 to 2 (typo). Under load, all connections saturated instantly. "
                      "Upstream services' thread pools filled with blocked requests waiting on timeouts, causing cascading 503s.",
        "fix": "Rolled back config to restore pool size 20. Added connection pool size validation in CI (min_pool >= 5).",
        "prevention": "Config change validation: alert if pool size drops below safe minimum. Load test config changes before production.",
        "time_to_detect": "12 minutes", "time_to_fix": "5 minutes (rollback)", "time_to_verify": "10 minutes",
        "affected_pct": "78%", "services": ["python-inventory-service", "java-order-service", "node-notification-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1203"
    },
    {
        "id": "INC-0991", "date": "2023-09-18",
        "title": "Database connection pool misconfiguration after asyncpg upgrade",
        "symptoms": ["TimeoutError waiting for DB connection", "Connection pool maxsize=2",
                     "Pending requests piling up", "CPU spike from queued requests",
                     "Gateway Timeout 504", "Transaction rollbacks from timeout"],
        "root_cause": "asyncpg version upgrade changed pool config format. New config read DB_POOL_SIZE as string '20', "
                      "asyncpg parsed first character as int → pool size 2. All connections saturated under normal load.",
        "fix": "Explicitly cast DB_POOL_SIZE to int in config loader. Set minsize=maxsize=20.",
        "prevention": "Add integration test that verifies actual pool size after config load. Monitor connection pool utilization with alert at 80%.",
        "time_to_detect": "45 minutes", "time_to_fix": "15 minutes", "time_to_verify": "20 minutes",
        "affected_pct": "65%", "services": ["python-inventory-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0991"
    },
    {
        "id": "INC-1150", "date": "2023-10-27",
        "title": "Thread pool saturation from downstream timeout cascade",
        "symptoms": ["Thread pool usage 50/50 exhausted", "New requests queueing indefinitely",
                     "All threads blocked waiting on downstream service", "HTTP 503 from thread exhaustion",
                     "Retry storms amplifying load", "ECONNREFUSED from overwhelmed service"],
        "root_cause": "Downstream service latency spike (from DB issue) caused all upstream threads to block on socket timeout (5s). "
                      "With 1200 req/min and 5s timeout, thread pool of 50 exhausted in ~2.5 seconds. Retries made it worse.",
        "fix": "Added circuit breaker pattern. Reduced socket timeout to 2s. Added bulkhead isolation for downstream calls.",
        "prevention": "Circuit breaker on all cross-service calls. Thread pool monitoring with alert at 80%. Retry budget (max 10% of traffic).",
        "time_to_detect": "3 minutes", "time_to_fix": "30 minutes", "time_to_verify": "15 minutes",
        "affected_pct": "90%", "services": ["java-order-service", "python-inventory-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1150"
    },
    # ── DEMO3-relevant incidents (race condition / data corruption) ──
    {
        "id": "INC-1287", "date": "2024-01-22",
        "title": "Race condition in coupon validation causing double-discount application",
        "symptoms": ["Coupon applied to multiple orders simultaneously", "usage_count exceeds max_uses",
                     "Revenue shortfall from duplicate discounts", "No HTTP errors (silent corruption)",
                     "Concurrent requests both read stale cache", "putIfAbsent race window"],
        "root_cause": "CouponValidator.java cached validation results in ConcurrentHashMap. Two threads read cache simultaneously "
                      "before first putIfAbsent completed. Both got cache miss → both applied discount. "
                      "Logical operation (read + validate + write) was not atomic despite ConcurrentHashMap being thread-safe.",
        "fix": "Replaced cache-based validation with database-level SELECT FOR UPDATE lock. "
               "Added unique constraint on (coupon_code, order_id) to prevent duplicates at DB level.",
        "prevention": "Use database locks for financial operations, not application-level caching. "
                      "Add reconciliation job to detect coupon count mismatches. Load test concurrent coupon redemptions.",
        "time_to_detect": "7 hours (discovered by finance team)", "time_to_fix": "2 hours", "time_to_verify": "1 hour",
        "affected_pct": "15% of flash sale orders", "services": ["java-order-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1287"
    },
    {
        "id": "INC-1045", "date": "2023-08-30",
        "title": "Silent data corruption from concurrent cache writes",
        "symptoms": ["Data integrity mismatch discovered hours later", "No error logs during incident",
                     "All HTTP responses 200 OK", "Duplicate entries in database",
                     "Cache returning stale data under high concurrency"],
        "root_cause": "In-memory cache used putIfAbsent for deduplication but read and write were not atomic. "
                      "Under high concurrency (>50 RPS), race window widened enough for consistent double-writes.",
        "fix": "Moved deduplication to database layer with unique constraints. Added distributed lock for cache updates.",
        "prevention": "Never use application-level cache for deduplication of financial transactions. "
                      "Add data integrity reconciliation jobs that run hourly.",
        "time_to_detect": "4 hours", "time_to_fix": "3 hours", "time_to_verify": "2 hours",
        "affected_pct": "8%", "services": ["java-order-service"],
        "post_mortem_link": "confluence/post-mortems/INC-1045"
    },
    {
        "id": "INC-0812", "date": "2023-04-14",
        "title": "Race condition in inventory reservation under flash sale load",
        "symptoms": ["Overselling during flash sale", "Stock count went negative",
                     "Concurrent reservations both succeeding", "No errors in logs",
                     "Revenue impact discovered by warehouse team"],
        "root_cause": "Inventory check (SELECT stock WHERE stock >= requested) and reservation (UPDATE stock SET stock = stock - requested) "
                      "were not in same transaction. Two concurrent requests both saw stock=1, both reserved, stock went to -1.",
        "fix": "Wrapped check + reserve in single transaction with SELECT FOR UPDATE. Added CHECK constraint stock >= 0.",
        "prevention": "All inventory operations must use pessimistic locking. Add negative-stock monitoring alert.",
        "time_to_detect": "2 hours", "time_to_fix": "45 minutes", "time_to_verify": "30 minutes",
        "affected_pct": "5%", "services": ["python-inventory-service"],
        "post_mortem_link": "confluence/post-mortems/INC-0812"
    }
]

RUNBOOKS = [
    {
        "id": "RB-001", "title": "Field mismatch between services — runbook",
        "steps": [
            "1. Check logs of receiving service for KeyError or 'missing field' messages",
            "2. Find the exact field name expected by receiver (grep for the field name in source)",
            "3. Find what sender is actually sending (grep request body construction)",
            "4. Apply targeted rename in ONE service to match the other",
            "5. Run integration tests",
            "6. Monitor error rate for 15 minutes post-fix"
        ]
    },
    {
        "id": "RB-002", "title": "Data truncation in DB — runbook",
        "steps": [
            "1. Query DB for rows with unexpectedly whole-number values in decimal columns",
            "2. Find where the INSERT happens in Java code",
            "3. Trace backwards to find the cast that strips decimals",
            "4. Remove cast, use appropriate numeric type (BigDecimal for money)",
            "5. Backfill affected rows if possible (use audit log to reconstruct original values)",
            "6. Add precision assertions to test suite"
        ]
    },
    {
        "id": "RB-003", "title": "Connection pool exhaustion / cascading timeout — runbook",
        "steps": [
            "1. Check DB connection pool metrics: active connections, pending queue depth, pool size",
            "2. Compare current pool size to expected config value (look for typos: 2 vs 20)",
            "3. Check recent deployments that touched pool config (DB_POOL_SIZE, minsize, maxsize)",
            "4. If pool misconfigured: rollback config or hot-fix pool size",
            "5. Check upstream services for thread pool saturation (all threads blocked on downstream timeout)",
            "6. If thread pools saturated: restart upstream services after fixing downstream pool",
            "7. Monitor recovery: pool utilization should drop below 50% within 5 minutes",
            "8. Verify retry storms have subsided (retry rate should return to < 1%)"
        ]
    },
    {
        "id": "RB-004", "title": "Race condition / concurrent data corruption — runbook",
        "steps": [
            "1. Identify the data integrity violation: compare expected vs actual counts (e.g. coupon usage_count vs unique orders)",
            "2. Look for concurrent operations on same resource within tight time windows (<100ms)",
            "3. Check if application uses in-memory caching for deduplication (ConcurrentHashMap, Redis without SETNX)",
            "4. Verify if read-check-write operations are atomic (must be in single DB transaction with SELECT FOR UPDATE)",
            "5. Fix: replace cache-based validation with database-level locking (SELECT FOR UPDATE or unique constraints)",
            "6. Add reconciliation query to quantify impact: SELECT count(*) vs SELECT count(DISTINCT key)",
            "7. Add monitoring: alert when usage_count > max_uses for any resource",
            "8. Load test with concurrent requests to verify fix holds under high concurrency"
        ]
    },
    {
        "id": "RB-005", "title": "Silent data corruption detection — runbook",
        "steps": [
            "1. Run data integrity checks: compare aggregated totals, counts, and checksums",
            "2. Look for anomalies in financial reports: revenue shortfall, discount overuse, duplicate entries",
            "3. Query for duplicate (resource_id, order_id) pairs that should be unique",
            "4. Check application logs for concurrent operations on same resource (same timestamp, different threads)",
            "5. Identify the code path: find where cache/memory is used instead of DB locks for critical operations",
            "6. Quantify blast radius: total affected orders, revenue impact, customer impact",
            "7. Plan remediation: refund excess charges, void duplicate discounts, notify affected customers"
        ]
    }
]

def _reset_engines():
    """Reset engines so they rebuild with updated INCIDENT_LIBRARY."""
    global _engine, _bm25_engine
    _engine = None
    _bm25_engine = None

def _get_engine() -> TFIDFSimilarityEngine:
    """Lazy-initialise the TF-IDF engine (built once, reused forever)."""
    global _engine
    if _engine is None:
        _engine = TFIDFSimilarityEngine().fit(INCIDENT_LIBRARY)
    return _engine


def _get_bm25_engine() -> BM25Engine:
    """Lazy-initialise the BM25 engine (built once, reused forever)."""
    global _bm25_engine
    if _bm25_engine is None:
        # Build BM25 corpus from incident library
        documents = []
        for inc in INCIDENT_LIBRARY:
            desc = f"{' '.join(inc.get('symptoms',[]))} {inc.get('root_cause','')} {inc.get('fix','')}"
            documents.append({"id": inc["id"], "title": inc["title"], "description": desc})
        _bm25_engine = BM25Engine()
        _bm25_engine.fit(documents)
    return _bm25_engine


# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_incidents",
            "description": "Search the incident library for matches to a query string. Uses TF-IDF cosine similarity. Returns top-k results with scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":  {"type": "string", "description": "Search query (anomaly description or error message)"},
                    "top_k":  {"type": "integer", "description": "Number of top results to return", "default": 4},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_incidents_multi",
            "description": "Search with multiple queries (e.g. one per anomaly). Merges results, keeping best score per incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries":  {"type": "array", "items": {"type": "string"}, "description": "List of search queries"},
                    "top_k":    {"type": "integer", "description": "Number of top results to return", "default": 4},
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident_details",
            "description": "Get the full details of a specific incident by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string", "description": "Incident ID (e.g. INC-1021)"},
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_runbooks",
            "description": "Search RUNBOOKS list for relevant runbooks by symptom keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symptom_keyword": {"type": "string", "description": "Keyword to search runbooks (e.g. 'field mismatch', 'data truncation')"},
                },
                "required": ["symptom_keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_incidents_bm25",
            "description": "Search the incident library using BM25 ranking (Okapi BM25 with term frequency saturation). Complements TF-IDF with better handling of term frequency. Returns top-k results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":  {"type": "string", "description": "Search query"},
                    "top_k":  {"type": "integer", "description": "Number of top results to return", "default": 4},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_symptoms",
            "description": "Compute term overlap between current symptoms and a past incident. Returns overlap percentage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_symptoms": {"type": "array", "items": {"type": "string"}, "description": "List of current symptoms"},
                    "incident_id": {"type": "string", "description": "Incident ID to compare against"},
                },
                "required": ["current_symptoms", "incident_id"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are KnowledgeBaseAgent, an AI agent in an incident Root Cause Analysis pipeline.

You have access to tools that search a library of past incidents and runbooks. Use them to
investigate the current anomalies autonomously — call whichever tools you need, in whatever order.

Your investigation strategy:
1. Search the incident library using BOTH search_incidents (TF-IDF) and search_incidents_bm25 (BM25) for comprehensive matching.
2. Get details of top-matching incidents to understand their root causes and fixes.
3. Compare symptoms between current issue and matched incidents to confirm relevance.
4. Search runbooks for relevant procedures that apply to the current situation.
5. Extract lessons learned: proven fixes, prevention gaps, time estimates.
6. When done, call finish_analysis with structured results including matches and recommended actions.

Every match is backed by algorithmic similarity scoring. Trust the scores but override with domain reasoning.
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit your final knowledge base analysis. Call this when you have matched incidents and extracted lessons.",
        "parameters": {
            "type": "object",
            "properties": {
                "matches":                  {"type": "array", "items": {"type": "object"}},
                "lessons_learned":          {"type": "array", "items": {"type": "string"}},
                "recommended_runbooks":     {"type": "array", "items": {"type": "string"}},
                "prevention_measures":      {"type": "array", "items": {"type": "string"}},
                "estimated_resolution_time_minutes": {"type": "integer"},
                "past_incident_summary":    {"type": "string"},
            },
            "required": ["matches", "lessons_learned"],
        },
    },
}

ALL_TOOLS = TOOLS + [FINISH_TOOL]

# ─────────────────────────────────────────────────────────────────────────────
# Tool executors — pure algorithmic implementations
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""

    if name == "search_incidents":
        query = args.get("query", "")
        top_k = int(args.get("top_k", 4))
        engine = _get_engine()
        results = engine.search(query, top_k=top_k, min_score=0.04)
        return json.dumps({
            "query": query,
            "matches": [
                {
                    "incident_id": r.incident_id,
                    "incident_title": r.incident_title,
                    "cosine_score": r.cosine_score,
                    "term_overlap_pct": r.term_overlap_pct,
                    "matching_terms": r.matching_terms[:8],
                }
                for r in results
            ],
            "count": len(results)
        })

    elif name == "search_incidents_multi":
        queries = args.get("queries", [])
        top_k = int(args.get("top_k", 4))
        engine = _get_engine()
        results = engine.search_multi(queries, top_k=top_k, min_score=0.04)
        return json.dumps({
            "query_count": len(queries),
            "matches": [
                {
                    "incident_id": r.incident_id,
                    "incident_title": r.incident_title,
                    "cosine_score": r.cosine_score,
                    "term_overlap_pct": r.term_overlap_pct,
                    "matching_terms": r.matching_terms[:8],
                }
                for r in results
            ],
            "count": len(results)
        })

    elif name == "get_incident_details":
        incident_id = args.get("incident_id", "")
        incident = next((i for i in INCIDENT_LIBRARY if i["id"] == incident_id), None)
        if not incident:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        return json.dumps({
            "incident": incident
        })

    elif name == "search_runbooks":
        keyword = args.get("symptom_keyword", "").lower()
        matching = [rb for rb in RUNBOOKS if keyword in rb["title"].lower()]
        return json.dumps({
            "keyword": keyword,
            "matching_runbooks": matching,
            "count": len(matching)
        })

    elif name == "search_incidents_bm25":
        query = args.get("query", "")
        top_k = int(args.get("top_k", 4))
        bm25 = _get_bm25_engine()
        results = bm25.search(query, top_k=top_k)
        return json.dumps({
            "query": query,
            "algorithm": "BM25 (Okapi BM25, k1=1.5, b=0.75)",
            "matches": [
                {
                    "incident_id": r.doc_id,
                    "bm25_score": round(r.score, 4),
                    "matching_terms": r.matching_terms[:8],
                    "incident_title": r.doc_title,
                }
                for r in results[:top_k]
            ],
            "count": min(len(results), top_k)
        })

    elif name == "compare_symptoms":
        current_symptoms = args.get("current_symptoms", [])
        incident_id = args.get("incident_id", "")
        incident = next((i for i in INCIDENT_LIBRARY if i["id"] == incident_id), None)
        if not incident:
            return json.dumps({"error": f"Incident {incident_id} not found"})

        # Simple term overlap
        current_text = " ".join(current_symptoms).lower()
        incident_text = " ".join(incident.get("symptoms", [])).lower()
        current_tokens = set(current_text.split())
        incident_tokens = set(incident_text.split())
        overlap = current_tokens & incident_tokens
        overlap_pct = len(overlap) / len(current_tokens) * 100 if current_tokens else 0

        return json.dumps({
            "incident_id": incident_id,
            "symptom_overlap_count": len(overlap),
            "symptom_overlap_pct": round(overlap_pct, 1),
            "matching_terms": list(overlap)[:10],
            "incident_symptoms": incident.get("symptoms", [])
        })

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def search_knowledge_base(anomalies: list, hypotheses: list = None) -> dict:
    """
    Real tool-using ReAct agent.
    The LLM decides which tools to call, calls them, sees results, and
    iterates until it has enough information to submit finish_analysis().

    Max iterations: 8 (prevents runaway loops).
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content":
         f"Search the knowledge base for matches to these anomalies:\n\n"
         f"{json.dumps({'anomalies': anomalies, 'hypotheses': hypotheses or []}, indent=2)}\n\n"
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
                result = _execute_tool(fn_name, fn_args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

        if all_done:
            break

    # Fallback: if agent never called finish_analysis, perform basic search
    if not final_result:
        engine = _get_engine()
        queries = [
            a.get("description", "") + " " + a.get("service", "")
            for a in anomalies
        ]
        matches = engine.search_multi(queries, top_k=4, min_score=0.04)

        final_result = {
            "matches": [
                {
                    "incident_id": r.incident_id,
                    "incident_title": r.incident_title,
                    "cosine_score": r.cosine_score,
                    "term_overlap_pct": r.term_overlap_pct,
                    "matching_terms": r.matching_terms[:8],
                }
                for r in matches
            ],
            "lessons_learned": [
                "Apply targeted field name standardization between services",
                "Use BigDecimal for monetary values to avoid truncation",
                "Implement contract testing before deployment",
            ],
            "recommended_runbooks": ["RB-001", "RB-002"],
            "prevention_measures": [
                "Add OpenAPI contract tests to CI pipeline",
                "Enforce schema validation on all cross-service APIs",
            ],
            "estimated_resolution_time_minutes": 90,
            "past_incident_summary": "Analysis fallback — agent did not complete structured search"
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    anomalies = [
        {"description": "Java sends 'qty', Python expects 'quantity'", "service": "java-order-service"},
        {"description": "int cast truncates $99.99 to $99", "service": "java-order-service"},
        {"description": "order_id sent, Node.js expects orderId", "service": "node-notification-service"}
    ]
    result = search_knowledge_base(anomalies)
    print(json.dumps(result, indent=2))
