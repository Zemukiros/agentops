# Progress

_Update at the end of every work session and every phase. Keep it short. This is how work resumes without lost context._

## Current status
- **Phase:** 5 — **planning** (2026-11-07). Phase 4 approved and merged (PR #3, merge commit `8a904e8`).
- **Branch:** `phase-5` (from `main` at `8a904e8`).
- **Last updated:** 2026-11-07
- **Test status:** 561 passed, 0 failed on `make test` (fake mode): 402 unit, 150 integration, 9 sandbox (real Docker). Lint, ruff format, mypy clean. Not in `make test`: 1 `model` test (real fastembed; passed), 4 `e2e` tests (real models).
- **Real-model spend:** Phase 4 $0.083560; Phase 5 $0.00 so far. Cumulative AgentOps: **$0.278175**.

## Phase plan
| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: repo, config, DB schema + migrations, checkpointer, job queue skeleton, fake model, health API, compose, CI | ☑ approved |
| 2 | Vertical slice: request → plan → one specialist → review → output, fake + real model | ☑ approved, merged |
| 3 | All specialists, scheduler waves + parallelism, review/retry, tool registry, native tools, MCP (stdio + HTTP), sandbox | ☑ approved, merged |
| 4 | Durability: crash/restart recovery, plan/action/escalation gates, idempotent execution, memory (pgvector) | ☑ approved, merged (PR #3) |
| 5 | UI + observability: SSE, plan DAG, approvals inbox, trace/cost, memory browser, replay | ☐ |
| 6 | Hardening + evals: injection suite, scenario harness, measured metrics, docs, deploy config | ☐ |

## Done (Phase 4)
Approved plan + the 3 user changes (side_effect_review inbox item, consolidate under budget exhaustion, expiry-vs-human race) + the decision that low final reviews escalate for partial deliverables too (ADR-029 amended).

| Step | What | Commit |
|---|---|---|
| 1 | Approvals inbox (ADR-035): migration 0005, semantic keys, `GET/POST /approvals` (reviewers see all, users their own; reviewer role to decide; 422 on invalid; 409 on a lost race), one version-checked `decide` shared by the API and the expiry sweep, worker-level budget/recursion pauses, escalation take_over, side_effect_review resolves records only | `61e07fc` |
| 2 | plan_gate (ADR-036): `min(self-reported, structural)`; approve / edit (DAG re-validated) / reject / take_over | `d94289f` |
| 3 | action_gate + execute_actions (ADR-037, ADR-004 update): one approval per action; side_effects insert → perform → mark done; Message-ID = key; unknown outcomes surfaced, never retried | `19528a1` |
| 4 | final_review on every deliverable; below threshold → escalation_gate (final mode); `delivery_outcome` decides completed vs completed_partial | `f145bda` |
| 5 | One `budget.warning` event per task at 80% | `9e6dd7d` |
| 6 | Memory (ADR-038): fastembed (vetted), recall (user-scoped, threshold, ranking, cap, labelled in prompts), screened consolidation on every terminal path, episode-only fallback when the ledger can't fit, memory API; model baked into the image | `a489e25` |
| 7 | Durability matrix: SIGKILL at plan/action/escalation gates (before and after the decision) and in execute_actions, resumed on a different worker | `e06daa8` |
| 8 | ADR-035…039; ADR-004 and ADR-029 amended | `4c0351c` |
| 9 | Real e2e (approval through the API, one email, memory, recall) | this commit |

**Bugs found (not visible before this phase's tests):**
- `memories.outcome` was `VARCHAR(16)`; `completed_partial` is 17 characters, so consolidation of partial runs failed. Widened to 32 in migration 0005.
- **Recovery bug (found by the durability matrix):** a `resume` job whose interrupt had already been consumed (its worker died later in the same job; the job was redelivered) was dropped as "nothing to resume", so the task stayed `running` forever. It now continues from the checkpoint (ADR-039).

**Manual check on the compose stack (fake models, real containers):** task via `POST /tasks` → paused for the email → the demo *user* trying to decide got 403, the *reviewer* got 200, a stale replay 409 → completed → email in Mailpit → memories written by the worker container's real fastembed (offline, model baked into the image; 65 MB) → `GET /memories/search` found the episode at similarity 0.89.

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
- Phase 3 real spend: **$0.154161**. Cumulative after Phase 3: $0.194615.

**Phase 4** (`tests/e2e/test_real_phase4.py`; same models as Phase 3, effort `low`; fixture search; real fastembed embeddings; approvals made by a reviewer through `POST /approvals/{id}/decision`):

| Date | Task | Calls | Tokens | Cost | Cap | Result |
|---|---|---|---|---|---|---|
| 2026-11-07 | A: research → writing proposes `send_email` → reviewer approves via the API | 11 | 25,949 | **$0.041365** | $0.20 | `completed`; exactly one email in Mailpit (matched by its idempotency key, subject "pgvector vs dedicated vector databases"); memories: an episode and a lesson ("When the official source fetch is blocked, state clearly which claims are unverified…", from the research agent's refused fetch of a non-fixture URL) |
| 2026-11-07 | B: similar request | 11 | 26,039 | **$0.042195** | $0.20 | `completed`; recalled task A's episode (similarity 0.887, score 0.781); no approvals needed |

- Phase 4 real spend: **$0.083560**. Cumulative real-model spend on AgentOps: **$0.278175**.

## Next up
- Phase 5 plan: UI (React) on the existing API and SSE: task detail with plan DAG and live activity, approvals inbox, trace/cost view, memory browser, replay (ADR-015 caveat: branch-aware checkpoint ids; event dedupe keys need a branch id).

## Known limitations / stubs (be honest)
- **Side-effect ambiguity window:** a crash between inserting the `side_effects` row and performing the action is treated as "outcome unknown" (surfaced for review, never retried), the same as a crash after performing. Conservative: it can under-execute, never double-execute.
- MCP calls have no idempotency key the notes server can use, so an MCP action interrupted mid-call ends "unknown" (ADR-004).
- Memory screening is heuristic (8-word copy check + instruction patterns). It stops copied tool text and obvious instructions, not a paraphrase. Injection resistance of real models is still unmeasured (Phase 6).
- `access_count` on memories can double-count when recall re-runs after a crash.
- `MEMORY_MIN_SIMILARITY=0.6` is set from one probe pair and two real runs (bge-small: related 0.735/0.887, unrelated 0.385). Not tuned on an eval set.
- Plan-confidence weights are judgement, not fitted (ADR-036).
- Worker-level pauses (budget, recursion) resume from the last checkpoint: the node that failed re-runs from its start, and its model calls before the failure are already in the ledger.
- Real mode makes one extra `count_tokens` request per attempt (free; adds latency). Output caps are tight with thinking on.
- `make eval` only prints a notice. No scenario harness until Phase 6. Fake scenarios test orchestration logic, not model quality.
- Replay forks (Phase 5) will need a branch id in event dedupe keys (ADR-025).

## Open risks
- LangGraph moves fast (1.2.20). Phase 4 relies on `interrupt(response_schema=...)` validation (verified against 1.2.20) and on node writes being discarded when a node is killed (tested).
- `mcp` 2.x is young (2.3.0); re-check its stdio env inheritance and error codes on upgrade (ADR-032).
- fastembed downloads the model at image build; a build without network access fails (runtime is offline by design).
- Replay forks share the thread head (ADR-015 caveat). The Phase 5 design must track branch checkpoint ids.

## Measured metrics (only from `evals/` runs, with date + config)
- none yet
