# Architecture Decisions

This log records where the build deviates from `docs/15-agent-orchestration-system.md` (the case study) and why.
The case study is the product reference. Where it conflicts with this file, **this file wins**.

Each entry: **Decision · Why · Trade-off · Revisit if.**
Status: `accepted` (build it this way) · `verify` (confirm the API/behavior against current docs before relying on it).

---

## Corrected workflow graph

```
START
  → intake            normalize request, init budget
  → recall            pgvector search, scoped by user_id
  → plan              structured Plan; DAG + specialist + budget validation; 1 re-plan on failure
  → plan_gate         interrupt(approve_plan) if effective confidence < threshold   [serial]
  → schedule ─────────────────────────────────────────────────────────────┐
      │ returns list[Send("subtask_worker", …)] for every ready subtask    │
      ▼                                                                    │
  subtask_worker  (compiled subgraph, one instance per subtask, parallel)  │
      specialist (ReAct, filtered tools, step cap)                         │
        → review → accept  → return result(status=accepted, proposed_actions)
                 → revise  → specialist   (max 2 revisions, counter in subgraph state)
                 → escalate / revisions exhausted → return result(status=needs_human)
      ▼                                                                    │
  schedule  (runs again after the wave; serial) ───────────────────────────┘
      ├─ ready subtasks exist            → next wave of Send
      ├─ any result needs_human          → escalation_gate (interrupt: approve / edit / reject / take_over)
      ├─ a dependency failed, no path    → finalize_failed
      └─ all accepted                    → action_gate
  → action_gate       for each proposed external/destructive action: interrupt(approve_action)   [serial]
  → execute_actions   run approved actions with idempotency keys                                [serial]
  → synthesize        compose final deliverable
  → final_review      reviewer scores deliverable; low score → escalation_gate
  → consolidate       write reviewer-accepted memories
  → END
```

**Invariants (each one needs a test):**
- Parallel branches **never** call `interrupt()` and **never** cause external side effects.
- Every `interrupt()` lives in a serial node that does only pure computation before the call.
- Every side effect runs in `execute_actions` and is guarded by the `side_effects` table.
- Every cycle (revise loop, re-plan, schedule loop) has a counter-based termination that does not depend on the model.
- The graph cannot end silently: every run finishes in exactly one terminal state —
  `completed`, `completed_partial`, `failed`, `rejected`, `cancelled`, `budget_exceeded`.

---

## ADR-001 · Review runs inside a per-subtask subgraph · accepted
**Decision.** `Send` targets a compiled `subtask_worker` subgraph containing specialist → review → revise loop. It returns one terminal result per subtask.
**Why.** The case study wires `specialist → review` as a plain edge. With `Send` fan-out, `review` would run once after the whole wave, on merged state, not once per subtask. Per-branch review and retry need their own loop.
**Trade-off.** A wave is a barrier: the next wave starts only after the slowest branch finishes. Fine for a few subtasks.
**Revisit if.** Plans regularly have long, uneven waves. Then schedule each subtask as soon as its own dependencies finish.

## ADR-002 · Scheduler only sees terminal results · accepted
**Decision.** `results[subtask_id]` holds only terminal statuses: `accepted | failed | needs_human`. Retries live in subgraph state. `schedule` handles every case explicitly (see graph above).
**Why.** The case study's `dispatch_ready` treats any key in `results` as "done". A subtask sent back for revision would never be re-dispatched, and "nothing ready + not all done" fell through, so the run ended silently.
**Trade-off.** None worth noting.

## ADR-003 · Propose → approve → execute; no side effects in specialists · accepted
**Decision.** Specialists can call `read` tools directly. For `write`/`external`/`destructive` tools they produce a `ProposedAction` (tool, validated args, rationale). `action_gate` interrupts for approval. `execute_actions` runs the approved actions.
Exception: `write` tools scoped to the task's own workspace directory (e.g. writing a scratch file) can run directly. They affect nothing outside the task.
**Why.** Interrupts inside parallel branches mean several pending interrupts per thread and resume-by-interrupt-ID bookkeeping. Moving every sensitive action to one serial point makes "never executes without approval" and "never executes twice" easy to enforce and test.
**Trade-off.** A specialist can't see the real result of an external action within its own loop. Acceptable: external actions are end-of-workflow deliverables (send, post, create issue), not inputs to reasoning.
**Verify.** Current LangGraph semantics for `interrupt()` / `Command(resume=…)`, including that the interrupted node re-runs from its start on resume.
**Verified 2026-11-05 (langgraph 1.2.20, Postgres checkpointer).** On resume, the interrupted node re-runs from its first line: code before `interrupt()` executed twice in a probe. The payload comes back as `result["__interrupt__"]` (a list of `Interrupt(value, id)`) and as `aget_state(cfg).tasks[i].interrupts`. New in 1.x: `interrupt(value, response_schema=…)` can validate the resume value. Worth using for approval decisions in Phase 4.

## ADR-004 · Idempotent side effects · accepted
**Decision.** `side_effects(idempotency_key PK, task_id, action_hash, status, result, created_at, completed_at)`. Key = `sha256(task_id + action_id + canonical_json(args))`.
Flow: `INSERT … ON CONFLICT DO NOTHING` → if the existing row is `done`, return the stored result → otherwise perform the action → mark `done`.
**Why.** Resume re-runs nodes. Worker crashes cause redelivery. Both must not repeat sends.
**Trade-off.** A crash *between* performing the action and marking it `done` is ambiguous. Mitigation: pass the key to the downstream system when it supports one (e.g. email `Message-ID`), and leave rows stuck in `in_progress` for human review instead of retrying them automatically. Document this as a known limitation.
**Update (Phase 4, built).** `execute_actions` inserts the `side_effects` row (`in_progress`) first, then performs through `registry.execute_approved` (the key is passed on: email `Message-ID`), then marks `done`/`failed`. On a re-run, an existing `done`/`failed` row is reused and never re-run. An existing `in_progress` row means a worker died after inserting it: the action is reported **unknown**, never retried, and a non-blocking `side_effect_review` inbox item lets a reviewer record "confirmed done" or "not done" (records only, never re-executes). Note the ambiguous window is insert → perform → mark-done: a crash after the insert but before the perform is also treated as unknown, which is the conservative reading. A crash *before* `execute_actions` inserts anything resumes and sends exactly once (tested with SIGKILL).

