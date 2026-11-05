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

## ADR-009 · Model layer · accepted
**Decision.** A `ModelClient` wrapper around LangChain chat models, configured per role (supervisor, research, analysis, writing, code, reviewer). The wrapper is the only path to a model. It enforces the budget **before** each call, records tokens/cost/latency, and retries with backoff.
`LLM_MODE=fake`: a deterministic scripted model (keyed by role + scenario) produces plans, tool calls and reviews, so the whole graph and UI run with no API key. Reviewer uses a different model than the specialist when real models are configured.
**Verify.** Current model IDs and pricing for whatever provider(s) are configured. Never hardcode prices from memory.
**Verified 2026-11-05 (langchain-core 1.7.6).** The built-in fake chat models (`GenericFakeChatModel`, `FakeMessagesListChatModel`) do **not** implement `bind_tools`, and `BaseChatModel.with_structured_output` is built on `bind_tools(tool_choice="any")`. So we wrote `models/fake.py::ScriptedChatModel`. It is stateless: the response is keyed by (role, scenario, turn = number of AI messages in the input), so a resumed run on another process gets the same answers. Structured output goes through `with_structured_output(schema, include_raw=True)`, which keeps the raw message (for token usage) and the parsing error (for a recorded re-ask). Real-model prices in `models/pricing.py` are `None` / `verified=False`. An unpriced call records `cost_usd = NULL` and increments `BudgetState.unpriced_calls`. It is never guessed.

## ADR-010 · Embeddings · accepted
**Decision.** `fastembed` (local ONNX model, no API key) for real mode. A deterministic hash embedder for unit tests. The embedding dimension is fixed in config and in the migration.
**Verify.** The current fastembed default model and its dimension.
**Verified 2026-11-05 (fastembed 0.9.1).** The default is `BAAI/bge-small-en-v1.5`, dimension **384**, read from `TextEmbedding.list_supported_models()` without downloading the model. `settings.EMBEDDING_DIM = 384` is used by the migration, and `Settings` refuses a different `embedding_dim`. fastembed is added as a dependency in Phase 4.

## ADR-011 · Code sandbox · accepted
**Decision.** One container per execution from a minimal `agentops-sandbox` image: `network_mode=none`, non-root, `cap_drop=ALL`, `no-new-privileges`, read-only root, tmpfs `/tmp`, memory/CPU/PID limits, wall-clock timeout with forced removal, output truncation, no env secrets. The worker reaches Docker only through a socket proxy that allows container create/start/wait/logs/remove.
**Windows/WSL2.** Docker Desktop with the WSL2 backend. The repo lives in the WSL filesystem (`~/…`), not `/mnt/c`. Bind-mounting the per-task workdir must work from the worker container, so check how paths map.
**Verify.** The docker-py parameters and the socket-proxy config, on Docker Desktop + WSL2.
**Verified 2026-11-05, partly.** This machine runs **native Docker Engine 29.1.3 inside WSL2 (Ubuntu 26.04)**, not Docker Desktop. The compose file targets both. Bind-mount paths are plain Linux paths here; Docker Desktop has to be re-checked in Phase 3. The daemon's minimum API version is 1.44. Proxy: `tecnativa/docker-socket-proxy:v0.5.0` with `CONTAINERS=1 POST=1` on an `internal` network that only the worker joins. Probed from the worker: `GET /containers/json` returns 200, `POST /containers/{id}/start` reaches the daemon, and images, networks, volumes, `POST /images/create` and `POST /exec/{id}/start` all return **403**. Caveat: `POST /containers/{id}/exec` (create) reaches the daemon because it sits under CONTAINERS, but the exec can never be *started*. The api container cannot reach the proxy at all. The sandbox image (`agentops-sandbox:py312`) builds and runs as uid 10001 with `--network none --read-only --cap-drop ALL`. docker-py 7.2.0 run kwargs (`network_mode`, `read_only`, `cap_drop`, `security_opt`, `tmpfs`, `mem_limit`, `nano_cpus`, `pids_limit`, `user`) are confirmed in its signature. Actually running containers from the worker is Phase 3.

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
**Known limitation.** Events emitted inside a node are not part of the checkpoint transaction. A node re-run after a crash emits its "started" event twice. The durability test asserts exactly this. Phase 4 side effects use the `side_effects` table instead.

## ADR-023 · Repo layout deviations · accepted
**Decision.** As in KICKOFF_PROMPT, with these differences: `core/graph.py` is a graph registry that holds only the `demo` durability probe in Phase 1. `scheduler.py`, `escalation.py`, `nodes/*`, `tools/*`, `memory/*`, `observability/{tracing,cost,replay}.py` are **not created** until their phase (no placeholder code). `mcp`, `langchain-mcp-adapters`, `fastembed` and `docker` were verified but are added as dependencies in the phase that uses them.

---

## Deferred (not in scope unless time allows)
- Nightly memory consolidation clustering (do a simple version: dedupe near-identical memories)
- gVisor runtime, warm sandbox pool
- Kubernetes manifests
- Learned escalation policy
