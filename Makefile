SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose

.PHONY: eval-estimate eval-tables prod-config prod-up reset-demo test-ui e2e help up down logs ps migrate seed test test-unit test-integration test-sandbox lint fmt eval build-sandbox sandbox-selfcheck workspaces clean-db

# Task workspaces live on the host so sandbox containers can bind-mount them (ADR-031).
export AGENTOPS_WORKSPACES_HOST_DIR ?= $(CURDIR)/var/workspaces

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

workspaces: ## Create the host workspace dir (worker uid 10000 and sandbox uid 10001 both write)
	mkdir -p $(AGENTOPS_WORKSPACES_HOST_DIR)
	chmod 777 $(AGENTOPS_WORKSPACES_HOST_DIR)

up: workspaces ## Start postgres, mailpit, socket proxy, mcp-notes, api, worker, ui (:5173) (runs migrations first)
	$(COMPOSE) up -d --build --wait postgres mailpit docker-socket-proxy mcp-notes api worker ui

down: ## Stop the stack (keeps data)
	$(COMPOSE) down

logs: ## Tail api + worker logs
	$(COMPOSE) logs -f api worker

ps:
	$(COMPOSE) ps

migrate: ## alembic upgrade head + LangGraph checkpointer setup + seed users (host)
	$(UV) run alembic upgrade head
	$(UV) run python -m agentops.db.checkpointer
	$(UV) run python -m agentops.db.seed

seed: ## Seed demo user + reviewer
	$(UV) run python -m agentops.db.seed

test: ## Unit + integration tests (fake model; needs `make up` for postgres)
	$(UV) run pytest -m "not e2e and not eval and not model"

test-unit: ## Fast tests, no services
	$(UV) run pytest -m unit

test-integration:
	$(UV) run pytest -m integration

lint: ## ruff + ruff format --check + mypy
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fmt:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

eval: ## Free eval: replay recorded cassettes + dry run (scripted models), $0. Paid run: docs/EVALUATION.md
	$(UV) run python -m evals replay
	$(UV) run python -m evals run --mode dry --k 1

eval-estimate: ## Cost estimate for the paid eval (free; count_tokens)
	$(UV) run python -m evals estimate --k 3

eval-tables: ## Regenerate README / EVALUATION.md tables from committed results
	$(UV) run python -m evals tables

build-sandbox: ## Build the agentops-sandbox image (never run as a service)
	$(COMPOSE) --profile build-only build sandbox

test-sandbox: ## Sandbox isolation tests against the local Docker daemon (needs build-sandbox)
	AGENTOPS_REQUIRE_SANDBOX=1 $(UV) run pytest -m sandbox

sandbox-selfcheck: ## Through-proxy check: worker container -> socket proxy -> sandbox, file round trip
	$(COMPOSE) exec worker python -m agentops.tools.sandbox --selfcheck

reset-demo: ## Dev only: delete local tasks, memories, workspaces, Mailpit (asks first; keeps users)
	$(UV) run python -m agentops.db.reset_demo

test-ui: ## UI: lint + typecheck + Vitest
	cd ui && npm run lint && npm run typecheck && npm test

e2e: ## Playwright walkthrough against the running stack (make up && make migrate first)
	cd ui && npx playwright test

PROD = docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml

prod-config: ## show the resolved prod profile (needs .env.prod; see docs/DEPLOYMENT.md)
	$(PROD) config

prod-up: ## start the prod profile (ENV=prod, no Mailpit, no DB host port; needs .env.prod)
	$(PROD) up -d --build
