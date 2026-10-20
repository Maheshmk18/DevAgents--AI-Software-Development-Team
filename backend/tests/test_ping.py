from fastapi.testclient import TestClient

from app.main import app


def test_ping_returns_pong_and_is_documented() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ping")
        openapi_response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json() == {"message": "pong"}

    ping_docs = openapi_response.json()["paths"]["/api/ping"]["get"]
    assert ping_docs["summary"] == "Ping the API"
    assert '"message": "pong"' in ping_docs["description"]
