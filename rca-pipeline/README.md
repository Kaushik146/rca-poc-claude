# AI-Powered Root Cause Analysis — Cross-Platform POC

Built for Aspire Systems by G. Kaushik Raj.

---

## What This Is

A working proof-of-concept for an automated Root Cause Analysis system that:
- Spans **Java + Python + Node.js + SQLite** simultaneously
- Detects bugs that **cross service boundaries** (field name mismatches, type errors, boundary conditions)
- Automatically **injects, tests, fixes, and verifies** 6 cross-platform bugs

---

## Architecture

```
13-Agent Pipeline:

JiraAgent → APMAgent → LogAgent → TraceAgent → CodeAgent →
DeploymentAgent → DatabaseAgent → KnowledgeBaseAgent →
HypothesisRankerAgent → ImpactAssessmentAgent →
FixGeneratorAgent → RegressionTestAgent
                    ↑ all orchestrated by Orchestrator
```

---

## Services

| Service | Language | Port | Purpose |
|---------|----------|------|---------|
| java-order-service | Java 11 / Maven | — | Checkout orchestrator + all business logic |
| python-inventory-service | Python / Flask | 5003 | Stock reservation |
| node-notification-service | Node.js | 5004 | Order notifications |

---

## API Keys & Environment Setup

### How the POC was actually tested

**No API keys were needed to run or validate this POC.** Here's why:

The 13-agent pipeline is the **production architecture design**. For the POC, each agent's job (detect anomaly → form hypothesis → generate fix → verify) is validated through a real Maven test suite instead of live LLM calls. The test suite acts as the ground truth — if a bug is present, tests fail; once fixed, tests pass. This lets us prove the full detection-to-fix-to-verification loop works end-to-end without needing API credits or a live model.

In short: **the tests are the agents for the purpose of this demo.**

---

### For the full production agent pipeline

The pipeline auto-detects your LLM provider based on which API key is set.
Set **one** (or both) in your `.env` file:

**Option A — Claude (recommended)**
```
ANTHROPIC_API_KEY=sk-ant-...
```
Get yours at: https://console.anthropic.com/keys

**Option B — OpenAI / GPT-4o**
```
OPENAI_API_KEY=sk-...
```
Get yours at: https://platform.openai.com/api-keys

**Optional overrides:**
```
LLM_PROVIDER=anthropic   # force a provider when both keys are set
LLM_MODEL=gpt-4o-mini    # override the default model
```

See `agents/llm_client.py` for the provider abstraction.

> ⚠️ Add `.env` to your `.gitignore` — never commit API keys to GitHub.

---

## Prerequisites

Make sure you have all four installed before you start:

| Tool | Min Version | Check |
|------|-------------|-------|
| Java | 11+ | `java -version` |
| Maven | 3.6+ | `mvn -version` |
| Python | 3.8+ | `python3 --version` |
| Node.js | 14+ | `node --version` |

---

## Setup — First Time Only

### 1. Install Python dependencies

```bash
cd python-inventory-service
pip3 install flask
cd ..
```

### 2. Install Node dependencies (none required — uses built-in `http` module)

No extra packages needed for the Node service.

### 3. Build the Java service

```bash
cd java-order-service
mvn compile -q
cd ..
```

---

## Running the Full Cross-Platform Bug Demo

This is the main show. One command does everything:

```bash
cd bigproject
python3 run_crossplatform_bugs.py
```

**What it does, step by step:**

1. Starts the Python inventory service on port 5003
2. Starts the Node.js notification service on port 5004
3. Injects all 6 cross-platform bugs one by one
4. Runs the Java test suite after each injection — you'll see failures
5. Applies the fix for each bug
6. Runs tests again — you'll see green
7. Prints a final summary: **88/88 tests passing**

