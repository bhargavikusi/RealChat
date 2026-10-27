"""HTTP API entry point.

Run with: uv run uvicorn --factory apps.api.main:create_app
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.config.settings import Settings, load_settings
from backend.core.logging import configure_logging
from backend.core.redis import create_redis, ping_redis
from backend.database.engine import create_engine, ping_database

logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0


async def _check(name: str, probe: Callable[[], Awaitable[None]]) -> bool:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            await probe()
    except Exception:
        # Details go to the server log (redacted), never to the HTTP response.
        logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
        return False
    return True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = create_engine(settings.database_url.get_secret_value())
        app.state.redis = create_redis(settings.redis_url.get_secret_value())
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    # Interactive docs and the schema would advertise every endpoint publicly.
    app = FastAPI(
        title="RealChat API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """The process is running. Does not touch external services."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """The process can reach PostgreSQL and Redis."""
        state = request.app.state
        database_ok, redis_ok = await asyncio.gather(
            _check("database", lambda: ping_database(state.engine)),
            _check("redis", lambda: ping_redis(state.redis)),
        )
        checks: dict[str, Any] = {
            "database": "ok" if database_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        }
        all_ok = database_ok and redis_ok
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content={"status": "ready" if all_ok else "not_ready", "checks": checks},
        )

    return app
