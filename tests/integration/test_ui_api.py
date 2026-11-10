"""Phase 5 read APIs for the UI: task list/detail, trace, cost/time, SSE tickets, CORS.
Ownership is checked on every endpoint (another user's task is a 404)."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.db.checkpointer import open_checkpointer
from agentops.observability.events import emit
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from tests.integration.conftest import SeededUser, create_task, parse_sse

pytestmark = pytest.mark.integration

SECRET = "sk-ant-api03-SECRETSECRETSECRET"


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def process(engine: AsyncEngine, saver: Any) -> None:
    s = get_settings()
    job = await jobs.claim(engine, "w-ui", s.job_lease_seconds)
    assert job is not None
    await Worker(s, engine, saver, "w-ui").process(job)


async def submit(client: httpx.AsyncClient, user: SeededUser, **body: Any) -> dict:
    r = await client.post("/tasks", headers=user.headers, json={"request": "Summarize", **body})
    assert r.status_code == 201, r.text
    return r.json()


async def email_task_done(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    rita: SeededUser,
    scenario: str = "proposes_email",
) -> str:
    """run -> action approval -> reviewer approves -> email sent -> done."""
    tid = (await submit(client, alice, scenario=scenario))["id"]
    await process(engine, saver)
    [a] = (await client.get("/approvals", headers=rita.headers, params={"task_id": tid})).json()
    r = await client.post(
        f"/approvals/{a['id']}/decision",
        headers=rita.headers,
        json={"version": a["version"], "decision": {"action": "approve"}},
    )
    assert r.status_code == 200, r.text
    await process(engine, saver)
    return tid


# ---- list ----------------------------------------------------------------------------------
async def test_list_is_owner_scoped_filtered_and_paginated(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser, bob: SeededUser
) -> None:
    ids = [(await submit(client, alice, request=f"alpha {i}"))["id"] for i in range(5)]
    await submit(client, bob, request="bob's")
    async with engine.begin() as c:
        await c.execute(text("UPDATE tasks SET status='completed' WHERE id=:t"), {"t": ids[0]})
        await c.execute(
            text("UPDATE task_budgets SET used_usd=0.25 WHERE task_id=:t"), {"t": ids[0]}
        )

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/tasks", headers=alice.headers, params=params)).json()
        seen += [t["id"] for t in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(ids))  # newest first, every task once, never bob's

    done = (
        await client.get("/tasks", headers=alice.headers, params={"status": "completed"})
    ).json()["items"]
    assert [t["id"] for t in done] == [ids[0]]
    assert done[0]["cost_usd"] == 0.25 and done[0]["status"] == "completed"
    q = (await client.get("/tasks", headers=alice.headers, params={"q": "alpha 3"})).json()
    assert [t["id"] for t in q["items"]] == [ids[3]]
    # LIKE metacharacters are literal
    none = (await client.get("/tasks", headers=alice.headers, params={"q": "%"})).json()
    assert none["items"] == []
    assert (
        await client.get("/tasks", headers=alice.headers, params={"cursor": "junk"})
    ).status_code == 400


async def test_create_with_budget_is_bounded_and_recorded(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    t = await submit(client, alice, budget={"max_usd": 0.5, "max_llm_calls": 20})
    async with engine.connect() as c:
        b = (
            await c.execute(
                text("SELECT max_usd, max_calls FROM task_budgets WHERE task_id=:t"),
                {"t": t["id"]},
            )
        ).one()
        cfg = (
            await c.execute(text("SELECT config FROM tasks WHERE id=:t"), {"t": t["id"]})
        ).scalar_one()
    assert (float(b.max_usd), b.max_calls) == (0.5, 20)
    assert cfg["budget"]["max_usd"] == 0.5  # the original limits, for replay forks
    s = get_settings()
    too_much = {"max_usd": s.task_budget_max_usd + 1}
    r = await client.post(
        "/tasks", headers=alice.headers, json={"request": "x", "budget": too_much}
    )
    assert r.status_code == 422
    r = await client.post(
        "/tasks", headers=alice.headers, json={"request": "x", "budget": {"max_usd": -1}}
    )
    assert r.status_code == 422


# ---- detail --------------------------------------------------------------------------------
async def test_detail_shows_plan_reviews_actions_mail_link_and_deliverable(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    bob: SeededUser,
    reviewer: SeededUser,
) -> None:
    tid = (await submit(client, alice, scenario="proposes_email"))["id"]
    await process(engine, saver)
    d = (await client.get(f"/tasks/{tid}/detail", headers=alice.headers)).json()
    assert d["status"] == "awaiting_approval"
    [st] = d["subtasks"]
    assert (st["id"], st["specialist"], st["status"]) == ("draft", "writing", "accepted")
    assert st["reviews"][0]["verdict"] == "accept" and st["reviews"][0]["score"] == 5
    [act] = d["actions"]
    assert act["tool"] == "send_email" and act["approval"]["status"] == "pending"
    assert act["outcome"] is None and act["mailpit_url"] is None
    assert [a["kind"] for a in d["approvals"]] == ["approve_action"]

    [a] = (await client.get("/approvals", headers=reviewer.headers, params={"task_id": tid})).json()
    r = await client.post(
        f"/approvals/{a['id']}/decision",
        headers=reviewer.headers,
        json={"version": a["version"], "decision": {"action": "approve"}},
    )
    assert r.status_code == 200
    await process(engine, saver)
    d = (await client.get(f"/tasks/{tid}/detail", headers=alice.headers)).json()
    assert d["status"] == "completed" and d["final"] and d["explanation"] is None
    [act] = d["actions"]
    assert act["approval"]["status"] == "approved" and act["outcome"]["status"] == "done"
    assert act["side_effect_status"] == "done"
    assert act["mailpit_url"].startswith(get_settings().mailpit_ui_url + "/view/")
    async with httpx.AsyncClient() as c:
        assert (await c.get(act["mailpit_url"])).status_code == 200
    assert d["budget"]["max_usd"] == get_settings().budget.max_usd

    assert (await client.get(f"/tasks/{tid}/detail", headers=bob.headers)).status_code == 404


async def test_detail_explains_a_partial_run(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = (await submit(client, alice, scenario="partial_ok"))["id"]
    await process(engine, saver)
    d = (await client.get(f"/tasks/{tid}/detail", headers=alice.headers)).json()
    assert d["status"] == "completed_partial"
    assert "subtasks not accepted" in d["explanation"]
    assert any(s["status"] != "accepted" for s in d["subtasks"])


# ---- trace ---------------------------------------------------------------------------------
async def test_trace_joins_calls_paginates_filters_and_redacts(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    bob: SeededUser,
    reviewer: SeededUser,
) -> None:
    tid = await email_task_done(client, engine, saver, alice, reviewer)
    # a tool call whose args and output carry credentials (as a leaky tool might)
    async with engine.begin() as c:
        row = await c.execute(
            text(
                "INSERT INTO tool_calls (task_id, agent, tool, risk, args, ok, output_preview, "
                "error, latency_ms, status) VALUES (:t, 'research', 'fetch_url', 'read', "
                "CAST(:a AS jsonb), false, :o, :e, 5, 'error') RETURNING id"
            ),
            {
                "t": tid,
                "a": json.dumps({"url": "https://x.test", "api_key": "plain-secret-value"}),
                "o": f"header Authorization: Bearer {'z' * 30}",
                "e": f"boom {SECRET}",
            },
        )
        tcid = row.scalar_one()
    await emit(
        engine, uuid.UUID(tid), "tool.call", payload={"tool_call_id": tcid, "password": "pw1"}
    )

    full = (await client.get(f"/tasks/{tid}/trace", headers=alice.headers)).json()
    body = json.dumps(full)
    for leaked in (SECRET, "plain-secret-value", "z" * 30):
        assert leaked not in body
    items = full["items"]
    llm = [i for i in items if i["kind"] == "llm"]
    assert llm and all(i["input_tokens"] > 0 and i["latency_ms"] >= 0 for i in llm)
    assert {i["agent"] for i in llm} >= {"supervisor", "writing", "reviewer"}
    async with engine.connect() as c:
        n_llm = (
            await c.execute(text("SELECT count(*) FROM llm_calls WHERE task_id=:t"), {"t": tid})
        ).scalar_one()
    assert len(llm) == n_llm  # every call once
    tools = [i for i in items if i["kind"] == "tool"]
    assert {t["status"] for t in tools} >= {"proposed", "executed", "error"}
    bad = next(t for t in tools if t["tool_call_id"] == tcid)
    assert bad["input"]["api_key"] == "[redacted]" and bad["ok"] is False
    assert full["next_cursor"] is None

    # pagination returns the same sequence
    seq, cur = [], 0
    while True:
        page = (
            await client.get(
                f"/tasks/{tid}/trace", headers=alice.headers, params={"cursor": cur, "limit": 7}
            )
        ).json()
        seq += [i["id"] for i in page["items"]]
        if page["next_cursor"] is None:
            break
        cur = page["next_cursor"]
    assert seq == [i["id"] for i in items]

    only_llm = (
        await client.get(f"/tasks/{tid}/trace", headers=alice.headers, params={"kind": "llm"})
    ).json()["items"]
    assert only_llm and {i["kind"] for i in only_llm} == {"llm"}
    errs = (
        await client.get(
            f"/tasks/{tid}/trace", headers=alice.headers, params={"errors_only": "true"}
        )
    ).json()["items"]
    assert [i.get("tool_call_id") for i in errs] == [tcid]
    by_agent = (
        await client.get(f"/tasks/{tid}/trace", headers=alice.headers, params={"agent": "reviewer"})
    ).json()["items"]
    assert by_agent and {i["agent"] for i in by_agent} == {"reviewer"}
    assert (await client.get(f"/tasks/{tid}/trace", headers=bob.headers)).status_code == 404


# ---- cost ----------------------------------------------------------------------------------
async def test_cost_totals_match_the_ledger_and_wait_is_separate(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    bob: SeededUser,
    reviewer: SeededUser,
) -> None:
    tid = await email_task_done(client, engine, saver, alice, reviewer)
    c = (await client.get(f"/tasks/{tid}/cost", headers=alice.headers)).json()
    async with engine.connect() as conn:
        n, tin, tout = (
            await conn.execute(
                text(
                    "SELECT count(*), sum(input_tokens), sum(output_tokens) FROM llm_calls "
                    "WHERE task_id=:t"
                ),
                {"t": tid},
            )
        ).one()
    assert (c["total"]["calls"], c["total"]["input_tokens"], c["total"]["output_tokens"]) == (
        n,
        tin,
        tout,
    )
    assert sum(a["calls"] for a in c["by_agent"].values()) == n
    assert c["tools"]["send_email"]["calls"] == 2  # proposed + executed
    t = c["time"]
    assert t["compute_s"] > 0 and t["human_wait_s"] > 0
    assert t["compute_s"] + t["human_wait_s"] + t["queue_s"] == pytest.approx(t["wall_s"], abs=0.01)
    [ap] = c["approvals"]
    assert ap["kind"] == "approve_action" and ap["status"] == "approved" and ap["wait_s"] > 0
    assert (await client.get(f"/tasks/{tid}/cost", headers=bob.headers)).status_code == 404


# ---- SSE tickets (ADR-041) -----------------------------------------------------------------
async def finished_task(engine: AsyncEngine, user: SeededUser) -> uuid.UUID:
    tid = await create_task(engine, user)
    await emit(engine, tid, "task.finished", payload={"status": "completed"})
    return tid


async def ticket(client: httpx.AsyncClient, user: SeededUser, tid: Any) -> httpx.Response:
    return await client.post(f"/tasks/{tid}/stream-ticket", headers=user.headers)


async def test_ticket_streams_once_and_only_for_its_task(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser, bob: SeededUser
) -> None:
    tid = await finished_task(engine, alice)
    other = await finished_task(engine, alice)
    r = await ticket(client, alice, tid)
    assert r.status_code == 201
    tk = r.json()["ticket"]
    async with engine.connect() as c:
        stored = (await c.execute(text("SELECT ticket_hash FROM sse_tickets"))).scalars().all()
    assert tk not in stored  # only the HMAC is stored

    # wrong task: refused, and the ticket is NOT consumed by the attempt
    assert (await client.get(f"/tasks/{other}/events", params={"ticket": tk})).status_code == 401
    ok = await client.get(f"/tasks/{tid}/events", params={"ticket": tk})
    assert ok.status_code == 200
    assert [e["type"] for e in parse_sse(ok.text)] == ["task.finished"]
    # single use
    assert (await client.get(f"/tasks/{tid}/events", params={"ticket": tk})).status_code == 401

    # another user's task: no ticket at all (404, not 403)
    assert (await ticket(client, bob, tid)).status_code == 404
    # no credentials / a bearer token in the URL is not a credential
    assert (await client.get(f"/tasks/{tid}/events")).status_code == 401
    token = alice.headers["Authorization"].split()[1]
    assert (await client.get(f"/tasks/{tid}/events", params={"ticket": token})).status_code == 401
    assert (
        await client.get(f"/tasks/{tid}/events", params={"access_token": token})
    ).status_code == 401
    # header auth still works for non-browser clients
    assert (await client.get(f"/tasks/{tid}/events", headers=alice.headers)).status_code == 200


async def test_expired_ticket_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    tid = await finished_task(engine, alice)
    tk = (await ticket(client, alice, tid)).json()["ticket"]
    async with engine.begin() as c:
        await c.execute(text("UPDATE sse_tickets SET expires_at = now() - interval '1 second'"))
    assert (await client.get(f"/tasks/{tid}/events", params={"ticket": tk})).status_code == 401


async def test_concurrent_redemption_succeeds_exactly_once(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    import asyncio  # noqa: PLC0415

    from agentops.api import tickets  # noqa: PLC0415

    tid = await finished_task(engine, alice)
    tk = (await ticket(client, alice, tid)).json()["ticket"]
    got = await asyncio.gather(*(tickets.redeem(engine, tk, tid) for _ in range(8)))
    assert sum(g is not None for g in got) == 1


# ---- CORS ----------------------------------------------------------------------------------
async def test_cors_allows_only_the_ui_origin(client: httpx.AsyncClient) -> None:
    ui = get_settings().ui_origins[0]
    pre = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    ok = await client.options("/tasks", headers={"Origin": ui, **pre})
    assert ok.headers.get("access-control-allow-origin") == ui
    bad = await client.options("/tasks", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers
    simple = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


async def test_ui_demo_scenario_walkthrough(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    """The scenario the UI e2e drives: research -> brief (revised once) -> email approval."""
    tid = await email_task_done(client, engine, saver, alice, reviewer, scenario="ui_demo")
    d = (await client.get(f"/tasks/{tid}/detail", headers=alice.headers)).json()
    assert d["status"] == "completed", d["explanation"]
    subs = {s["id"]: s for s in d["subtasks"]}
    assert (subs["research"]["specialist"], subs["brief"]["depends_on"]) == (
        "research",
        ["research"],
    )
    assert subs["brief"]["revisions"] == 1
    assert [r["verdict"] for r in subs["brief"]["reviews"]] == ["revise", "accept"]
    assert d["final"].startswith("# Vector search")
    [a] = d["actions"]
    assert a["outcome"]["status"] == "done" and a["mailpit_url"]
    tools = {
        (i["tool"], i["status"])
        for i in (await client.get(f"/tasks/{tid}/trace", headers=alice.headers)).json()["items"]
        if i["kind"] == "tool"
    }
    assert {("web_search", "ok"), ("fetch_url", "ok"), ("send_email", "executed")} <= tools
