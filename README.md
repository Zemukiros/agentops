# AgentOps

Durable multi-agent orchestration with a human in the loop. A supervisor plans, specialist
agents (research, analysis, code, writing) work in parallel waves, a reviewer checks every
result, and nothing leaves the system (an email, a note) until a human approves it. Runs
survive worker crashes, never repeat a side effect, stay inside a hard cost budget, and can be
replayed from any checkpoint.

Built on LangGraph with Postgres doing the heavy lifting: checkpoints, a job queue, an event
log streamed to the UI over SSE, a budget ledger, and pgvector memory. React UI, Docker
sandbox for generated code, MCP tools, and an eval harness that measures the whole system
with real models.

**Status:** Completed locally (all six phases built; Phase 6 is in review as PR #6). See
[`docs/PROGRESS.md`](docs/PROGRESS.md).

```mermaid
flowchart LR
  U([User]) --> UI[React UI] --> API[FastAPI]
  API --> PG[("Postgres<br/>checkpoints · queue · events<br/>ledger · pgvector")]
  W[LangGraph workers] --> PG
  W --> P[plan] --> S[parallel specialists<br/>+ reviewer] --> G{{human approval}} --> X[execute once<br/>idempotent] --> D[deliverable]
  S -.-> SB[Docker sandbox]
  S -.-> T[tools · MCP]
```

## Measured results

From the eval harness (`evals/`): 60 scenarios × 3 runs with real models (Claude Sonnet 5.5 and
Haiku 5.5 as agents, Claude Opus 5.5 as an independent judge). This table is generated from the
committed results; methodology, per-category numbers, the fixes the eval led to and the
re-measure are in [`docs/EVALUATION.md`](docs/EVALUATION.md).

<!-- eval-table:headline -->
Baseline: first paid run `2026-11-10-163339-real` + `2026-11-10-172318-real` (the 3 runs its cost reservation skipped). Judge `claude-opus-5-5`. Wilson 95% intervals.

| Metric | Baseline |
|---|---|
| Pass rate, all 60 scenarios (k=3) | 61.1% (110/180; 95% CI 53.8%–67.9%) |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall (planted defects) | 100.0% / 100.0% (tp 18, fp 0, fn 0, tn 12) |
| Escalation rate (agentic runs) | 40.9% (54/132; 95% CI 32.9%–49.4%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/198; 95% CI 0.0%–1.9%) |
| Memory lift (pass rate, memory on − off) | +11.1 pts (n=18 pairs; bootstrap 95% CI +0.0 to +27.8) |
| Cost per agentic run, p50 / p95 | $0.0473 / $0.1200 |
| Latency per agentic run, p50 / p95 | 35.9 s / 100.3 s |
| Eval spend (agents + judge) | $13.3857 |
<!-- /eval-table:headline -->

The baseline exposed three problems. The planner couldn't see which specialist owns the
database or the email tool (ADR-047), and remembered preferences never reached the final text
(ADR-048). Both are fixed and re-measured on the scenarios they target:

<!-- eval-table:after -->
| Re-measured after the fixes | Baseline | After fixes |
|---|---|---|
| The 21 scenarios the fixes target | 9.5% (6/63; 95% CI 4.4%–19.3%) | 75.0% (39/52; 95% CI 61.8%–84.8%) |
| Memory lift (on − off) | +11.1 pts (n=18 pairs; bootstrap 95% CI +0.0 to +27.8) | +80.0 pts (n=5 pairs; bootstrap 95% CI +40.0 to +100.0) |
| Unsafe actions | 0 | 0 |
<!-- /eval-table:after -->

The re-measure covers only the targeted scenarios, and the memory evidence is k=1 (5 pairs).
Per-scenario detail and caveats are in `docs/EVALUATION.md`. A real-model walkthrough through
the UI ($0.0764 under a $0.20 cap) completed with one escalation and one approved email, sent
exactly once. It also exposed a deliverable that misreported that email, now fixed (ADR-052).

## Gallery

| | |
|---|---|
| ![Task detail](docs/screenshots/task-completed-light-1440.png) | ![Approvals inbox](docs/screenshots/approvals-dark-1440.png) |
| Task detail: live plan DAG, reviews, approved email, deliverable | Approvals inbox: edit form generated from the tool's schema |
| ![Trace and cost](docs/screenshots/trace-cost-light-1440.png) | ![Replay compare](docs/screenshots/compare-light-1440.png) |
| Trace & cost: compute vs human wait, per-agent cost, timeline | Replay: fork at a checkpoint, compare branches |

Every page in light and dark, desktop and mobile: [`docs/screenshots/`](docs/screenshots/).
The gallery shows real API data from a fake-mode run. A real-model run (Claude Sonnet and Haiku)
is in [`real-completed`](docs/screenshots/real-completed-1440.png) and
[`real-trace-cost`](docs/screenshots/real-trace-cost-1440.png).

## Quick start

Linux, or Windows with WSL2 (Docker Engine inside WSL; tested). macOS with Docker Desktop or
Colima should work but is untested; see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

```bash
cp .env.example .env
uv sync
make build-sandbox   # image for generated code (never a service)
make up              # postgres, mailpit, docker-socket-proxy, mcp-notes, api, worker, ui
make migrate         # schema + checkpointer + seed users (tokens -> .agentops-tokens)
```
Open http://localhost:5173, sign in with `demo-user` from `.agentops-tokens`, and run the
`ui_demo` scenario. [`docs/DEMO.md`](docs/DEMO.md) is a 5-minute walkthrough. Fake mode
(scripted models, $0) is the default; real models need `LLM_MODE=real` and an Anthropic key.

```bash
make test        # backend: unit + integration + sandbox, fake models, no API keys
make test-ui     # UI: ESLint + tsc + Vitest
make e2e         # Playwright walkthrough + axe against the running stack
make eval        # free: replay recorded model cassettes + dry run of all 60 scenarios
```

## How it holds up

- **Never acts without approval, never twice.** Specialists can only *propose* external
  actions. One serial node executes approved ones, behind an idempotency key that is also
  the email's `Message-ID`. SIGKILL tests cover every step of the send path.
- **Crash-safe.** Workers hold leases. A killed worker's task resumes from its last checkpoint
  on another worker.
- **Bounded.** Every loop has a counter cap, and every run ends in exactly one terminal state.
  Every model call reserves its worst-case cost first, so a task can't overspend its budget.
- **Injection-aware.** Tool output is framed as untrusted data, outbound actions need a human,
  `fetch_url` can't exfiltrate through invented URLs, and memories can't become standing orders.
  [`docs/SECURITY.md`](docs/SECURITY.md) has the threat model.

## Limitations

- The re-measure after the fixes covers only the scenarios they target (previously passing
  scenarios weren't re-run), and the memory evidence is k=1. ADR-052 is tested in fake mode only.
- Fixture web pages and a synthetic demo database, not the live web.
- Auth is seeded bearer tokens with two roles (no OAuth/SSO, no token expiry).
- The compose files have no TLS or backups; production needs a reverse proxy and `pg_dump`.
- Tested on Docker Engine in WSL2 only.

## Docs

[Architecture](docs/ARCHITECTURE.md) · [Evaluation](docs/EVALUATION.md) ·
[Security](docs/SECURITY.md) · [Deployment](docs/DEPLOYMENT.md) · [Demo](docs/DEMO.md) ·
[Decisions (ADR-001…050)](docs/DECISIONS.md) · [Progress](docs/PROGRESS.md) ·
[Requirements](docs/BRIEF.md)
