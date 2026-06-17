from fastapi.testclient import TestClient


def test_health(client):
    response = TestClient(client).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
