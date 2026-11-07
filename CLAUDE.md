# CLAUDE.md — AgentOps (multi-agent orchestration platform)

## Read first, every session
1. `docs/PROGRESS.md`: current phase, what's done, what's next, open risks.
2. `docs/DECISIONS.md`: architecture decisions. **Overrides the case study where they conflict.**
3. `docs/BRIEF.md`: requirements and success criteria.
4. `docs/15-agent-orchestration-system.md`: product/architecture reference (case study).

## Non-negotiable rules
- **No fabricated metrics.** The case study's numbers are targets. Report only numbers produced by `evals/`, with methodology.
- **Verify fast-moving APIs** (LangGraph, langgraph-checkpoint-postgres, mcp / FastMCP, langchain-mcp-adapters, docker-py, fastembed, provider SDKs) against installed versions or current docs before writing code that depends on them. Don't copy case-study snippets blindly.
- **Graph invariants** (DECISIONS.md, top section): no `interrupt()` or side effects in parallel branches; side effects only in `execute_actions` behind the `side_effects` table; every cycle is counter-bounded; every run ends in exactly one terminal state.
- **Never** send real external email, call paid APIs, or touch anything outside the repo during tests. Tests run in `LLM_MODE=fake` and `SEARCH_MODE=fixture` unless marked `e2e`/`eval`.
- No secrets in code, prompts, graph state, checkpoints, events, or logs. Settings use `SecretStr`.
- No placeholder/TODO implementations reported as done. If something is stubbed, say so in PROGRESS.md.
- Generated code runs only in the Docker sandbox, never via `subprocess`/`exec` in the worker.

## Workflow
- Work phase by phase (see PROGRESS.md). At the end of each phase: run the full test suite, update PROGRESS.md, add any new decisions to DECISIONS.md, then **stop and summarize** for review before starting the next phase.
- Prefer small, tested increments. Keep earlier phases passing.
- Record a new ADR whenever you deviate from DECISIONS.md or the case study.

## Commands (keep this section accurate as the project evolves)
```bash
uv sync                          # install deps (lockfile committed)
cp .env.example .env             # first time only
make up                          # docker compose: postgres(5433), mailpit, socket-proxy, mcp-notes, migrate, api(8000), worker
make migrate                     # alembic upgrade head + checkpointer setup + seed users (tokens -> .agentops-tokens)
make test                        # unit + integration (fake mode; needs postgres from `make up`)
make test-unit                   # fast, no services
make lint                        # ruff + ruff format --check + mypy
make build-sandbox               # build agentops-sandbox image (never run as a service)
make test-sandbox                # sandbox isolation tests (real Docker; own CI job)
make sandbox-selfcheck           # worker container -> socket proxy -> sandbox, file round trip
make eval                        # scenario suite (Phase 6; currently prints a notice)
uv run pytest -m e2e tests/e2e/<file> -s   # REAL models, costs money: ledger caps $0.10 (test_real_slice), $0.40 (test_real_phase3); needs ANTHROPIC_API_KEY in .env and, for phase3, the sandbox image
docker compose --profile observability up -d jaeger   # tracing UI on :16686
```

## Environment
- Developer is on Windows 11 + WSL2 (Ubuntu). **Docker Engine runs natively inside WSL2, not Docker Desktop.** The repo lives in the WSL filesystem. Scripts must work in bash on Ubuntu (WSL).
- Python 3.12 via `uv`. Node LTS for the UI.