**Expected output (end of run):**
```
Bug 1 DETECTED  ✓  (qty → quantity field mismatch)
Bug 2 DETECTED  ✓  (int cast truncates $99.99 → $99)
Bug 3 DETECTED  ✓  (order_id → orderId field mismatch)
Bug 4 DETECTED  ✓  (off-by-one in stock check)
Bug 5 DETECTED  ✓  (PERCENT vs PERCENTAGE string mismatch)
Bug 6 DETECTED  ✓  (EUR rate 0.108 → 1.08)

All fixes applied. Running final test suite...
Tests run: 88, Failures: 0, Errors: 0
BUILD SUCCESS
```

---

## Running Just the Java Tests

If you only want to run the test suite without the bug injection demo:

```bash
cd java-order-service
mvn test
```

Clean run with no bugs injected → **88 tests, all green.**

---

## Running Services Individually

You can also spin up each service on its own for manual testing.

**Python inventory service:**
```bash
cd python-inventory-service
python3 app.py
# Listening on http://localhost:5003
```

**Node.js notification service:**
```bash
cd node-notification-service
node server.js
# Listening on http://localhost:5004
```

**Java order service (tests only — no standalone server):**
```bash
cd java-order-service
mvn test
```

---

## Running the Real AI Agent Pipeline

The `agents/` folder contains the actual LLM-powered pipeline wired to GPT-4o.

**Setup:**
```bash
pip3 install openai python-dotenv
# Make sure .env has OPENAI_API_KEY=sk-...
```

**Run with built-in demo logs:**
```bash
python3 agents/orchestrator.py --demo
```

**Run against your own logs:**
```bash
python3 agents/orchestrator.py --logs "your log text" --service java-order-service
python3 agents/orchestrator.py --log-file /path/to/service.log --service python-inventory-service
```

**What it does:**
1. **LogAgent** — reads raw logs, extracts structured anomalies using GPT-4o
2. **HypothesisRankerAgent** — reasons across all anomalies, ranks root causes by confidence
3. **FixGeneratorAgent** — generates the exact code fix for each hypothesis

---

## The 6 Cross-Platform Bugs

These bugs were deliberately designed to be the kind that slip through unit tests but blow up in integration — each one crosses a service boundary or exploits a language quirk.

| # | Bug | Root Cause | Platform Boundary |
|---|-----|------------|-------------------|
| 1 | `qty` sent, Python expects `quantity` | Field name mismatch in JSON contract | Java → Python |
| 2 | `(int)` cast truncates `$99.99` → `$99` | Lossy integer cast before DB write | Java → SQLite |
| 3 | `order_id` sent, Node.js expects `orderId` | camelCase vs snake_case mismatch | Java → Node.js |
| 4 | `stock > qty` (last unit never sellable) | Off-by-one boundary condition | Python internal |
| 5 | `"PERCENT"` checked, API returns `"PERCENTAGE"` | String constant out of sync | Java internal |
| 6 | EUR rate `0.108` instead of `1.08` | Decimal typo — 10× underpriced | Java internal |

---

## Project Structure

```
bigproject/
├── README.md
├── run_crossplatform_bugs.py       # Main demo runner — start here
├── java-order-service/
│   ├── pom.xml
│   └── src/
│       ├── main/java/              # Business logic, HTTP clients
│       └── test/java/              # 88 JUnit tests
├── python-inventory-service/
│   └── app.py                      # Flask app, stock reservation endpoint
└── node-notification-service/
    └── server.js                   # HTTP server, order notification endpoint
```

---

## Troubleshooting

**Port already in use (5003 or 5004)**
```bash
# Kill whatever is using the port
lsof -ti:5003 | xargs kill -9
lsof -ti:5004 | xargs kill -9
```

**Maven build fails — Java version mismatch**
```bash
# Confirm you're on Java 11+
java -version
# If using multiple Java versions (macOS):
export JAVA_HOME=$(/usr/libexec/java_home -v 11)
```

**Flask not found**
```bash
pip3 install flask
# or if using a venv:
python3 -m pip install flask
```

**`python3` not found on Windows**
Use `python` instead of `python3` throughout.

---

*G. Kaushik Raj — Aspire Systems Internship POC, 2024*
