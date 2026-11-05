# Progress

_Update at the end of every work session and every phase. Keep it short. This is how work resumes without lost context._

## Current status
- **Phase:** 2 — Vertical slice: **complete in fake mode, awaiting review.** One real-model e2e run is pending an `ANTHROPIC_API_KEY` in `.env` (see "Real-model runs"). Phase 3 not started.
- **Last updated:** 2026-11-05
- **Test status:** 200 passed, 0 failed (141 unit, 59 integration), fake mode, 2026-11-05. 2 e2e tests (real model) are deselected by default. `make lint` clean (ruff, ruff format, mypy).
- **Branch:** `phase-2` (not pushed yet).

## Phase plan
| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: repo, config, DB schema + migrations, checkpointer, job queue skeleton, fake model, health API, compose, CI | ☑ approved |
| 2 | Vertical slice: request → plan → one specialist → review → output, fake + real model | ☑ (awaiting review; real e2e pending key) |
| 3 | All specialists, scheduler waves + parallelism, review/retry, tool registry, native tools, MCP (stdio + HTTP), sandbox | ☐ |
| 4 | Durability: crash/restart recovery, plan/action/escalation gates, idempotent execution, memory (pgvector) | ☐ |
| 5 | UI + observability: SSE, plan DAG, approvals inbox, trace/cost, memory browser, replay | ☐ |
| 6 | Hardening + evals: injection suite, scenario harness, measured metrics, docs, deploy config | ☐ |

## Done (Phase 2)
**Phase 1 fixes**
- Idempotent node events (ADR-025): `events.dedupe_key` + partial unique index; re-run nodes no longer duplicate events. The durability test now asserts exactly one `node.started` after a SIGKILL + re-run.
- Budget ledger (ADR-024): worst case (exact input + `max_output_tokens`, priced) reserved before every attempt by one atomic conditional UPDATE, settled after. Tests: a single call that doesn't fit is refused before reaching the model; spend never exceeds the limit over 100 calls; 5 concurrent reservations → exactly 1 granted; orphans are charged at worst case once.
- Worker tests: lost lease cancels the run; exhausted job is reaped and its task ends `failed`. Writing the lease test exposed a real bug: the worker treated *any* cancellation as lease loss and swallowed it. Fixed and tested (an external cancel now propagates).
- CLAUDE.md / ADR-011: native Docker Engine in WSL2; the proxy blocking exec is intentional (sandbox = run + wait + logs + remove).

**Vertical slice** (`main` graph, ADR-026)
- intake → plan (structured `Plan`, validation: ids, unknown deps, Kahn cycle check, enabled specialist, ≤ 6 subtasks; one re-plan) → schedule (pure `scheduler.decide`, terminal results only) → `subtask_worker` subgraph via `Send` (writing specialist ⇄ reviewer, ≤ 2 revisions) → synthesize. Plus `escalation_gate` with a real `interrupt()` (approve / edit / reject) and `finalize_failed` / `finalize_rejected`.
- Worker: runs any registered graph spec, builds a ModelClient per task, supports `resume` jobs, and maps budget / recursion / missing outcome to exactly one terminal state.
- API: `POST /tasks` defaults to `graph=main`; `scenario` only in fake mode (422 otherwise).
- Fake mode: 8 scripted scenarios (`models/scenarios.py`); scripts keyed by role + purpose (ADR-027).
- Real mode: `langchain-anthropic` 1.7.10; verified prices filled in; request shape verified offline (json_schema structured output, no temperature, no forced tool_choice); fail-fast config check (ADR-009 note).
- Checkpoint serde allowlist for state types (ADR-028).
- Manual check on the compose stack: `revise_once` task via curl → worker container → SSE showed revise → accept → `task.finished completed` (6 model calls, $0).

## Real-model runs
| Date | What | Calls | Cost (ledger) | Result |
|---|---|---|---|---|
| — | `tests/e2e/test_real_slice.py` | — | not run yet (no key in `.env`) | — |

## Next up
- Run the e2e once (`uv run pytest -m e2e tests/e2e -s`, ≤ $0.10 cap) and record the cost here.
- Review Phase 2; then Phase 3 plan (all specialists, tool registry + native tools, MCP, sandbox, true parallel waves).

## Known limitations / stubs (be honest)
- Only the `writing` specialist exists and it makes a single model call (no tools, no ReAct loop, no step cap yet: Phase 3).
- Not built yet (inserted later): `recall`, `plan_gate`, `action_gate`, `execute_actions`, `final_review`, `consolidate`.
- Escalation has no approvals table/API/inbox yet (Phase 4). A `resume` job with `{"resume": {...}}` is the only way to continue an escalated run.
- A failed subtask fails the whole run immediately (no `completed_partial` yet).
- Real-mode input-token reservation uses a margin (×1.15 + 512), because `count_tokens` can't see the structured-output schema or thinking config. It has not been checked against real calls yet; the e2e test asserts it.
- Real mode makes one extra `count_tokens` request per attempt (free; adds latency).
- `make eval` only prints a notice. No scenario harness until Phase 6. Fake scenarios test orchestration logic only and say nothing about model quality.
- Reviewers see only their own tasks (owner filter everywhere). The cross-user approvals inbox needs a scoped rule in Phase 4.
- Replay forks (Phase 5) will need a branch id in event dedupe keys (ADR-025).

## Open risks
- LangGraph moves fast (1.2.20). It warns that unregistered checkpoint types will be blocked; we register ours (ADR-028).
- `mcp` is held at 1.x by `langchain-mcp-adapters`.
- Sandbox bind mounts from the worker container are untested (Phase 3).
- Replay forks share the thread head (ADR-015 caveat). The Phase 5 design must track branch checkpoint ids.

## Measured metrics (only from `evals/` runs, with date + config)
- none yet
