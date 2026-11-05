"""Worker process: claims jobs, holds the per-thread lock, heartbeats, runs graphs.

Run: `python -m agentops.queue.worker [--worker-id ID] [--once]`

Crash semantics: if this process dies mid-run, its lease expires and its advisory lock is
released by Postgres. The next worker that claims the job invokes the graph with `None`
input, so LangGraph continues from the last checkpoint instead of restarting.
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.graph import GRAPHS
from agentops.core.state import DemoState, RunContext
from agentops.db.checkpointer import open_checkpointer
from agentops.db.session import make_engine
from agentops.observability.events import emit_on
from agentops.queue import jobs
from agentops.queue.locks import thread_lock
from agentops.settings import Settings, get_settings

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
            await emit_on(conn, task_id, "task.status", payload={"status": status})


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        checkpointer: BaseCheckpointSaver[Any],
        worker_id: str,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.checkpointer = checkpointer
        self.worker_id = worker_id
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self, *, once: bool = False) -> None:
        """Poll for jobs until stopped. `once`: return after handling one claimed job."""
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            await self.reap_once()
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

    async def _execute(self, job: jobs.ClaimedJob) -> None:
        assert job.thread_id is not None
        task_id = job.thread_id
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT graph, request, status FROM tasks WHERE id = :id"), {"id": task_id}
                )
            ).first()
        if row is None:
            raise RuntimeError(f"task {task_id} not found")
        graph_name, request, status = row
        if status in {
            "completed",
            "completed_partial",
            "failed",
            "rejected",
            "cancelled",
            "budget_exceeded",
        }:
            log.info("task %s already terminal (%s); nothing to do", task_id, status)
            return
        builder = GRAPHS.get(graph_name)
        if builder is None:
            raise RuntimeError(f"unknown graph {graph_name!r}")
        graph = builder(self.checkpointer)
        config: RunnableConfig = {
            "configurable": {"thread_id": str(task_id)},
            "recursion_limit": self.settings.budget.recursion_limit,
        }
        existing = await self.checkpointer.aget_tuple(config)
        graph_input: DemoState | None = (
            None if existing is not None else {"request": request, "log": []}
        )
        await set_task_status(self.engine, task_id, "running")
        async with self.engine.begin() as c:
            await emit_on(
                c,
                task_id,
                "run.segment_started",
                payload={
                    "worker": self.worker_id,
                    "job_id": job.id,
                    "attempt": job.attempts,
                    "resumed_from_checkpoint": existing is not None,
                },
            )
        ctx = RunContext(task_id=task_id, engine=self.engine)
        result = await graph.ainvoke(graph_input, config, context=ctx)
        state = await graph.aget_state(config)
        if state.next:
            # Interrupted (approval gates arrive in Phase 4).
            await set_task_status(self.engine, task_id, "awaiting_approval")
            return
        await set_task_status(
            self.engine, task_id, "completed", result={"log": result.get("log", [])}, terminal=True
        )


async def main(
    worker_id: str | None = None, *, once: bool = False, exit_after: float | None = None
) -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    wid = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    engine = make_engine(pool_size=4)
    async with open_checkpointer() as saver:
        await saver.setup()
        worker = Worker(settings, engine, saver, wid)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)
        try:
            if exit_after is not None:
                loop.call_later(exit_after, worker.stop)
            await worker.run_forever(once=once)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id")
    p.add_argument("--once", action="store_true", help="exit after handling one claimed job")
    p.add_argument("--exit-after", type=float, help="stop polling after N seconds")
    a = p.parse_args()
    asyncio.run(main(a.worker_id, once=a.once, exit_after=a.exit_after))
