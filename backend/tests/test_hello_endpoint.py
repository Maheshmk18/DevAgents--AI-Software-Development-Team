from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_hello_endpoint_returns_agent_workflow_message():
    response = client.get("/api/hello")
    assert response.status_code == 200
    assert response.json() == {"message": "Agent workflow works"}