"""Worker process: claims jobs, holds the per-thread lock, heartbeats, runs graphs.

Run: `python -m agentops.queue.worker [--worker-id ID] [--once]`

Crash semantics: if this process dies mid-run, its lease expires and its advisory lock is
released by Postgres. The next worker that claims the job invokes the graph with `None`
input, so LangGraph continues from the last checkpoint instead of restarting.

Terminal states (DECISIONS invariant: exactly one per run):
- the graph's `outcome` (completed / failed / rejected)
- BudgetExceededError -> budget_exceeded;  GraphRecursionError -> failed
- a graph that ends without an outcome -> failed ("graph ended without an outcome")
- the job's attempts exhausted -> failed (retry path / reaper)
An interrupted run is not terminal: the task becomes `awaiting_approval` and a `resume` job
(payload {"resume": <value>}) continues it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops import approvals
from agentops.core import replay
from agentops.core.budget import BudgetExceededError, BudgetLedger
from agentops.core.graph import GRAPHS
from agentops.core.state import ReplayContext, RunContext
from agentops.db.checkpointer import open_checkpointer
from agentops.db.models import TERMINAL_TASK_STATUSES
from agentops.db.session import get_sessionmaker, make_engine
from agentops.memory.service import MemoryService
from agentops.models.client import ChatFactory, ModelClient, fake_factory
from agentops.models.scenarios import DEFAULT_SCENARIO, SCENARIOS
from agentops.observability import tracing
from agentops.observability.events import emit_on
from agentops.queue import jobs
from agentops.queue.locks import thread_lock
from agentops.settings import LLMMode, Settings, get_settings
from agentops.tools.catalog import build_registry, build_registry_with_mcp
from agentops.tools.mcp import manager_from_settings
from agentops.tools.registry import ToolRegistry

log = logging.getLogger("agentops.worker")

LOCK_BUSY_RETRY_SECONDS = 2.0


class LeaseLostError(Exception):
    """Another worker took over this job; stop advancing the thread."""


async def set_task_status(
    engine: AsyncEngine,
    task_id: uuid.UUID,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    terminal: bool = False,
    payload: dict[str, Any] | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tasks SET status = CAST(:s AS task_status), updated_at = now(), "
                "result = COALESCE(CAST(:r AS jsonb), result), error = COALESCE(:e, error) "
                "WHERE id = :id"
            ),
            {
                "s": status,
                "r": json.dumps(result) if result is not None else None,
                "e": error,
                "id": task_id,
            },
        )
        if terminal:
            await emit_on(
                conn, task_id, "task.finished", payload={"status": status, "error": error}
            )
        else:
            await emit_on(
                conn, task_id, "task.status", payload={"status": status, **(payload or {})}
            )


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        checkpointer: BaseCheckpointSaver[Any],
        worker_id: str,
        *,
        chat_factory: ChatFactory | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        """`chat_factory` overrides model selection (tests); otherwise fake mode uses the
        task's scripted scenario and real mode the provider factory."""
        self.settings = settings
        self.engine = engine
        self.checkpointer = checkpointer
        self.worker_id = worker_id
        self._stop = asyncio.Event()
        self._sessionmaker = get_sessionmaker(engine)
        self._ledger = BudgetLedger(self._sessionmaker)
        self._chat_factory = chat_factory
        self._real_factory: ChatFactory | None = None
        self._tools = tools if tools is not None else build_registry(settings)
        self._memory = MemoryService(settings, engine)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self, *, once: bool = False) -> None:
        """Poll for jobs until stopped. `once`: return after handling one claimed job."""
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            await self.reap_once()
            for approval_id in await approvals.sweep_expired(self.engine):
                log.info("approval %s expired; resolved as reject", approval_id)
            job = await jobs.claim(self.engine, self.worker_id, self.settings.job_lease_seconds)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self.settings.job_poll_seconds)
                continue
            await self.process(job)
            if once:
                return

    async def reap_once(self) -> list[int]:
        """Fail jobs whose lease expired on their final attempt, and end their tasks as
        `failed`, so a task can never hang after its last worker died (ADR-022)."""
        reaped = await jobs.reap_exhausted(self.engine)
        for job_id, thread_id in reaped:
            log.warning("job %s exhausted its attempts after lease expiry", job_id)
            if thread_id is not None:
                await set_task_status(
                    self.engine, thread_id, "failed", error="worker lost", terminal=True
                )
        return [job_id for job_id, _ in reaped]

    async def process(self, job: jobs.ClaimedJob) -> None:
        if job.thread_id is None:
            await jobs.fail(self.engine, job.id, self.worker_id, f"unsupported job kind {job.kind}")
            return
        async with thread_lock(self.engine, job.thread_id) as owned:
            if not owned:
                log.info("thread %s locked elsewhere; releasing job %s", job.thread_id, job.id)
                await jobs.release(self.engine, job.id, self.worker_id, LOCK_BUSY_RETRY_SECONDS)
                return
            await self._run_with_heartbeat(job)

    async def _run_with_heartbeat(self, job: jobs.ClaimedJob) -> None:
        lease_lost = asyncio.Event()
        run = asyncio.create_task(self._execute(job))
        hb = asyncio.create_task(self._heartbeat(job, run, lease_lost))
        try:
            await run
        except LeaseLostError:
            log.warning("lease lost for job %s; abandoning", job.id)
            return
        except asyncio.CancelledError:
            # Only swallow the cancellation the heartbeat caused. A cancel from outside
            # (shutdown, a caller's timeout) must propagate.
            current = asyncio.current_task()
            if lease_lost.is_set() and (current is None or current.cancelling() == 0):
                log.warning("job %s cancelled after lease loss", job.id)
                return
            raise
        except Exception as exc:
            log.exception("job %s failed", job.id)
            new_status = await jobs.fail(self.engine, job.id, self.worker_id, repr(exc))
            if new_status == "failed" and job.thread_id is not None:
                await set_task_status(
                    self.engine, job.thread_id, "failed", error=repr(exc)[:2000], terminal=True
                )
            return
        finally:
            if not run.done():
                run.cancel()
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        await jobs.complete(self.engine, job.id, self.worker_id)

    async def _heartbeat(
        self, job: jobs.ClaimedJob, run: asyncio.Task[None], lease_lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_seconds)
            ok = await jobs.heartbeat(
                self.engine, job.id, self.worker_id, self.settings.job_lease_seconds
            )
            if not ok:
                lease_lost.set()
                run.cancel()
                return

    def _model_client(self, task_config: dict[str, Any]) -> ModelClient:
        factory = self._chat_factory
        if factory is None:
            if self.settings.llm_mode is LLMMode.FAKE:
                scenario = str(task_config.get("scenario") or DEFAULT_SCENARIO)
                factory = fake_factory(SCENARIOS, scenario)
            else:
                if self._real_factory is None:
                    from agentops.models.anthropic import real_factory  # noqa: PLC0415

                    self._real_factory = real_factory(self.settings)
                factory = self._real_factory
        return ModelClient(self.settings, self._sessionmaker, factory, ledger=self._ledger)

    async def _execute(self, job: jobs.ClaimedJob) -> None:
        assert job.thread_id is not None
        task_id = job.thread_id
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT graph, request, status, config, owner_id, kind FROM tasks "
                        "WHERE id = :id"
                    ),
                    {"id": task_id},
                )
            ).first()
        if row is None:
            raise RuntimeError(f"task {task_id} not found")
        graph_name, request, status, task_config, owner_id, task_kind = row
        if status in TERMINAL_TASK_STATUSES:
            log.info("task %s already terminal (%s); nothing to do", task_id, status)
            return
        spec = GRAPHS.get(graph_name)
        if spec is None:
            raise RuntimeError(f"unknown graph {graph_name!r}")
        graph = spec.build(self.checkpointer)
        config: RunnableConfig = {
            "configurable": {"thread_id": str(task_id)},
            "recursion_limit": self.settings.budget.recursion_limit,
        }
        existing = await self.checkpointer.aget_tuple(config)
        go, graph_input = await self._select_input(
            job, graph=graph, config=config, existing=existing, spec=spec, request=request
        )
        if not go:
            return

        # This segment holds the thread's advisory lock: nothing else spends for this task, so
        # reservations still open were left by a crashed segment (ADR-024).
        orphans = await self._ledger.charge_orphans(task_id) if spec.needs_model else 0
        await set_task_status(self.engine, task_id, "running")
        async with self.engine.begin() as c:
            await emit_on(
                c,
                task_id,
                "run.segment_started",
                payload={
                    "worker": self.worker_id,
                    "job_id": job.id,
                    "job_kind": job.kind,
                    "attempt": job.attempts,
                    "resumed_from_checkpoint": existing is not None,
                    "orphaned_reservations_charged": orphans,
                },
            )
        model = self._model_client(dict(task_config or {})) if spec.needs_model else None
        replay_ctx = None
        if task_kind == "replay":
            replay_ctx = ReplayContext(lineage=await replay.lineage(self.engine, task_id))
            await self._restore_fork_workspace(task_id, dict(task_config or {}), graph)
        ctx = RunContext(
            task_id=task_id,
            engine=self.engine,
            model=model,
            tools=self._tools if spec.needs_model else None,
            owner_id=owner_id,
            memory=self._memory if spec.needs_model else None,
            replay=replay_ctx,
        )
        try:
            with tracing.task_trace(
                task_id,
                "run.segment",
                **{
                    "agentops.job_id": job.id,
                    "agentops.job_kind": job.kind,
                    "agentops.worker": self.worker_id,
                    "agentops.attempt": job.attempts,
                    "agentops.replay": replay_ctx is not None,
                },
            ):
                await graph.ainvoke(graph_input, config, context=ctx)
        except BudgetExceededError as exc:
            # Can happen inside a parallel branch, where interrupt() is forbidden: pause at the
            # worker level instead (ADR-035). The graph resumes from its last checkpoint.
            snap = await self._ledger.snapshot(task_id)
            await self._pause(
                task_id,
                kind="budget",
                payload={
                    "reason": exc.reason,
                    "max_usd": snap.max_usd,
                    "used_usd": snap.usd_used,
                    "llm_calls": snap.llm_calls,
                    "max_llm_calls": snap.max_llm_calls,
                    "tokens_used": snap.tokens_used,
                    "max_tokens": snap.max_tokens,
                    "actions": ["approve (with a raised max_usd)", "reject"],
                },
            )
            return
        except GraphRecursionError as exc:
            await self._pause(
                task_id,
                kind="recursion",
                payload={
                    "reason": f"recursion limit {self.settings.budget.recursion_limit} reached",
                    "detail": str(exc)[:500],
                    "actions": [
                        "approve (continue)",
                        "take_over (supply the deliverable)",
                        "reject",
                    ],
                },
            )
            return
        await self._after_invoke(graph, config, task_id, spec)

    async def _select_input(
        self,
        job: jobs.ClaimedJob,
        *,
        graph: Any,
        config: RunnableConfig,
        existing: Any,
        spec: Any,
        request: str,
    ) -> tuple[bool, Any]:
        """(continue?, graph input) for this job."""
        assert job.thread_id is not None
        worker_resume = job.payload.get("worker_resume") if job.kind == "resume" else None
        if worker_resume is not None:
            # A decision on a worker-level pause (budget / recursion): apply it, then either
            # finish here or continue the graph from its last checkpoint.
            ended = await self._apply_worker_decision(
                job.thread_id, worker_resume, spec.needs_model
            )
            return (not ended), None
        if job.kind == "resume":
            pending = await graph.aget_state(config)
            if existing is not None and pending.interrupts:
                return True, Command(resume=job.payload.get("resume"))
            if existing is not None and pending.next:
                # The interrupt was already consumed by an earlier attempt of THIS job, which
                # then died mid-run (lease expired, job redelivered): continue from the
                # checkpoint instead of dropping the run (found by the Phase 4 durability tests).
                log.warning("resume job %s: interrupt already consumed; continuing", job.id)
                return True, None
            log.warning("resume job %s: task %s has nothing to resume", job.id, job.thread_id)
            return False, None
        return True, (
            None if existing is not None else spec.initial_input(str(job.thread_id), request)
        )

    async def _after_invoke(
        self, graph: Any, config: RunnableConfig, task_id: uuid.UUID, spec: Any
    ) -> None:
        state = await graph.aget_state(config)
        if state.next:
            interrupts = [{"id": i.id, "value": i.value} for i in state.interrupts]
            async with self.engine.begin() as c:
                for i in state.interrupts:
                    v = i.value if isinstance(i.value, dict) else {}
                    if "kind" in v and "key" in v:
                        await approvals.record_pending(
                            c,
                            task_id,
                            kind=v["kind"],
                            key=v["key"],
                            payload=v,
                            settings=self.settings,
                        )
            await set_task_status(
                self.engine,
                task_id,
                "awaiting_approval",
                result={"interrupts": interrupts},
                payload={"interrupts": interrupts},
            )
            return
        status_, result, error = spec.result(state.values)
        # the graph's consolidate node skips a run without an outcome: write its episode here
        no_outcome = spec.needs_model and state.values.get("outcome") is None
        await self._finish(task_id, status_, result, error, spec.needs_model, episode=no_outcome)

    async def _pause(self, task_id: uuid.UUID, *, kind: str, payload: dict[str, Any]) -> None:
        """Worker-level pause. The key counts only DECIDED approvals of this kind, so a crashed
        and re-run segment that pauses again produces the same key (no duplicate row)."""
        async with self.engine.begin() as c:
            decided = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM approvals WHERE task_id = :t AND level = :k "
                        "AND status <> 'pending'"
                    ),
                    {"t": task_id, "k": kind},
                )
            ).scalar_one()
            key = f"{kind}:{'b' if kind == 'budget' else 'x'}{int(decided) + 1}"
            await approvals.record_pending(
                c,
                task_id,
                kind=kind,
                key=key,
                payload=payload,
                settings=self.settings,
                source="worker",
            )
        await set_task_status(
            self.engine,
            task_id,
            "awaiting_approval",
            result={"approval_required": {"kind": kind, "key": key, **payload}},
            payload={"approval_required": kind},
        )

    async def _apply_worker_decision(
        self, task_id: uuid.UUID, resume: dict[str, Any], with_budget: bool
    ) -> bool:
        """Returns True if the decision ended the run, False if the graph should continue."""
        kind, decision = resume["kind"], resume["decision"]
        action = decision["action"]
        note = decision.get("note")
        if kind == "budget":
            if action == "approve":
                async with self.engine.begin() as c:
                    await c.execute(
                        text(
                            "UPDATE task_budgets SET "
                            "max_usd = coalesce(CAST(:usd AS numeric), max_usd), "
                            "max_calls = coalesce(CAST(:calls AS integer), max_calls), "
                            "max_tokens = coalesce(CAST(:tok AS integer), max_tokens), "
                            "updated_at = now() WHERE task_id = :t"
                        ),
                        {
                            "usd": decision.get("max_usd"),
                            "calls": decision.get("max_llm_calls"),
                            "tok": decision.get("max_tokens"),
                            "t": task_id,
                        },
                    )
                    await emit_on(
                        c,
                        task_id,
                        "budget.raised",
                        payload={
                            k: decision.get(k) for k in ("max_usd", "max_llm_calls", "max_tokens")
                        },
                    )
                return False
            reason = "budget exhausted; raise " + (
                "expired" if note == "approval expired" else "rejected"
            )
            await self._finish(task_id, "budget_exceeded", {}, reason, with_budget, episode=True)
            return True
        if kind == "recursion":
            if action == "approve":
                return False
            if action == "take_over":
                await self._finish(
                    task_id,
                    "completed",
                    {"final": decision["deliverable"], "taken_over": True},
                    "taken over by a human after the recursion limit",
                    with_budget,
                    episode=True,
                )
                return True
            await self._finish(
                task_id,
                "failed",
                {},
                "recursion limit reached; continuation rejected",
                with_budget,
                episode=True,
            )
            return True
        raise RuntimeError(f"unknown worker-level approval kind {kind!r}")

    async def _finish(
        self,
        task_id: uuid.UUID,
        status: str,
        result: dict[str, Any],
        error: str | None,
        with_budget: bool,
        *,
        episode: bool = False,
    ) -> None:
        if with_budget:
            result = {**result, "budget": (await self._ledger.snapshot(task_id)).model_dump()}
        if episode and with_budget and not await self._is_replay(task_id):
            # Terminal reached outside the graph (budget / recursion decisions, or a graph that
            # ended without an outcome): consolidate still runs, as an episode only (no LLM
            # call, so it can't overshoot an exhausted budget). Phase 4 change 2.
            await self._write_episode(task_id, status, error)
        if status == "completed" and error is not None:
            result = {**result, "note": error}
            error = None
        await set_task_status(
            self.engine, task_id, status, result=result, error=error, terminal=True
        )

    async def _is_replay(self, task_id: uuid.UUID) -> bool:
        async with self.engine.connect() as c:
            kind = (
                await c.execute(text("SELECT kind FROM tasks WHERE id = :t"), {"t": task_id})
            ).scalar_one_or_none()
        return kind == "replay"

    async def _restore_fork_workspace(
        self, task_id: uuid.UUID, task_config: dict[str, Any], graph: Any
    ) -> None:
        """A branch gets its own copy of the source workspace as of the fork point (ADR-040).
        Idempotent; runs before every segment but copies once."""
        fork = task_config.get("fork") or {}
        source = uuid.UUID(fork["source_task_id"])
        wave = int(fork.get("wave") or 0)
        src_state = await graph.aget_state({"configurable": {"thread_id": str(source)}})
        live_wave = int((src_state.values or {}).get("wave") or 0)
        how = await asyncio.to_thread(
            replay.restore_workspace, self.settings, task_id, source, wave, live_wave
        )
        async with self.engine.begin() as c:
            await emit_on(
                c,
                task_id,
                "replay.workspace_restored",
                payload={"source_task_id": str(source), "wave": wave, "from": how},
                dedupe_key="replay:workspace",
            )

    async def _write_episode(self, task_id: uuid.UUID, status: str, reason: str | None) -> None:
        async with self.engine.connect() as c:
            owner, request = (
                await c.execute(
                    text("SELECT owner_id, request FROM tasks WHERE id = :t"), {"t": task_id}
                )
            ).one()
        written = await self._memory.write_episode(
            task_id=task_id, user_id=owner, request=request, status=status, reason=reason
        )
        async with self.engine.begin() as c:
            await emit_on(
                c,
                task_id,
                "memory.consolidated",
                payload={"written": len(written.ids), "llm_skipped": "worker-level terminal"},
                dedupe_key="consolidate:worker",
            )


