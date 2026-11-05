# Progress

_Update at the end of every work session and every phase. Keep it short. This is how work resumes without lost context._

## Current status
- **Phase:** 1 — Architecture and Foundation: **complete, awaiting review.** Phase 2 not started.
- **Last updated:** 2026-11-05
- **Test status:** 50 passed, 0 failed (24 unit, 26 integration), fake mode, 2026-11-05. `make lint` clean (ruff, ruff format, mypy strict on core/tools/queue).

## Phase plan
| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: repo, config, DB schema + migrations, checkpointer, job queue skeleton, fake model, health API, compose, CI | ☑ (awaiting review) |
| 2 | Vertical slice: request → plan → one specialist → review → output, fake + real model | ☐ |
| 3 | All specialists, scheduler waves + parallelism, review/retry, tool registry, native tools, MCP (stdio + HTTP), sandbox | ☐ |
| 4 | Durability: crash/restart recovery, plan/action/escalation gates, idempotent execution, memory (pgvector) | ☐ |
| 5 | UI + observability: SSE, plan DAG, approvals inbox, trace/cost, memory browser, replay | ☐ |
| 6 | Hardening + evals: injection suite, scenario harness, measured metrics, docs, deploy config | ☐ |

## Done (Phase 1)
- Stack verified against installed versions. Findings are in DECISIONS.md (ADR-003/008/009/010/011/015 verify notes, ADR-016…023).
- uv project (Python 3.12, exact pins, `uv.lock`), ruff, mypy, pytest markers, pre-commit (ruff, detect-secrets with a clean `.secrets.baseline`), Makefile.
- `agentops/settings.py`: pydantic-settings, `SecretStr`, LLM/search modes, per-role models, budgets, OTel, DB, SMTP, queue timing, embedding dim guard.
- Compose: postgres (pgvector, pg17), mailpit, one-shot migrate, api, worker, docker-socket-proxy (internal network, containers-only), jaeger (profile `observability`), sandbox image (profile `build-only`).
- Schema + Alembic `0001`: users, tasks, jobs, events, llm_calls, tool_calls, approvals, side_effects, memories (vector(384) + HNSW cosine). Up, down and up again verified; `alembic check` is clean. Checkpointer `setup()` runs via `agentops.db.checkpointer`.
- Postgres job queue: SKIP LOCKED claim, lease, owner-fenced heartbeat/complete/fail, backoff retry, release, expired-lease reclaim, exhausted-job reaper, per-thread advisory lock.
- `ModelClient` + `ScriptedChatModel`: budget check before every attempt, an `llm_calls` row per attempt (retries and structured re-asks), versioned price table with unverified prices left NULL.
- Events writer (insert + NOTIFY in one transaction). Per-process LISTEN broadcaster. SSE `GET /tasks/{id}/events` with `Last-Event-ID` replay; it closes after `task.finished`.
- API: `GET /health` (DB + checkpointer, 3 s timeouts, 200/503), `GET /me`, bearer auth, `require_role`, `owned_by` (cross-tenant reads → 404), minimal `POST /tasks` / `GET /tasks/{id}` for the demo graph.
- `demo` graph (step_one → step_two) on AsyncPostgresSaver, run by the worker. **Cross-process resume test passes:** worker A is SIGKILLed inside step_two, worker B resumes from the checkpoint, step_one ran once, and there is one `input` checkpoint in history. **Race test passes:** a second worker on the same thread releases the job without running it.
- Manual checks against the real compose stack: a task submitted through the API ran in the worker container and streamed over SSE to `completed`. `/health` returned 503 with postgres stopped and 200 after restart. The socket proxy denies images/networks/volumes/exec-start. The sandbox runs as uid 10001 with no network.
- CI workflow: lint → mypy → unit → integration (pgvector service container), plus image builds. **First run green** on GitHub (run 41979604907, 2026-11-05). Repo: https://github.com/Zemukiros/agentops (public).

## Next up (Phase 2, after review)
- Real `TaskState` (plan, results with reducers, reviews, budget), intake → plan → one specialist → review → synthesize, structured `Plan` + DAG validation.
- Real-model factory (`LLM_MODE=real`): verify current model IDs and **prices** first, and fill in `pricing.py` with source and date.
- First fake-mode scenario script for the vertical slice.

## Known limitations / stubs (be honest)
- `make eval` only prints a notice. There is no scenario harness until Phase 6.
- `ModelClient` has no real-model factory yet (`LLM_MODE=real` raises NotImplementedError). Real prices are unverified (NULL).
- Only the `demo` graph exists. `POST /tasks` accepts `graph="demo"` only.
- `ui/`, `evals/`, `mcp_servers/` contain only READMEs.
- Implemented but not covered by tests: the worker cancelling a run when its heartbeat finds the lease lost, and the task being set to `failed` when the reaper fails an exhausted job (the reaper itself is tested).
- Budget check compares *consumed* budget before a call. It does not estimate the upcoming call's cost, so one call can overshoot the limit.
- Reviewers see only their own tasks (owner filter everywhere). The cross-user approvals inbox needs a scoped rule in Phase 4.
- Node-emitted events are not transactional with checkpoints (ADR-022), so a re-run node can emit duplicates.

## Open risks
- LangGraph moves fast (1.2.20 shipped today). The lockfile pins it; bump deliberately.
- `mcp` is held at 1.x by `langchain-mcp-adapters`.
- Sandbox bind mounts from the worker container are untested (Phase 3).
- Replay forks share the thread head (ADR-015 caveat). The Phase 5 design must track branch checkpoint ids.

## Measured metrics (only from `evals/` runs, with date + config)
- none yet
