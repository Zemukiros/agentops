"""Real-model run of the Phase 3 graph: research -> analysis -> writing. COSTS MONEY.

    uv run pytest -m e2e tests/e2e/test_real_phase3.py -s

- Models: supervisor/reviewer/analysis/code on claude-sonnet-5-5, research/writing on
  claude-haiku-5-5, effort low. No Opus.
- Tools are real: web_search/fetch_url in FIXTURE mode (no paid search API), SQL on the
  seeded demo schema via the read-only role, run_python in the Docker sandbox.
- Hard cap: the task's budget ledger allows $0.40; every call reserves its worst case first.
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Budget, LLMMode, RoleModel, SearchMode, Settings, get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.e2e

MAX_USD = 0.40
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"


def role(model: str, out: int) -> RoleModel:
    return RoleModel(provider="anthropic", model=model, max_output_tokens=out, effort="low")


ROLES = {
    "supervisor": role(SONNET, 3000),
    "reviewer": role(SONNET, 2000),
    "research": role(HAIKU, 2000),
    "analysis": role(SONNET, 3000),
    "code": role(SONNET, 3000),
    "writing": role(HAIKU, 2500),
}
REQUEST = (
    "Prepare a short internal brief. (1) Research: use web search to find what pgvector is and "
    "when a team should choose it over a dedicated vector database. (2) Analysis: using the demo "
    "sales database, compute total revenue (quantity x unit_price) by customer region, and use "
    "Python to compute each region's share of the total in percent. (3) Writing: combine both "
    "into a brief of at most 250 words with a small revenue table. Plan exactly three subtasks: "
    "one research, one analysis, one writing that depends on the other two."
)


def real_settings(tmp: Path) -> Settings:
    s = Settings(
        llm_mode=LLMMode.REAL,
        search_mode=SearchMode.FIXTURE,
        role_models=ROLES,
        budget=Budget(max_usd=MAX_USD, max_llm_calls=40, max_tokens=400_000),
        database_url=get_settings().database_url,
        workspace_root=tmp / "ws",
        mcp_servers=[],
    )
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value():
        pytest.skip("ANTHROPIC_API_KEY not set in .env")
    return s


async def q(engine: AsyncEngine, sql: str, **p: object) -> list[dict[str, Any]]:
    async with engine.connect() as c:
        return [dict(r._mapping) for r in await c.execute(text(sql), p)]


def money(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [{**r, key: float(r[key]) if r[key] is not None else None} for r in rows]


async def test_real_research_analysis_writing(
    engine: AsyncEngine, alice: SeededUser, tmp_path: Path
) -> None:
    settings = real_settings(tmp_path)
    tid = await create_task(engine, alice, REQUEST, graph="main", budget=settings.budget)
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(settings.libpq_dsn, max_size=4) as saver:
        worker = Worker(settings, engine, saver, "e2e3")
        job = await jobs.claim(engine, "e2e3", settings.job_lease_seconds)
        assert job is not None
        await worker.process(job)

    [task] = await q(
        engine, "SELECT status::text AS status, result, error FROM tasks WHERE id=:t", t=tid
    )
    calls = money(
        await q(
            engine,
            "SELECT role, purpose, model, attempt, ok, input_tokens, output_tokens, cost_usd, "
            "latency_ms, error FROM llm_calls WHERE task_id=:t ORDER BY id",
            t=tid,
        ),
        "cost_usd",
    )
    reservations = money(
        await q(
            engine,
            "SELECT r.usd, r.tokens, r.status FROM llm_calls l "
            "JOIN budget_reservations r ON r.id = l.reservation_id "
            "WHERE l.task_id=:t ORDER BY l.id",
            t=tid,
        ),
        "usd",
    )
    tools = await q(
        engine,
        "SELECT subtask_id, agent, tool, status, revision, step, left(coalesce(error, ''), 160) AS err "
        "FROM tool_calls WHERE task_id=:t ORDER BY id",
        t=tid,
    )
    [budget] = money(
        money(
            await q(
                engine,
                "SELECT used_usd, used_tokens, used_calls, reserved_usd FROM task_budgets WHERE task_id=:t",
                t=tid,
            ),
            "used_usd",
        ),
        "reserved_usd",
    )

    print("\n=== Phase 3 real run ===")
    print(f"status: {task['status']}  error: {task['error']}")
    result = task["result"] or {}
    plan = result.get("plan") or {}
    for s in plan.get("subtasks", []):
        r = result.get("subtasks", {}).get(s["id"], {})
        print(
            f"  subtask {s['id']:<12} {s['specialist']:<9} deps={s['depends_on']} -> "
            f"{r.get('status')} (revisions {r.get('revisions')})"
        )
    print(
        f"{'role':<11}{'purpose':<26}{'model':<19}{'in':>7}{'out':>7}{'cost $':>11}{'reserved $':>12}"
    )
    for c, r in zip(calls, reservations, strict=False):
        print(
            f"{c['role']:<11}{c['purpose']:<26}{c['model']:<19}{c['input_tokens']:>7}"
            f"{c['output_tokens']:>7}{(c['cost_usd'] or 0):>11.6f}{r['usd']:>12.6f}"
            + ("" if c["ok"] else f"  ERR {str(c['error'])[:80]}")
        )
    print("tool calls:")
    for t in tools:
        print(
            f"  {t['subtask_id']:<12}{t['agent']:<9}{t['tool']:<16}{t['status']:<17}r{t['revision']} s{t['step']} {t['err']}"
        )
    print(
        f"total: ${budget['used_usd']:.6f}, {budget['used_calls']} calls, {budget['used_tokens']} tokens "
        f"(cap ${MAX_USD:.2f})"
    )
    if result.get("final"):
        print("--- final ---\n" + result["final"])

    # The run must end in a legitimate state, within budget, with every call priced and covered
    assert task["status"] in {"completed", "completed_partial", "awaiting_approval"}, task
    assert budget["used_usd"] <= MAX_USD and budget["reserved_usd"] == 0
    for c, r in zip(calls, reservations, strict=True):
        assert c["cost_usd"] is not None
        assert c["input_tokens"] + c["output_tokens"] <= r["tokens"], (c, r)
        assert c["cost_usd"] <= r["usd"] + 1e-9, (c, r)
    specialists = {s["specialist"] for s in plan.get("subtasks", [])}
    assert {"research", "analysis", "writing"} <= specialists, plan
    used = {(t["agent"], t["tool"]) for t in tools if t["status"] == "ok"}
    assert any(a == "research" and tool in {"web_search", "fetch_url"} for a, tool in used)
    assert any(a == "analysis" and tool in {"sql_query", "describe_schema"} for a, tool in used)
    assert not [t for t in tools if t["status"] == "proposed"]  # nothing outside the task
