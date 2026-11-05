Paste everything below the line into Claude Code as your first message.

---

We're building AgentOps, a production-style multi-agent orchestration platform. Before writing any code, read these in order:

1. `CLAUDE.md`: working rules for this repo
2. `docs/DECISIONS.md`: architecture decisions. These override the case study where they conflict.
3. `docs/BRIEF.md`: requirements and success criteria
4. `docs/15-agent-orchestration-system.md`: product/architecture reference

Then do **Phase 1: Architecture and Foundation** only.

## Phase 1 scope

**1. Verify the stack before you build on it.**
Check the current stable versions and APIs for: langgraph, langgraph-checkpoint-postgres (AsyncPostgresSaver setup), langchain-core (fake chat models, `bind_tools`, `with_structured_output`), mcp / FastMCP, langchain-mcp-adapters, pgvector + SQLAlchemy, fastembed, docker-py, sse-starlette. Note anything that differs from the case study or DECISIONS.md as a new ADR or an update to an existing one, especially every item marked `verify`.

**2. Repo layout** (adjust if you have a good reason, and record it):
```
agentops/
  core/        state.py, graph.py, nodes/, scheduler.py, budget.py, escalation.py
  models/      client.py (ModelClient wrapper), fake.py, pricing.py
  tools/       registry.py, native/, mcp_client.py, sandbox.py
  memory/      longterm.py, embeddings.py, scoring.py
  queue/       jobs.py, worker.py, locks.py
  api/         main.py, routes/, auth.py, sse.py
  observability/ events.py, tracing.py, cost.py, replay.py
  db/          models.py, session.py
  settings.py
migrations/    alembic
mcp_servers/   notes server (stdio + HTTP)
sandbox/       Dockerfile for agentops-sandbox
ui/            (Phase 5; empty scaffold is fine)
evals/         (Phase 6)
tests/         unit/, integration/, e2e/
```

**3. Tooling.** `uv` with a committed lockfile, Python 3.12, ruff + ruff format, mypy (strict on `core/`, `tools/`, `queue/`), pytest + pytest-asyncio with markers `unit`, `integration`, `e2e`, `eval`. Pre-commit with detect-secrets. Makefile with the targets listed in CLAUDE.md.

**4. Config.** `settings.py` (pydantic-settings, `SecretStr`), `.env.example` with no real secrets. Include `LLM_MODE=fake|real`, `SEARCH_MODE=fixture|live`, per-role model config, budget defaults, `OTEL_ENABLED`, DB URL, Mailpit SMTP settings.

**5. Docker compose.** `postgres` (pgvector image), `mailpit`, `api`, `worker`, `docker-socket-proxy`. Add `jaeger` + otel collector under an `observability` profile. Build the `sandbox` image but don't run it as a service. All of it must work on Docker Desktop + WSL2.

**6. Database schema + Alembic migrations.** `users` (hashed API token, role), `tasks` (status uses the terminal states in DECISIONS.md), `jobs` (lease, heartbeat, attempts), `events`, `llm_calls`, `tool_calls`, `approvals` (version, expiry), `side_effects`, `memories` (pgvector column, `user_id`, kind, outcome, source_task_id, superseded flag). Also run the LangGraph checkpointer's `setup()` at startup or in a migration step. Seed one `user` and one `reviewer`.

**7. Foundations with tests (no agents yet):**
- Job queue: enqueue, claim with `FOR UPDATE SKIP LOCKED`, lease/heartbeat, expired-lease reclaim, advisory lock per `thread_id`. Integration tests, including two workers racing for the same thread.
- `ModelClient` wrapper + deterministic fake model: budget check before each call, records `llm_calls` with tokens/cost/latency, versioned price table (no hardcoded real prices; mark entries "to verify").
- Events writer + `NOTIFY`, and an SSE endpoint that replays from `Last-Event-ID`.
- `GET /health` (DB + checkpointer reachable), auth dependency (bearer token → user + role), ownership filter helper.
- A trivial 2-node LangGraph graph compiled with `AsyncPostgresSaver`, run by the worker. A test proves it continues from its checkpoint when its job is reclaimed by a second worker process. This de-risks durability before the real graph exists.

**8. CI.** GitHub Actions: lint → mypy → unit → integration (Postgres service container).

## Acceptance checks for Phase 1
- `make up && make migrate && make test` passes from a clean clone in WSL2.
- `make lint` is clean.
- The cross-process resume test passes.
- `docs/PROGRESS.md` and `docs/DECISIONS.md` are updated, with any API findings from step 1.

## When Phase 1 is done
Stop. Don't start Phase 2. Give me a summary covering:
- what you built
- test results (counts and pass/fail)
- every deviation from DECISIONS.md and why
- API surprises you found while verifying
- open risks for Phase 2
