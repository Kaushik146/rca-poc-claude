# ════════════════════════════════════════════════════════════
#  RCA Pipeline on Claude Code — Makefile
#  Aspire Systems  |  G. Kaushik Raj
# ════════════════════════════════════════════════════════════

MVN     ?= mvn
PYTHON  ?= python3
NODE    ?= node
PIP     ?= pip3

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo ""
	@echo "  RCA Pipeline on Claude Code — Aspire Systems"
	@echo ""
	@echo "  Setup:"
	@echo "    make install        Install Python dependencies"
	@echo "    make build          Compile the Java demo service (used by harness)"
	@echo "    make setup          install + build in one shot"
	@echo ""
	@echo "  Running:"
	@echo "    make shakedown      Dry-run end-to-end against INC-DEMO-001 fixture"
	@echo "    make ci-chassis     Full offline regression suite (used by CI)"
	@echo "    make services       Start demo Python + Node.js services"
	@echo "    make stop           Stop demo services"
	@echo ""
	@echo "  Tests:"
	@echo "    make fixture                Fixture harness only"
	@echo "    make test-scripts           Unit tests under scripts/tests/"
	@echo "    make test-handoff           Handoff round-trip"
	@echo "    make test-sanitize          Prompt-injection sanitizer corpus"
	@echo "    make test-chassis-contracts Selector + proposal-size + prose-lint"
	@echo "    make test-checkpoint        Mid-phase checkpoint/resume"
	@echo "    make smoke-cli              Claude Code CLI flag + topology smoke"
	@echo ""
	@echo "  Dev:"
	@echo "    make lint           Python syntax check"
	@echo "    make clean          Remove build artifacts and temp files"
	@echo ""

.PHONY: install
install:
	@echo "→ Installing Python dependencies..."
	$(PIP) install -r requirements.txt
	@echo "✅ Done. For live mode, see README.md for required env vars."

.PHONY: build
build:
	cd java-order-service && $(MVN) compile -q
	@echo "✅ Java build complete"

.PHONY: setup
setup: install build
	@echo "✅ Setup complete. Run 'make shakedown' for the dry-run pass."

.PHONY: test
test:
	cd java-order-service && $(MVN) test --no-transfer-progress

.PHONY: services
services:
	cd python-inventory-service && $(PYTHON) app.py &
	cd node-notification-service && $(NODE) server.js &
	@echo "✅ Services running. Use 'make stop' to shut down."

.PHONY: stop
stop:
	-lsof -ti:5003 | xargs kill -9 2>/dev/null || true
	-lsof -ti:5004 | xargs kill -9 2>/dev/null || true
	@echo "✅ Services stopped."

.PHONY: fixture
fixture:
	$(PYTHON) scripts/fixture_harness.py INC-DEMO-001

.PHONY: verify-mcp
verify-mcp:
	$(PYTHON) scripts/verify_mcp_config.py

.PHONY: verify-mcp-strict
verify-mcp-strict:
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
	RCA_DRY_RUN=1 RCA_STAGE=propose-only RCA_REQUIRE_FIX_APPROVAL=1 \
	    $(PYTHON) scripts/fixture_harness.py INC-DEMO-001
	@echo "✅ Shakedown passed."

.PHONY: test-handoff
test-handoff:
	$(PYTHON) -m unittest scripts.tests.test_handoff_roundtrip -v 2>&1 | tail -20

.PHONY: test-sanitize
test-sanitize:
	$(PYTHON) -m unittest scripts.tests.test_sanitize -v 2>&1 | tail -20

.PHONY: test-chassis-contracts
test-chassis-contracts:
	$(PYTHON) -m unittest scripts.tests.test_chassis_contracts -v 2>&1 | tail -30

.PHONY: test-checkpoint
test-checkpoint:
	$(PYTHON) -m unittest scripts.tests.test_checkpoint -v 2>&1 | tail -20

.PHONY: test-scripts
test-scripts: test-handoff test-sanitize test-chassis-contracts test-checkpoint

.PHONY: smoke-cli
smoke-cli:
	@if command -v claude >/dev/null 2>&1; then \
		$(PYTHON) scripts/smoke_cli.py; \
	else \
		echo "::warning::claude CLI not installed"; \
	fi

.PHONY: ci-chassis
ci-chassis: verify-mcp fixture test-scripts smoke-cli
	@$(PYTHON) scripts/seed_bug.py
	@set +e; cd python-inventory-service && $(PYTHON) -m pytest tests/test_reserve_boundary.py -q; \
		rc=$$?; cd ..; \
		$(PYTHON) scripts/seed_bug.py --revert; \
		if [ $$rc -eq 0 ]; then echo "✗ ERROR"; exit 1; else echo "✓ seeded bug breaks tests"; fi
	@cd python-inventory-service && $(PYTHON) -m pytest tests/test_reserve_boundary.py -q
	@echo "✅ chassis CI green"

.PHONY: lint
lint:
	$(PYTHON) -m py_compile agents/validation.py agents/algorithms/*.py && echo "✅ algorithms OK"
	$(PYTHON) -m py_compile scripts/*.py && echo "✅ scripts OK"
	$(PYTHON) -m py_compile python-inventory-service/app.py && echo "✅ Flask OK"

.PHONY: clean
clean:
	-cd java-order-service && $(MVN) clean -q 2>/dev/null || true
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	-find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Clean complete"

.PHONY: clean-all
clean-all: clean stop
	-rm -rf reports/
	@echo "✅ Full clean complete"
