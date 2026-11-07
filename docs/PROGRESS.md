# Progress

_Update at the end of every work session and every phase. Keep it short. This is how work resumes without lost context._

## Current status
- **Phase:** 3 — **complete, awaiting review** (2026-11-07). Phase 4 not started.
- **Branch:** `phase-3` (pushed; no PR yet — CI's new `sandbox` job runs on the PR).
- **Last updated:** 2026-11-07
- **Test status:** 511 passed, 0 failed on `make test` (fake mode): 393 unit, 109 integration, 9 sandbox (real Docker). Lint, ruff format, mypy clean. 3 e2e tests (real models) deselected by default.
- **Real-model spend this phase:** $0.154161 (two runs). Cumulative AgentOps: **$0.194615**.

## Phase plan
| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: repo, config, DB schema + migrations, checkpointer, job queue skeleton, fake model, health API, compose, CI | ☑ approved |
| 2 | Vertical slice: request → plan → one specialist → review → output, fake + real model | ☑ approved, merged |
| 3 | All specialists, scheduler waves + parallelism, review/retry, tool registry, native tools, MCP (stdio + HTTP), sandbox | ☑ (awaiting review) |
| 4 | Durability: crash/restart recovery, plan/action/escalation gates, idempotent execution, memory (pgvector) | ☐ |
| 5 | UI + observability: SSE, plan DAG, approvals inbox, trace/cost, memory browser, replay | ☐ |
| 6 | Hardening + evals: injection suite, scenario harness, measured metrics, docs, deploy config | ☐ |

## Done (Phase 3)
Approved plan + the 7 user changes (partial-result rule, proposal dedupe, MCP stdio env/timeouts/restart, host-path sandbox mount + through-proxy check, step cap = failed attempt, separate sandbox CI job, readability-lxml vetting).

| Step | What | Commit |
|---|---|---|
| 0 | Failure policy: `blocked`, independent work continues; `completed_partial` needs ≥1 accepted + a deterministic "What's missing" section + final review ≥ `FINAL_REVIEW_MIN_SCORE`; otherwise `failed` (ADR-029). Fake scripts: qualified purposes, scenario-first lookup, per-attempt turn index (ADR-027 update) | `579e743` |
| 1 | Tool registry: deny → validate → propose → rate limit → timeout → `tool_calls` + event; untrusted framing; Postgres token bucket; workspace tools (ADR-030, ADR-034) | `11ad03a` |
| 2 | ReAct specialists (research, analysis, code, writing); step cap = failed attempt (counts toward revisions, never raises); reviewer sees the tool log; parallel waves (barrier test proves concurrency) | `f08d09c` |
| 3 | SQL tools: `demo` schema, read-only LOGIN role, sqlglot single-SELECT, statement timeout (ADR-033) | `96f2ebe` |
| 4 | `web_search` (fixtures + Tavily adapter) and `fetch_url` (SSRF guard after DNS, IP pinning, redirect re-checks, caps, readability) | `6cbbb7f` |
| 5 | Sandbox + `run_python`: container per run, host-path bind mount, isolation tests (ADR-011, ADR-031) | `6ef96f8` (WIP, confirmed by the full suite at `52d8c48`) |
| 6 | MCP on SDK v2: notes server over stdio + streamable HTTP; allowlist, expected server name, `server__tool`, default destructive, sanitized descriptions, schema subset; stdio env fully chosen by us (canary test), startup/call timeouts, restart on crash with one retry for reads (ADR-032) | `52d8c48` |
| 7 | ProposedActions: `send_email` (proposal-only; real Mailpit executor for Phase 4), every proposal-only tool tested for every allowed agent, executor confined to the registry (AST), dedupe across crash re-run and across revisions (mutation-checked) | `21fa815` |
| 8 | Prompt injection: an injected fixture page causes no external action and no proposal; injected text reaches later prompts only inside data delimiters; a compromised writer can only propose | `a0f4744` |
| 9 | Compose/CI: host-path workspace mount, `mcp-notes` service (no env file), `make up` creates `var/workspaces`; **through-proxy selfcheck OK** (worker → proxy → sandbox reads the worker's file, writes one back, worker reads it); CI `sandbox` job (fails rather than skips without Docker), Mailpit for the integration job | `13982e6` |
| 10 | Real e2e (research → analysis → writing). Run 1 found a bug (below); fixed; run 2 passed. `llm_calls.reservation_id` (migration 0004) links each call to its reservation | `5326057` |

**Bug found by the real run (would not show in fake mode):** the reviewer of a dependent subtask was never shown the accepted upstream outputs the specialist worked from (`reviewer_messages` built the subtask block with an empty context). In run 1 it judged every number the writer carried over as unsupported, sent the brief back, then escalated it. Fixed: reviewers get the same `<context>` blocks, and the prompt says those outputs were already reviewed and accepted. Regression unit test added. Run 2: all three subtasks accepted first time.

**Also found (test bug, not a system bug):** the e2e check paired `llm_calls` with `budget_reservations` by row order. Under parallel branches reservations (written before a call) and call rows (written after) interleave, so the check compared a call with another call's reservation. Re-checked run 1 through the `llm.call` events' recorded reservation: every cost and token count was within its own reservation (max 88%). Now linked explicitly by `llm_calls.reservation_id`.

**Manual checks:** pinned-TLS fetch of example.com succeeded (certificate verified against the hostname while connecting to the checked IP); `localhost` blocked after real DNS. Worker container loaded all native tools plus `notes__*` (stdio) and `notes_http__*` (HTTP). The brief's revenue table matched an independent SQL query exactly.

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
All figures are from the task's budget ledger (`task_budgets`) and the `llm_calls` rows; they agree to the 1e-9 USD.

| Date | What | Models | Calls | Tokens (in / out) | Cost | Cap | Result |
|---|---|---|---|---|---|---|---|
| 2026-11-05 | `tests/e2e/test_real_slice.py` (1 task, default request) | Sonnet 5.5 (plan, review, synthesize), Haiku 5.5 (draft); effort `low` | 6 | 10,966 (6,067 / 4,899) | **$0.040454** | $0.10 | `completed`: reviewer sent draft 1 back (≈180 words vs the 120–150 limit, 2/5), accepted the revision 5/5 |

Per call (input, output, actual cost; reserved = worst case held by the ledger, not spent):

| Call | Model | In | Out (cap) | Cost | Reserved |
|---|---|---|---|---|---|
| plan | claude-sonnet-5-5 | 1,105 | 355 (3,000) | $0.005760 | $0.033272 |
| draft r0 | claude-haiku-5-5 | 414 | 308 (2,000) | $0.000195 | $0.001099 |
| review r0 | claude-sonnet-5-5 | 1,287 | 1,301 (2,000) | $0.015584 | $0.024042 |
| draft r1 | claude-haiku-5-5 | 915 | 1,602 (2,000) | $0.000892 | $0.001156 |
| review r1 | claude-sonnet-5-5 | 1,944 | 1,085 (2,000) | $0.014738 | $0.025554 |
| synthesize | claude-sonnet-5-5 | 402 | 248 (3,000) | $0.003284 | $0.031950 |

- Model IDs confirmed via the Models API first (free).
- Every call stayed within its reservation. The real-mode input margin (×1.15 + 512) reserved 1.43–2.43× the actual input tokens, so the margin is safe for this run and generous on short prompts.
- One test assertion crashed after the run completed (raw SQL returns `NUMERIC` as `Decimal`; the check added a float). Fixed by extracting `check_report()`. The fixed checks were re-run against this run's recorded rows rather than paying for a second run.

**Phase 3** (`tests/e2e/test_real_phase3.py`; research/writing on Haiku 5.5, plan/analysis/review/synthesis on Sonnet 5.5, effort `low`; search and pages from fixtures, no paid search API; SQL via the read-only role; Python in the real sandbox):

| Date | Run | Calls | Tokens | Cost | Cap | Result |
|---|---|---|---|---|---|---|
| 2026-11-07 | 1 | 16 | 49,724 | **$0.078255** | $0.40 | `awaiting_approval`: research and analysis accepted (4/5); the brief was escalated by a reviewer that couldn't see the upstream outputs (bug, fixed above). Every call within its own reservation (re-checked via events) |
| 2026-11-07 | 2 (after fix; approved re-run) | 13 | 34,514 | **$0.075906** | $0.40 | `completed`: research (2 searches, 2 fetches), analysis (`describe_schema`, `sql_query`, `run_python` in the sandbox), brief — all accepted first time; revenue table verified against the database |

- Output caps are still tight with thinking on: the brief used 2,004 of 2,500 output tokens; one review used 1,364 of 2,000.
- Phase 3 real spend: **$0.154161**. Cumulative real-model spend on AgentOps: **$0.194615**.

## Next up
- Review Phase 3 (PR `phase-3` → `main`; the new CI `sandbox` job runs there for the first time).
- Phase 4 plan: plan/action/escalation gates with an approvals table + API, `execute_actions` behind `side_effects` (proposals are already hash-keyed and deduped), final review for complete deliverables, memory (pgvector), recovery hardening.

## Known limitations / stubs (be honest)
- **Proposed actions are recorded, never executed.** `action_gate` / `execute_actions` are Phase 4. `send_email`'s executor is real (Mailpit) and tested directly, but nothing calls it yet.
- Not built yet (inserted later): `recall`, `plan_gate`, `action_gate`, `execute_actions`, `consolidate`. `final_review` exists but only scores partial deliverables.
- Escalation has no approvals table/API/inbox yet (Phase 4). A `resume` job with `{"resume": {...}}` is the only way to continue an escalated run.
- Injection tests prove the guards with scripted models, not real-model resistance (Phase 6 suite). A fooled writing agent can still *propose* an email; only human approval (Phase 4) stops it.
- MCP: only a supported JSON Schema subset is loaded (objects of primitives, string enums, arrays of primitives); other tools are skipped. MCP calls don't pass the task's idempotency key yet.
- `run_python` returns stdout/stderr and the names of new files; there is no per-task CPU/time budget beyond each run's own limits.
- Sandbox via the socket proxy can create containers with any bind mount; the worker code (not the model) builds the mount (ADR-031). Only native Docker Engine in WSL2 is tested.
- Real-mode reservations (input margin ×1.15 + 512, plus the full output cap) checked on three real runs, 35 calls: every call's actual cost and tokens within its own reservation (asserted per call); highest observed cost/reserved ratio 0.88.
- Output caps are tight with thinking on (brief 2,004 / 2,500); a capped call becomes a retry.
- Real mode makes one extra `count_tokens` request per attempt (free; adds latency).
- `make eval` only prints a notice. No scenario harness until Phase 6. Fake scenarios test orchestration logic only and say nothing about model quality.
- Reviewers see only their own tasks (owner filter everywhere). The cross-user approvals inbox needs a scoped rule in Phase 4.
- Replay forks (Phase 5) will need a branch id in event dedupe keys (ADR-025).

## Open risks
- LangGraph moves fast (1.2.20). It warns that unregistered checkpoint types will be blocked; we register ours (ADR-028, tested).
- `mcp` 2.x is young (2.3.0); its stdio client's inherited-env list and error codes were read from the installed package. Re-check on upgrade (ADR-032).
- The new CI `sandbox` job has not run on GitHub yet (CI triggers on PRs and pushes to `main`).
- Replay forks share the thread head (ADR-015 caveat). The Phase 5 design must track branch checkpoint ids.

## Measured metrics (only from `evals/` runs, with date + config)
- none yet
