"""The REST app (port 8000). The MCP app (port 8001) runs beside it in the same process."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mulligan.api.auth import TokenAuthMiddleware
from mulligan.api.routes import router
from mulligan.services import Services
from mulligan.settings import Settings

ServicesFactory = Callable[[Settings], Awaitable[Services]]


def create_app(
    settings: Settings | None = None,
    *,
    services: Services | None = None,
    build: ServicesFactory | None = None,
) -> FastAPI:
    """`services` is used as is (tests). Otherwise `build` makes them at startup."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = services is None
        svc = services if services is not None else await (build or _no_build)(settings)
        app.state.services = svc
        if owned:
            svc.start_background()
        try:
            yield
        finally:
            if owned:
                await svc.close()

    app = FastAPI(
        title="Mulligan gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    if services is not None:
        app.state.services = services
    # Added first, so it runs inside CORS (Starlette wraps later middleware around earlier ones).
    app.add_middleware(
        TokenAuthMiddleware,
        dashboard_token=settings.dashboard_token,
        service_token=settings.service_token,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Mulligan-Service-Token",
            "Last-Event-ID",
        ],
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


async def _no_build(settings: Settings) -> Services:
    raise RuntimeError("create_app needs `services` or `build`")
