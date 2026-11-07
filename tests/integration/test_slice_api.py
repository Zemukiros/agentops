"""The Phase 2 vertical slice over HTTP, fake mode: POST /tasks -> job -> worker process ->
graph -> terminal state, with the whole run streamed over SSE."""

import os
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from agentops.api.main import create_app
from agentops.settings import LLMMode, get_settings
from tests.integration.conftest import ROOT, SeededUser, parse_sse

pytestmark = pytest.mark.integration


async def test_full_slice_streams_to_completion(
    client: httpx.AsyncClient, alice: SeededUser
) -> None:
    r = await client.post(
        "/tasks",
        json={"request": "Summarize what pgvector is.", "scenario": "revise_once"},
        headers=alice.headers,
    )
    assert r.status_code == 201, r.text
    task = r.json()
    assert (task["graph"], task["status"]) == ("main", "queued")

    worker = subprocess.Popen(
        [sys.executable, "-m", "agentops.queue.worker", "--worker-id", "slice", "--once"],
        cwd=ROOT,
        env={**os.environ, "JOB_POLL_SECONDS": "0.1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # The stream starts before the worker finishes and closes itself after task.finished.
        resp = await client.get(f"/tasks/{task['id']}/events", headers=alice.headers, timeout=60)
        out, _ = worker.communicate(timeout=60)
    finally:
        if worker.poll() is None:
            worker.kill()
    assert worker.returncode == 0, out
    events = parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "task.created" and types[-1] == "task.finished"
    assert events[-1]["payload"]["status"] == "completed"
    ids = [e["id"] for e in events]
    assert ids == sorted(set(ids))  # ordered, no duplicates
    # the stream carried the whole story
    order = [
        "run.segment_started",
        "plan.created",
        "subtask.dispatched",
        "review.verdict",
        "review.verdict",
        "subtask.finished",
        "task.finished",
    ]
    it = iter(types)
    assert all(t in it for t in order), types
    assert types.count("llm.call") == 7  # incl. the final review of the deliverable

    final = (await client.get(f"/tasks/{task['id']}", headers=alice.headers)).json()
    assert final["status"] == "completed"
    assert final["result"]["final"].startswith("Final deliverable")
    assert final["result"]["subtasks"]["draft"]["revisions"] == 1


async def test_unknown_scenario_is_rejected(client: httpx.AsyncClient, alice: SeededUser) -> None:
    r = await client.post(
        "/tasks", json={"request": "x", "scenario": "nope"}, headers=alice.headers
    )
    assert r.status_code == 422 and "unknown scenario" in r.text


async def test_scenario_is_refused_in_real_mode(test_db: str, alice: SeededUser) -> None:
    app: FastAPI = create_app(get_settings().model_copy(update={"llm_mode": LLMMode.REAL}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/tasks", json={"request": "x", "scenario": "default"}, headers=alice.headers
            )
    assert r.status_code == 422 and "only allowed in fake mode" in r.text
