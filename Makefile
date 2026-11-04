# Developer entry points. Every target except eval-live and demo runs offline: no API key, no
# LLM calls (ANTHROPIC_BASE_URL points at a dead port, as in CI).
#   make help

UV      ?= uv
RUNS    ?= runs/make
E2E_OUT ?= $(or $(TMPDIR),/tmp)/ui-e2e
# Extra arguments for eval-live, e.g. make eval-live EVAL_ARGS="-d L1 --limit 5 --routing"
EVAL_ARGS ?=
OFFLINE := ANTHROPIC_API_KEY= ANTHROPIC_BASE_URL=http://127.0.0.1:9
EVAL    := $(UV) run python -m analyst_agent.eval

.DEFAULT_GOAL := help
.PHONY: help install lint format test coverage fuzz check eval eval-live e2e demo clean

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## uv sync with the extras CI uses (no torch)
	$(UV) sync --locked --all-extras --no-extra embeddings

lint: ## ruff lint + format check (what CI runs)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## apply ruff format and autofixable lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test: ## full pytest suite, LLM mocked
	$(OFFLINE) $(UV) run pytest -q

coverage: ## tests with branch coverage + CI's coverage gate (slow tests deselected)
	$(OFFLINE) $(UV) run pytest -q --cov=analyst_agent --cov-report=term-missing:skip-covered
	$(UV) run coverage report --fail-under=92 > /dev/null
	$(UV) run coverage report --include='*/analyst_agent/sandbox/*' --fail-under=97
	$(UV) run coverage report --include='*/analyst_agent/core/loop.py' --fail-under=98

fuzz: ## sandbox escape fuzzer, default seed (slow, ~3 min; nightly in CI)
	$(OFFLINE) $(UV) run pytest -m slow -q -p no:cacheprovider tests/sandbox/test_fuzz_escape.py

check: lint coverage eval ## everything the CI test + eval-smoke jobs run

eval: ## offline eval gate, same three steps as CI's eval-smoke job
	$(OFFLINE) $(EVAL) run --agent oracle --no-judge --limit 5 \
	  --fail-under 1.0 --fail-on-error --runs-dir $(RUNS)/oracle
	$(OFFLINE) $(EVAL) run --agent dry-run --no-score --limit 3 \
	  --timeout 120 --fail-on-error --runs-dir $(RUNS)/dry-run
	$(OFFLINE) AGENT_ROUTING_ENABLED=true AGENT_SEMANTIC_LAYER_ENABLED=true \
	  AGENT_STRUCTURED_REPORT_ENABLED=true AGENT_MEMORY_ENABLED=true \
	  AGENT_MEMORY_DB_PATH=$(RUNS)/memory.sqlite3 AGENT_APPROVAL_ENABLED=true \
	  AGENT_APPROVAL_ROW_THRESHOLD=0 AGENT_TRACE_STORE_ENABLED=true AGENT_LOG_JSON_ENABLED=true \
	  $(EVAL) run --agent dry-run --no-score --limit 5 \
	  --timeout 120 --fail-on-error --runs-dir $(RUNS)/dry-run-flags

eval-live: ## real benchmark run (needs ANTHROPIC_API_KEY; costs money); EVAL_ARGS=...
	@test -n "$$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY is not set" >&2; exit 1; }
	$(EVAL) run --runs-dir $(RUNS)/live $(EVAL_ARGS)

e2e: ## headless-browser UI e2e (needs node + playwright resolvable)
	scripts/ui-e2e/run.sh $(E2E_OUT)

demo: ## docker compose stack with bundled samples; prints the URL
	scripts/demo.sh

clean: ## remove make's eval runs and tool caches
	rm -rf $(RUNS) .pytest_cache .ruff_cache
