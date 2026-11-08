"""OpenTelemetry (ADR-019): one trace per task whose id is the task UUID, and span attributes
whose ids match our llm_calls / tool_calls / events rows. In-memory exporter; no Jaeger."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.db.checkpointer import open_checkpointer
from agentops.observability import tracing
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Settings, get_settings
from tests.integration.conftest import SeededUser, create_task, decide

pytestmark = pytest.mark.integration


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tracing.use_provider(tracing.make_provider("test", SimpleSpanProcessor(exporter)))
    yield exporter
    tracing.use_provider(None)


def attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


async def run(engine: AsyncEngine, saver: Any) -> None:
    s = get_settings()
    job = await jobs.claim(engine, "w-otel", s.job_lease_seconds)
    assert job is not None
    await Worker(s, engine, saver, "w-otel").process(job)


async def rows(engine: AsyncEngine, sql: str, tid: uuid.UUID) -> list[tuple]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(text(sql), {"t": tid})]


async def test_span_ids_match_events_and_call_rows(
    engine: AsyncEngine, alice: SeededUser, spans: InMemorySpanExporter
) -> None:
    tid = await create_task(
        engine, alice, "share", graph="main", config={"scenario": "proposes_email"}
    )
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as saver:
        await run(engine, saver)  # pauses at action_gate
        await decide(engine, tid, "approve_action", {"action": "approve"})
        await run(engine, saver)  # resumes, sends, finishes

    finished = spans.get_finished_spans()
    assert finished
    # one trace for the whole task, across both run segments; its id is the task id
    assert {s.context.trace_id for s in finished} == {tid.int}
    assert tracing.trace_id_for(tid) == format(tid.int, "032x")
    segments = [s for s in finished if s.name == "run.segment"]
    assert len(segments) == 2 and all(s.parent is None for s in segments)
    assert {attrs(s)["agentops.job_kind"] for s in segments} == {"run", "resume"}

    llm_events = dict(
        await rows(
            engine,
            "SELECT (payload->>'llm_call_id')::bigint, id FROM events "
            "WHERE task_id=:t AND type='llm.call'",
            tid,
        )
    )
    llm_spans = {attrs(s)["agentops.llm_call_id"]: s for s in finished if s.name == "llm.call"}
    assert set(llm_spans) == set(llm_events)  # every call row has exactly one span
    for call_id, span in llm_spans.items():
        assert attrs(span)["agentops.event_id"] == llm_events[call_id]
        assert attrs(span)["agentops.task_id"] == str(tid)
        assert span.parent is not None  # inside a node / segment, not floating

    tool_events = dict(
        await rows(
            engine,
            "SELECT (payload->>'tool_call_id')::bigint, id FROM events "
            "WHERE task_id=:t AND type='tool.call'",
            tid,
        )
    )
    tool_spans = {attrs(s)["agentops.tool_call_id"]: s for s in finished if s.name == "tool.call"}
    assert set(tool_spans) == set(tool_events) and len(tool_spans) == 2  # proposed, executed
    for call_id, span in tool_spans.items():
        assert attrs(span)["agentops.event_id"] == tool_events[call_id]

    # every event row appears as a span event with its id
    event_ids = {r[0] for r in await rows(engine, "SELECT id FROM events WHERE task_id=:t", tid)}
    span_event_ids = {
        e.attributes["agentops.event_id"]
        for s in finished
        for e in s.events
        if e.attributes and "agentops.event_id" in e.attributes
    }
    # worker-level events emitted outside a segment span (task.status, run.segment_started,
    # approvals via the API, task.finished) are not inside a span; graph events all are
    graph_events = {
        r[0]
        for r in await rows(
            engine, "SELECT id FROM events WHERE task_id=:t AND node IS NOT NULL", tid
        )
    }
    assert graph_events and graph_events <= span_event_ids <= event_ids

    nodes = {attrs(s)["agentops.node"] for s in finished if s.name.startswith("node.")}
    assert {"intake", "plan", "schedule", "action_gate", "execute_actions", "final_review"} <= nodes
    gate = [s for s in finished if s.name == "node.action_gate"]
    assert any(attrs(s).get("agentops.paused") for s in gate)  # the interrupt: a pause
    assert all(s.status.status_code.name != "ERROR" for s in gate)

    # spans carry ids and numbers, never text: no prompt/output/argument content
    blob = repr([dict(s.attributes or {}) for s in finished])
    assert "team@example.com" not in blob and "Summary ready" not in blob


def test_tracing_is_off_unless_enabled() -> None:
    assert tracing.setup(Settings(otel_enabled=False), "x") is None
    tracing.use_provider(None)
    assert tracing.tracer().__class__.__name__ == "NoOpTracer"
