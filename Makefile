.PHONY: help up down logs test lint typecheck secrets-check check smoke-live e2e n8n-status eval eval-freeze vm-deploy vm-e2e

COMPOSE := docker compose

help:  ## Show available targets
	@grep -E '^[a-zA-Z0-9 _-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-22s %s\n", $$1, $$2}'

up:  ## Build and start all services, wait until healthy
	$(COMPOSE) up -d --build --wait

down:  ## Stop all services (the database volume is kept)
	$(COMPOSE) down

logs:  ## Follow service logs
	$(COMPOSE) logs -f

test:  ## Run the test suite against the compose PostgreSQL and Mailpit
	$(COMPOSE) up -d --wait postgres mailpit
	uv run pytest

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

typecheck:  ## Static type check
	uv run mypy

secrets-check:  ## Fail if files git tracks contain secret-like strings
	scripts/check_secrets.sh

check: lint typecheck secrets-check test  ## Everything CI runs

# AC-2.7 (UD-4, 2026-09-26): one real LLM call with synthetic data. Needs ANTHROPIC_API_KEY in
# the environment or in .env (git-ignored). Override with e.g. `make smoke-live SMOKE_MODEL=...`.
SMOKE_PROVIDER ?= anthropic
SMOKE_MODEL ?= claude-sonnet-5
SMOKE_PRICE_INPUT ?= 2
SMOKE_PRICE_OUTPUT ?= 10

smoke-live:  ## AC-2.7: one real LLM call, synthetic data (needs ANTHROPIC_API_KEY)
	$(COMPOSE) up -d --wait postgres
	@set -a; if [ -f .env ]; then . ./.env; fi; set +a; \
	LLM_PROVIDER=$(SMOKE_PROVIDER) LLM_MODEL=$(SMOKE_MODEL) \
	LLM_PRICE_INPUT_USD_PER_MTOK=$(SMOKE_PRICE_INPUT) LLM_PRICE_OUTPUT_USD_PER_MTOK=$(SMOKE_PRICE_OUTPUT) \
	uv run pytest -m live -s -q -p no:cacheprovider tests/test_live_anthropic.py

# AC-4.2 and AC-4.3 against the running stack (fake LLM, Mailpit only). Stops and restarts the
# api container on the way, to show the n8n alert.
e2e:  ## Form -> review mail -> approve -> customer mail; then api down -> alert mail
	$(COMPOSE) up -d --build --wait
	uv run python scripts/e2e.py

# AC-4.1 (U7): after `make up`, n8n itself shows W1-W3 imported and active (CLI and HTTP).
n8n-status:  ## Show that n8n has W1-W3 imported, published and registered
	uv run python scripts/n8n_status.py

# AC-5.3. Fake provider by default (checks the harness, measures no model). A real model runs
# only with frozen labels, prices, and EVAL_BUDGET_USD above the worst-case cost of the run.
eval:  ## Qualify eval/dataset.csv and report agreement, latency and cost (fake unless configured)
	uv run python scripts/eval.py

eval-freeze:  ## Fix the person's labels (sha256 in eval/FREEZE.json) before the first real run
	uv run python scripts/eval_freeze.py

# M6 (UD-7): the Linux VM on this Mac, apart from Docker Desktop. Create it once with
#   ~/.local/lima/bin/limactl start --name=p01-vm --tty=false deploy/vm/lima.yaml
LIMACTL ?= $(HOME)/.local/lima/bin/limactl

vm-deploy:  ## Copy the same images and the config into the VM, migrate, start, wait for healthy
	deploy/vm/deploy.sh

vm-e2e:  ## Run the e2e checks inside the VM, against the deployed stack
	$(LIMACTL) shell --workdir / p01-vm -- sudo /opt/sales-ops/e2e.sh
