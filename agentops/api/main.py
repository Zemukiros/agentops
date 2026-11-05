"""FastAPI app factory. Run: `uvicorn agentops.api.main:app`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from agentops.api.routes import health, tasks
from agentops.api.sse import EventBroadcaster
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
            broadcaster = EventBroadcaster(settings.libpq_dsn)
            await broadcaster.start()
            stack.push_async_callback(broadcaster.stop)
            app.state.broadcaster = broadcaster
            yield

    app = FastAPI(title="AgentOps", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(tasks.router)
    return app


app = create_app()