async def main(
    worker_id: str | None = None, *, once: bool = False, exit_after: float | None = None
) -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    wid = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    provider = tracing.setup(settings, "agentops-worker")  # None unless OTEL_ENABLED
    engine = make_engine(pool_size=4)
    async with open_checkpointer() as saver:
        await saver.setup()
        factory: ChatFactory | None = None
        if settings.llm_mode is LLMMode.REAL:
            from agentops.models.anthropic import real_factory  # noqa: PLC0415

            factory = real_factory(settings)  # fail fast on misconfiguration
        mcp = manager_from_settings(settings)  # unknown server names fail fast here
        tools = await build_registry_with_mcp(settings, mcp)
        log.info("tools loaded: %s", ", ".join(sorted(tools.specs)))
        if mcp is not None and mcp.skipped:
            log.warning("MCP tools/servers skipped: %s", mcp.skipped)
        worker = Worker(settings, engine, saver, wid, chat_factory=factory, tools=tools)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)
        try:
            if exit_after is not None:
                loop.call_later(exit_after, worker.stop)
            await worker.run_forever(once=once)
        finally:
            if mcp is not None:
                await mcp.close()
            await engine.dispose()
            if provider is not None:
                provider.shutdown()  # flush pending spans


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id")
    p.add_argument("--once", action="store_true", help="exit after handling one claimed job")
    p.add_argument("--exit-after", type=float, help="stop polling after N seconds")
    a = p.parse_args()
    asyncio.run(main(a.worker_id, once=a.once, exit_after=a.exit_after))
