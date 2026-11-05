SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose

.PHONY: help up down logs ps migrate seed test test-unit test-integration lint fmt eval build-sandbox clean-db

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

up: ## Start postgres, mailpit, socket proxy, api, worker (runs migrations first)
	$(COMPOSE) up -d --build --wait postgres mailpit docker-socket-proxy api worker

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
	$(UV) run pytest -m "not e2e and not eval"

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

eval: ## Scenario suite (Phase 6; not implemented yet)
	@echo "eval: scenario harness is not implemented until Phase 6 (see docs/PROGRESS.md)"

build-sandbox: ## Build the agentops-sandbox image (never run as a service)
	$(COMPOSE) --profile build-only build sandbox
