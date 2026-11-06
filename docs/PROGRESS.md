# Progress

_Update at the end of every work session and every phase. Keep it short. This is how work resumes without lost context._

## Current status
- **Phase:** 3 — **in progress, stopped after step 5 of 10** (2026-11-06). Phase 2 approved and merged (PR #1).
- **Branch:** `phase-3` (pushed; no PR yet). Last commit is a WIP commit for step 5 (sandbox).
- **Last updated:** 2026-11-06
- **Test status at stop:** lint, ruff format and mypy clean. Unit 381 passed; sandbox 9 passed (real Docker). Integration: 467 passed for the whole suite at the step 4 commit (`6cbbb7f`); **not re-run after the step 5 changes** (they only add `run_python` to the tool catalog) — run `make test` first thing.

## Phase plan
| Phase | Goal | Status |
|---|---|---|
| 1 | Foundation: repo, config, DB schema + migrations, checkpointer, job queue skeleton, fake model, health API, compose, CI | ☑ approved |
| 2 | Vertical slice: request → plan → one specialist → review → output, fake + real model | ☑ approved, merged |
| 3 | All specialists, scheduler waves + parallelism, review/retry, tool registry, native tools, MCP (stdio + HTTP), sandbox | ◐ steps 0–5 of 10 done |
| 4 | Durability: crash/restart recovery, plan/action/escalation gates, idempotent execution, memory (pgvector) | ☐ |
| 5 | UI + observability: SSE, plan DAG, approvals inbox, trace/cost, memory browser, replay | ☐ |
| 6 | Hardening + evals: injection suite, scenario harness, measured metrics, docs, deploy config | ☐ |

## Phase 3 — where it stopped
Approved plan + the 7 user changes (partial-result rule, proposal dedupe, MCP stdio env/timeouts/restart, host-path sandbox mount + through-proxy check, step cap = failed attempt, separate sandbox CI job, readability-lxml vetting).

| Step | What | Status | Commit |
|---|---|---|---|
| 0 | Failure policy: `blocked`, independent work continues, `completed_partial` needs ≥1 accepted + deterministic "What's missing" section + final review ≥ `FINAL_REVIEW_MIN_SCORE` (ADR-029). Fake scripts: qualified purposes, scenario-first lookup, per-attempt turn index (ADR-027 update) | ☑ | `579e743` |
| 1 | Tool registry (deny → validate → propose → rate limit → timeout → log + event), untrusted framing, Postgres token bucket, workspace tools, migration 0003 | ☑ | `11ad03a` |
| 2 | ReAct specialists (research, analysis, code, writing), step cap = failed attempt (counts toward revisions, never raises), reviewer sees the tool log, parallel waves (barrier test) | ☑ | `f08d09c` |
| 3 | SQL tools: `demo` schema, read-only LOGIN role, sqlglot single-SELECT, statement timeout | ☑ | `96f2ebe` |
| 4 | `web_search` (fixture + Tavily adapter) and `fetch_url` (SSRF guard after DNS, IP pinning, redirect re-checks, caps, readability) | ☑ | `6cbbb7f` |
| 5 | Sandbox + `run_python`: container per run, host-path bind mount of the task workspace, isolation tests (no network, no secrets, loop killed, fork bomb contained, truncation, never exec) | ☑ code + tests; WIP commit (integration suite not re-run) | WIP |
| 6 | **MCP — next.** `mcp==2.3.0` `MCPServer` "notes" server (stdio + streamable HTTP), allowlist config, `server__tool` names, default risk destructive, sanitized descriptions, JSON Schema → Pydantic. Stdio subprocess with an explicit minimal env + canary test, startup/call timeouts, restart on crash | ☐ | |
| 7 | ProposedAction: reducer dedupe test (crash re-run can't duplicate proposals), "no specialist path executes a proposal" test, `send_email` spec (proposal-only) | ☐ (model, hash and reducer already exist from step 1; tests + send_email spec remain) | |
| 8 | Prompt-injection test with `fixtures/web/injected.html` (page already added in step 4) | ☐ | |
| 9 | Compose/CI wiring: worker `WORKSPACE_ROOT=/workspaces` + `WORKSPACE_HOST_ROOT` (host path) bind mount, `make` target creating `var/workspaces` (mode 0777: worker uid 10000, sandbox uid 10001), `mcp-notes` service, SQL role env; **through-proxy check** `docker compose exec worker python -m agentops.tools.sandbox --selfcheck`; CI: separate `sandbox` job (build image, `AGENTOPS_REQUIRE_SANDBOX=1 pytest -m sandbox`) | ☐ | |
| 10 | Full suite → one real e2e run (research → analysis → writing, ledger cap $0.40, estimate $0.10–0.25) → docs → summary | ☐ | |

**Resume checklist (next session):**
1. `git checkout phase-3 && make up && make migrate && make test` (confirms the step 5 WIP against the integration suite), plus `uv run pytest -m sandbox tests/sandbox`.
2. Step 6: re-read the `mcp` 2.3.0 server/client API from the installed package before writing code (FastMCP was renamed `MCPServer` in 2.x; `mcp` is not a dependency yet — add `mcp==2.3.0`).
3. Not yet written: ADR-030 (registry + risk/scope table), ADR-031 (sandbox host-path mount), ADR-032 (MCP on SDK v2, replaces ADR-008's 1.x pin), ADR-033 (SQL read-only role), ADR-034 (untrusted framing). The design is in the code docstrings (`tools/registry.py`, `tools/spec.py`, `tools/sandbox.py`, `tools/native/sql.py`, `tools/framing.py`); copy into DECISIONS.md.

**Decisions made this phase (record as ADRs):**
- MCP: `mcp==2.3.0` directly (user choice); no `langchain-mcp-adapters`.
- Live search: Tavily via httpx (user choice), verified against Tavily's API reference 2026-11-06 (Bearer auth, `search_depth: "basic"` = 1 credit). Never called unless `TAVILY_API_KEY` is set.
- readability-lxml 0.9 vetted before install: pure wheel identical to the sdist, no install hooks/entry points/.pth; its only network (`urlopen`) and `webbrowser` code is in the CLI `main()`, unreachable from `Document(...).summary()`; single individual maintainer, Apache-2.0.
- New dependencies (exact pins): `sqlglot==30.29.0`, `readability-lxml==0.9` (+ lxml 6.1.3, lxml-html-clean 0.4.6, chardet 5.2.0, cssselect 1.3.0), `docker==7.2.0`. mypy ignores missing types for readability/lxml/docker only.
- Final review runs only for partial deliverables in Phase 3; reviewing complete deliverables arrives with the Phase 4 gates.
- Manual smoke (not a test): `guarded_fetch("https://example.com/")` succeeded with the connection pinned to the checked IP and the TLS certificate verified against the hostname; `localhost` was blocked after real DNS resolution.

**Real-model spend in Phase 3 so far: $0.00.** Cumulative AgentOps: $0.040454.

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
- Cumulative real-model spend on AgentOps: **$0.040454**.

## Next up (as of Phase 2; see the Phase 3 section above for the current next step)
- Review Phase 2; then Phase 3 plan (all specialists, tool registry + native tools, MCP, sandbox, true parallel waves).

## Known limitations / stubs (be honest)
- Only the `writing` specialist exists and it makes a single model call (no tools, no ReAct loop, no step cap yet: Phase 3).
- Not built yet (inserted later): `recall`, `plan_gate`, `action_gate`, `execute_actions`, `final_review`, `consolidate`.
- Escalation has no approvals table/API/inbox yet (Phase 4). A `resume` job with `{"resume": {...}}` is the only way to continue an escalated run.
- A failed subtask fails the whole run immediately (no `completed_partial` yet).
- Real-mode input-token reservation uses a margin (×1.15 + 512), because `count_tokens` can't see the structured-output schema or thinking config. Checked on one real run (6 calls): 1.43–2.43× actual input. One run is not a distribution; the e2e test keeps asserting it.
- Output caps are tight with thinking on: draft r1 used 1,602 of 2,000 output tokens and review r0 1,301 of 2,000. A call that hits the cap returns truncated text or an invalid structured answer, which becomes a retry. Watch this in Phase 3; raise caps per role if it happens (each raise also raises the reserved worst case).
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
