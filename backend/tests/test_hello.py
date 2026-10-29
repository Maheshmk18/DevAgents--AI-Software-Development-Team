from fastapi.testclient import TestClient

from app.main import app


def test_hello_returns_expected_message() -> None:
    with TestClient(app) as client:
        response = client.get("/api/hello")

    assert response.status_code == 200
    assert response.json() == {"message": "Agent workflow works"}