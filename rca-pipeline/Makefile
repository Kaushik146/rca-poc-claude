# ════════════════════════════════════════════════════════════
#  AI-Powered RCA Pipeline — Makefile
#  Aspire Systems  |  G. Kaushik Raj
# ════════════════════════════════════════════════════════════

MVN     ?= mvn
PYTHON  ?= python3
NODE    ?= node
PIP     ?= pip3

.DEFAULT_GOAL := help

# ── Help ──────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "  AI-Powered RCA Pipeline — Aspire Systems"
	@echo ""
	@echo "  Setup:"
	@echo "    make install        Install all Python dependencies"
	@echo "    make build          Compile the Java order service"
	@echo "    make setup          install + build in one shot"
	@echo ""
	@echo "  Running:"
	@echo "    make demo           Run the full 13-agent AI pipeline (demo mode)"
	@echo "    make demo-fix       Run demo AND auto-apply generated fixes"
	@echo "    make bugs           Run the cross-platform bug injection demo"
	@echo "    make test           Run the Java Maven test suite"
	@echo "    make services       Start Python + Node.js services in background"
	@echo "    make stop           Stop background services"
	@echo ""
	@echo "  Dev:"
	@echo "    make agent-test     Smoke test each agent individually"
	@echo "    make lint           Check Python files for syntax errors"
	@echo "    make clean          Remove build artifacts and temp files"
	@echo "    make report         Open the latest RCA report"
	@echo ""
	@echo "  Prerequisites: Java 11+, Maven 3.6+, Python 3.8+, Node.js 14+"
	@echo "  API key:       OPENAI_API_KEY in .env file"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────
.PHONY: install
install:
	@echo "→ Installing Python dependencies..."
	$(PIP) install -r requirements.txt
	@echo "✅ Done. Now add your OPENAI_API_KEY to .env"

.PHONY: build
build:
	@echo "→ Compiling Java order service..."
	cd java-order-service && $(MVN) compile -q
	@echo "✅ Java build complete"

.PHONY: setup
setup: install build
	@echo ""
	@echo "✅ Setup complete. Run 'make demo' to start the RCA pipeline."

# ── Running ───────────────────────────────────────────────────
.PHONY: demo
demo:
	@echo "→ Running 13-agent RCA pipeline (demo mode)..."
	$(PYTHON) agents/orchestrator.py --demo

.PHONY: demo-fix
demo-fix:
	@echo "→ Running pipeline with auto-fix enabled..."
	$(PYTHON) agents/orchestrator.py --demo --apply-fixes

.PHONY: demo-report
demo-report:
	@echo "→ Running pipeline and saving report to ./reports/..."
	mkdir -p reports
	$(PYTHON) agents/orchestrator.py --demo --output-dir ./reports

.PHONY: bugs
bugs:
	@echo "→ Running cross-platform bug injection demo..."
	$(PYTHON) run_crossplatform_bugs.py

.PHONY: test
test:
	@echo "→ Running Java test suite..."
	cd java-order-service && $(MVN) test --no-transfer-progress

.PHONY: services
services:
	@echo "→ Starting Python inventory service (port 5003)..."
	cd python-inventory-service && $(PYTHON) app.py &
	@echo "→ Starting Node.js notification service (port 5004)..."
	cd node-notification-service && $(NODE) server.js &
	@echo "✅ Services running. Use 'make stop' to shut down."

.PHONY: stop
stop:
	@echo "→ Stopping background services..."
	-lsof -ti:5003 | xargs kill -9 2>/dev/null || true
	-lsof -ti:5004 | xargs kill -9 2>/dev/null || true
	@echo "✅ Services stopped."

# ── Claude Code RCA chassis ──────────────────────────────────
.PHONY: fixture
fixture:
	@echo "→ Running offline fixture harness (INC-DEMO-001)..."
	$(PYTHON) scripts/fixture_harness.py INC-DEMO-001

.PHONY: verify-mcp
verify-mcp:
	@echo "→ Checking .mcp.json structure..."
	$(PYTHON) scripts/verify_mcp_config.py

.PHONY: verify-mcp-strict
verify-mcp-strict:
	@echo "→ Checking .mcp.json incl. env var presence..."
	$(PYTHON) scripts/verify_mcp_config.py --require-env

.PHONY: seed-bug
seed-bug:
	$(PYTHON) scripts/seed_bug.py

.PHONY: unseed-bug
unseed-bug:
	$(PYTHON) scripts/seed_bug.py --revert

.PHONY: test-inventory
test-inventory:
	cd python-inventory-service && $(PYTHON) -m pytest tests/ -q

