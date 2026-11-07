"""One real-model run of the Phase 2 slice (LLM_MODE=real). COSTS MONEY.

Never runs in CI or `make test` (marker `e2e` is deselected by default). Run explicitly:

    uv run pytest -m e2e tests/e2e/test_real_slice.py -s

Reads ANTHROPIC_API_KEY from .env. Hard cap: the task's budget ledger allows $0.10, and every
call reserves its worst case first, so the run cannot spend more than that.
"""

import uuid
from decimal import Decimal
from typing import Any

import anthropic
import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.graph import build_main_graph
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Budget, LLMMode, RoleModel, Settings, get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.e2e

MAX_USD = 0.10
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
ROLES = {
    "supervisor": RoleModel(
        provider="anthropic", model=SONNET, max_output_tokens=3000, effort="low"
    ),
    "writing": RoleModel(provider="anthropic", model=HAIKU, max_output_tokens=2000, effort="low"),
    "reviewer": RoleModel(provider="anthropic", model=SONNET, max_output_tokens=2000, effort="low"),
    # unused in Phase 2, but real mode requires every role to have a real provider
    "research": RoleModel(provider="anthropic", model=HAIKU, max_output_tokens=1000),
    "analysis": RoleModel(provider="anthropic", model=HAIKU, max_output_tokens=1000),
    "code": RoleModel(provider="anthropic", model=HAIKU, max_output_tokens=1000),
}
REQUEST = (
    "Write a 120-150 word plain-English explanation of what pgvector is and one situation "
    "where a team should choose it over a dedicated vector database."
)


def real_settings() -> Settings:
    # Settings reads ANTHROPIC_API_KEY from .env (tests/conftest.py removes it from os.environ).
    base = Settings(
        llm_mode=LLMMode.REAL,
        role_models=ROLES,
        budget=Budget(max_usd=MAX_USD, max_llm_calls=12),
        database_url=get_settings().database_url,
    )
    if base.anthropic_api_key is None or not base.anthropic_api_key.get_secret_value():
        pytest.skip("ANTHROPIC_API_KEY not set in .env")
    return base


def test_model_ids_exist() -> None:
    """Free: the Models API confirms both IDs before anything is spent."""
    s = real_settings()
    assert s.anthropic_api_key is not None
    client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value())
    for mid in (SONNET, HAIKU):
        m = client.models.retrieve(mid)
        assert m.id == mid


async def test_real_slice_end_to_end(engine: AsyncEngine, alice: SeededUser) -> None:
    settings = real_settings()
    tid = await create_task(engine, alice, REQUEST, graph="main", budget=settings.budget)
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(settings.libpq_dsn, max_size=4) as saver:
        worker = Worker(settings, engine, saver, "e2e")
        job = await jobs.claim(engine, "e2e", settings.job_lease_seconds)
        assert job is not None
        await worker.process(job)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
        values = (await build_main_graph(saver).aget_state(cfg)).values

    report = await _report(engine, tid)
    print_report(report, values)

    check_report(report)


def check_report(report: dict[str, Any]) -> None:
    status = report["task"]["status"]
    # A real reviewer may legitimately send work to a human; both are valid slice outcomes.
    assert status in {"completed", "awaiting_approval"}, report["task"]
    assert report["budget"]["used_usd"] <= MAX_USD
    assert report["budget"]["reserved_usd"] == 0
    assert len(report["calls"]) == len(report["reservations"])
    for c, r in zip(report["calls"], report["reservations"], strict=True):
        assert c["cost_usd"] is not None, c  # every real call is priced
        assert r["status"] == "settled", r
        # actual usage never exceeded the worst case reserved for it (ADR-024 margin check)
        assert c["input_tokens"] + c["output_tokens"] <= r["tokens"], (c, r)
        assert c["cost_usd"] <= r["usd"] + 1e-9, (c, r)
    if status == "completed":
        assert report["task"]["result"]["final"]


def _floats(row: dict[str, Any]) -> dict[str, Any]:
    """Raw SQL returns NUMERIC as Decimal; compare money as float like the ledger does."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


async def _report(engine: AsyncEngine, tid: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as c:
        task = (
            (
                await c.execute(
                    text("SELECT status::text AS status, result, error FROM tasks WHERE id=:t"),
                    {"t": tid},
                )
            )
            .one()
            ._mapping
        )
        calls = [
            _floats(dict(r._mapping))
            for r in await c.execute(
                text(
                    "SELECT role, purpose, model, attempt, ok, input_tokens, output_tokens, "
                    "cost_usd, latency_ms, error FROM llm_calls WHERE task_id=:t ORDER BY id"
                ),
                {"t": tid},
            )
        ]
        reservations = [
            _floats(dict(r._mapping))
            for r in await c.execute(
                text(
                    # paired by the explicit link: under parallel branches, reservation
                    # and call rows are written in different orders
                    "SELECT r.usd, r.tokens, r.status FROM llm_calls l "
                    "JOIN budget_reservations r ON r.id = l.reservation_id "
                    "WHERE l.task_id=:t ORDER BY l.id"
                ),
                {"t": tid},
            )
        ]
        budget = _floats(
            dict(
                (
                    await c.execute(
                        text(
                            "SELECT used_usd, used_tokens, used_calls, reserved_usd "
                            "FROM task_budgets WHERE task_id=:t"
                        ),
                        {"t": tid},
                    )
                )
                .one()
                ._mapping
            )
        )
    return {"task": dict(task), "calls": calls, "reservations": reservations, "budget": budget}


def print_report(report: dict[str, Any], values: Any) -> None:
    print("\n=== real-model slice ===")
    print(f"status: {report['task']['status']}  error: {report['task']['error']}")
    print(
        f"{'role':<11}{'purpose':<11}{'model':<19}{'try':>4}{'in':>7}{'out':>7}"
        f"{'cost $':>11}{'reserved $':>12}{'ms':>7}"
    )
    for c, r in zip(report["calls"], report["reservations"], strict=False):
        print(
            f"{c['role']:<11}{c['purpose']:<11}{c['model']:<19}{c['attempt']:>4}"
            f"{c['input_tokens']:>7}{c['output_tokens']:>7}{(c['cost_usd'] or 0):>11.6f}"
            f"{r['usd']:>12.6f}{c['latency_ms']:>7}" + ("" if c["ok"] else f"  ERR {c['error']}")
        )
    b = report["budget"]
    print(
        f"total: ${b['used_usd']:.6f} over {b['used_calls']} calls, {b['used_tokens']} tokens "
        f"(cap ${MAX_USD:.2f})"
    )
    for r in values.get("reviews", []):
        print(f"review r{r.revision}: {r.verdict.verdict} {r.verdict.score}/5 {r.verdict.issues}")
    final = report["task"]["result"].get("final") if report["task"]["result"] else None
    if final:
        print("--- final ---\n" + final)
