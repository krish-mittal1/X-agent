from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


async def test_health_liveness(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-correlation-id"]


async def test_ready_reports_database_and_schema(client) -> None:  # type: ignore[no-untyped-def]
    response = await client.get("/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["pgvector"]["ok"] is True
    assert body["checks"]["migrations"]["ok"] is True
    assert "revision" in body["checks"]["migrations"]["detail"]


async def test_metrics_requires_token(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get("/metrics/summary")).status_code == 401
    bad = await client.get("/metrics/summary", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 403


async def test_metrics_summary_ok(client, admin_headers) -> None:  # type: ignore[no-untyped-def]
    response = await client.get("/metrics/summary", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["posts"]["total"] == 0
    assert body["llm_spend"]["month_usd"] >= 0


async def test_application_lifespan_starts_and_stops(settings) -> None:  # type: ignore[no-untyped-def]
    app = create_app(settings, start_scheduler=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            response = await http.get("/ready")
        assert response.status_code == 200, response.text
    from app.memory.database import get_engine

    with pytest.raises(RuntimeError, match="not initialised"):
        get_engine()


async def test_admin_api_disabled_without_token(settings) -> None:  # type: ignore[no-untyped-def]
    no_token = Settings(
        _env_file=None,
        app_env="test",
        database_url=settings.database_url,
        admin_api_token=None,
    )
    app = create_app(no_token, start_scheduler=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        response = await http.get("/metrics/summary", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert "ADMIN_API_TOKEN" not in response.text  # don't leak env-var guidance to callers