## ADR-005 · Postgres + pgvector instead of ChromaDB · accepted
**Decision.** Long-term memory is a `memories` table with a `vector` column (pgvector), queried with `WHERE user_id = :uid`.
**Why.** One less stateful service. User memory deletion happens in the same transaction as task data. Postgres row filters enforce tenant isolation. The case study lists this as its own next improvement.
**Trade-off.** Less "vector DB" on the résumé. Worth it: "I consolidated to fewer services" is a stronger interview story.

## ADR-006 · Postgres job queue instead of Celery + Redis; Redis dropped · accepted
**Decision.** A `jobs` table (`run` / `resume` / `maintenance`) claimed with `SELECT … FOR UPDATE SKIP LOCKED`, with lease + heartbeat. One or more asyncio worker processes. A Postgres advisory lock per `thread_id` stops two workers from advancing the same thread. An expired lease makes the job claimable again, and the run continues from the last checkpoint.
Progress streaming: write to an `events` table and `NOTIFY`. The API's SSE endpoint `LISTEN`s, and replays missed events from the table using `Last-Event-ID`.
Rate limits: a Postgres-backed token bucket (one atomic `UPDATE … RETURNING`).
**Why.** Celery is synchronous and the graph is async, so they fit awkwardly. Redis would only be a broker, a pub/sub channel and a rate limiter, and Postgres covers all three at this scale with one less service. Job state becomes queryable with SQL.
**Trade-off.** Throughput ceiling is far below Redis. Irrelevant for this workload: runs last minutes, and there are few jobs per second.
**Revisit if.** Jobs/second or SSE fan-out show up as a measured bottleneck.

## ADR-007 · Events table is the UI's source of truth; OpenTelemetry export is extra · accepted
**Decision.** Every node transition, LLM call, tool call, review, approval, memory read/write, retry and failure is written to `events` (and `llm_calls` / `tool_calls` for detail). The UI reads these. OTel spans with the same IDs are exported to Jaeger when `OTEL_ENABLED=1` (compose profile `observability`).
**Why.** The UI needs to query by task, filter, and aggregate cost. That's easy in SQL and awkward through a trace backend. OTel stays as the vendor-neutral export.
**Cost accounting.** Recorded in the model-client wrapper on **every** attempt (including retries and structured-output re-asks), from a versioned price table. This fixes case study Problem 12.

## ADR-008 · Real integrations that are safe to run locally · accepted
| Tool | Risk | Implementation |
|---|---|---|
| `web_search` | read | Real search API (Tavily or Brave, behind an interface). `SEARCH_MODE=fixture` replays recorded results |
| `fetch_url` | read | httpx + readability extraction. SSRF guard: block private/loopback/link-local IPs after DNS resolution; size + time caps |
| `workspace_read/write/list` | read / write | Per-task directory; resolve the path, then check the prefix |
| `sql_query` + `describe_schema` | read | Seeded demo dataset in a separate schema; read-only role; `statement_timeout`; single-statement `SELECT` check (sqlglot) |
| `run_python` | write | Docker sandbox (ADR-011) |
| `send_email` | external | Real SMTP to **Mailpit** in dev (captures the mail, sends nothing out). The UI links to the Mailpit inbox |
| MCP (stdio) | per config | Our own FastMCP server, e.g. `notes` (create/search/delete notes; delete = destructive) |
| MCP (streamable HTTP) | per config | Same or a second server over HTTP, to prove both transports |
**Why.** "Tools actually execute" with no external accounts and no chance of sending real email. MCP tools default to the highest risk level unless config overrides it, and names are namespaced `server__tool`.
**Verify.** The current `mcp` Python SDK / FastMCP API and the `langchain-mcp-adapters` API.
**Verified 2026-11-05.** `langchain-mcp-adapters` 0.3.2 (the latest) requires `mcp<2.0`, but `mcp` is at 2.3.0. We pin **`mcp` 1.30.0** (the latest 1.x) until the adapters support 2.x. FastMCP 1.30 has `run(transport="stdio"|"streamable-http")` and `streamable_http_app()`. `MultiServerMCPClient(connections, tool_name_prefix=False, handle_tool_errors=True)` has `get_tools()`. Its optional prefix is `server_tool` (single underscore), so the registry namespaces tools as `server__tool` itself. Neither package is a dependency yet; both are added in Phase 3.
**Update (Phase 3, user decision).** Superseded by ADR-032: we use `mcp==2.3.0` directly (FastMCP is called `MCPServer` in 2.x) and do **not** use `langchain-mcp-adapters`, so there is no `mcp<2` pin. Live search is Tavily over plain httpx (verified 2026-11-06: `POST https://api.tavily.com/search`, `Authorization: Bearer`, `search_depth: "basic"` = 1 credit), used only when `TAVILY_API_KEY` is set; tests and e2e runs use fixtures. Text extraction is `readability-lxml` 0.9 (vetted before install: pure wheel identical to the sdist, no install hooks/entry points/.pth; its network and browser code is reachable only from its CLI `main()`).

