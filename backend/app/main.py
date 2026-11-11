"""FastAPI application.

Phase 0 exposes only the public health endpoint. The interactive API docs are
switched off on purpose: every route except /api/health must require login,
and login arrives with the dashboard in Phase 7.
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.app import __version__
from backend.app.config import get_settings

app = FastAPI(
    title="TradePilot AI",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness check. Feed and worker report real states from Phases 3 and 6."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "symbol": settings.symbol,
        "feed": "not_configured",
        "worker": "not_running",
    }
