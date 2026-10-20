from fastapi.testclient import TestClient

from app.main import app


def test_healthcheck_returns_ok_and_existing_health_route_is_unchanged() -> None:
    with TestClient(app) as client:
        healthcheck_response = client.get("/api/healthcheck")
        health_response = client.get("/api/health")
        openapi_response = client.get("/openapi.json")

    assert healthcheck_response.status_code == 200
    assert healthcheck_response.json() == {"status": "ok"}

    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
    assert health_response.json()["service"] == "ai-dev-team"
    assert "llm" in health_response.json()

    assert openapi_response.status_code == 200
    assert (
        openapi_response.json()["paths"]["/api/healthcheck"]["get"]["summary"]
        == "Health check"
    )
