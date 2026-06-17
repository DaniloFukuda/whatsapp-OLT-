from fastapi.testclient import TestClient

from app.core.config import get_settings


def test_get_webhook_with_correct_token(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    response = TestClient(client).get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-token",
            "hub.challenge": "abc123",
        },
    )

    assert response.status_code == 200
    assert response.text == "abc123"


def test_get_webhook_with_incorrect_token(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    response = TestClient(client).get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "abc123",
        },
    )

    assert response.status_code == 403


def test_webhook_com_status_delivered_nao_envia_resposta(client, monkeypatch):
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.fake",
                                    "status": "delivered",
                                    "timestamp": "1730000000",
                                    "recipient_id": "556198266551",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    response = TestClient(client).post("/webhook/whatsapp", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "messages": []}
