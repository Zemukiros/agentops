"""Real-model run of Phase 4: approval through the API, exactly-once email, memory, recall.
COSTS MONEY.   uv run pytest -m e2e tests/e2e/test_real_phase4.py -s

- Models as in Phase 3 (Sonnet 5.5 for supervisor/reviewer/analysis/code, Haiku 5.5 for
  research/writing), effort low. Fixture search. Real fastembed embeddings (bge-small).
- Ledger cap $0.20 per task, two tasks: at most $0.40 in total.
- Task A: research -> writing proposes send_email -> paused -> a REVIEWER approves through
  POST /approvals/{id}/decision -> exactly one email in Mailpit (matched by its idempotency
  key) -> memories written. Task B (similar request) must recall task A's memory.
"""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.api.main import create_app
from agentops.core.nodes.actions import idempotency_key
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Budget, LLMMode, RoleModel, SearchMode, Settings, get_settings
from tests.integration.conftest import SeededUser, create_task, graph_state_values

pytestmark = pytest.mark.e2e

CAP = 0.20
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"


def role(model: str, out: int) -> RoleModel:
    return RoleModel(provider="anthropic", model=model, max_output_tokens=out, effort="low")


ROLES = {
    "supervisor": role(SONNET, 3000),
    "reviewer": role(SONNET, 2000),
    "research": role(HAIKU, 2000),
    "writing": role(HAIKU, 2500),
    "analysis": role(SONNET, 3000),
    "code": role(SONNET, 3000),
}
REQUEST_A = (
    "Research what pgvector is and when a team should use it instead of a dedicated vector "
    "database, then write a short summary (under 150 words) and email it to team@example.com. "
    "Plan exactly two subtasks: one research, one writing that depends on it; the writing "
    "subtask sends the email with the send_email tool."
)
REQUEST_B = (
    "Research pgvector and write a brief summary for the team about when to use it. "
    "Plan exactly two subtasks: research, then writing."
)


def real_settings(tmp: Path) -> Settings:
    s = Settings(
        llm_mode=LLMMode.REAL,
        search_mode=SearchMode.FIXTURE,
        role_models=ROLES,
        budget=Budget(max_usd=CAP, max_llm_calls=40, max_tokens=400_000),
        database_url=get_settings().database_url,
        workspace_root=tmp / "ws",
        mcp_servers=[],
        embedding_mode="fastembed",
        fastembed_cache_path=Path("var/fastembed"),
    )
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value():
        pytest.skip("ANTHROPIC_API_KEY not set in .env")
    return s


async def q(engine: AsyncEngine, sql: str, **p: object) -> list[dict[str, Any]]:
    async with engine.connect() as c:
        return [dict(r._mapping) for r in await c.execute(text(sql), p)]


async def process(engine: AsyncEngine, saver: Any, settings: Settings) -> None:
    job = await jobs.claim(engine, "e2e4", settings.job_lease_seconds)
    assert job is not None
    await Worker(settings, engine, saver, "e2e4").process(job)


async def drive(
    engine: AsyncEngine,
    saver: Any,
    settings: Settings,
    client: httpx.AsyncClient,
    reviewer: SeededUser,
    tid: uuid.UUID,
    *,
    approve_email: bool,
) -> list[str]:
    """Run until terminal, answering every pause through the API. Returns the decisions."""
    log: list[str] = []
    await process(engine, saver, settings)
    for _ in range(8):
        [t] = await q(engine, "SELECT status::text AS s FROM tasks WHERE id=:t", t=tid)
        if t["s"] != "awaiting_approval":
            return log
        r = await client.get("/approvals", params={"task_id": str(tid)}, headers=reviewer.headers)
        [a] = r.json()
        kind = a["kind"]
        if kind == "approve_action":
            decision = {"action": "approve" if approve_email else "reject"}
        elif kind == "budget":
            decision = {"action": "reject"}
        else:
            decision = {"action": "approve"}
        resp = await client.post(
            f"/approvals/{a['id']}/decision",
            json={"version": a["version"], "decision": decision},
            headers=reviewer.headers,
        )
        assert resp.status_code == 200, resp.text
        log.append(f"{kind}:{decision['action']}")
        await process(engine, saver, settings)
    raise AssertionError("did not finish within 8 decisions")


