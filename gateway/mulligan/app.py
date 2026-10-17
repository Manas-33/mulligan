"""The REST app (port 8000). The MCP app (port 8001) runs beside it in the same process."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mulligan.api.auth import TokenAuthMiddleware
from mulligan.settings import Settings

api = APIRouter(prefix="/api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Mulligan gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
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

    app.include_router(api)
    return app