.PHONY: shakedown
shakedown:
	@echo "→ Dry-run shakedown against INC-DEMO-001 fixture..."
	@echo "  (RCA_DRY_RUN=1 substitutes fixtures for MCP; no PR will open)"
	RCA_DRY_RUN=1 RCA_STAGE=propose-only RCA_REQUIRE_FIX_APPROVAL=1 \
	    $(PYTHON) scripts/fixture_harness.py INC-DEMO-001
	@echo "✅ Shakedown passed. Chassis topology exercised end-to-end offline."
	@echo "   Before pilot: platform-eng must run /rca manually on ONE real"
	@echo "   incident in propose-only mode (see RUNBOOK.md \"Shakedown\")."

.PHONY: test-handoff
test-handoff:
	@echo "→ Handoff round-trip (propose-only → hydrate → SHA-256)..."
	$(PYTHON) -m unittest scripts.tests.test_handoff_roundtrip -v 2>&1 | tail -20

.PHONY: test-sanitize
test-sanitize:
	@echo "→ Sanitizer adversarial corpus..."
	$(PYTHON) -m unittest scripts.tests.test_sanitize -v 2>&1 | tail -20

.PHONY: test-chassis-contracts
test-chassis-contracts:
	@echo "→ Chassis contract tests (selector + proposal size + prose lint)..."
	$(PYTHON) -m unittest scripts.tests.test_chassis_contracts -v 2>&1 | tail -30

.PHONY: test-scripts
test-scripts: test-handoff test-sanitize test-chassis-contracts

.PHONY: smoke-cli
smoke-cli:
	@echo "→ Claude Code CLI smoke (flag + chassis topology)..."
	@# Only runs if the `claude` CLI is installed. On a bare CI runner
	@# without Node.js / claude-code installed, this degrades to a
	@# warning rather than a hard fail — the flag check is still the
	@# most valuable piece when it runs, and we don't want the chassis
	@# gate to depend on a CLI install step upstream of every PR.
	@if command -v claude >/dev/null 2>&1; then \
		$(PYTHON) scripts/smoke_cli.py; \
	else \
		echo "::warning::claude CLI not installed — skipping smoke_cli.py"; \
		echo "         (install with: npm install -g @anthropic-ai/claude-code)"; \
	fi

.PHONY: ci-chassis
ci-chassis: verify-mcp fixture test-scripts smoke-cli
	@echo "→ Seeded-bug round-trip..."
	@$(PYTHON) scripts/seed_bug.py
	@set +e; cd python-inventory-service && $(PYTHON) -m pytest tests/test_reserve_boundary.py -q; \
		rc=$$?; cd ..; \
		$(PYTHON) scripts/seed_bug.py --revert; \
		if [ $$rc -eq 0 ]; then \
			echo "✗ ERROR: seeded bug did not break tests"; exit 1; \
		else \
			echo "✓ seeded bug breaks tests (rc=$$rc)"; \
		fi
	@cd python-inventory-service && $(PYTHON) -m pytest tests/test_reserve_boundary.py -q
	@echo "✅ chassis CI green"

# ── Dev ───────────────────────────────────────────────────────
.PHONY: agent-test
agent-test:
	@echo "→ Smoke testing JiraAgent..."
	$(PYTHON) agents/jira_agent.py
	@echo "→ Smoke testing APMAgent..."
	$(PYTHON) agents/apm_agent.py
	@echo "→ Smoke testing LogAgent..."
	$(PYTHON) agents/log_agent.py
	@echo "→ Smoke testing KnowledgeBaseAgent..."
	$(PYTHON) agents/knowledge_base_agent.py
	@echo "✅ Agent smoke tests complete"

.PHONY: lint
lint:
	@echo "→ Checking Python syntax..."
	$(PYTHON) -m py_compile agents/*.py && echo "✅ All agents syntax OK"
	$(PYTHON) -m py_compile run_crossplatform_bugs.py && echo "✅ Bug runner syntax OK"
	$(PYTHON) -m py_compile python-inventory-service/app.py && echo "✅ Flask app syntax OK"

.PHONY: report
report:
	@LATEST=$$(ls -t rca_report_*.md 2>/dev/null | head -1); \
	if [ -z "$$LATEST" ]; then \
		echo "No RCA reports found. Run 'make demo' first."; \
	else \
		echo "Opening $$LATEST"; \
		cat "$$LATEST"; \
	fi

.PHONY: clean
clean:
	@echo "→ Cleaning build artifacts..."
	-cd java-order-service && $(MVN) clean -q 2>/dev/null || true
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	-find . -name "*.pyc" -delete 2>/dev/null || true
	-rm -f rca_report_*.md rca_results_*.json 2>/dev/null || true
	@echo "✅ Clean complete"

.PHONY: clean-all
clean-all: clean stop
	@echo "→ Removing all generated reports..."
	-rm -rf reports/
	@echo "✅ Full clean complete"