async def report(engine: AsyncEngine, tid: uuid.UUID, label: str) -> dict[str, Any]:
    [t] = await q(
        engine, "SELECT status::text AS status, result, error FROM tasks WHERE id=:t", t=tid
    )
    calls = await q(
        engine,
        "SELECT role, purpose, model, input_tokens AS i, output_tokens AS o, "
        "cost_usd FROM llm_calls WHERE task_id=:t ORDER BY id",
        t=tid,
    )
    [b] = await q(
        engine, "SELECT used_usd, used_tokens, used_calls FROM task_budgets WHERE task_id=:t", t=tid
    )
    print(f"\n=== {label}: {t['status']} ({t['error']}) ===")
    for c in calls:
        print(
            f"  {c['role']:<11}{c['purpose']:<26}{c['model']:<19}{c['i']:>7}{c['o']:>7} ${float(c['cost_usd'] or 0):.6f}"
        )
    print(
        f"  total ${float(b['used_usd']):.6f}, {b['used_calls']} calls, {b['used_tokens']} tokens (cap ${CAP:.2f})"
    )
    return {"task": t, "budget": b}


async def test_real_phase4_approval_email_memory_recall(
    engine: AsyncEngine, alice: SeededUser, reviewer: SeededUser, tmp_path: Path
) -> None:
    settings = real_settings(tmp_path)
    app = create_app(get_settings())
    async with (
        app.router.lifespan_context(app),
        open_checkpointer(settings.libpq_dsn, max_size=4) as saver,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as client:
            # ---- task A: approval through the API, one email, memories ----
            ta = await create_task(engine, alice, REQUEST_A, graph="main", budget=settings.budget)
            await jobs.enqueue(engine, "run", thread_id=ta)
            decisions_a = await drive(
                engine, saver, settings, client, reviewer, ta, approve_email=True
            )
            ra = await report(engine, ta, "task A")
            print("  decisions:", decisions_a)
            values = await graph_state_values(ta)
            emails = [
                p for p in values.get("proposed_actions", {}).values() if p.tool == "send_email"
            ]
            assert emails, "the writing specialist did not propose send_email"
            assert "approve_action:approve" in decisions_a
            key = idempotency_key(str(ta), emails[0].hash, emails[0].args)
            async with httpx.AsyncClient() as mp:
                msgs = (
                    await mp.get("http://localhost:8025/api/v1/messages", params={"limit": 1000})
                ).json()["messages"]
            sent = [m for m in msgs if m.get("MessageID") == f"{key}@agentops"]
            print(
                f"  mailpit messages with this action's key: {len(sent)}; subject: {sent[0]['Subject'] if sent else None!r}"
            )
            assert len(sent) == 1
            mems_a = await q(
                engine, "SELECT kind, outcome, text FROM memories WHERE source_task_id=:t", t=ta
            )
            for m in mems_a:
                print(f"  memory {m['kind']:<10} {m['outcome']:<17} {m['text'][:110]}")
            assert any(m["kind"] == "episode" for m in mems_a)

            # ---- task B: similar request recalls task A ----
            tb = await create_task(engine, alice, REQUEST_B, graph="main", budget=settings.budget)
            await jobs.enqueue(engine, "run", thread_id=tb)
            decisions_b = await drive(
                engine, saver, settings, client, reviewer, tb, approve_email=False
            )
            rb = await report(engine, tb, "task B")
            print("  decisions:", decisions_b)
            [recalled] = await q(
                engine,
                "SELECT payload FROM events WHERE task_id=:t AND type='memory.recalled'",
                t=tb,
            )
            hits = recalled["payload"]["memories"]
            for h in hits:
                print(
                    f"  recalled {h['kind']:<10} sim={h['similarity']:.3f} score={h['score']:.3f} {h['text'][:90]}"
                )
            src = {
                r["id"]
                for r in await q(
                    engine, "SELECT id::text AS id FROM memories WHERE source_task_id=:t", t=ta
                )
            }
            assert any(h["id"] in src for h in hits), "task B recalled nothing from task A"
            total = float(ra["budget"]["used_usd"]) + float(rb["budget"]["used_usd"])
            print(f"\n=== Phase 4 real run total: ${total:.6f} (cap ${2 * CAP:.2f}) ===")
            assert ra["task"]["status"] in {"completed", "completed_partial"}
            assert rb["task"]["status"] in {"completed", "completed_partial"}
