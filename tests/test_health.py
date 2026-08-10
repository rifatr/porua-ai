"""Health endpoints, and the shape of an error response.

The error-shape test matters more than it looks. Every later slice raises errors
through the same path, so if this shape breaks we want to know immediately.
"""

from httpx import AsyncClient


async def test_healthz_reports_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reaches_the_database(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_unknown_path_uses_the_problem_json_shape(client: AsyncClient) -> None:
    response = await client.get("/no-such-endpoint")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    for field in ("type", "title", "status", "detail", "instance", "code"):
        assert field in body, f"error body is missing {field!r}"
    assert body["status"] == 404
    assert body["instance"] == "/no-such-endpoint"


async def test_openapi_schema_builds(client: AsyncClient) -> None:
    """Catches a broken response model or a bad type hint before it reaches /docs."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Porua AI"