## ADR-009 · Model layer · accepted
**Decision.** A `ModelClient` wrapper around LangChain chat models, configured per role (supervisor, research, analysis, writing, code, reviewer). The wrapper is the only path to a model. It enforces the budget **before** each call, records tokens/cost/latency, and retries with backoff.
`LLM_MODE=fake`: a deterministic scripted model (keyed by role + scenario) produces plans, tool calls and reviews, so the whole graph and UI run with no API key. Reviewer uses a different model than the specialist when real models are configured.
**Verify.** Current model IDs and pricing for whatever provider(s) are configured. Never hardcode prices from memory.
**Verified 2026-11-05 (langchain-core 1.7.6).** The built-in fake chat models (`GenericFakeChatModel`, `FakeMessagesListChatModel`) do **not** implement `bind_tools`, and `BaseChatModel.with_structured_output` is built on `bind_tools(tool_choice="any")`. So we wrote `models/fake.py::ScriptedChatModel`. It is stateless: the response is keyed by (role, scenario, turn = number of AI messages in the input), so a resumed run on another process gets the same answers. Structured output goes through `with_structured_output(schema, include_raw=True)`, which keeps the raw message (for token usage) and the parsing error (for a recorded re-ask). Real-model prices in `models/pricing.py` are `None` / `verified=False`. An unpriced call records `cost_usd = NULL` and increments `BudgetState.unpriced_calls`. It is never guessed.
**Verified 2026-11-05 (Phase 2, langchain-anthropic 1.7.10 / anthropic 2.6.0).** Real mode goes through `ChatAnthropic` (subclassed as `MarginChatAnthropic`). Prices from platform.claude.com/docs/en/about-claude/pricing: **claude-sonnet-5-5 $2 / $10 per MTok; claude-haiku-5-5 $0.10 / $0.50 per MTok for prompts up to 100,000 tokens, $0.50 / $2.50 above** (price table has a long-prompt tier). Request-shape findings, each covered by an offline unit test: (1) structured output must use `method="json_schema"` (`output_config.format`); the default `function_calling` forces `tool_choice`, which Sonnet 5.5 rejects with a 400; (2) `temperature` defaults to `None` and is never sent; non-default sampling returns a 400 on these models, so `RoleModel.temperature` was removed; (3) `reasoning_effort` maps to `output_config.effort` and merges with `format`; it turns on adaptive thinking, whose tokens bill as output and count toward `max_tokens`, which we always set; (4) `max_retries=0`, so the ModelClient owns retries and every attempt is reserved and recorded. Roles: supervisor and reviewer on Sonnet 5.5, writing on Haiku 5.5, no Opus. Model IDs are confirmed against the Models API (free) by the e2e test before any paid call. `ROLE_MODELS__*` env vars replace the whole default role map, so `real_factory` checks every role the graph uses at worker start and refuses unpriced or fake ones.

## ADR-010 · Embeddings · accepted
**Decision.** `fastembed` (local ONNX model, no API key) for real mode. A deterministic hash embedder for unit tests. The embedding dimension is fixed in config and in the migration.
**Verify.** The current fastembed default model and its dimension.
**Verified 2026-11-05 (fastembed 0.9.1).** The default is `BAAI/bge-small-en-v1.5`, dimension **384**, read from `TextEmbedding.list_supported_models()` without downloading the model. `settings.EMBEDDING_DIM = 384` is used by the migration, and `Settings` refuses a different `embedding_dim`. fastembed is added as a dependency in Phase 4.

## ADR-011 · Code sandbox · accepted
**Decision.** One container per execution from a minimal `agentops-sandbox` image: `network_mode=none`, non-root, `cap_drop=ALL`, `no-new-privileges`, read-only root, tmpfs `/tmp`, memory/CPU/PID limits, wall-clock timeout with forced removal, output truncation, no env secrets. The worker reaches Docker only through a socket proxy that allows container create/start/wait/logs/remove.
**Windows/WSL2.** The dev machine runs **Docker Engine natively inside WSL2** (no Docker Desktop). The repo lives in the WSL filesystem (`~/…`), not `/mnt/c`, so bind-mount paths are plain Linux paths. Bind-mounting the per-task workdir must work from the worker container, so check how paths map.
**Exec is blocked on purpose.** The socket proxy refuses `POST /exec/{id}/start`, and this is intended, not a gap. The sandbox only ever uses `containers.run` → `wait` → `logs` → `remove`; it never execs into a container. A Phase 3 test asserts that the sandbox code never calls `exec_run` / the exec API.
**Verify.** The docker-py parameters and the socket-proxy config, on native Docker Engine in WSL2.
**Verified 2026-11-05, partly.** This machine runs **native Docker Engine 29.1.3 inside WSL2 (Ubuntu 26.04)**, not Docker Desktop. Bind-mount paths are plain Linux paths. (The compose file has nothing Desktop-specific, but only native Engine is tested.) The daemon's minimum API version is 1.44. Proxy: `tecnativa/docker-socket-proxy:v0.5.0` with `CONTAINERS=1 POST=1` on an `internal` network that only the worker joins. Probed from the worker: `GET /containers/json` returns 200, `POST /containers/{id}/start` reaches the daemon, and images, networks, volumes, `POST /images/create` and `POST /exec/{id}/start` all return **403**. Caveat: `POST /containers/{id}/exec` (create) reaches the daemon because it sits under CONTAINERS, but the exec can never be *started*. The api container cannot reach the proxy at all. The sandbox image (`agentops-sandbox:py312`) builds and runs as uid 10001 with `--network none --read-only --cap-drop ALL`. docker-py 7.2.0 run kwargs (`network_mode`, `read_only`, `cap_drop`, `security_opt`, `tmpfs`, `mem_limit`, `nano_cpus`, `pids_limit`, `user`) are confirmed in its signature. Actually running containers from the worker is Phase 3.
**Update (Phase 3).** Built as specified: create → start → wait → logs → kill → remove (never exec; AST-tested), `network_mode=none`, uid 10001, `cap_drop=ALL`, `no-new-privileges`, read-only root, tmpfs `/tmp` 64 MB, 512 MB memory with no extra swap, 1 CPU, 64 PIDs, an **empty environment**, json-file logs capped at 1 MB, wall-clock timeout then kill + forced removal, output truncated at 20k chars. Isolation is tested against a real daemon (marker `sandbox`, own CI job). Workspace mounting: see ADR-031.

## ADR-012 · Auth and tenancy (kept small) · accepted
**Decision.** Seeded users with bearer API tokens (hashed in DB), each with a role: `user` or `reviewer`. Every task, approval, memory and event query filters by owner. The reviewer role is required to decide approvals. No OAuth.
**Why.** The brief requires isolation between users. Full auth would be scope creep.

## ADR-013 · Frontend · accepted
**Decision.** React + Vite + TypeScript, Tailwind, TanStack Query, SSE for live progress. Plan DAG drawn with React Flow (or similar). Pages: New task · Task detail (plan DAG, live agent activity, results, deliverable) · Approvals inbox · Trace/timeline + cost · Memory browser (search, inspect, delete) · Replay (fork from a checkpoint, diff branches) · Task history.
**Rule.** No hardcoded demo data. Fake mode drives the UI through the real backend.

