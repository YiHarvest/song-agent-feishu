from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .adapters.http_api import api_router
from .bootstrap.v1 import ApplicationRuntime, build_runtime
from .config import Settings, load_settings


def create_app(
    settings: Settings | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        current = runtime or build_runtime(settings or load_settings())
        app.state.runtime = current
        await current.start()
        try:
            yield
        finally:
            await current.close()

    app = FastAPI(title="Song Agent", version="1.0.0", lifespan=lifespan)
    app.include_router(api_router())
    return app


app = create_app()
