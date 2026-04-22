"""
Orchestrator — Production-grade 18-agent RCA pipeline.

Architecture:
  ┌─ PHASE 1: INTAKE (sequential) ──────────────────────────────────┐
  │  JiraAgent → extract symptoms, keywords, blast radius            │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ PHASE 2: SIGNAL COLLECTION (parallel, 7 agents) ───────────────┐
  │  APMAgent │ LogAgent │ TraceAgent │ DeploymentAgent              │
  │  DatabaseAgent │ CodeAgent │ ChangeIntelligenceAgent             │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ PHASE 2.5: SIGNAL FUSION (parallel, 3 agents) ─────────────────┐
  │  DependencyGraphAgent │ AlertCorrelatorAgent │ AnomalyCorrelator │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ PHASE 3: REASONING (sequential, fuses all signals) ────────────┐
  │  KnowledgeBaseAgent → HypothesisRankerAgent → ImpactAgent       │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ PHASE 4: RESOLUTION ────────────────────────────────────────────┐
  │  FixGeneratorAgent → RegressionTestAgent                         │
  └──────────────────────────────────────────────────────────────────┘
  ┌─ PHASE 5: REPORT + POSTMORTEM ───────────────────────────────────┐
  │  ReportAgent → PostMortemAgent (5-whys, timeline, action items)  │
  └──────────────────────────────────────────────────────────────────┘

Features:
  • Parallel signal collection   (ThreadPoolExecutor — cuts wall time by ~60%)
  • Per-agent timing             (know exactly where time is spent)
  • Retry with exponential backoff (survive transient API blips)
  • Confidence gating            (short-circuit if top hypothesis >95% early)
  • Dynamic routing              (skip irrelevant agents based on ticket context)
  • Rich context passing         (every agent gets relevant outputs from prior agents)
  • Structured RCA report        (saved to rca_report.md + rca_results.json)
  • Agent health dashboard       (final summary shows success/fail/skip per agent)

Usage:
  python3 agents/orchestrator.py --demo
  python3 agents/orchestrator.py --ticket t.txt --logs l.txt --metrics m.txt
  python3 agents/orchestrator.py --demo --apply-fixes
  python3 agents/orchestrator.py --demo --output-dir ./reports
"""

import os, sys, json, time, argparse, traceback, random, pathlib, logging, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_client import get_client, get_model, get_provider
from config import LLM_TIMEOUT, CB_MAX_FAILURES, CB_RESET_TIMEOUT, REPORT_DIR

from jira_agent              import analyze_ticket
from apm_agent               import analyze_metrics
from log_agent               import analyze_logs
from trace_agent             import analyze_trace
from deployment_agent        import analyze_deployments
from database_agent          import analyze_database
from code_agent              import analyze_code
from knowledge_base_agent    import search_knowledge_base
from hypothesis_ranker_agent import rank_hypotheses
from impact_assessment_agent import assess_impact
from fix_generator_agent     import generate_fix
from java_fix_agent          import fix_java_hypothesis, load_java_files
from regression_test_agent   import verify_fix
# ── New agents (18-agent system) ────────────────────────────────────────────
from change_intelligence_agent import analyze_changes
from dependency_graph_agent    import analyze_dependencies
from alert_correlator_agent    import correlate_alerts
from anomaly_correlator_agent  import correlate_anomalies
from postmortem_agent          import generate_postmortem
from validation import (
    validate_log_anomalies, validate_apm_result, validate_trace_result,
    validate_code_result, validate_deploy_result, validate_kb_result,
    validate_hypothesis_result, pipeline_sanity_check,
)

client = get_client()  # Supports OpenAI or Anthropic — see llm_client.py

# ── Configure logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
)
logger = logging.getLogger("rca.orchestrator")

# ── Circuit Breaker ────────────────────────────────────────────────────────
class CircuitBreaker:
    """Simple circuit breaker: opens after consecutive_failures, auto-resets after reset_timeout."""
    def __init__(self, max_failures=3, reset_timeout=60):
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.state = "closed"  # closed, open, half-open
        self._lock = threading.Lock()

    def can_execute(self):
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.last_failure_time > self.reset_timeout:
                    self.state = "half-open"
                    return True
                return False
            return True  # half-open allows one attempt

    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = "closed"

    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()
            if self.failures >= self.max_failures:
                self.state = "open"

# ── Terminal colours ────────────────────────────────────────────────────────
GRN ="\033[32m"; RED ="\033[31m"; CYA ="\033[36m"; YEL ="\033[33m"
BOLD="\033[1m";  MAG ="\033[35m"; DIM ="\033[2m";  RST ="\033[0m"
BLU ="\033[34m"; WHT ="\033[97m"

# ── Pretty printers (with logging) ──────────────────────────────────────────
def hr(char="═", w=68):
    msg = f"{char*w}"
    print(f"{BOLD}{msg}{RST}")
    logger.info(msg)

def section(title, emoji=""):
    print(f"\n{BOLD}{BLU}{'─'*68}{RST}")
    print(f"{BOLD}{WHT}  {emoji}  {title}{RST}")
    print(f"{BOLD}{BLU}{'─'*68}{RST}")
    logger.info(f"{emoji}  {title}")

def agent_header(n, total, name, desc):
    print(f"\n{BOLD}{CYA}  [{n:02d}/{total}] {name}{RST}{DIM} — {desc}{RST}")
    logger.info(f"[{n:02d}/{total}] {name} - {desc}")

def ok(m):
    print(f"{GRN}     ✅  {m}{RST}")
    logger.info(f"✅  {m}")

def warn(m):
    print(f"{YEL}     ⚠️   {m}{RST}")
    logger.warning(f"⚠️  {m}")

def err(m):
    print(f"{RED}     ❌  {m}{RST}")
    logger.error(f"❌  {m}")

def info(m):
    print(f"         {DIM}{m}{RST}")
    logger.info(m)

def bullet(m):
    print(f"     {MAG}▸{RST} {m}")
    logger.info(f"  - {m}")

def timing(s):
    print(f"     {DIM}⏱  {s:.1f}s{RST}")
    logger.info(f"⏱  {s:.1f}s")

# ── Agent health tracker ────────────────────────────────────────────────────
class AgentStatus:
    def __init__(self, name):
        self.name = name
        self.status = "pending"   # pending | running | ok | warn | error | skipped
        self.duration = 0.0
        self.note = ""

    def start(self):
        self.status = "running"
        self._t = time.time()

    def done(self, note=""):
        self.status = "ok"
        self.duration = time.time() - self._t
        self.note = note

    def skipped(self, reason=""):
        self.status = "skipped"
        self.note = reason

    def failed(self, reason=""):
        self.status = "error"
        self.duration = time.time() - getattr(self, '_t', time.time())
        self.note = reason

# ── Retry wrapper ────────────────────────────────────────────────────────────
# ── Global circuit breaker for LLM calls ──────────────────────────────────
llm_circuit_breaker = CircuitBreaker(max_failures=CB_MAX_FAILURES, reset_timeout=CB_RESET_TIMEOUT)