## ADR-014 · Metrics integrity · accepted
**Decision.** The case study's numbers (82% success, $0.21/task, 87% reviewer recall, 14% escalation, loop-rate drop, memory lift, etc.) are **targets only**. They must not appear in the README, UI, or docs. All reported metrics come from `evals/` runs, each with its date, model config, scenario count, runs per scenario, and a Wilson 95% CI.

## ADR-015 · Replay · verified (with a caveat)
**Decision.** Use checkpoint history (`aget_state_history`), fork with `aupdate_state` at a chosen checkpoint, and continue on the fork. The UI shows the branches side by side. For deterministic replay, recorded LLM/tool responses (from `llm_calls` / `tool_calls`) are served up to the fork point.
**Verify.** Current fork/time-travel API and how forked checkpoints are identified.
**Verified 2026-11-05.** `aget_state_history(cfg)` yields snapshots newest first, each with `config.configurable.checkpoint_id`, `next`, `metadata.step` and `metadata.source` (`input` / `loop` / `update`). `aupdate_state(snapshot.config, values)` writes a new checkpoint **on the same thread_id**, with the old one as its parent, and returns its config. `ainvoke(None, fork_cfg)` continues from there. **Caveat:** the fork becomes the thread's latest checkpoint, so a plain `aget_state({"thread_id"})` now shows the fork, not the original run. Phase 5 replay must address branches by `checkpoint_id` (and record each branch's head in our DB), or copy the fork into a new replay thread. Decide in Phase 5.

## ADR-016 · Exact dependency pins · accepted
**Decision.** Every runtime dependency is pinned exactly in `pyproject.toml`, and `uv.lock` is committed. Phase 1 set (checked on PyPI 2026-11-05): langgraph 1.2.20, langgraph-checkpoint-postgres 3.2.0, langchain-core 1.7.6, SQLAlchemy 2.1.8, psycopg 3.3.8, alembic 1.20.1, pgvector 0.5.1, FastAPI 0.162.0, sse-starlette 3.6.0, pydantic 2.14.2. Images: `pgvector/pgvector:0.8.7-pg17`, `axllent/mailpit:v1.33`, `tecnativa/docker-socket-proxy:v0.5.0`, `jaegertracing/jaeger:2.22.0`.
**Why.** LangGraph shipped 1.2.20 on the day this was written. The case study's snippets predate 1.x.
**Revisit if.** A phase needs a newer API. Then bump deliberately and re-run the suite.

## ADR-017 · One Postgres driver: psycopg 3 · accepted
**Decision.** SQLAlchemy async (`postgresql+psycopg://`), the LangGraph checkpointer and LISTEN/NOTIFY all use psycopg 3. No asyncpg. The checkpointer gets its own `psycopg_pool.AsyncConnectionPool` with `autocommit=True, prepare_threshold=0, row_factory=dict_row`, the settings `from_conn_string` uses. A pool works (verified). `setup()` is idempotent and uses `CREATE INDEX CONCURRENTLY`, so it runs outside a transaction: `python -m agentops.db.checkpointer`, called by `make migrate`, the compose `migrate` service and worker startup.
**Why.** The checkpointer requires psycopg, so one driver means one set of connection semantics to understand.

## ADR-018 · API tokens: HMAC-SHA256 with a server pepper · accepted
**Decision.** Tokens are random 256-bit strings, stored as `HMAC-SHA256(API_TOKEN_PEPPER, token)` hex, with a unique index for lookup. `make seed` writes generated tokens to `.agentops-tokens` (mode 0600, gitignored), never to stdout or container logs. Settings refuse the dev pepper when `ENV=prod`.
**Why.** For high-entropy random tokens a slow password hash adds nothing, and a keyed hash allows indexed lookup.

## ADR-019 · Jaeger v2 only; no separate OTel collector · accepted
**Decision.** The `observability` compose profile runs `jaegertracing/jaeger:2.22.0`, which receives OTLP directly. The case study's separate collector is dropped.
**Revisit if.** We need tail sampling, redaction processors, or multiple exporters.

## ADR-020 · Compose and dev ergonomics · accepted
**Decision.** (1) A one-shot `migrate` service (alembic + checkpointer setup) runs before `api`/`worker` start, so `make up` alone yields a working stack. `make migrate` is idempotent and also seeds users. (2) All host ports bind to 127.0.0.1 and are configurable. Postgres defaults to **5433** because 5432/8080 are often taken (they are on the dev machine). (3) Tests use a separate `agentops_test` database, recreated per session with the real migration.

## ADR-021 · Per-run dependencies go in LangGraph runtime context · accepted
**Decision.** Nodes get non-serializable dependencies (DB engine, later the ModelClient and tool registry) through `StateGraph(..., context_schema=RunContext)` and `ainvoke(..., context=…)`, read as `runtime.context`. They do not go through `configurable`.
**Why.** Verified that context is not persisted in checkpoint metadata. It keeps checkpoints free of secrets and handles, and lets a resumed run inject fresh ones.

