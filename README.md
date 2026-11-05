# AgentOps

Multi-agent orchestration platform: a supervisor plans, specialist agents (research, analysis, writing, code) do the work, a reviewer checks it, and a human approves anything risky. Built on LangGraph with a Postgres checkpointer, a Postgres job queue, pgvector memory, and an event log that drives live progress over SSE.

**Status:** Phase 2 of 6. One working vertical slice: supervisor plan → writing specialist ⇄ reviewer (revise loop, capped) → synthesis, with escalation to a human, a per-task budget ledger, and live SSE events. Other specialists, tools, MCP and the sandbox are Phase 3. See [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Quick start (WSL2 / Linux, Docker)
```bash
cp .env.example .env
uv sync
make up          # postgres, mailpit, docker-socket-proxy, api, worker
make migrate     # alembic upgrade head + LangGraph checkpointer setup + seed users
make test        # unit + integration (fake model, no API keys, no network)
```

Run a task (fake mode, scripted models, $0):
```bash
TOKEN=$(awk -F= '/^demo-user=/{print $2}' .agentops-tokens)
curl -s -X POST localhost:8000/tasks -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"request":"Explain pgvector.","scenario":"revise_once"}'
curl -N localhost:8000/tasks/<id>/events -H "Authorization: Bearer $TOKEN"   # live progress
```
Scenarios: `default`, `revise_once`, `revisions_exhausted`, `reviewer_escalates`, `chain`, `bad_plan_then_fixed`, `bad_plan_twice`, `specialist_error` (`agentops/models/scenarios.py`). Real models: set `LLM_MODE=real` and the role models in `.env` (see `.env.example`).

Docs: [`docs/BRIEF.md`](docs/BRIEF.md) (requirements) · [`docs/DECISIONS.md`](docs/DECISIONS.md) (architecture decisions) · [`docs/PROGRESS.md`](docs/PROGRESS.md) (status).
