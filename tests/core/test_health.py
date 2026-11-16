import re

from httpx import AsyncClient


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_request_id_header_on_every_response(client: AsyncClient) -> None:
    generated = await client.get("/health")
    assert re.fullmatch(r"[0-9a-f-]{36}", generated.headers["X-Request-ID"])

    echoed = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert echoed.headers["X-Request-ID"] == "trace-123"

    missing = await client.get("/no-such-route")
    assert missing.status_code == 404
    assert "X-Request-ID" in missing.headers


async def test_ready_checks_database(client: AsyncClient, test_database_url: str) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


async def test_version_reports_app_and_env(client: AsyncClient) -> None:
    response = await client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"name", "version", "env"}
    assert body["env"] == "test"