def with_retry(fn, *args, max_attempts=5, base_delay=3, label="", **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff + jitter + circuit breaker.
    Handles OpenAI rate limits (429) with longer delays."""
    max_delay = 120  # cap at 2 minutes

    # Check circuit breaker before attempting
    if not llm_circuit_breaker.can_execute():
        raise RuntimeError(f"Circuit breaker is open for {label}. Service temporarily unavailable.")

    for attempt in range(1, max_attempts + 1):
        try:
            result = fn(*args, **kwargs)
            llm_circuit_breaker.record_success()
            return result
        except Exception as e:
            llm_circuit_breaker.record_failure()
            if attempt == max_attempts:
                raise
            err_str = str(e)
            # Rate limit errors need longer backoff
            if "429" in err_str or "rate_limit" in err_str:
                base_exp = base_delay * (2 ** attempt)  # 6, 12, 24, 48s
            else:
                base_exp = base_delay ** attempt         # 3, 9, 27s
            # Add jitter to prevent thundering herd
            delay = min(base_exp + random.uniform(0, 1), max_delay)
            warn(f"{label} attempt {attempt} failed ({e}). Retrying in {delay:.1f}s...")
            logger.warning(f"{label} attempt {attempt} failed: {e}")
            time.sleep(delay)

# ── Confidence gate ──────────────────────────────────────────────────────────
CONFIDENCE_GATE = 0.96   # if top hypothesis exceeds this, skip remaining signal agents

# ── Demo data ─────────────────────────────────────────────────────────────────
DEMO = {
    "ticket": """
TICKET: RCA-2041 — Orders failing at checkout with 400 error
Priority: HIGH | Environment: production | Labels: checkout, inventory, cross-service
Reporter: qa-team | Status: Open

Description:
Since 10:00am UTC, ~30% of checkout attempts are failing. Customers see "Order could not
be completed". Logs show HTTP 400 from the inventory service. Some orders go through with
wrong totals — $99 instead of $99.99. Notification emails not being sent.

Steps to reproduce: Add item to cart → checkout → submit.

Comments:
[DevOps 10:45] order-service v2.3.1 deployed 9:45am. Changed HTTP client code.
  Files: HttpInventoryClient.java, HttpNotificationClient.java,
         SqliteOrderRepository.java, CouponValidator.java, CurrencyConverter.java
[Backend 11:02] Inventory logs show KeyError: 'quantity'. We send 'qty'.
[QA 11:15] Notification service 400 — we send order_id, they expect orderId.
[Finance 11:30] ~150 orders in 2hrs with whole-number totals. $150 revenue gap.
""",
    "metrics": """
Time window: 2024-01-15 09:45 – 11:00 UTC

java-order-service:
  CPU: 15% → 78% (spike at 10:23)
  Memory: stable 420MB
  Error rate: 0.1% → 31.4% (at 10:23)
  Latency p50: 45ms → 890ms
  Latency p95: 120ms → 3100ms
  Latency p99: 200ms → 4200ms
  Throughput: 850 req/min → 590 req/min
  Active threads: 12 → 48 (pool size 50)

python-inventory-service:
  CPU: stable 8%
  Error rate: 0% → 29.1% (at 10:23, all HTTP 400s)
  Latency p99: 45ms → 50ms (stable — not overloaded)
  Throughput mirrors java-order-service drop

node-notification-service:
  CPU: stable 3%
  Error rate: 0% → 12.3% (at 10:24, HTTP 400s, slightly delayed)
  Latency p99: 22ms → 25ms (stable)

sqlite (order-db):
  Query time avg: 2ms → 180ms (at 10:23)
  Lock waits: 0 → 14/min
  Disk I/O: normal
""",
    "logs": {
        "java-order-service": """
2024-01-15 10:23:41 ERROR HttpInventoryClient - POST http://localhost:5003/reserve returned 400
Response: {"error": "Missing required field: quantity"}
Request:  {"qty": 2, "product_id": "PROD-001"}
2024-01-15 10:23:41 ERROR OrderService - Inventory reservation failed for ORD-8821
2024-01-15 10:23:42 ERROR SqliteOrderRepository - Stored total=99 expected 99.99 for ORD-8822
SQL: INSERT INTO orders (id, total) VALUES ('ORD-8822', 99)
2024-01-15 10:23:43 ERROR HttpNotificationClient - POST http://localhost:5004/notify returned 400
Response: {"error": "Missing required field: orderId"}
Request:  {"order_id": "ORD-8823", "status": "confirmed"}
2024-01-15 10:23:43 ERROR OrderService - Notification failed for ORD-8823
""",
        "python-inventory-service": """
2024-01-15 10:23:41 INFO Flask — POST /reserve from 127.0.0.1
2024-01-15 10:23:41 ERROR app — KeyError: 'quantity' — received keys: ['qty', 'product_id']
2024-01-15 10:23:41 INFO Flask — 400 POST /reserve
""",
        "node-notification-service": """
[2024-01-15T10:24:15.442Z] POST /notify
[2024-01-15T10:24:15.443Z] ERROR: Missing required field: orderId
[2024-01-15T10:24:15.443Z] Received: { order_id: 'ORD-8823', status: 'confirmed' }
[2024-01-15T10:24:15.444Z] 400 Bad Request
"""
    },
    "trace": """
TraceID: abc-8821-xyz | Request: POST /checkout | 2024-01-15T10:23:41Z

Span 1  | java-order-service       | OrderService.checkout()           | 4187ms | ERROR
  └─ Span 2 | java-order-service   | HttpInventoryClient.reserve()     |   52ms | ERROR → HTTP 400
       └─ Span 3 | python-inv-svc  | Flask POST /reserve               |   48ms | ERROR → KeyError:'quantity'
  └─ Span 4 | java-order-service   | SqliteOrderRepository.insert()    |  180ms | OK    → stored total=99 (should be 99.99)
  └─ Span 5 | java-order-service   | HttpNotificationClient.notify()   |   31ms | ERROR → HTTP 400
       └─ Span 6 | node-notif-svc  | Node POST /notify                 |   28ms | ERROR → Missing: orderId
  └─ Span 7 | java-order-service   | CouponValidator.validate()        |    5ms | OK    → checked "PERCENT" (API returns "PERCENTAGE")
  └─ Span 8 | java-order-service   | CurrencyConverter.convert(EUR)    |    3ms | OK    → returned 0.108 (should be 1.08)
""",
    "deployments": """
2024-01-15 09:45 UTC — java-order-service v2.3.1 → production (38m before incident)
  Changed: HttpInventoryClient.java, HttpNotificationClient.java,
           SqliteOrderRepository.java, CouponValidator.java, CurrencyConverter.java
  Commit: "feat: refactor HTTP clients, update currency rates"
  Deployed by: ci-pipeline

2024-01-14 16:30 UTC — python-inventory-service v1.1.2 → production
  Changed: app.py (logging improvements only)
  Commit: "chore: improve request logging"

2024-01-13 11:00 UTC — node-notification-service v1.0.8 → production
  Changed: server.js (retry logic)
  Commit: "feat: add notification retry"
""",
    "db_context": """
Table: orders
Columns: id TEXT, customer_id TEXT, total REAL, status TEXT, created_at TEXT

Sample rows (10 most recent, post-deployment):
  ORD-8825 | total = 99     | 2024-01-15 10:24:01  ← should be  99.99
  ORD-8824 | total = 149    | 2024-01-15 10:23:55  ← should be 149.99
  ORD-8823 | total = 29     | 2024-01-15 10:23:50  ← should be  29.50
  ORD-8822 | total = 199.99 | 2024-01-14 16:05:11  ← CORRECT (pre-deployment)
  ORD-8821 | total = 59.99  | 2024-01-14 14:22:08  ← CORRECT

Table: audit_log
  ORD-8825 | CHECKOUT_COMPLETE | {"total":99, "currency":"USD"}   ← truncated
"""
}

DEMO2 = {
    "ticket": """
TICKET: RCA-2087 — Widespread 503 errors during flash sale
Priority: CRITICAL | Environment: production | Labels: availability, performance, inventory, timeout
Reporter: incident-commander | Status: Open

Description:
At 10:30am UTC, marketing campaign (email + social media push) drove traffic 3x normal baseline.
Within minutes, customers experienced widespread HTTP 503 errors on checkout. Checkout latency
spiked from ~200ms to 5-8 seconds. Some requests timeout completely. Order processing came to
a near halt for 15 minutes (10:30-10:45am).

Approximately 2,400 failed checkout attempts, ~$180K in lost revenue during this window.
Mobile app and web both affected. Database appears stuck — no orders created between 10:30-10:45.

Symptoms:
- Customers see "Service Unavailable" (HTTP 503)
- Some see "Connection Timeout"
- Order service completely unresponsive for some requests
- Notification emails not being sent for any orders
- Inventory service responding with timeouts intermittently

Timeline:
10:28am — Marketing campaign launched (3x traffic expected)
10:30am — Support tickets begin arriving (503 errors)
10:31am — DevOps notified, on-call monitoring alerts triggered
10:32am — Error rate hitting 78% on order-service
10:35am — Database query times at 18+ seconds
10:40am — python-inventory-service CPU spike observed
10:43am — Incident escalated, rollback decision made
10:45am — Database connection count returns to normal
10:50am — Service partially recovered, P95 latency still 6s
11:00am — Full recovery (normal load, normal latency)

Comments:
[DevOps 10:35] python-inventory-service v1.2.0 deployed at 08:00 (2.5hrs prior). Checking deploy diff.
[DBA 10:40] Database showing extreme connection pool wait times. Inventory queries piling up.
[DevOps 10:42] Found it! Inventory deploy changed DB_POOL_SIZE from 20 to 2. Typo in config.
[Backend 10:44] Java order service thread pool at 50/50 (saturated). All threads waiting on inventory.
[DevOps 10:48] Rolled back inventory to v1.1.9. Service recovered within 2 minutes.
""",
    "metrics": """
Time window: 2024-02-22 10:28 – 11:05 UTC

python-inventory-service:
  CPU: 12% → 89% (spike at 10:31)
  Memory: stable 380MB
  Error rate: 0.2% → 68.3% (at 10:35)
  Latency p50: 35ms → 2400ms
  Latency p95: 50ms → 8900ms
  Latency p99: 70ms → 12000ms (peak)
  Throughput: 450 req/min → 380 req/min (retries + backoff)
  GC time: 120ms avg → 2100ms avg
  DB connection pool: 2 active, 47 pending (peak at 10:34)
  DB connection wait time: <1ms → 5800ms (p99)

java-order-service:
  CPU: 22% → 95% (spike at 10:33)
  Memory: stable 680MB
  Error rate: 0.1% → 78.2% (at 10:32)
  Latency p50: 120ms → 5400ms
  Latency p95: 280ms → 7600ms
  Latency p99: 420ms → 8100ms
  Throughput: 1200 req/min → 280 req/min
  Active threads: 18 → 50 (pool size 50, exhausted at 10:33)
  Thread wait time: 0ms → 4200ms
  Calls to inventory: 1200/min → retries increasing 1800/min (exponential backoff, max 3 retries)
  Retry rate: 0.5% → 65% (at 10:35)
  Calls to notification: 1200/min → 220/min (blocked, can't reach order service)

node-notification-service:
  CPU: 5% → 28% (spike at 10:36)
  Error rate: 0% → 41.2% (at 10:36, ECONNREFUSED errors)
  Latency p99: 15ms → 45ms
  Throughput: 1100 req/min → 0 req/min (completely blocked after 10:35)
  Failed connection attempts to order-service: 0 → 3200+ (exponential backoff retries)

postgresql (inventory-db):
  Connection pool usage: 15/20 → 2/2 (saturated) at 10:31
  Active connections: 12 → 2 (pool size 2, bottleneck)
  Pending connections: 0 → 47 (waiting in queue)
  Query time avg: 8ms → 2100ms
  Query time p99: 45ms → 9500ms
  Lock waits: 0 → 12/sec
  Transaction rollbacks: 0 → 340 (timeout-induced)

sqlite (order-db):
  Query time avg: 3ms → 180ms
  Lock waits: 0 → 8/min
  Transactions blocked on inventory: 0 → 2300+
""",
    "logs": {
        "python-inventory-service": """
2024-02-22 10:30:15 INFO Uvicorn — Started server process
2024-02-22 10:30:42 WARNING asyncpg — Connection pool exhausted: 2/2 active, 8 pending
2024-02-22 10:30:58 ERROR asyncpg — TimeoutError waiting for DB connection from pool (timeout=30s)
Request: GET /stock/PROD-4821 | Client: 172.30.0.15
2024-02-22 10:31:04 ERROR asyncpg — TimeoutError waiting for DB connection from pool (timeout=30s)
Request: GET /stock/PROD-5103 | Client: 172.30.0.16
2024-02-22 10:31:15 WARNING asyncpg — Connection pool exhausted: 2/2 active, 23 pending
2024-02-22 10:31:22 CRITICAL asyncpg — 47 requests waiting for DB connection (max pool: 2)
Connection pool stats: active=2, idle=0, initialized=2, minsize=2, maxsize=2
2024-02-22 10:31:30 ERROR app — TimeoutError on POST /reserve/PROD-6442 after 30s wait for DB
2024-02-22 10:32:05 WARNING Uvicorn — 156 requests queued, processing rate 4 req/sec, 39s wait time
2024-02-22 10:32:15 ERROR asyncpg — 47 pending requests, backing off
2024-02-22 10:33:02 CRITICAL app — Connection pool failure: unable to acquire connection within 30s (47 in queue)
Stack: File "app.py", line 387, in get_stock
    conn = await db_pool.acquire()
2024-02-22 10:33:45 ERROR app — POST /reserve returned 504 (Gateway Timeout)
2024-02-22 10:34:18 CRITICAL DATABASE — All 2 connections busy, throughput collapsed to 4 req/min
2024-02-22 10:45:12 INFO Deployment — Rolling back to v1.1.9 (previous config DB_POOL_SIZE=20)
2024-02-22 10:45:45 INFO asyncpg — Connection pool initialized: minsize=20, maxsize=20
2024-02-22 10:46:02 INFO app — Request queue emptied, processing resumed at normal rate
2024-02-22 10:46:15 INFO Uvicorn — All services healthy, latency returning to baseline
""",
        "java-order-service": """
2024-02-22 10:30:51 INFO OrderServiceImpl — Received 1200 checkout requests/min (3x baseline)
2024-02-22 10:31:03 WARN InventoryClient — POST http://inventory:5003/reserve timeout after 5000ms (attempt 1/3)
Request: {product_id: 'PROD-4821', qty: 3, reserve_duration_sec: 600}
TraceID: trace-2891-8f3c
2024-02-22 10:31:08 WARN InventoryClient — Retrying (backoff 100ms) for TraceID: trace-2891-8f3c
2024-02-22 10:31:15 ERROR InventoryClient — POST http://inventory:5003/reserve timeout after 5000ms (attempt 2/3)
java.net.SocketTimeoutException: Connection timed out
2024-02-22 10:31:18 WARN InventoryClient — Retrying (backoff 200ms) for TraceID: trace-2891-8f3c
2024-02-22 10:31:25 ERROR InventoryClient — POST http://inventory:5003/reserve timeout after 5000ms (attempt 3/3)
java.net.SocketTimeoutException: Connection timed out
2024-02-22 10:31:28 ERROR OrderServiceImpl — Inventory reservation failed after 3 retries for ORD-9104, TraceID: trace-2891-8f3c
2024-02-22 10:31:29 ERROR CheckoutController — HTTP 503 Service Unavailable (inventory unavailable)
2024-02-22 10:31:42 WARN ThreadPoolExecutor — Thread pool usage: 18/50
2024-02-22 10:32:04 WARN ThreadPoolExecutor — Thread pool usage: 35/50
2024-02-22 10:32:51 CRITICAL ThreadPoolExecutor — Thread pool usage: 50/50 (EXHAUSTED)
2024-02-22 10:32:52 CRITICAL ThreadPoolExecutor — All 50 worker threads blocked waiting on inventory service
2024-02-22 10:32:53 CRITICAL OrderServiceImpl — Thread pool saturation: new requests queueing indefinitely
New checkout request from customer 'CUST-8821' cannot be processed
2024-02-22 10:33:01 ERROR CheckoutController — HTTP 503 Service Unavailable (thread pool exhausted)
Request blocked at queue position 412
2024-02-22 10:33:15 CRITICAL InventoryClient — 340 pending requests to inventory, 240 have timed out
2024-02-22 10:34:02 ERROR NotificationClient — Cannot reach order-service, retrying with exponential backoff
java.net.ConnectException: Connection refused to http://notification:5004/notify
2024-02-22 10:34:15 WARN NotificationClient — 1200+ ECONNREFUSED errors (notification service cannot connect to order-service)
2024-02-22 10:45:00 INFO Deployment — Inventory v1.1.9 rolled back successfully
2024-02-22 10:45:30 INFO OrderServiceImpl — Thread pool usage returning to normal: 18/50
2024-02-22 10:46:15 INFO InventoryClient — All pending requests resolved, timeout rate returning to 0.1%
2024-02-22 10:47:00 INFO ThreadPoolExecutor — Thread pool health: nominal
""",
        "node-notification-service": """
[2024-02-22T10:30:45.221Z] INFO NotificationWorker — Starting batch email processor
[2024-02-22T10:31:12.450Z] INFO NotificationWorker — Queue depth: 1100 messages, processing at 1100 msg/min
[2024-02-22T10:31:58.693Z] DEBUG NotificationWorker — Fetching order details from http://order-service:7001/order/ORD-9104
[2024-02-22T10:31:63.401Z] WARN RequestHandler — Timeout on GET http://order-service:7001/order/ORD-9105 (5000ms)
[2024-02-22T10:32:15.622Z] ERROR RequestHandler — Connection timeout to order-service (attempt 1/3)
[2024-02-22T10:32:20.833Z] WARN RequestHandler — Retrying with exponential backoff...
[2024-02-22T10:32:31.044Z] ERROR RequestHandler — Socket connection refused to http://order-service:7001/order/ORD-9106
Error: ECONNREFUSED (order-service not accepting new connections)
[2024-02-22T10:32:35.255Z] WARN RequestHandler — Exponential backoff retry 2/3 (delay 400ms)
[2024-02-22T10:33:01.466Z] ERROR RequestHandler — ECONNREFUSED after 3 attempts to order-service
Cannot fetch order details for email notifications
[2024-02-22T10:33:02.577Z] CRITICAL NotificationWorker — Order service unresponsive, 2340 notifications stuck in queue
[2024-02-22T10:33:45.788Z] WARN NotificationWorker — Queue depth: 2340 (growth rate: 45 msg/min), unable to drain
[2024-02-22T10:34:02.009Z] ERROR NotificationWorker — ECONNREFUSED × 3 on ORD-9120, unable to send email
[2024-02-22T10:35:30.142Z] CRITICAL NotificationWorker — Service degraded: 0 emails sent in last 2 minutes (was 18/min)
[2024-02-22T10:36:15.333Z] INFO RequestHandler — Retry queue at 3200+ attempts, backing off further
[2024-02-22T10:45:00.111Z] INFO Deployment — Order service recovered (inventory rollback complete)
[2024-02-22T10:45:31.222Z] DEBUG RequestHandler — Connection to order-service established
[2024-02-22T10:46:00.333Z] INFO NotificationWorker — Queue depth: 340, draining at 80 msg/min
[2024-02-22T10:47:15.444Z] INFO NotificationWorker — Backlog cleared, normal email throughput resumed
"""
    },
    "trace": """
TraceID: trace-2891-8f3c | Request: POST /checkout | 2024-02-22T10:31:03Z | SPAN_TIMEOUT

Span 1  | java-order-service       | CheckoutController.POST /checkout       | 25847ms | ERROR (503)
  └─ Span 2 | java-order-service   | OrderServiceImpl.processCheckout()       | 25840ms | ERROR
       └─ Span 3 | java-order-service | InventoryClient.reserve()             | 5102ms | ERROR → SocketTimeoutException
            └─ Span 4 | python-inv-svc | asyncio pool.acquire()              | 4998ms | TIMEOUT → all 2 connections busy
            └─ Span 5 | postgresql     | SELECT stock FOR UPDATE (pending)     | (never acquired connection)
       └─ Span 6 | java-order-service | InventoryClient.reserve() [RETRY 2]   | 5203ms | ERROR → SocketTimeoutException
       └─ Span 7 | java-order-service | InventoryClient.reserve() [RETRY 3]   | 5302ms | ERROR → SocketTimeoutException
       └─ Span 8 | java-order-service | ThreadPool.queue()                    | 9000ms | QUEUED (pool exhausted 50/50)
  └─ Span 9 | java-order-service   | SqliteOrderRepository.insertPending()   | (never executed, thread blocked)
  └─ Span 10 | node-notif-svc      | NotificationWorker.getOrderDetails()    | (blocked, order service unreachable)

Active concurrent traces (at 10:35am):
  - 2300+ traces in STATE_RETRYING (waiting on inventory)
  - 1850+ traces in STATE_BLOCKED (thread pool queue)
  - 3200+ notification traces in STATE_ECONNREFUSED (order service unavailable)
""",
    "deployments": """
2024-02-22 08:00 UTC — python-inventory-service v1.2.0 → production (2.5 hours before incident)
  Changed: app.py (connection pool config), requirements.txt (asyncpg version bump)
  Config change: DB_POOL_SIZE: 20 → 2  ⚠️ TYPO (meant to set to 20, not 2)
  Commit: "feat: improve DB pool management with new asyncpg version"
  Deployed by: ci-pipeline
  Related PRs: #4821 (asyncpg upgrade), #4832 (pool config refactor)

2024-02-21 16:30 UTC — java-order-service v3.2.1 → production
  Changed: InventoryClient.java, CheckoutController.java (retry logic, timeout handling)
  Config: SocketTimeout=5000ms (unchanged), RetryPolicy=ExponentialBackoff(max_retries=3)
  Commit: "chore: update dependency versions"

2024-02-20 09:15 UTC — node-notification-service v2.1.4 → production
  Changed: NotificationWorker.js, RequestHandler.js (stability improvements)
  Config: OrderServiceTimeout=5000ms, MaxRetries=3
  Commit: "fix: improve error handling in notification queue"

No deployments to java-order-service or node-notification-service between 08:00-10:30 on 2024-02-22.
""",
    "db_context": """
PostgreSQL (inventory-db):
Table: inventory
Columns: product_id TEXT PRIMARY KEY, stock_qty INT, reserved_qty INT, updated_at TIMESTAMP

Connection pool configuration (before incident):
  Config file: /etc/postgres/pool.conf
  DB_POOL_SIZE: 20 (set by python-inventory-service v1.2.0 as: 2  ⚠️ BUG)
  Min connections: 2
  Max connections: 2  ⚠️ ACTUAL VALUE IN PROD
  Connection timeout: 30s
  Idle timeout: 5min

Connection pool metrics (at 10:34am, peak):
  Active connections: 2/2 (100% utilization, saturated)
  Idle connections: 0
  Pending requests: 47 (waiting for a connection to become available)
  Oldest pending request: waiting 24 seconds
  Average wait time: 12 seconds

Query performance before incident (10:00am):
  SELECT stock_qty WHERE product_id = ? | avg 8ms, p99 45ms
  Transaction (SELECT FOR UPDATE + INSERT): avg 15ms, p99 60ms

Query performance during incident (10:34am):
  SELECT stock_qty WHERE product_id = ? | avg 2100ms, p99 9500ms (because: waiting for connection)
  Locks held by pending queries: 8 rows with 30s+ locks
  Transaction rollbacks: 340 (timeout-induced)

SQLite (order-db):
Table: orders
Sample rows created before incident (pre-10:30):
  ORD-9102 | 2024-02-22 10:28:45 | status=completed
  ORD-9103 | 2024-02-22 10:29:12 | status=completed

Gap in order creation (incident window 10:30-10:45):
  [NO ORDERS CREATED DURING THIS 15-MINUTE WINDOW]

Sample rows created after recovery (post-10:45):
  ORD-9104 | 2024-02-22 10:46:02 | status=completed (backlogged order, marked as inserted at 10:46)
  ORD-9105 | 2024-02-22 10:46:15 | status=completed
  ORD-9106 | 2024-02-22 10:47:01 | status=completed

Estimated lost orders: ~2300-2400 (based on 1200 req/min baseline × 15 minutes × ~78% failure rate)
Revenue impact: ~$180K (avg order value $75)

WAL (Write-Ahead Log) analysis:
  Spike in rolled-back transactions at 10:30-10:35
  No new transactions committed during 10:30-10:45 window
  Transaction log shows: "ROLLBACK: checkout transaction timed out waiting on inventory"
"""
}

DEMO3 = {
    "ticket": """
TICKET: RCA-2103 — Revenue shortfall from coupon abuse (flash sale)
Priority: HIGH | Environment: production | Labels: coupon, data-integrity, race-condition
Reporter: finance-team | Status: Open | Assigned: backend-team

Description:
Finance team discovered $22,500 revenue gap during flash sale on 2024-02-10 14:00–16:00 UTC.
Initial suspicion: coupon code abuse or fraud. Investigation shows legitimate coupon codes
(FLASH50 for 50% discount, single-use) were applied to 2+ orders each. Example: coupon FLASH50
redemption count = 14, but only 7 unique customers used it (both orders per customer got discount).
All orders processed without HTTP errors (200 OK responses), so detection was silent.
java-order-service has race condition in CouponValidator (added in v2.2.0 3 weeks ago).
Two concurrent requests for same coupon both read it as "unused", both apply discount, both mark as used.
Under normal traffic (5 RPS), race window never overlaps. Flash sale at 100 RPS makes race condition
reproduce consistently. Bug caused ~15% of flash sale orders to get double discounts.

Steps to reproduce: Send 2 requests for FLASH50 within 5ms, same coupon code, different orders.
Expected: 1st gets discount, 2nd rejected. Actual: both get discount.

Comments:
[Finance 2024-02-10 17:30] "Revenue shortfall detected during flash sale. Coupon costs = $22,500."
[Backend 2024-02-11 10:00] "Analyzed git log: CouponValidator.java changed in v2.2.0 commit
  'perf: add coupon caching for performance'. Added line: putIfAbsent(coupon_cache, ...)
  which reads stale cache value. Logic: Thread 1 reads cache (miss) → sets cache (not used).
  Thread 2 reads cache (SAME miss, concurrent) → sets cache (not used). Both apply discount."
[QA 2024-02-11 11:30] "Confirmed: two traces for orders ORD-9401 and ORD-9402 both validate
  FLASH50 within 2ms of each other (overlapping spans in trace). Both get discount_applied=true."
[DBA 2024-02-11 12:00] "Database shows: coupons.FLASH50.usage_count = 14 (single-use coupon),
  but only 7 unique orders redeemed it. Orders ORD-9401 and ORD-9402 created 47ms apart,
  both have coupon_code=FLASH50 and discount_applied=1."
""",
    "metrics": """
Time window: 2024-02-10 13:30 – 16:30 UTC

java-order-service:
  CPU: 12% → 68% (flash sale spike at 14:00, no anomalies)
  Memory: 480MB → 620MB (normal, linear with load)
  Error rate: 0.05% → 0.08% (HEALTHY — no errors!)
  Latency p50: 42ms → 180ms (normal, linear with load)
  Latency p95: 110ms → 520ms (reasonable)
  Latency p99: 180ms → 920ms (no timeout)
  Throughput: 5 req/min (pre-sale) → 100 req/min (14:00–16:00) → 5 req/min (after)
  Active threads: 8 → 45 (expected during load)

python-inventory-service:
  CPU: 4% → 38% (mirrors java load)
  Memory: 160MB → 280MB (normal)
  Error rate: 0.0% (CLEAN)
  Latency p50: 12ms → 55ms (normal)
  Latency p99: 30ms → 210ms (normal)

node-notification-service:
  CPU: 1% → 18% (higher due to more emails)
  Memory: stable 95MB
  Error rate: 0.0% (sends emails, but wrong totals)
  Latency p50: 8ms → 25ms (normal)

sqlite (order-db):
  Query time avg: 1.2ms → 4.5ms (slightly higher, normal)
  Lock waits: 0.1/min → 0.3/min (virtually none — no contention!)
  Disk I/O: normal

⚠️  KEY ANOMALY (not obvious in metrics):
  coupon_redemption_count for FLASH50 = 14
  unique_coupon_count in orders = 7
  RATIO MISMATCH: 2x redemption rate suggests double application
""",
    "logs": {
        "java-order-service": """
2024-02-10 14:00:23.401 INFO CouponValidator - applying discount FLASH50 to ORD-9401 (28ms before peer)
2024-02-10 14:00:23.429 INFO CouponValidator - applying discount FLASH50 to ORD-9402 (concurrent, 28ms later)
2024-02-10 14:00:23.401 DEBUG CouponValidator - read coupon cache FLASH50: used=false (cache miss, Thread 1)
2024-02-10 14:00:23.402 DEBUG CouponValidator - putIfAbsent(FLASH50, {used=true}) → stored (Thread 1)
2024-02-10 14:00:23.429 DEBUG CouponValidator - read coupon cache FLASH50: used=false (cache miss, Thread 2 — STALE READ!)
2024-02-10 14:00:23.430 DEBUG CouponValidator - putIfAbsent(FLASH50, {used=true}) → already present, not stored (Thread 2)
2024-02-10 14:00:23.430 WARN CouponValidator - coupon FLASH50 already marked used, proceeding with cached validation result (stale=true)
2024-02-10 14:00:23.401 INFO OrderService - ORD-9401: discount=50%, new_total=49.50 (was 99.00)
2024-02-10 14:00:23.429 INFO OrderService - ORD-9402: discount=50%, new_total=49.50 (was 99.00)
2024-02-10 14:00:24.510 INFO CouponValidator - applying discount FLASH50 to ORD-9403
2024-02-10 14:00:24.510 INFO OrderService - ORD-9403: discount=50%, new_total=49.50 (was 99.00)
[... continues for 14 instances during 14:00–16:00 ...]
2024-02-10 16:00:45.999 INFO CouponValidator - flash sale ended, traffic drop
""",
        "python-inventory-service": """
[2024-02-10T14:00:23.400Z] INFO: Processing checkout ORD-9401: qty=1, coupon=FLASH50, discount_applied=true
[2024-02-10T14:00:23.428Z] INFO: Processing checkout ORD-9402: qty=1, coupon=FLASH50, discount_applied=true
[2024-02-10T14:00:24.510Z] INFO: Processing checkout ORD-9403: qty=1, coupon=FLASH50, discount_applied=true
[2024-02-10T14:00:25.601Z] INFO: Processing checkout ORD-9404: qty=1, coupon=FLASH50, discount_applied=true
[...all with discount_applied=true...]
""",
        "node-notification-service": """
[2024-02-10T14:00:23.410Z] Sending confirmation email: ORD-9401, total=$49.50, item_price=$99.00
[2024-02-10T14:00:23.438Z] Sending confirmation email: ORD-9402, total=$49.50, item_price=$99.00
[2024-02-10T14:00:24.520Z] Sending confirmation email: ORD-9403, total=$49.50, item_price=$99.00
[2024-02-10T14:00:25.610Z] Sending confirmation email: ORD-9404, total=$49.50, item_price=$99.00
[...all show 50% discount applied, though catalog says item=$99...]
"""
    },
    "trace": """
TraceID: flash-sale-order-9401 | Request: POST /checkout | 2024-02-10T14:00:23.401Z

Span 1  | java-order-service       | OrderService.checkout()           |  42ms | OK
  └─ Span 2 | java-order-service   | CouponValidator.validate()        |   5ms | OK → discount_applied=true ✓
       └─ Span 3 | java-order-service | CouponCache.get(FLASH50)       |   1ms | HIT (cache miss) → used=false
       └─ Span 4 | java-order-service | CouponCache.putIfAbsent()     |   2ms | OK
  └─ Span 5 | python-inventory-svc | POST /reserve                     |  18ms | OK
  └─ Span 6 | java-order-service   | OrderRepository.insert()          |   8ms | OK
  └─ Span 7 | node-notification   | POST /notify                       |   9ms | OK

(Concurrent trace — OVERLAPPING validation logic):
TraceID: flash-sale-order-9402 | Request: POST /checkout | 2024-02-10T14:00:23.429Z (28ms after 9401)

Span 11  | java-order-service      | OrderService.checkout()          |  41ms | OK
  └─ Span 12 | java-order-service  | CouponValidator.validate()       |   5ms | OK → discount_applied=true ✓
       └─ Span 13 | java-order-service | CouponCache.get(FLASH50)     |   1ms | HIT (STALE!) → used=false [RACE]
       └─ Span 14 | java-order-service | CouponCache.putIfAbsent()   |   2ms | NO-OP (already stored)
       └─ Span 15 | java-order-service | WARN: stale validation result |   0ms | RACE CONDITION ⚠️
  └─ Span 16 | python-inventory-svc | POST /reserve                    |  17ms | OK
  └─ Span 17 | java-order-service   | OrderRepository.insert()         |   7ms | OK
  └─ Span 18 | node-notification   | POST /notify                      |  10ms | OK

Annotation on both spans: span.event(discount_applied=true, coupon=FLASH50) ← BOTH marked applied!
Timeline overlap: Span 3 and Span 13 both execute during [14:00:23.402–14:00:23.404]
""",
    "deployments": """
2024-02-10 (not relevant) — no recent deployment
2024-02-01 (3 weeks before) — java-order-service v2.2.0 → production
  Changed: CouponValidator.java (added coupon caching)
  Commit: "perf: add coupon caching for validation speed"
  Introduced bug: cache uses putIfAbsent() but concurrent threads read before first putIfAbsent() completes
  Details: CouponCache is ConcurrentHashMap, but logical operation (read + write) is NOT atomic

2024-01-22 (pre-DEMO3 incident, post-DEMO2) — java-order-service v2.2.5
  Deployed to fix DEMO2 timeout issue. v2.2.0 code still has caching, not reverted.

v2.2.0 still in production at time of DEMO3 incident
""",
    "db_context": """
Table: orders
Columns: id TEXT PRIMARY KEY, customer_id TEXT, coupon_code TEXT, discount_applied BOOLEAN,
         original_total REAL, final_total REAL, created_at TEXT

Sample rows (flash sale period):
  ORD-9401 | customer=C001 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:00:23
  ORD-9402 | customer=C002 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:00:23
  ORD-9403 | customer=C001 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:00:24  ← SAME CUSTOMER!
  ORD-9404 | customer=C003 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:00:24
  ORD-9405 | customer=C002 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:00:25  ← SAME CUSTOMER!
  [... 9 more orders with FLASH50 discount applied ...]
  ORD-9414 | customer=C005 | coupon=FLASH50 | discount_applied=1 | original=99.00  | final=49.50 | 2024-02-10 14:01:12

Table: coupons
Columns: code TEXT PRIMARY KEY, coupon_type TEXT, discount_amount REAL, max_uses INT, usage_count INT, created_at TEXT

Sample rows:
  FLASH50    | PERCENTAGE | discount=50    | max_uses=1 | usage_count=14 ⚠️  | 2024-02-10 00:00:00
  FLASH25    | PERCENTAGE | discount=25    | max_uses=1 | usage_count=8      | 2024-02-10 00:00:00

ANOMALY: FLASH50 has usage_count=14 but only 7 unique customer_ids used it.
         Distinct (customer_id) where coupon_code='FLASH50' = 7
         Count (*) where coupon_code='FLASH50' = 14
         Each customer appears exactly 2x in the flash sale window.

Time analysis:
  Pairs with <50ms delta: 7 pairs
  Example pair 1: (ORD-9401, 14:00:23.401) and (ORD-9402, 14:00:23.429) — 28ms apart, same coupon
  Example pair 2: (ORD-9403, 14:00:24.510) and (ORD-9404, 14:00:24.520) — 10ms apart, same coupon
  (Each pair processes "simultaneously" on different threads, race condition window)

Lock waits: <0.5/min (no contention — all writes succeeded, data integrity not violated)
            (contrast with DEMO2: thousands of lock waits)
"""
}


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

def run_pipeline(ticket_text=None, metrics_text=None, logs_by_service=None,
                 trace_text=None, deployment_text=None, db_context=None,
                 apply_fixes=False, output_dir=None):

    pipeline_start = time.time()
    statuses = {}
    results  = {}
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def track(name):
        s = AgentStatus(name)
        statuses[name] = s
        return s

    hr()
    msg = f"🤖  AI-POWERED ROOT CAUSE ANALYSIS PIPELINE"
    print(f"{BOLD}{WHT}  {msg}{RST}")
    logger.info(msg)
    msg2 = f"Aspire Systems  |  18-Agent System  |  {now_str}"
    print(f"{DIM}  {msg2}{RST}")
    logger.info(msg2)
    hr()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 1 — INTAKE
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 1 — INTAKE", "📥")

    # ── JiraAgent ──────────────────────────────────────────────────────────
    s = track("JiraAgent")
    agent_header(1, 18, "JiraAgent", "Parse ticket, extract symptoms & search keywords")
    ticket_ctx = {}
    if ticket_text:
        s.start()
        try:
            ticket_ctx = with_retry(analyze_ticket, ticket_text, label="JiraAgent")
            s.done(f"Ticket {ticket_ctx.get('ticket_id','?')} | {ticket_ctx.get('priority','?')} priority")
            ok(s.note)
            symptoms = ticket_ctx.get('reported_symptoms', [])
            for sym in symptoms[:3]: info(sym)
            info(f"Search keywords → {', '.join(ticket_ctx.get('search_keywords', [])[:6])}")
            info(f"Regression: {ticket_ctx.get('is_regression', '?')} | "
                 f"Signal: {ticket_ctx.get('strongest_signal', '')[:80]}")
        except Exception as e:
            s.failed(str(e)); err(f"JiraAgent failed: {e}")
        timing(s.duration)
    else:
        s.skipped("no ticket provided"); warn(s.note)
    results["ticket"] = ticket_ctx

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 — PARALLEL SIGNAL COLLECTION
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 2 — SIGNAL COLLECTION (parallel)", "📡")
    keywords = ticket_ctx.get("search_keywords", [])
    incident_time = "unknown"

    # Define all parallel tasks
    parallel_tasks = {}

    def run_apm():
        if not metrics_text: return {}
        return with_retry(analyze_metrics, metrics_text, label="APMAgent")

    def run_logs():
        if not logs_by_service: return []
        all_a = []
        for svc, log in logs_by_service.items():
            all_a.extend(with_retry(analyze_logs, log, svc, label=f"LogAgent/{svc}"))
        return all_a

    def run_trace():
        if not trace_text: return {}
        return with_retry(analyze_trace, trace_text, label="TraceAgent")

    def run_deployment():
        if not deployment_text: return {}
        return with_retry(analyze_deployments, deployment_text, incident_time, label="DeploymentAgent")

    def run_database():
        if not db_context: return {}
        return with_retry(analyze_database, db_context, label="DatabaseAgent")

    def run_code():
        code_ctx = ""  # pass empty context, agent reads files itself via tools
        return with_retry(analyze_code, code_ctx, keywords, label="CodeAgent")

    def run_change_intel():
        return with_retry(analyze_changes, deployment_text or "", incident_time, label="ChangeIntelAgent")

    # Agent metadata for display
    TOTAL_AGENTS = 18
    agent_meta = {
        "APMAgent":               (2,  "APMAgent",               "Analyze CPU/latency/error rate metrics (autoencoder+IsolationForest ensemble)"),
        "LogAgent":               (3,  "LogAgent",               "Extract anomalies from service logs (ReAct + regex parser)"),
        "TraceAgent":             (4,  "TraceAgent",             "Reconstruct distributed trace (DAG+DFS graph algorithms)"),
        "DeploymentAgent":        (5,  "DeploymentAgent",        "Correlate with recent deployments (real git commands)"),
        "DatabaseAgent":          (6,  "DatabaseAgent",          "Inspect schema and data integrity (real SQL queries)"),
        "CodeAgent":              (7,  "CodeAgent",              "Strategic code analysis across all services"),
        "ChangeIntelligenceAgent":(8,  "ChangeIntelligenceAgent","Deep change analysis: configs, deps, schemas, API contracts"),
    }

    task_fns = {
        "APMAgent":               run_apm,
        "LogAgent":               run_logs,
        "TraceAgent":             run_trace,
        "DeploymentAgent":        run_deployment,
        "DatabaseAgent":          run_database,
        "CodeAgent":              run_code,
        "ChangeIntelligenceAgent":run_change_intel,
    }

    # Track status objects
    for name in agent_meta:
        s = track(name)

    # ── Batched scheduler ───────────────────────────────────────────────────
    # Batch 1: gpt-4o-mini agents (parallel, low token cost)
    # Batch 2: gpt-4o agents (serialized, high token cost — avoid rate limits)
    # This replaces the old "all 7 in parallel" strategy that blew through TPM.

    batch_mini = ["APMAgent", "DeploymentAgent", "DatabaseAgent", "ChangeIntelligenceAgent"]
    batch_4o   = ["LogAgent", "TraceAgent", "CodeAgent"]  # serialized — gpt-4o rate limits

    phase2_start = time.time()
    phase2_results = {}

    def _run_agent(name):
        """Run a single agent with logging."""
        n, display_name, desc = agent_meta[name]
        s = statuses[name]
        s._t = time.time()
        try:
            result = task_fns[name]()
            s.done()
            phase2_results[name] = result
            elapsed = time.time() - s._t
            _print_agent_result(name, display_name, result, elapsed, s)
        except Exception as e:
            s.failed(str(e))
            print(f"  {RED}❌{RST} {BOLD}{display_name}{RST} — FAILED: {str(e)[:100]}")
            phase2_results[name] = {}

    def _print_agent_result(name, display_name, result, elapsed, s):
        """Format and print agent results."""
        if name == "APMAgent":
            anom = result.get("anomalies", []) if result else []
            s.note = f"{len(anom)} metric anomalies"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
            for a in anom[:2]:
                print(f"     {DIM}[{a.get('severity','?')}] {a.get('description','')[:70]}{RST}")
        elif name == "LogAgent":
            count = len(result) if result else 0
            s.note = f"{count} anomalies across all services"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
        elif name == "TraceAgent":
            rf = (result or {}).get("root_failure", {})
            fp = rf.get("service","?") + " → " + rf.get("error","?") if rf else "no trace"
            s.note = fp
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
            silents = (result or {}).get("silent_corruptions", [])
            if silents:
                print(f"     {YEL}⚠️  {len(silents)} silent corruption(s) detected{RST}")
        elif name == "DeploymentAgent":
            verdict = (result or {}).get("verdict","?")
            sus = len((result or {}).get("suspicious_deployments", []))
            s.note = f"{verdict} | {sus} suspicious deployment(s)"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
        elif name == "DatabaseAgent":
            issues = len((result or {}).get("schema_issues", []))
            anoms = len((result or {}).get("data_anomalies", []))
            s.note = f"{issues} schema issues, {anoms} data anomalies"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
        elif name == "CodeAgent":
            phase2_results["CodeAgent"] = result
            issues = len((result or {}).get("code_issues", (result or {}).get("issues", [])))
            s.note = f"{issues} code issues"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")
            for issue in (result or {}).get("code_issues", (result or {}).get("issues", []))[:2]:
                print(f"     {DIM}[{issue.get('severity','?')}] {issue.get('description','')[:70]}{RST}")
        elif name == "ChangeIntelligenceAgent":
            configs = len((result or {}).get("config_changes", []))
            deps    = len((result or {}).get("dependency_changes", []))
            contracts = len((result or {}).get("api_contract_mismatches", []))
            s.note = f"{configs} config, {deps} dep, {contracts} contract changes"
            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")

    # ── Batch 1: gpt-4o-mini agents (parallel — they share a separate, higher TPM limit)
    active_mini = [n for n in batch_mini if n in task_fns]
    if active_mini:
        msg = f"Batch 1: {len(active_mini)} lightweight agents (gpt-4o-mini, parallel)..."
        print(f"\n{DIM}  {msg}{RST}\n")
        logger.info(msg)
        with ThreadPoolExecutor(max_workers=len(active_mini)) as executor:
            futures = {executor.submit(_run_agent, n): n for n in active_mini}
            for f in as_completed(futures):
                f.result()  # exceptions already caught inside _run_agent

    # ── Brief pause to let TPM window recover before gpt-4o agents
    time.sleep(2)

    # ── Batch 2: gpt-4o agents (serialized — 30K TPM limit on gpt-4o)
    active_4o = [n for n in batch_4o if n in task_fns]
    if active_4o:
        msg = f"Batch 2: {len(active_4o)} deep-analysis agents (gpt-4o, serialized)..."
        print(f"\n{DIM}  {msg}{RST}\n")
        logger.info(msg)
        for name in active_4o:
            _run_agent(name)
            time.sleep(3)  # 3s gap between gpt-4o agents to avoid TPM spike

    phase2_elapsed = time.time() - phase2_start
    msg = f"Phase 2 complete in {phase2_elapsed:.1f}s (batched)"
    print(f"\n{DIM}  {msg}{RST}")
    logger.info(msg)

    # Unpack phase 2 results
    apm_result    = phase2_results.get("APMAgent") or {}
    all_anomalies = phase2_results.get("LogAgent") or []
    trace_result  = phase2_results.get("TraceAgent") or {}
    deploy_result = phase2_results.get("DeploymentAgent") or {}
    db_result     = phase2_results.get("DatabaseAgent") or {}
    code_result   = phase2_results.get("CodeAgent") or {}
    change_result = phase2_results.get("ChangeIntelligenceAgent") or {}

    # ── Inter-agent validation ──────────────────────────────────────────────
    # Validate and normalize all outputs before passing downstream.
    # This catches type mismatches, missing fields, and format issues
    # that would silently break DBSCAN, HypothesisRanker, etc.
    validation_warnings = []

    all_anomalies, w = validate_log_anomalies(all_anomalies)
    validation_warnings.extend(w)

    apm_result, w = validate_apm_result(apm_result)
    validation_warnings.extend(w)

    trace_result, w = validate_trace_result(trace_result)
    validation_warnings.extend(w)

    code_result, w = validate_code_result(code_result)
    validation_warnings.extend(w)

    deploy_result, w = validate_deploy_result(deploy_result)
    validation_warnings.extend(w)

    code_issues = code_result.get("code_issues", code_result.get("issues", []))

    if validation_warnings:
        msg = f"Validation ({len(validation_warnings)} fix-ups applied)"
        print(f"\n{DIM}  ── {msg} ──{RST}")
        logger.info(msg)
        for vw in validation_warnings[:5]:
            warn(vw)
        if len(validation_warnings) > 5:
            info(f"  ... and {len(validation_warnings) - 5} more")

    # ── Pipeline sanity check ───────────────────────────────────────────────
    is_healthy, sanity_issues = pipeline_sanity_check(
        all_anomalies, apm_result, trace_result, code_issues,
        deploy_result, db_result, change_result
    )
    if not is_healthy:
        msg = "PIPELINE HEALTH: DEGRADED"
        print(f"\n{RED}{BOLD}  ⚠️  {msg}{RST}")
        logger.error(msg)
        for si in sanity_issues[:5]:
            warn(si)
        msg2 = "Pipeline will continue but results may be incomplete."
        print(f"{DIM}  {msg2}{RST}")
        logger.warning(msg2)
    else:
        active_sources = sum(1 for si in [
            all_anomalies, apm_result.get("anomalies"), trace_result.get("root_failure"),
            code_issues, deploy_result.get("suspicious_deployments"),
            db_result.get("data_anomalies"), change_result.get("config_changes"),
        ] if si)
        msg = f"Pipeline health: OK ({active_sources}/7 signal sources active)"
        print(f"\n{GRN}  ✅ {msg}{RST}")
        logger.info(msg)

    results.update({
        "apm": apm_result, "anomalies": all_anomalies,
        "trace": trace_result, "deployments": deploy_result,
        "database": db_result, "code": code_result,
        "change_intelligence": change_result,
        "validation_warnings": validation_warnings,
        "pipeline_healthy": is_healthy,
    })

    # ── Confidence gate: if deployment + logs already scream a specific cause ─
    early_confidence = 0.0
    if deploy_result.get("verdict") == "deployment_likely_cause" and len(all_anomalies) >= 3:
        early_confidence = 0.75
        info(f"Early confidence gate: {early_confidence*100:.0f}% — deployment correlation + log anomalies aligned")

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2.5 — SIGNAL FUSION (parallel, 3 agents)
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 2.5 — SIGNAL FUSION (parallel)", "🔗")

    def run_dep_graph():
        return with_retry(analyze_dependencies, trace_result, code_issues, all_anomalies, label="DepGraphAgent")

    def run_alert_corr():
        apm_anomalies = apm_result.get("anomalies", [])
        return with_retry(correlate_alerts, all_anomalies, apm_anomalies, label="AlertCorrAgent")

    def run_anomaly_corr():
        return with_retry(correlate_anomalies,
                          log_results=all_anomalies, apm_results=apm_result,
                          trace_results=trace_result, code_results=code_result,
                          deployment_results=deploy_result, db_results=db_result,
                          label="AnomalyCorrAgent")

    fusion_meta = {
        "DependencyGraphAgent": (9,  "DependencyGraphAgent", "Build service dependency graph, run PageRank blame scoring"),
        "AlertCorrelatorAgent": (10, "AlertCorrelatorAgent", "Cluster alerts into incidents (DBSCAN)"),
        "AnomalyCorrelatorAgent":(11,"AnomalyCorrelatorAgent","Cross-correlate all Phase 2 signals"),
    }
    fusion_fns = {
        "DependencyGraphAgent":  run_dep_graph,
        "AlertCorrelatorAgent":  run_alert_corr,
        "AnomalyCorrelatorAgent":run_anomaly_corr,
    }

    for name in fusion_meta:
        track(name)

    print(f"\n{DIM}  Launching 3 fusion agents (gpt-4o-mini, parallel)...{RST}\n")
    phase25_start = time.time()
    phase25_results = {}

    def _run_fusion(name):
        n, display_name, desc = fusion_meta[name]
        s = statuses[name]
        s._t = time.time()
        try:
            result = fusion_fns[name]()
            s.done()
            phase25_results[name] = result
            elapsed = time.time() - s._t

            if name == "DependencyGraphAgent":
                ranking = result.get("blame_ranking", [])
                top_blame = ranking[0].get("service","?") if ranking else "?"
                s.note = f"PageRank top: {top_blame} | {len(ranking)} services scored"
            elif name == "AlertCorrelatorAgent":
                groups = result.get("incident_groups", [])
                noise  = len(result.get("noise_alerts", []))
                s.note = f"{len(groups)} incident cluster(s), {noise} noise"
            elif name == "AnomalyCorrelatorAgent":
                agreement = result.get("signal_agreement", {})
                contradictions = result.get("contradictions", [])
                s.note = f"{len(agreement)} services scored, {len(contradictions)} contradictions"

            print(f"  {GRN}✅{RST} {BOLD}{display_name}{RST} {DIM}({elapsed:.1f}s){RST} — {s.note}")

        except Exception as e:
            s.failed(str(e))
            print(f"  {RED}❌{RST} {BOLD}{display_name}{RST} — FAILED: {str(e)[:100]}")
            phase25_results[name] = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_run_fusion, n): n for n in fusion_fns}
        for f in as_completed(futures):
            f.result()

    dep_graph_result = phase25_results.get("DependencyGraphAgent") or {}
    alert_corr_result = phase25_results.get("AlertCorrelatorAgent") or {}
    anomaly_corr_result = phase25_results.get("AnomalyCorrelatorAgent") or {}
    phase25_elapsed = time.time() - phase25_start
    msg = f"Phase 2.5 complete in {phase25_elapsed:.1f}s (parallel)"
    print(f"\n{DIM}  {msg}{RST}")
    logger.info(msg)

    results.update({
        "dependency_graph": dep_graph_result,
        "alert_correlation": alert_corr_result,
        "anomaly_correlation": anomaly_corr_result,
    })

    # ─────────────────────────────────────────────────────────────────────────
    # ADAPTIVE ROUTING — Signal Strength Analysis
    # ─────────────────────────────────────────────────────────────────────────
    # Evaluate signal quality from Phase 2 + 2.5 to decide routing for Phase 3+.
    # Weak signals → re-run agents with refined queries.
    # Strong signals → fast-track high-confidence path.

    signal_strength = {
        "log_anomalies":   len(all_anomalies),
        "apm_anomalies":   len(apm_result.get("anomalies", [])),
        "trace_failures":  1 if trace_result.get("root_failure") else 0,
        "code_issues":     len(code_issues),
        "deploy_suspect":  1 if deploy_result.get("verdict") == "deployment_likely_cause" else 0,
        "db_anomalies":    len(db_result.get("data_anomalies", [])),
        "change_configs":  len(change_result.get("config_changes", [])),
        "dep_graph_blame": len(dep_graph_result.get("blame_ranking", [])),
        "alert_clusters":  len(alert_corr_result.get("incident_groups", [])),
        "cross_signal_agreement": len(anomaly_corr_result.get("signal_agreement", {})),
    }
    total_signal_score = sum(signal_strength.values())
    strong_signals = sum(1 for v in signal_strength.values() if v > 0)

    print(f"\n{DIM}  ── Adaptive Routing ──{RST}")
    msg = f"Signal strength: {total_signal_score} total ({strong_signals}/10 active channels)"
    print(f"{DIM}  {msg}{RST}")
    logger.info(msg)
    for k, v in signal_strength.items():
        icon = "█" if v > 0 else "░"
        msg = f"{icon} {k}: {v}"
        print(f"{DIM}    {msg}{RST}")
        logger.info(f"  {k}: {v}")

    # Decision: if signals are very weak, try to get more data
    if total_signal_score < 3 and not all_anomalies and not code_issues:
        warn("⚡ Signal strength LOW — Phase 3 may produce weak hypotheses")
        info("  Consider re-running with more log data or broader search scope")

    # Decision: if strong early confidence + trace agrees, fast-track
    if (early_confidence >= 0.75 and trace_result.get("root_failure") and
        dep_graph_result.get("blame_ranking")):
        top_blame = dep_graph_result["blame_ranking"][0].get("service", "") if dep_graph_result["blame_ranking"] else ""
        root_svc = trace_result["root_failure"].get("service", "")
        if top_blame and root_svc and (top_blame in root_svc or root_svc in top_blame):
            early_confidence = min(0.90, early_confidence + 0.10)
            info(f"⚡ Fast-track: trace root + PageRank blame agree → confidence boosted to {early_confidence*100:.0f}%")

    results["signal_strength"] = signal_strength
    results["adaptive_routing"] = {
        "total_score": total_signal_score,
        "active_channels": strong_signals,
        "early_confidence": early_confidence,
    }

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 3 — REASONING
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 3 — REASONING", "🧠")

    # ── KnowledgeBaseAgent ────────────────────────────────────────────────
    s = track("KnowledgeBaseAgent")
    agent_header(12, 18, "KnowledgeBaseAgent", "Semantic search across 6 past incidents + runbooks (TF-IDF + BM25)")
    s.start()
    try:
        kb_result = with_retry(search_knowledge_base, all_anomalies, code_issues, label="KnowledgeBaseAgent")
        matches = kb_result.get("matches", [])
        s.done(f"{len(matches)} past incident matches")
        ok(s.note)
        for m in matches[:3]:
            score = int(m.get("similarity_score", 0) * 100)
            bullet(f"[{score}%] {m.get('incident_id','?')}: {m.get('incident_title','')}")
        proven = kb_result.get("proven_fixes", [])
        if proven:
            info(f"Estimated resolution: {kb_result.get('estimated_total_resolution','?')}")
        if kb_result.get("data_backfill_needed"):
            warn("Data backfill required — orders may have corrupt totals")
    except Exception as e:
        s.failed(str(e)); err(f"KnowledgeBaseAgent failed: {e}")
        kb_result = {}
    timing(s.duration)
    results["knowledge_base"] = kb_result

    # ── HypothesisRankerAgent ─────────────────────────────────────────────
    s = track("HypothesisRankerAgent")
    agent_header(13, 18, "HypothesisRankerAgent", "Fuse all signals, rank root causes by confidence (Bayesian scoring)")
    hypotheses = []
    if not all_anomalies and not code_issues:
        s.skipped("no signals to rank"); warn(s.note)
    else:
        s.start()
        try:
            hypotheses = with_retry(
                rank_hypotheses,
                anomalies=all_anomalies,
                code_issues=code_issues,
                trace_data=trace_result,
                deployment_data=deploy_result,
                kb_matches=kb_result.get("matches", []),
                apm_data=apm_result,
                label="HypothesisRankerAgent"
            )
            s.done(f"{len(hypotheses)} hypotheses ranked")
            ok(s.note)
            for h in hypotheses[:4]:
                conf = int(h.get("confidence", 0) * 100)
                bar_len = conf // 5
                bar = "█" * bar_len + "░" * (20 - bar_len)
                etype = h.get("hypothesis_type", "")
                print(f"  {MAG}  #{h.get('rank','?')}{RST} [{bar}] {conf}%  "
                      f"{BOLD}{h.get('hypothesis','')[:60]}{RST}")
                info(f"     Type: {etype} | Fix: {h.get('fix_category','')} | "
                     f"Est. {h.get('estimated_fix_time','?')}")

            top_conf = hypotheses[0].get("confidence", 0) if hypotheses else 0
            if top_conf >= CONFIDENCE_GATE:
                msg = f"HIGH CONFIDENCE ({int(top_conf*100)}%) — root cause identified with certainty"
                print(f"\n  {GRN}{BOLD}⚡ {msg}{RST}")
                logger.info(msg)
        except Exception as e:
            s.failed(str(e)); err(f"HypothesisRankerAgent failed: {e}")
        timing(s.duration)
    results["hypotheses"] = hypotheses

    # ── ImpactAssessmentAgent ─────────────────────────────────────────────
    s = track("ImpactAssessmentAgent")
    agent_header(14, 18, "ImpactAssessmentAgent", "Quantify revenue/user/SLA impact + escalation plan")
    impact = {}
    if hypotheses:
        s.start()
        try:
            impact = with_retry(assess_impact, hypotheses, apm_result, ticket_ctx, label="ImpactAssessmentAgent")
            sev = impact.get("severity", "?")
            urg = impact.get("urgency", "?")
            s.done(f"{sev} | {urg}")
            sev_color = RED if sev in ("P0", "P1") else YEL
            ok(f"Severity: {sev_color}{sev}{RST} | Urgency: {urg}")
            rev = impact.get("revenue_impact", {})
            info(f"Revenue at risk: {rev.get('per_hour','?')}/hr  |  "
                 f"Since incident: {rev.get('since_incident_start','?')}")
            sla = impact.get("sla", {})
            breached = sla.get("sla_breached", False)
            info(f"SLA: {'🔴 BREACHED' if breached else '🟡 At risk'} | "
                 f"Availability: {sla.get('current_availability','?')}")
            esc = impact.get("escalation", {})
            if esc.get("page_now"):
                warn(f"Page NOW: {', '.join(esc.get('page_now', []))}")
            rollback = impact.get("rollback", {})
            if rollback.get("recommended"):
                warn(f"Rollback recommended to: {rollback.get('version','?')}")
        except Exception as e:
            s.failed(str(e)); err(f"ImpactAssessmentAgent failed: {e}")
        timing(s.duration)
    else:
        s.skipped("no hypotheses"); warn(s.note)
    results["impact"] = impact

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4 — RESOLUTION
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 4 — RESOLUTION", "🔧")

    # ── FixGeneratorAgent ─────────────────────────────────────────────────
    s = track("FixGeneratorAgent")
    agent_header(15, 18, "FixGeneratorAgent", "Generate precise code fixes with risk assessment")
    fixes_generated = []
    java_fixes_needed = []

    # Service → source file map for Python/Node auto-fix
    file_map = {
        "python-inventory-service": os.path.join(ROOT, "python-inventory-service", "app.py"),
        "node-notification-service": os.path.join(ROOT, "node-notification-service", "server.js"),
    }

    if hypotheses:
        s.start()
        java_files_cache = load_java_files()   # load once, reuse for all Java hypotheses

        for h in hypotheses:
            services = h.get("affected_services", [])
            fix_cat  = h.get("fix_category", "")

            # ── Try Python / Node fix first ──────────────────────────────
            target_file = rel_path = None
            for svc in services:
                if svc in file_map:
                    target_file = file_map[svc]
                    rel_path = f"{svc}/{os.path.basename(target_file)}"
                    break

            if target_file:
                try:
                    with open(target_file) as f: source = f.read()
                    fix = with_retry(generate_fix, h, source, rel_path, label=f"Fix#{h.get('rank')}")
                    fix["_hypothesis_rank"] = h.get("rank")
                    fixes_generated.append(fix)
                    risk = fix.get("estimated_risk", "unknown")
                    risk_color = RED if "high" in risk else YEL if "medium" in risk else GRN
                    ok(f"Fix #{h.get('rank')} [{svc}]: {fix.get('fix_description','')[:65]}")
                    info(f"File: {fix.get('file_to_edit','')} | Risk: {risk_color}{risk}{RST}")
                    if apply_fixes:
                        # Apply fix inline: write patched content to file
                        fix_file = fix.get("file_to_edit", "")
                        fix_content = fix.get("patched_content", "")
                        if fix_file and fix_content:
                            try:
                                abs_path = os.path.join(ROOT, fix_file) if not os.path.isabs(fix_file) else fix_file
                                # Path traversal validation
                                abs_path = str(pathlib.Path(abs_path).resolve())
                                if '..' in abs_path:
                                    raise ValueError(f"Path traversal blocked: {abs_path}")
                                with open(abs_path, "w") as fp: fp.write(fix_content)
                                ok("Applied to disk ✓")
                            except Exception as ae:
                                warn(f"Apply failed: {ae}")
                        else:
                            warn("No patched content — manual apply needed")
                except Exception as e:
                    warn(f"Fix #{h.get('rank')} failed: {e}")

            # ── Try Java auto-fix if java-order-service is explicitly involved
            #    and the fix category is code-related (not infrastructure/unknown).
            elif ("java-order-service" in services and
                  fix_cat not in ("unknown", "infrastructure", "capacity", "performance")):
                print(f"\n  {CYA}  Attempting Java auto-fix for hypothesis #{h.get('rank')}...{RST}")
                try:
                    java_result = with_retry(
                        fix_java_hypothesis, h,
                        verify_compile=apply_fixes,   # only compile-verify if apply mode
                        label=f"JavaFix#{h.get('rank')}"
                    )
                    applied_fixes = java_result.get("applied", [])
                    gen_fixes     = java_result.get("generated", {}).get("fixes", [])

                    for af in applied_fixes:
                        status = af.get("status", "?")
                        if status in ("applied_and_verified", "applied"):
                            ok(f"Java fix #{h.get('rank')}: {af.get('fix_description','')[:65]}")
                            info(f"File: {af.get('file','')} | Status: {status}")
                            fixes_generated.append({**af, "_hypothesis_rank": h.get("rank"),
                                                    "file_to_edit": af.get("file",""),
                                                    "fix_description": af.get("fix_description","")})
                        elif status == "rolled_back":
                            warn(f"Java fix #{h.get('rank')} rolled back (compile failed)")
                        elif status == "not_found":
                            warn(f"Java fix #{h.get('rank')}: pattern not found — "
                                 f"bug may not be injected currently")
                        else:
                            warn(f"Java fix #{h.get('rank')}: {status}")
                except Exception as e:
                    java_fixes_needed.append(h)
                    warn(f"Java auto-fix #{h.get('rank')} failed ({e}) — add to manual list")
            else:
                if "java-order-service" in services and fix_cat in ("unknown", "infrastructure", "capacity", "performance"):
                    warn(f"Hypothesis #{h.get('rank')}: skipped Java auto-fix (fix_category='{fix_cat}' — not a code bug)")
                else:
                    java_fixes_needed.append(h)
                    warn(f"Hypothesis #{h.get('rank')}: no auto-fixable file found")

        s.done(f"{len(fixes_generated)} fixes generated (Python/Node + Java auto-fix)")

        if java_fixes_needed:
            print(f"\n  {BOLD}{YEL}Remaining manual Java fixes:{RST}")
            for h in java_fixes_needed:
                print(f"  {MAG}  #{h.get('rank')} {h.get('hypothesis','')[:70]}{RST}")
        timing(s.duration)
    else:
        s.skipped("no hypotheses"); warn(s.note)
    results["fixes"] = fixes_generated

    # ── RegressionTestAgent ───────────────────────────────────────────────
    s = track("RegressionTestAgent")
    agent_header(16, 18, "RegressionTestAgent", "Run Maven test suite, verify fix, interpret results")
    s.start()
    try:
        verification = with_retry(verify_fix, fixes_generated or None, label="RegressionTestAgent")
        tr   = verification.get("test_results", {})
        interp = verification.get("interpretation", {})
        passed = tr.get("passed", 0)
        total_t = tr.get("tests_run", 0)
        failures = tr.get("failures", 0)
        build_ok = tr.get("build_success", False)

        if build_ok:
            s.done(f"BUILD SUCCESS — {passed}/{total_t} passing")
            ok(s.note)
        else:
            s.failed(f"BUILD FAILURE — {failures} failures")
            err(s.note)
            for ft in tr.get("failed_tests", [])[:5]:
                bullet(f"FAILED: {ft}")

        if interp:
            ready = interp.get("ready_for_production", False)
            verdict = interp.get("verdict","?")
            icon = "✅" if ready else "❌"
            info(f"Verdict: {verdict} | Production-ready: {icon}")
            for reg in interp.get("regressions", []):
                warn(f"Regression: {reg}")
    except Exception as e:
        s.failed(str(e)); err(f"RegressionTestAgent failed: {e}")
        verification = {}
    timing(s.duration)
    results["regression"] = verification

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 5 — REPORT
    # ─────────────────────────────────────────────────────────────────────────
    section("PHASE 5 — REPORT + POSTMORTEM", "📄")

    s = track("ReportAgent")
    agent_header(17, 18, "ReportAgent", "Synthesise all signals into structured incident report")
    s.start()
    rca_report = generate_rca_report(results)
    s.done("RCA report generated")
    ok(s.note)

    # ── PostMortemAgent ──────────────────────────────────────────────────
    s = track("PostMortemAgent")
    agent_header(18, 18, "PostMortemAgent", "Generate 5-whys, timeline, action items & lessons learned")
    s.start()
    try:
        postmortem = with_retry(generate_postmortem, results, label="PostMortemAgent")
        whys = postmortem.get("five_whys", [])
        actions = postmortem.get("action_items", [])
        s.done(f"{len(whys)} whys, {len(actions)} action items")
        ok(s.note)
        for w in whys[:3]:
            bullet(f"Why: {w.get('question','')[:60]} → {w.get('answer','')[:50]}")
        if actions:
            info(f"Top action: {actions[0].get('description','')[:70]}")
        lessons = postmortem.get("lessons_learned", [])
        if lessons:
            info(f"{len(lessons)} lessons learned captured")
    except Exception as e:
        s.failed(str(e)); err(f"PostMortemAgent failed: {e}")
        postmortem = {}
    timing(s.duration)
    results["postmortem"] = postmortem

    # Save outputs
    # Default to REPORT_DIR from config instead of cluttering the project root
    if not output_dir:
        output_dir = REPORT_DIR
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path   = os.path.join(output_dir, f"rca_report_{ts}.md")
    json_path = os.path.join(output_dir, f"rca_results_{ts}.json")

    # Path traversal validation before writes
    md_path = str(pathlib.Path(md_path).resolve())
    json_path = str(pathlib.Path(json_path).resolve())
    if '..' in md_path or '..' in json_path:
        raise ValueError(f"Path traversal blocked")

    with open(md_path, "w")  as f: f.write(rca_report)
    with open(json_path, "w") as f: json.dump(results, f, indent=2, default=str)

    # ── Print report preview in terminal ─────────────────────────────────
    print(f"\n{BOLD}{'─'*68}{RST}")
    msg = "📋  RCA REPORT PREVIEW"
    print(f"{BOLD}{WHT}  {msg}{RST}")
    logger.info(msg)
    print(f"{BOLD}{'─'*68}{RST}\n")
    for line in rca_report.split("\n")[:60]:
        # Style markdown headers
        if line.startswith("## "):
            print(f"\n{BOLD}{BLU}{line}{RST}")
        elif line.startswith("### "):
            print(f"{BOLD}{CYA}{line}{RST}")
        elif line.startswith("**") and line.endswith("**"):
            print(f"{BOLD}{line}{RST}")
        elif line.startswith("- ") or line.startswith("* "):
            print(f"  {MAG}▸{RST} {line[2:]}")
        elif line.startswith("> "):
            print(f"  {DIM}{line}{RST}")
        else:
            print(f"  {line}")
    total_lines = len(rca_report.split("\n"))
    if total_lines > 60:
        print(f"\n  {DIM}... ({total_lines - 60} more lines — see full report below){RST}")
    print(f"\n{BOLD}{'─'*68}{RST}")
    info(f"Full report : {md_path}")
    info(f"Raw results : {json_path}")
    timing(s.duration)

    # ─────────────────────────────────────────────────────────────────────────
    # DASHBOARD SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    total_elapsed = time.time() - pipeline_start
    section("PIPELINE COMPLETE — DASHBOARD", "📊")

    # Agent health table
    print(f"\n  {'Agent':<25} {'Status':<10} {'Time':>6}  Notes")
    print(f"  {'─'*25} {'─'*10} {'─'*6}  {'─'*30}")
    status_icons = {"ok":"✅", "error":"❌", "skipped":"⏭️", "pending":"⏳", "running":"🔄"}
    for name, s in statuses.items():
        icon = status_icons.get(s.status, "?")
        dur  = f"{s.duration:.1f}s" if s.duration else "—"
        note = s.note[:40] if s.note else ""
        print(f"  {name:<25} {icon} {s.status:<8} {dur:>6}  {DIM}{note}{RST}")

    # Metrics summary
    tr = results.get("regression",{}).get("test_results",{})
    print(f"\n  {'─'*68}")
    print(f"  Anomalies detected    {len(all_anomalies):>4}    "
          f"Code issues       {len(code_issues):>4}")
    print(f"  KB incident matches   {len(results.get('knowledge_base',{}).get('matches',[])):>4}    "
          f"Hypotheses ranked {len(hypotheses):>4}")
    print(f"  Fixes generated       {len(fixes_generated):>4}    "
          f"Tests passing     {tr.get('passed',0):>4}/{tr.get('tests_run',0):<4}")
    print(f"  Total pipeline time   {total_elapsed:.1f}s "
          f"  (Phase 2: {phase2_elapsed:.1f}s | Phase 2.5: {phase25_elapsed:.1f}s)")

    # Top result
    if hypotheses:
        top = hypotheses[0]
        conf = int(top.get("confidence",0)*100)
        bar  = "█" * (conf//5) + "░" * (20 - conf//5)
        print(f"\n  {BOLD}TOP ROOT CAUSE ({conf}% confidence):{RST}")
        print(f"  [{bar}] {MAG}{BOLD}{top.get('hypothesis','')}{RST}")
        print(f"  Type: {top.get('hypothesis_type','')} | "
              f"Services: {', '.join(top.get('affected_services',[]))}")

    imp = results.get("impact", {})
    if imp:
        print(f"\n  {BOLD}BUSINESS IMPACT:{RST}")
        rev = imp.get("revenue_impact", {})
        print(f"  Severity: {RED}{imp.get('severity','?')}{RST} | "
              f"Revenue/hr: {rev.get('per_hour','?')} | "
              f"Urgency: {imp.get('urgency','?')}")

    print(f"\n  {DIM}Full report saved → {md_path}{RST}")
    hr()
    print()

    return results


# ═══════════════════════════════════════════════════════════════════════════
#  REPORT GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

REPORT_PROMPT = """You are an expert Site Reliability Engineer writing a formal Root Cause Analysis report.

Given the full pipeline results (anomalies, hypotheses, code issues, impact data, test results),
write a professional, precise, structured RCA report in Markdown.

Include sections:
1. **Executive Summary** (3-4 sentences: what broke, why, impact, status)
2. **Incident Timeline** (key events with timestamps)
3. **Root Cause Analysis** (ranked hypotheses with evidence, what caused each, which service boundary)
4. **Business Impact** (affected users, revenue at risk, SLA status)
5. **Technical Deep Dive** (code-level explanation of each bug with before/after)
6. **Fixes Applied / Recommended** (what was auto-fixed vs needs manual fix, with exact code changes)
7. **Verification** (test results, what passed, what still needs attention)
8. **Immediate Action Items** (numbered, prioritised, who owns each)
9. **Prevention Recommendations** (what process/tooling changes prevent recurrence)

Be specific, technical, and precise. Reference actual field names, line numbers if available,
service names, and confidence scores. This report goes to the engineering director.
"""

def generate_rca_report(results: dict) -> str:
    summary_payload = {
        "hypotheses":    results.get("hypotheses", [])[:5],
        "anomalies":     results.get("anomalies", [])[:10],
        "code_issues":   results.get("code", {}).get("issues", [])[:6],
        "impact":        results.get("impact", {}),
        "kb_matches":    results.get("knowledge_base", {}).get("matches", [])[:3],
        "proven_fixes":  results.get("knowledge_base", {}).get("proven_fixes", []),
        "fixes":         results.get("fixes", []),
        "test_results":  results.get("regression", {}).get("test_results", {}),
        "ticket":        results.get("ticket", {}),
        "deployment":    results.get("deployments", {}),
        "trace":         results.get("trace", {})
    }

    response = client.chat.completions.create(
        model=get_model(),
        messages=[
            {"role": "system", "content": REPORT_PROMPT},
            {"role": "user", "content": f"Pipeline results:\n{json.dumps(summary_payload, indent=2, default=str)}"}
        ],
        temperature=0.2,
        max_tokens=3000,
        timeout=60
    )
    return response.choices[0].message.content


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="18-Agent AI-Powered RCA Orchestrator — Aspire Systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 agents/orchestrator.py --demo
  python3 agents/orchestrator.py --demo --apply-fixes
  python3 agents/orchestrator.py --demo --output-dir ./reports
  python3 agents/orchestrator.py --ticket t.txt --logs l.txt --metrics m.txt --trace tr.txt
        """
    )
    parser.add_argument("--demo",        action="store_true", help="Demo 1: Cross-service field mismatch (qty vs quantity)")
    parser.add_argument("--demo2",       action="store_true", help="Demo 2: Cascading timeout / connection pool exhaustion")
    parser.add_argument("--demo3",       action="store_true", help="Demo 3: Silent data corruption from race condition")
    parser.add_argument("--ticket",      type=str, help="Path to Jira ticket text file")
    parser.add_argument("--metrics",     type=str, help="Path to APM metrics file")
    parser.add_argument("--logs",        type=str, help="Path to combined log file (single service)")
    parser.add_argument("--trace",       type=str, help="Path to distributed trace file")
    parser.add_argument("--deployments", type=str, help="Path to deployment history file")
    parser.add_argument("--db",          type=str, help="Path to SQLite DB file or DB context text")
    parser.add_argument("--apply-fixes", action="store_true", help="Apply generated fixes to source files")
    parser.add_argument("--output-dir",  type=str, default=None, help="Directory to save report + JSON results")
    args = parser.parse_args()

    def read(path):
        if path and os.path.exists(path):
            with open(path) as f: return f.read()
        return None

    # Select demo data
    demo_data = None
    if args.demo:
        demo_data = DEMO
        print(f"\n{BOLD}{CYA}  ▶ DEMO 1: Cross-service field name mismatch{RST}\n")
    elif args.demo2:
        demo_data = DEMO2
        print(f"\n{BOLD}{CYA}  ▶ DEMO 2: Cascading timeout / connection pool exhaustion{RST}\n")
    elif args.demo3:
        demo_data = DEMO3
        print(f"\n{BOLD}{CYA}  ▶ DEMO 3: Silent data corruption from race condition{RST}\n")

    if demo_data:
        run_pipeline(
            ticket_text=demo_data["ticket"],
            metrics_text=demo_data["metrics"],
            logs_by_service=demo_data["logs"],
            trace_text=demo_data["trace"],
            deployment_text=demo_data["deployments"],
            db_context=demo_data["db_context"],
            apply_fixes=args.apply_fixes,
            output_dir=args.output_dir
        )
    else:
        run_pipeline(
            ticket_text=read(args.ticket),
            metrics_text=read(args.metrics),
            logs_by_service={"combined": read(args.logs)} if args.logs else None,
            trace_text=read(args.trace),
            deployment_text=read(args.deployments),
            db_context=read(args.db),
            apply_fixes=args.apply_fixes,
            output_dir=args.output_dir
        )
