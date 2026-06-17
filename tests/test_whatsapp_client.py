import logging

import httpx

from app.core.config import get_settings
from app.integrations.whatsapp.client import send_text_message


def clear_settings(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v25.0")
    get_settings.cache_clear()


def test_env_test_forca_envio_mock(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
    get_settings.cache_clear()

    result = send_text_message("556198266551", "Ola")

    assert result == {"to": "556198266551", "body": "Ola", "status": "mocked"}


def test_envio_real_simulado_com_sucesso(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v25.0")
    get_settings.cache_clear()
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"messages": [{"id": "wamid.fake"}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_text_message("556198266551", "Mensagem real simulada")

    assert result == {
        "to": "556198266551",
        "body": "Mensagem real simulada",
        "status": "sent",
        "message_id": "wamid.fake",
    }
    assert calls[0]["url"] == "https://graph.facebook.com/v25.0/1148807428322172/messages"
    assert calls[0]["headers"] == {
        "Authorization": "Bearer fake-token",
        "Content-Type": "application/json",
    }
    assert calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "to": "556198266551",
        "type": "text",
        "text": {"body": "Mensagem real simulada"},
    }


def test_erro_http_da_meta_retorna_status_e_resposta_sem_token(monkeypatch, caplog):
    clear_settings(monkeypatch)
    token = "super-secret-token"
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", token)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    get_settings.cache_clear()

    def fake_post(url, headers, json, timeout):
        return httpx.Response(
            400,
            json={"error": {"message": f"Invalid token {token}", "code": 190}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    with caplog.at_level(logging.ERROR):
        result = send_text_message("556198266551", "Mensagem")

    assert result["status"] == "error"
    assert result["status_code"] == 400
    assert result["response"]["error"]["message"] == "Invalid token [REDACTED]"
    assert token not in str(result)
    assert token not in caplog.text


def test_modo_mock_quando_config_incompleta(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "")
    get_settings.cache_clear()

    result = send_text_message("556198266551", "Ola")

    assert result["status"] == "mocked"


def test_force_mock_impede_envio_real(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    get_settings.cache_clear()

    def fake_post(*args, **kwargs):
        raise AssertionError("httpx.post nao deveria ser chamado em force_mock")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_text_message("556198266551", "Mensagem", force_mock=True)

    assert result == {"to": "556198266551", "body": "Mensagem", "status": "mocked"}
