"""FastAPI app factory. Run: `uvicorn agentops.api.main:app`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentops.api.routes import approvals, health, memories, tasks
from agentops.api.sse import EventBroadcaster
from agentops.core.graph import build_main_graph
from agentops.db.checkpointer import open_checkpointer
from agentops.db.session import get_sessionmaker, make_engine
from agentops.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=settings.log_level)
        async with AsyncExitStack() as stack:
            engine = make_engine(settings.sqlalchemy_url, pool_size=settings.db_pool_size)
            stack.push_async_callback(engine.dispose)
            app.state.settings = settings
            app.state.engine = engine
            app.state.sessionmaker = get_sessionmaker(engine)
            # Pool opens lazily-tolerant: /health reports failures instead of crashing startup.
            app.state.checkpointer = await stack.enter_async_context(
                open_checkpointer(settings.libpq_dsn, max_size=4)
            )
            # read-only use: aget_state for task detail / checkpoints (never invoked here)
            app.state.graphs = {"main": build_main_graph(app.state.checkpointer)}
            broadcaster = EventBroadcaster(settings.libpq_dsn)
            await broadcaster.start()
            stack.push_async_callback(broadcaster.stop)
            app.state.broadcaster = broadcaster
            yield

    app = FastAPI(title="AgentOps", version="0.1.0", lifespan=lifespan)
    # ADR-042: only the UI origin; bearer tokens, not cookies, so no credentials mode.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ui_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        allow_credentials=False,
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(memories.router)
    return app


app = create_app()
