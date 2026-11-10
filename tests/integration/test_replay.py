"""Replay branches (ADR-040): branch isolation, upstream reuse, edits, budgets, workspaces,
forkable roots, and the core guarantee: a fork never re-sends an email."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core import replay
from agentops.db.checkpointer import open_checkpointer
from agentops.models.client import fake_factory
from agentops.models.fake import ScriptedTurn
from agentops.models.scenarios import build_scenarios
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Settings, get_settings
from tests.integration.conftest import SeededUser

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def process(
    engine: AsyncEngine, saver: Any, settings: Settings | None = None, factory: Any = None
) -> None:
    s = settings or get_settings()
    job = await jobs.claim(engine, "w-replay", s.job_lease_seconds)
    assert job is not None, "no job queued"
    await Worker(s, engine, saver, "w-replay", chat_factory=factory).process(job)


async def submit(client: httpx.AsyncClient, user: SeededUser, **body: Any) -> str:
    r = await client.post("/tasks", headers=user.headers, json={"request": "Summarize", **body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def checkpoints(client: httpx.AsyncClient, user: SeededUser, tid: str) -> dict:
    r = await client.get(f"/tasks/{tid}/checkpoints", headers=user.headers)
    assert r.status_code == 200, r.text
    return r.json()


def at(cps: dict, node: str, wave: int | None = None) -> str:
    """Newest forkable checkpoint whose next node is `node` (optionally at a wave)."""
    for c in cps["checkpoints"]:
        if c["forkable"] and c["next"] == [node] and (wave is None or c["wave"] == wave):
            return c["checkpoint_id"]
    raise AssertionError(f"no forkable checkpoint before {node} (wave {wave})")


async def fork(
    client: httpx.AsyncClient, user: SeededUser, tid: str, cp: str, **body: Any
) -> httpx.Response:
    return await client.post(
        f"/tasks/{tid}/branches", headers=user.headers, json={"checkpoint_id": cp, **body}
    )


async def scalar(engine: AsyncEngine, sql: str, **p: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text(sql), p)).scalar_one()


async def purposes(engine: AsyncEngine, tid: str) -> list[str]:
    async with engine.connect() as c:
        rows = await c.execute(
            text("SELECT purpose FROM llm_calls WHERE task_id=:t ORDER BY id"), {"t": tid}
        )
        return list(rows.scalars())


async def mails_with(key: str) -> int:
    async with httpx.AsyncClient() as c:
        r = await c.get("http://localhost:8025/api/v1/messages", params={"limit": 1000})
    return sum(1 for m in r.json()["messages"] if m.get("MessageID") == f"{key}@agentops")


async def approve_pending(client: httpx.AsyncClient, rita: SeededUser, tid: str) -> None:
    [a] = (await client.get("/approvals", headers=rita.headers, params={"task_id": tid})).json()
    r = await client.post(
        f"/approvals/{a['id']}/decision",
        headers=rita.headers,
        json={"version": a["version"], "decision": {"action": "approve"}},
    )
    assert r.status_code == 200, r.text


# ---- isolation and reuse -------------------------------------------------------------------
async def test_fork_reruns_only_downstream_and_leaves_the_root_untouched(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    root = await submit(client, alice)
    await process(engine, saver)
    assert await scalar(engine, "SELECT status::text FROM tasks WHERE id=:t", t=root) == "completed"
    cps = await checkpoints(client, alice, root)
    assert cps["fork_blocked_reason"] is None
    # parallel-wave and end-of-run checkpoints are never offered
    reasons = {c["reason"] for c in cps["checkpoints"] if not c["forkable"]}
    assert "end of the run" in reasons and "not a fork point (subtask_worker)" in reasons

    before = {
        t: await scalar(engine, f"SELECT count(*) FROM {t} WHERE task_id=:t", t=root)
        for t in ("events", "llm_calls", "tool_calls", "approvals")
    }
    n_cps = await scalar(engine, "SELECT count(*) FROM checkpoints WHERE thread_id=:t", t=root)
    memories = await scalar(engine, "SELECT count(*) FROM memories")
    root_final = (await client.get(f"/tasks/{root}/detail", headers=alice.headers)).json()["final"]

    r = await fork(
        client, alice, root, at(cps, "final_review"), edits={"final": "An edited deliverable."}
    )
    assert r.status_code == 201, r.text
    b = r.json()
    assert (b["kind"], b["parent_task_id"], b["root_task_id"]) == ("replay", root, root)
    await process(engine, saver)

    d = (await client.get(f"/tasks/{b['id']}/detail", headers=alice.headers)).json()
    assert d["status"] == "completed" and d["final"] == "An edited deliverable."
    # upstream (plan, specialists, reviews, synthesis) was reused, not re-run
    assert await purposes(engine, b["id"]) == ["final_review"]
    assert await scalar(engine, "SELECT count(*) FROM tool_calls WHERE task_id=:t", t=b["id"]) == 0
    # the root is exactly as it was; the fork wrote no memories
    for t, n in before.items():
        assert await scalar(engine, f"SELECT count(*) FROM {t} WHERE task_id=:t", t=root) == n, t
    assert (
        await scalar(engine, "SELECT count(*) FROM checkpoints WHERE thread_id=:t", t=root) == n_cps
    )
    assert (await client.get(f"/tasks/{root}/detail", headers=alice.headers)).json()[
        "final"
    ] == root_final
    assert await scalar(engine, "SELECT count(*) FROM memories") == memories
    # branch-aware ids: the branch's checkpoints live in its own thread; its events have their
    # own dedupe keys (the same node events exist for both, under different task ids)
    assert (
        await scalar(engine, "SELECT count(*) FROM checkpoints WHERE thread_id=:t", t=b["id"]) > 0
    )
    dup = await scalar(
        engine,
        "SELECT count(*) FROM events a JOIN events b ON a.dedupe_key = b.dedupe_key "
        "WHERE a.task_id=:r AND b.task_id=:f",
        r=root,
        f=b["id"],
    )
    assert dup >= 1  # e.g. final_review.verdict in both, without colliding

    cmp = (await client.get(f"/tasks/{root}/compare/{b['id']}", headers=alice.headers)).json()
    assert cmp["final_diff"] and "+An edited deliverable." in cmp["final_diff"]
    assert cmp["left"]["cost"]["calls"] > cmp["right"]["cost"]["calls"] == 1
    lin = (await client.get(f"/tasks/{b['id']}/lineage", headers=alice.headers)).json()
    assert [t["id"] for t in lin["tasks"]] == [root, b["id"]]
    listed = (await client.get("/tasks", headers=alice.headers)).json()["items"]
    assert [t["id"] for t in listed] == [root] and listed[0]["branches"] == 1


# ---- side effects --------------------------------------------------------------------------
async def test_a_fork_never_resends_an_email(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    root = await submit(client, alice, scenario="proposes_email")
    await process(engine, saver)
    await approve_pending(client, reviewer, root)
    await process(engine, saver)
    key = await scalar(engine, "SELECT idempotency_key FROM side_effects WHERE task_id=:t", t=root)
    assert await mails_with(key) == 1
    cps = await checkpoints(client, alice, root)

    branch_ids = []
    for node in ("action_gate", "synthesize", "final_review"):  # before and after execution
        r = await fork(client, alice, root, at(cps, node))
        assert r.status_code == 201, r.text
        branch_ids.append(r.json()["id"])
        await process(engine, saver)
    # a fork of a fork still finds the root's side effect through its lineage
    first = branch_ids[0]
    r = await fork(client, alice, first, at(await checkpoints(client, alice, first), "action_gate"))
    assert r.status_code == 201
    branch_ids.append(r.json()["id"])
    await process(engine, saver)
    assert await replay.lineage(engine, uuid.UUID(branch_ids[-1])) == (
        uuid.UUID(first),
        uuid.UUID(root),
    )

    assert await mails_with(key) == 1  # never re-sent
    assert await scalar(engine, "SELECT count(*) FROM side_effects") == 1  # no new rows
    for bid in branch_ids:
        d = (await client.get(f"/tasks/{bid}/detail", headers=alice.headers)).json()
        assert d["status"] == "completed", (bid, d["explanation"])
        [a] = d["actions"]
        assert a["outcome"]["status"] == "done" and a["outcome"]["idempotency_key"] == key
        assert d["approvals"] == []  # no approval was asked for in any branch
    replayed = (await client.get(f"/tasks/{branch_ids[0]}/detail", headers=alice.headers)).json()[
        "actions"
    ][0]
    assert replayed["outcome"]["detail"] == "done in the original branch (not re-sent)"
    sends = await scalar(
        engine,
        "SELECT count(*) FROM tool_calls WHERE tool='send_email' AND status='executed'",
    )
    assert sends == 1


async def test_fork_of_a_paused_root_leaves_its_pending_approval_alone(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    root = await submit(client, alice, scenario="proposes_email")
    await process(engine, saver)
    assert await scalar(engine, "SELECT status::text FROM tasks WHERE id=:t", t=root) == (
        "awaiting_approval"
    )
    appr_before = (await client.get("/approvals", headers=reviewer.headers)).json()
    cps = await checkpoints(client, alice, root)
    assert cps["fork_blocked_reason"] is None
    r = await fork(client, alice, root, at(cps, "action_gate"))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    await process(engine, saver)

    d = (await client.get(f"/tasks/{bid}/detail", headers=alice.headers)).json()
    [a] = d["actions"]
    # the original never decided it, so the branch doesn't execute it (and doesn't ask)
    assert a["outcome"]["status"] == "rejected" and d["approvals"] == []
    assert d["status"] == "completed_partial"
    # the root and its approval are unchanged...
    assert (await client.get("/approvals", headers=reviewer.headers)).json() == appr_before
    assert await scalar(engine, "SELECT status::text FROM tasks WHERE id=:t", t=root) == (
        "awaiting_approval"
    )
    assert await scalar(engine, "SELECT count(*) FROM side_effects") == 0
    # ...and still works: approving it sends exactly once
    await approve_pending(client, reviewer, root)
    await process(engine, saver)
    key = await scalar(engine, "SELECT idempotency_key FROM side_effects WHERE task_id=:t", t=root)
    assert await mails_with(key) == 1


async def test_a_running_or_queued_task_cannot_be_forked(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    root = await submit(client, alice)
    await process(engine, saver)
    cp = at(await checkpoints(client, alice, root), "final_review")
    for st in ("running", "queued"):
        async with engine.begin() as c:
            await c.execute(
                text("UPDATE tasks SET status=CAST(:s AS task_status) WHERE id=:t"),
                {"s": st, "t": root},
            )
        assert (await checkpoints(client, alice, root))["fork_blocked_reason"]
        r = await fork(client, alice, root, cp)
        assert r.status_code == 409 and st in r.json()["detail"]
    assert await scalar(engine, "SELECT count(*) FROM tasks WHERE kind='replay'") == 0


# ---- edits ---------------------------------------------------------------------------------
async def test_edits_are_allowed_only_where_they_make_sense_and_are_validated(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser, bob: SeededUser
) -> None:
    root = await submit(client, alice)
    await process(engine, saver)
    cps = await checkpoints(client, alice, root)
    end = next(c for c in cps["checkpoints"] if c["reason"] == "end of the run")
    cases = [
        (at(cps, "plan_gate"), {"final": "x"}, 422, "cannot edit ['final']"),
        (at(cps, "final_review"), {"request": "x"}, 422, "cannot edit ['request']"),
        (at(cps, "synthesize"), {"outputs": {"ghost": "x"}}, 422, "no accepted output"),
        (
            at(cps, "plan_gate"),
            {
                "plan": {
                    "confidence": 0.9,
                    "rationale": "r",
                    "subtasks": [
                        {
                            "id": "a",
                            "description": "d",
                            "specialist": "writing",
                            "depends_on": ["a"],
                            "expected_output": "o",
                        }
                    ],
                }
            },
            422,
            "invalid plan",
        ),
        (at(cps, "plan_gate"), {"surprise": 1}, 422, None),
        (end["checkpoint_id"], {}, 422, "end of the run"),
        ("nope", {}, 404, None),
    ]
    for cp, edits, code, msg in cases:
        r = await fork(client, alice, root, cp, edits=edits)
        assert r.status_code == code, (edits, r.text)
        if msg:
            assert msg in str(r.json()["detail"]), r.text
    assert (await fork(client, bob, root, at(cps, "plan_gate"))).status_code == 404
    assert (await client.get(f"/tasks/{root}/checkpoints", headers=bob.headers)).status_code == 404

    # a valid request edit at the plan step re-plans from the new request
    r = await fork(client, alice, root, at(cps, "plan"), edits={"request": "A different ask"})
    assert r.status_code == 201, r.text
    await process(engine, saver)
    d = (await client.get(f"/tasks/{r.json()['id']}/detail", headers=alice.headers)).json()
    assert d["request"] == "A different ask" and d["status"] == "completed"
    assert "plan/p1" in await purposes(
        engine, r.json()["id"]
    )  # planning re-ran downstream of the edit
    assert await scalar(engine, "SELECT count(*) FROM tasks WHERE kind='replay'") == 1


# ---- budget --------------------------------------------------------------------------------
async def test_fork_budget_defaults_to_the_roots_original_limits(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    root = await submit(client, alice, budget={"max_usd": 0.5, "max_llm_calls": 30})
    await process(engine, saver)
    async with engine.begin() as c:  # a reviewer raised it later; some was spent
        await c.execute(
            text(
                "UPDATE task_budgets SET max_usd=1.5, used_usd=0.4, used_calls=9 WHERE task_id=:t"
            ),
            {"t": root},
        )
    cps = await checkpoints(client, alice, root)
    assert cps["default_budget"]["max_usd"] == 0.5 and cps["default_budget"]["max_llm_calls"] == 30
    cp = at(cps, "final_review")
    default = (await fork(client, alice, root, cp)).json()["id"]
    custom = (await fork(client, alice, root, cp, budget={"max_usd": 0.2})).json()["id"]
    too_much = await fork(client, alice, root, cp, budget={"max_usd": 99})
    assert too_much.status_code == 422

    async def limits(t: str) -> tuple:
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT max_usd, max_calls, used_usd, used_calls FROM task_budgets "
                        "WHERE task_id=:t"
                    ),
                    {"t": t},
                )
            ).one()
        return float(row[0]), row[1], float(row[2]), row[3]

    assert await limits(default) == (0.5, 30, 0.0, 0)  # original, not 1.5 and not 0.1 left
    assert await limits(custom) == (0.2, 30, 0.0, 0)


# ---- workspaces ----------------------------------------------------------------------------
def _st(sid: str, deps: list[str]) -> dict:
    return {
        "id": sid,
        "description": f"{sid} step",
        "specialist": "writing",
        "depends_on": deps,
        "expected_output": "text",
        "complexity": "low",
    }


def files_script() -> Any:
    """Wave 1: `a` writes notes.md = A1. Wave 2: `b` reads notes.md, overwrites it with B2 and
    writes b.md."""
    s = build_scenarios()
    s.add(
        "supervisor",
        "t",
        ScriptedTurn(
            structured={
                "subtasks": [_st("a", []), _st("b", ["a"])],
                "confidence": 0.9,
                "rationale": "r",
            }
        ),
        purpose="plan",
    )

    def w(path: str, content: str) -> dict:
        return {"name": "workspace_write", "args": {"path": path, "content": content}}

    s.add(
        "writing",
        "t",
        ScriptedTurn(tool_calls=[w("notes.md", "A1")]),
        ScriptedTurn(content="a done"),
        purpose="act/r0/a",
    )
    s.add(
        "writing",
        "t",
        ScriptedTurn(tool_calls=[{"name": "workspace_read", "args": {"path": "notes.md"}}]),
        ScriptedTurn(tool_calls=[w("notes.md", "B2"), w("b.md", "from b")]),
        ScriptedTurn(content="b done"),
        purpose="act/r0/b",
    )
    return s


async def test_fork_gets_its_own_copy_of_the_workspace_at_the_fork_point(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser, tmp_path: Path
) -> None:
    settings = get_settings().model_copy(update={"workspace_root": tmp_path / "ws"})
    factory = fake_factory(files_script(), "t")
    root = await submit(client, alice)
    await process(engine, saver, settings, factory)
    root_ws = settings.workspace_root / root
    assert (root_ws / "notes.md").read_text() == "B2" and (root_ws / "b.md").exists()
    root_files = {p.name: (p.read_text(), p.stat().st_mtime_ns) for p in root_ws.iterdir()}

    cps = await checkpoints(client, alice, root)
    r = await fork(client, alice, root, at(cps, "schedule", wave=1))  # between wave 1 and 2
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    snap = replay.snapshots_root(settings) / root / "w1"
    assert {p.name: p.read_text() for p in snap.iterdir()} == {"notes.md": "A1"}
    await process(engine, saver, settings, factory)

    d = (await client.get(f"/tasks/{bid}/detail", headers=alice.headers)).json()
    assert d["status"] == "completed", d["explanation"]
    async with engine.connect() as c:
        calls = (
            await c.execute(
                text(
                    "SELECT tool, status, output_preview FROM tool_calls WHERE task_id=:t "
                    "ORDER BY id"
                ),
                {"t": bid},
            )
        ).all()
        restored = (
            await c.execute(
                text(
                    "SELECT payload FROM events WHERE task_id=:t "
                    "AND type='replay.workspace_restored'"
                ),
                {"t": bid},
            )
        ).scalar_one()
    # wave 2 re-ran in the branch and found wave 1's file (never a missing-file failure)
    assert calls[0].tool == "workspace_read" and calls[0].status == "ok"
    assert "A1" in calls[0].output_preview
    assert restored["from"] == "snapshot w1"
    fork_ws = settings.workspace_root / bid
    assert {p.name: p.read_text() for p in fork_ws.iterdir()} == {
        "notes.md": "B2",
        "b.md": "from b",
    }
    # the original's workspace was never written by the branch
    assert {p.name: (p.read_text(), p.stat().st_mtime_ns) for p in root_ws.iterdir()} == root_files
    # snapshots live outside every task workspace (invisible to tools and the sandbox)
    assert not (root_ws / ".snapshots").exists() and not (fork_ws / ".snapshots").exists()


def test_restore_falls_back_safely(tmp_path: Path) -> None:
    s = get_settings().model_copy(update={"workspace_root": tmp_path})
    src, f1, f2, f3 = (uuid.uuid4() for _ in range(4))
    (tmp_path / str(src)).mkdir()
    (tmp_path / str(src) / "x.txt").write_text("live")
    assert replay.restore_workspace(s, f1, src, 0, 2).startswith("empty")
    assert list((tmp_path / str(f1)).iterdir()) == []
    assert replay.restore_workspace(s, f1, src, 0, 2) == "already restored"
    # no snapshot, but no wave ran after the fork point: the live workspace is identical
    assert replay.restore_workspace(s, f2, src, 2, 2).startswith("source workspace")
    assert (tmp_path / str(f2) / "x.txt").read_text() == "live"
    with pytest.raises(replay.ReplayError, match="missing"):
        replay.restore_workspace(s, f3, src, 1, 2)


async def test_checkpoint_fields_prefill_the_fork_form(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser, bob: SeededUser
) -> None:
    root = await submit(client, alice)
    await process(engine, saver)
    cps = await checkpoints(client, alice, root)
    early = at(cps, "plan_gate")
    late = at(cps, "final_review")
    e = (await client.get(f"/tasks/{root}/checkpoints/{early}", headers=alice.headers)).json()
    assert e["editable"] == ["plan"] and e["plan"]["subtasks"] and e["outputs"] == {}
    assert e["final"] is None and e["request"] == "Summarize"
    late_f = (await client.get(f"/tasks/{root}/checkpoints/{late}", headers=alice.headers)).json()
    assert late_f["editable"] == ["final"] and late_f["final"] and late_f["outputs"]
    assert (
        await client.get(f"/tasks/{root}/checkpoints/nope", headers=alice.headers)
    ).status_code == 404
    assert (
        await client.get(f"/tasks/{root}/checkpoints/{late}", headers=bob.headers)
    ).status_code == 404


async def test_a_failed_snapshot_never_fails_the_task(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-046 (found by the eval dry run): a snapshot directory written by a worker running as
    another user raised PermissionError and failed the user's task."""

    def denied(*_a: Any, **_k: Any) -> Any:
        raise PermissionError("[Errno 13] Permission denied: '.snapshots/x'")

    monkeypatch.setattr(replay, "snapshot_workspace", denied)
    tid = await submit(client, alice)
    await process(engine, saver)
    assert await scalar(engine, "SELECT status::text FROM tasks WHERE id=:t", t=tid) == "completed"
    n = await scalar(
        engine,
        "SELECT count(*) FROM events WHERE task_id=:t AND type='replay.snapshot_failed'",
        t=tid,
    )
    assert n >= 1