## ADR-022 · Worker run-segment semantics · accepted
**Decision.** A worker claims a job, takes the per-thread advisory lock on a dedicated AUTOCOMMIT connection, heartbeats every `JOB_HEARTBEAT_SECONDS`, and invokes the graph with the initial input only if the thread has no checkpoint (otherwise `None`, which continues). A lock it can't get means the job is released without consuming an attempt. A lost lease cancels the run. A task already in a terminal state makes the job a no-op. A job whose lease expires on its final attempt is reaped to `failed`, and its task ends `failed`, so it cannot hang forever. The SSE stream closes after `task.finished`.
**Update (Phase 2).** The Phase 1 limitation (a node re-run after a crash emitted its "started" event twice) is fixed by ADR-025. Events are still not part of the checkpoint transaction, but re-emitting is now a no-op. A lost lease cancels the run only when the heartbeat itself saw the loss; any other cancellation (shutdown, a caller's timeout) propagates (tested).

## ADR-023 · Repo layout deviations · accepted
**Decision.** As in KICKOFF_PROMPT, with these differences: `core/graph.py` is a graph registry that holds only the `demo` durability probe in Phase 1. `scheduler.py`, `escalation.py`, `nodes/*`, `tools/*`, `memory/*`, `observability/{tracing,cost,replay}.py` are **not created** until their phase (no placeholder code). `mcp`, `langchain-mcp-adapters`, `fastembed` and `docker` were verified but are added as dependencies in the phase that uses them.
**Update (Phase 2).** Created: `core/scheduler.py`, `core/prompts.py`, `core/testhooks.py`, `core/nodes/{intake,plan,schedule,subtask,escalation,synthesize,finalize,common}.py`, `models/{anthropic,scenarios}.py`. Added dependency: `langchain-anthropic==1.7.10` (pulls `anthropic` 2.6.0).
**Update (Phase 3).** Created: `tools/{spec,registry,framing,ratelimit,catalog,mcp,sandbox}.py`, `tools/native/{workspace,sql,web,python_tool,email}.py` (+ `fixtures/web/`), `mcp_servers/notes/`, `config/mcp_servers.toml`, migrations `0003` and `0004`. Added (exact pins): `sqlglot==30.29.0`, `readability-lxml==0.9` (+ lxml 6.1.3, lxml-html-clean 0.4.6, chardet 5.2.0, cssselect 1.3.0), `docker==7.2.0`, `mcp==2.3.0` (+ mcp-types, jsonschema, pyjwt, cryptography, python-multipart). mypy ignores missing types only for readability/lxml/docker.

## ADR-024 · Budget ledger: reserve worst case, settle actual · accepted
**Decision.** The source of truth for spend is a `task_budgets` row per task (limits, used, reserved) plus one `budget_reservations` row per model attempt. Before every attempt (retries and structured re-asks included) the ModelClient reserves the attempt's **worst case**: exact input tokens (`get_num_tokens_from_messages`; the fake model counts with the same function that produces its usage, and in real mode ChatAnthropic calls the free count_tokens API) plus `max_output_tokens`, priced from the price table. The reservation is one atomic `UPDATE … WHERE used + reserved + worst <= max`, so it is either granted or the call never happens (`BudgetExceededError`). After the call, `settle` swaps the reservation for actual usage. The task row is created with the task (`POST /tasks`).
**Why.** Phase 1 compared *consumed* spend before a call, so one call could overshoot. A counter in graph state also can't stop parallel branches (Phase 3) from each spending the same remainder, because every branch gets its own copy of state. A row-locked UPDATE can.
**Crash between reserve and settle.** The reservation stays open. The next run segment holds the thread's advisory lock, so nothing else is spending for the task, and charges open reservations at their **worst case** (`orphaned`). The provider may have billed the call, so over-counting is the honest direction. A late settle from a zombie attempt does not double-count.
**No DB CHECK on used ≤ max.** Settle records actual usage even if it exceeds the reservation (that would be an estimation bug; it is logged). Rejecting it would lose a real charge.
**Precision.** `NUMERIC(18,10)`, exact numeric binds, reservations rounded *up* to that scale. Haiku-class input costs ~1e-7 USD/token, below `NUMERIC(12,6)`.
**Real-mode input margin.** `count_tokens` sees messages, system and tools, but not the structured-output schema (sent as `output_config.format`) or the thinking config. So in real mode the reserved input is `ceil(counted × 1.15) + 512` (`LLM_INPUT_MARGIN_RATIO` / `_TOKENS`). Each `llm.call` event records `reserved_usd` next to the actual cost, and the e2e test asserts actual ≤ reserved for every call.
**Real mode** refuses a model with no verified price (`ModelConfigError`), because its worst case can't be reserved. Fake mode still allows an unpriced model and records `cost_usd = NULL` + `unpriced_calls`.
**Trade-off.** Two extra small transactions per attempt, plus a token-count call in real mode (free, adds latency). Worst-case reservation is conservative: a task can be refused while its real spend is still a little under the limit.

## ADR-025 · Idempotent node events · accepted
**Decision.** `events.dedupe_key` with a partial unique index `(task_id, dedupe_key) WHERE dedupe_key IS NOT NULL`. `emit_on` does `INSERT … ON CONFLICT DO NOTHING`; on a conflict it sends no NOTIFY. Node events build their key with `node_key(node, event, *coordinates)` from **checkpointed state** (plan attempt, wave, subtask id, revision), so a node re-run after a crash or a resume produces the same key. Events that legitimately repeat (`run.segment_started`, `task.status`, `llm.call`) use `NULL`.
**Why.** LangGraph re-runs an interrupted node from its first line (ADR-003 verify). Events aren't in the checkpoint transaction, so without a key the stream showed duplicates.
**Trade-off / caveat.** The first emission wins: a re-run's payload (e.g. a new pid) is dropped. Phase 5 replay forks run the same nodes again on purpose, so fork events will need a branch id in the key.

## ADR-026 · Phase 2 graph subset; terminal-outcome contract; failure policy · accepted
**Decision.** The `main` graph is built with the real shape from the corrected workflow: a compiled `subtask_worker` subgraph per `Send` (ADR-001), a serial `schedule` node that sees only terminal results and stores a `ScheduleDecision` the router reads (ADR-002), a serial `escalation_gate` that calls `interrupt()`, and terminal nodes. Not built yet, and inserted later between existing nodes without rewriting: `recall` (after intake), `plan_gate` (after plan), `action_gate` + `execute_actions` (before synthesize), `final_review` + `consolidate` (after synthesize). Phase 2 enables one specialist (`writing`); plan validation rejects others.
**Terminal contract.** Every path to END sets `outcome` (completed / failed / rejected). The worker maps `BudgetExceededError` → `budget_exceeded`, `GraphRecursionError` → `failed`, and a run that ends **without** an outcome → `failed` ("graph ended without an outcome"). Each case is tested, and each emits exactly one `task.finished`. An interrupted run is `awaiting_approval` (not terminal). A `resume` job with `{"resume": <EscalationDecision>}` continues it; the approvals table, API and inbox are Phase 4.
**Cycle bounds.** Re-plan: 2 plan attempts. Revise loop: 2 revisions (≤ 3 specialist + 3 review calls), counter in subgraph state; the routing is property-tested over all 81 four-verdict sequences. Schedule loop: dispatch waves ≤ number of subtasks. Escalation rounds: each resolves every needs_human subtask.
**Failure policy.** ~~Once any subtask is `failed`, `schedule` fails the run immediately.~~ Superseded by ADR-029 (Phase 3). Ready work runs before escalation; escalation happens only when nothing else can run.
**`escalation.requested` is emitted by `schedule`**, not by the gate, so the gate does only pure work before `interrupt()`. The reviewer never interrupts; parallel-branch code is checked by an AST test for any reference to `interrupt`.
**Revisit if.** Partial results become useful (Phase 4 could offer `completed_partial` via a human decision instead of failing).

## ADR-027 · Fake scripts keyed by role + purpose; models see their own prior turns · accepted
**Decision.** `FakeScript` keys are `role:purpose:scenario`, with fallback (purpose, scenario) → (*, scenario) → (purpose, *) → (*, *). Purposes: `plan`, `synthesize`, `draft`, `review`. Prompts put a model's own earlier answers in the conversation as AI turns: the planner sees its rejected plan, the writer its previous draft, and the reviewer its earlier verdicts. So the fake model's turn index equals the plan attempt / revision, deterministically and statelessly, and real models get the history they need to improve. `ScriptedChatModel.get_num_tokens_from_messages` returns exactly the input usage it will report, and output usage is capped at `max_tokens` like a provider's.
**Why.** The supervisor both plans and synthesizes, both at turn 0, so they collided under (role, scenario, turn) keys. The reviewer was turn 0 on every call, so "revise once, then accept" couldn't be scripted.
**Update (Phase 3).** Purposes are qualified: `plan/p2`, `draft/r1/<subtask>`, `review/r0/<subtask>`, `act/…`. Lookup tries the scenario's own scripts first (most specific purpose first, through every subset of qualifiers), then the shared `*` defaults the same way. The turn index is now *AI messages since the last human message*: every new attempt (re-plan, revision, review) starts with a human message at turn 0, and ReAct steps within an attempt (AI → tool result → AI) are turns 1, 2… ModelClient's structured re-ask messages are tagged `name="reask"` and continue the attempt. `llm_calls.purpose` widened to 64 chars. Reports group by the base purpose (`split_part(purpose, '/', 1)`).

## ADR-028 · Checkpoint serializer msgpack allowlist · accepted
**Decision.** `open_checkpointer` builds `JsonPlusSerializer(allowed_msgpack_modules=[…])` with exactly the Pydantic classes in `core/state.py::CHECKPOINT_TYPES`. New state classes must be added there; a test run fails loudly otherwise (they come back as dicts).
**Why.** langgraph 1.2.20 logs, for any Pydantic class stored in a checkpoint: "Deserializing unregistered type … This will be blocked in a future version." Registering our types now means a LangGraph bump can't silently break resume, and anything unexpected in a checkpoint is refused rather than imported (verified: an unregistered model is blocked and degrades to a dict).

## ADR-029 · Failures don't stop independent work; partial results need a final review · accepted
**Decision.** Replaces ADR-026's failure policy. `decide` marks every pending subtask that depends, directly or transitively, on a `failed` or `blocked` subtask as **`blocked`** (a new terminal status, set only by the scheduler, written by `schedule` with a `subtask.blocked` event). Independent subtasks keep running, and escalation still happens. When everything is terminal:
- **≥ 1 accepted** → `partial` → `synthesize` writes the body from accepted outputs only, then appends a **"What's missing" section generated from state** (each failed/blocked subtask, its reason, and its expected output). This is deterministic, not left to the model. Then `final_review` (reviewer role) scores the partial deliverable: **score ≥ `FINAL_REVIEW_MIN_SCORE` (default 3) → `completed_partial`**, otherwise → `failed` (the deliverable stays in the result). A failed review call → `failed`.
- **nothing accepted** → `failed` ("no subtask was accepted"), without synthesis.
Complete runs (all accepted) skip `final_review` in Phase 3. The DECISIONS graph also reviews complete deliverables, with low scores going to escalation; that arrives with the Phase 4 gates.
**Amended (Phase 4, user decision).** `final_review` now scores **every** deliverable, and a score below `FINAL_REVIEW_MIN_SCORE` goes to `escalation_gate` (final-review mode) for complete and partial deliverables alike, instead of ending partial runs `failed`. A human can approve (→ `completed` / `completed_partial` per `delivery_outcome`), edit the deliverable, take over, or reject (→ `rejected`); expiry → reject. Unperformed actions also make a run `completed_partial` and are listed in the "What's missing" section.
**Why.** A failed side branch shouldn't throw away finished, independent work. But a partial result is only worth returning if it says plainly what's missing and a reviewer judges it still useful.
**Tests.** Unit: blocking only dependents, transitive blocking through blocked, independent sibling dispatched, partial vs nothing-accepted, escalation alongside a failure, plus the combination sweep with `blocked`. Integration: `partial_ok` → completed_partial (with the missing section and blocked event), `partial_low_score` → failed, `specialist_error` → failed without synthesis, failure + escalation + approve → completed_partial.

## ADR-030 · Tool registry: one path to every tool; risk × scope decides execute vs propose · accepted
**Decision.** `ToolRegistry.invoke` is the only caller of a tool function (static AST test). Pipeline: (1) allowed agent? else `denied`, with the same message whether or not the tool exists; (2) Pydantic validation, input models use `extra="forbid"`; errors go back to the model as the tool result (`validation_error`) so it can self-correct; (3) risk policy (table below); (4) Postgres token bucket per `tool:owner` (one atomic refill-and-take `UPDATE`); (5) timeout; any exception becomes an `error` result, never a graph crash; (6) a `tool_calls` row (status, revision, step, model tool_call_id) and a `tool.call` event for every outcome.

| risk | scope | specialist call |
|---|---|---|
| read | any | executes |
| write | `task_workspace` or `sandbox` | executes (affects nothing outside the task) |
| write | `outside` | **ProposedAction**, not executed |
| external / destructive | any | **ProposedAction**, not executed |

**ProposedAction** = tool + validated args + risk + agent + subtask + revision + rationale, keyed by `sha256(task, tool, canonical args)`. Proposals live in graph state under a reducer that keeps the **first** proposal per hash, and they appear in the task result. Execution is Phase 4 (`action_gate` → `execute_actions`, behind `side_effects`). Dedupe, as tested: a crash re-run commits one proposal because LangGraph discards a killed node's writes; repeated proposals that *are* committed (later revisions, parallel subtasks) collapse via the reducer (mutation-checked).
**Specialist loop.** Each attempt is a ReAct loop (specialist ⇄ tools) with a tool-call cap (`BUDGET__MAX_STEPS_PER_SPECIALIST`). Exceeding the cap ends the attempt as a failed attempt that counts toward revisions; it never raises. The reviewer sees the attempt's tool log **and the dependency context** the specialist had (the context part was a bug found by the Phase 3 real run, see PROGRESS).
**Why.** One choke point makes "never executes without approval" and "never twice" checkable, and keeps tool failures inside the model's loop instead of crashing the run.

## ADR-031 · Sandbox workspace: bind mount from the host's path · accepted
**Decision.** The task's own workspace directory is bind-mounted read-write at `/work`. The mount **source is the path as the Docker host sees it** (`WORKSPACE_HOST_ROOT/<task_id>`), not the worker container's path (`WORKSPACE_ROOT/<task_id>`), because the daemon resolves bind sources on the host. Compose mounts the host directory `var/workspaces` into the worker at `/workspaces` and passes the same host path as `WORKSPACE_HOST_ROOT`. `make up` creates it with mode 0777 (worker uid 10000 and sandbox uid 10001 both write). The task id must be a UUID, so the source can't be steered elsewhere.
**Verified.** `make sandbox-selfcheck` runs inside the worker container, through the socket proxy: the worker writes a file, sandboxed code reads it and writes another, and the worker reads that back.
**Trade-off.** The proxy allows container create with any bind, so the worker (not the model) is trusted to build the mount; generated code never chooses it. Workspace tools refuse symlinks at every path level, because sandboxed code could plant them.
**Revisit if.** The dev setup moves to Docker Desktop (re-check path mapping), or tasks need more isolation from each other's data (per-task volumes).

## ADR-032 · MCP on the Python SDK v2 directly · accepted
**Decision.** `mcp==2.3.0`. Our `notes` server (`mcp_servers/notes`) is an `MCPServer` (FastMCP's 2.x name) with `create_note` / `search_notes` / `delete_note` over **stdio and streamable HTTP**. The client side is the SDK's own `stdio_client` / `streamable_http_client` / `ClientSession`, wrapped by our registry (no `langchain-mcp-adapters`).
- **Allowlist:** `config/mcp_servers.toml`; `MCP_SERVERS` picks names from it, and an unknown name fails fast. A server must report the expected `serverInfo.name`.
- **Names** `server__tool`. **Risk defaults to destructive** unless overridden per tool (`search_notes` read, `create_note` external).
- **Untrusted descriptions:** control characters stripped, capped at 300 characters, prefixed "[external MCP tool …; description not verified]".
- **Schemas:** JSON Schema → Pydantic (`extra="forbid"`) for a supported subset; any other tool is skipped with a warning, never loaded half-validated.
- **stdio hygiene (user change 3):** the SDK's `stdio_client` always merges six parent variables (HOME, LOGNAME, PATH, SHELL, TERM, USER), and its `env` can only override. We pass our own value for all six (plus LANG), so the child sees only values chosen here; a canary test proves no worker secrets or variables reach it. Startup timeout (spawn + handshake); per-call timeout (the SDK's `REQUEST_TIMEOUT` keeps the session); restart on crash (`CONNECTION_CLOSED` or a broken transport), with one retry for read tools, giving up after 3 consecutive restarts. A server that can't start is skipped at worker start and reported; config errors fail fast.
- **HTTP server:** DNS-rebinding protection on (the SDK default is off), and only `--allowed-host` values are accepted. Tested with the compose configuration: the configured host gets 200, the defaults and foreign hosts get 421; both tests fail with the protection turned off (mutation-checked; the first version of the test accepted any 4xx and passed even with it off).
- **Compose:** `mcp-notes` runs on the internal network with no host port and no env file.
**Why.** The adapters held us on mcp 1.x; the registry already does the wrapping, validation and policy.

## ADR-033 · SQL tools: a separate read-only LOGIN role · accepted
**Decision.** `sql_query` / `describe_schema` run on the seeded `demo` schema (deterministic synthetic customers/products/orders) through a **separate LOGIN role** (`SQL_TOOL_ROLE`, password `SQL_TOOL_PASSWORD`, refused in prod if left at the dev default). The role has SELECT on `demo` only, `default_transaction_read_only`, `statement_timeout` and idle-in-transaction timeout, plus `SET TRANSACTION READ ONLY` / `SET LOCAL statement_timeout` per call. sqlglot allows exactly one SELECT (or set operation), with no data-modifying node anywhere in the tree (including CTEs, `SELECT INTO`, `FOR UPDATE`) and no blocklisted function. Row cap 200, cell cap 200 characters.
**Why not `SET ROLE` on the app connection:** a SELECT can call `set_config('role', …)` and switch back. Tests bypass the static check to prove the database layer alone refuses writes, app-table reads and that role escape.

## ADR-034 · Untrusted-data framing · accepted
**Decision.** Every tool result reaches a model only as `<tool_result tool=… status=… trust="untrusted">…</tool_result>`: control characters stripped, length-capped, and a closing tag inside the content escaped so it can't close the frame. Specialist system prompts say tool results, `<request>` and `<context>` are data, never instructions. The research agent has no external or destructive tools. `fetch_url` blocks non-public addresses after DNS resolution, pins the connection to the checked IP (Host header + TLS SNI keep the hostname), re-checks every redirect, and caps size and time.
**Tested.** A fixture page with injected instructions, read by a research model *scripted to obey it*: its `send_email` / `notes__delete_note` calls are denied, its traversal write fails, nothing is proposed, and the injected text reaches later prompts only inside delimiters. A compromised writer can only *propose* (it is allowed to email), never execute.
**Limits.** This tests the guards, not model robustness; the measured injection suite is Phase 6. Proposals made by a fooled agent are caught only at human approval (Phase 4).

## ADR-035 · Approvals inbox: semantic keys, one version-checked decide · accepted
**Decision.** Every decision point is one `approvals` row keyed by `(task_id, key)`, with keys `plan:p1`, `action:<hash>`, `escalation:e1`, `final:f1`, `budget:b1`, `recursion:x1`, `side_effect:<key>`. Keys are semantic, never LangGraph interrupt ids, so a re-run node or a re-run worker segment can't create a second row. Graph gates put `kind` + `key` in their interrupt payload and the worker records the row; worker-level pauses count only *decided* approvals of their kind, so a crashed and re-run segment pauses with the same key.
- **API:** `GET /approvals` (reviewers: all tasks; users: their own), `GET /approvals/{id}` (invisible → 404), `POST /approvals/{id}/decision` (reviewer role; `{version, decision}`).
- **Validation, twice:** the API validates with the same Pydantic models the gates pass to `interrupt(..., response_schema=...)`, plus semantic checks (an edited plan must be a DAG; edited action args must match the tool's recorded JSON schema; a budget raise must exceed the current limit and stay ≤ `BUDGET_MAX_RAISE_USD`). Invalid → 422, nothing written. Verified: LangGraph validates the resume value and raises *inside the node* if it's invalid, so the API check is what keeps bad input out.
- **One transition path:** `approvals.decide` is a single `UPDATE … WHERE id AND version = :v AND status = 'pending'`. The API and the expiry sweep both use it, so two reviewers, or a reviewer and an expiry, can't both win (tested; the loser gets 409). The winner's transaction also enqueues the `resume` job (or, for `side_effect_review`, only updates the side_effects row).
- **Expiry:** `APPROVAL_TTL_SECONDS` (24 h). The worker loop sweeps overdue *blocking* approvals into `expired` with a system reject. Non-blocking review items never expire.

## ADR-036 · plan_gate: structural confidence · accepted
**Decision.** `effective = min(self_reported, structural)`, where `structural = 1 − 0.15·#high − 0.05·#medium − 0.10·max(0, depth − 2) − 0.10·(1 − best_memory_similarity)`, clamped to [0, 1]. Below `PLAN_CONFIDENCE_THRESHOLD` (0.6) → `approve_plan` (approve / edit, re-validated as a DAG / reject → `rejected` / take_over → the human's deliverable, `completed`). The confidence is computed and evented in `plan`, so `plan_gate` is pure before `interrupt()`. The unfamiliarity penalty is capped at 0.1, so a brand-new user's simple plan passes.
**Trade-off.** The weights are judgement, not fitted. Phase 6 evals can tune them against escalation outcomes.

## ADR-037 · action_gate and execute_actions · accepted
**Decision.** `action_gate` considers ProposedActions from **accepted** subtasks only (others are recorded `not_considered`), one approval per action hash, asked in turn (the loop is bounded by the number of actions). Decisions: approve, edit (args re-validated by the tool), reject (not executed), take_over (a human did it; not executed). `execute_actions` then follows ADR-004 as updated. Any action that didn't end `done` or `human_done` makes the run `completed_partial`.
**Invariant kept.** Tool functions run only inside the registry (`invoke` for specialists, `execute_approved` for approved actions); the AST test still holds.

## ADR-038 · Long-term memory: recall, screened consolidation, API · accepted
**Decision.**
- **Embeddings:** `fastembed==0.9.1` (`BAAI/bge-small-en-v1.5`, 384 dims). Vetted before install: the Qdrant team's pure wheel with no hooks or telemetry; network only for model download; onnxruntime's subprocess/network code is in its conversion tooling, not the inference path. The model is baked into the image at build (`FASTEMBED_CACHE_PATH=/opt/fastembed`), and the runtime sets `HF_HUB_OFFLINE=1`. Tests use a deterministic bag-of-words hash embedder; one `model`-marked test checks the real one (related 0.735 vs unrelated 0.385 on a probe pair, which is why `MEMORY_MIN_SIMILARITY` defaults to 0.6).
- **recall** (after intake): `WHERE user_id` only; drop below the minimum similarity; rank by `α·sim + β·importance + γ·recency` (recency = 0.5^(age/half-life)); keep `MEMORY_TOP_K`. Memories reach the planner inside `<memory note="past example, may not apply">` blocks, and the best similarity feeds plan confidence.
- **consolidate** (on every terminal path through the graph):
  - always one *episode* (request + outcome, no LLM)
  - plus at most 3 lessons/preferences/facts proposed by one supervisor call from reviewer-accepted outputs, the outcome and human decisions
  - candidates are dropped if they share any 8-word sequence with a tool output of the task (`tool_calls.output_text`, full capped output) or read like an instruction to an agent
  - every memory carries `source_task_id` and the run's outcome
  - if the call can't fit the ledger, or the model fails, only the episode is written (no overshoot, no pause)
  - worker-level terminals (budget/recursion decisions, a graph without an outcome) write their episode from the worker
  - writes are idempotent per (task, kind, text)
- **API:** `GET /memories`, `GET /memories/search?q=`, `GET /memories/{id}`, `DELETE /memories/{id}`, `DELETE /memories` (one transaction). Owner-only; another user's memory → 404.
**Limits.** Screening is heuristic: it stops copied tool text and obvious instructions, not a paraphrase. `access_count` can double-count when recall re-runs after a crash.

## ADR-039 · Worker-level pauses; redelivered resume jobs continue · accepted
**Decision.**
- **Budget:** budget exhaustion can happen inside a parallel branch, where `interrupt()` is forbidden. So `BudgetExceededError` reaches the worker, which records a `budget` approval (source `worker`) and sets `awaiting_approval`. Approve raises any of max_usd / max_llm_calls / max_tokens and continues from the last checkpoint; reject or expiry → `budget_exceeded` (with an episode memory). One `budget.warning` event per task at 80%.
- **Recursion:** `GraphRecursionError` → `recursion` approval: continue, take_over (→ `completed` with the human's deliverable) or reject (→ `failed`).
- **Resume edge case:** a `resume` job whose interrupt was already consumed (its worker died later in the same job and the job was redelivered) **continues from the checkpoint** instead of being dropped. The Phase 4 durability matrix found this: before the fix, such a task stayed `running` forever.

---

## Deferred (not in scope unless time allows)
- Nightly memory consolidation clustering (do a simple version: dedupe near-identical memories)
- gVisor runtime, warm sandbox pool
- Kubernetes manifests
- Learned escalation policy
