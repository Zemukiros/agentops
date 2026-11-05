# AgentOps

Multi-agent orchestration platform: a supervisor plans, specialist agents (research, analysis, writing, code) do the work, a reviewer checks it, and a human approves anything risky. Built on LangGraph with a Postgres checkpointer, a Postgres job queue, pgvector memory, and an event log that drives live progress over SSE.

**Status:** Phase 1 (foundation) of 6. No agents yet. See [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Quick start (WSL2 / Linux, Docker)
```bash
cp .env.example .env
uv sync
make up          # postgres, mailpit, docker-socket-proxy, api, worker
make migrate     # alembic upgrade head + LangGraph checkpointer setup + seed users
make test        # unit + integration (fake model, no API keys, no network)
```

Docs: [`docs/BRIEF.md`](docs/BRIEF.md) (requirements) · [`docs/DECISIONS.md`](docs/DECISIONS.md) (architecture decisions) · [`docs/PROGRESS.md`](docs/PROGRESS.md) (status).
