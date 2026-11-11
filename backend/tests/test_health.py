from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import __version__
from backend.app.main import app

client = TestClient(app)


def test_health_reports_ok_before_feed_and_worker_exist():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "symbol": "XAUUSD",
        "feed": "not_configured",
        "worker": "not_running",
    }


def test_interactive_api_docs_are_not_public():
    # Every route except /api/health must require login (from Phase 7),
    # so the auto-generated docs stay switched off.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
