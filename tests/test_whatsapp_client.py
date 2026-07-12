import logging

import httpx

from app.core.config import get_settings
from app.integrations.whatsapp.client import send_text_message, send_whatsapp_message


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


def test_send_whatsapp_message_usa_botoes_para_sim_nao_em_mock(monkeypatch):
    clear_settings(monkeypatch)

    result = send_whatsapp_message("556198266551", "Deseja continuar?\n\n1. Sim\n2. Nao")

    assert result == {
        "to": "556198266551",
        "body": "Deseja continuar?",
        "status": "mocked",
        "type": "interactive",
        "interactive_type": "button",
        "buttons": [{"id": "option_1", "title": "Sim"}, {"id": "option_2", "title": "Nao"}],
    }


def test_send_whatsapp_message_usa_ids_especificos_para_mao_de_obra(monkeypatch):
    clear_settings(monkeypatch)

    result = send_whatsapp_message(
        "556198266551",
        "Este pedido necessita de mão de obra?\n\n1. Sim\n2. Não",
    )

    assert result["interactive_type"] == "button"
    assert result["buttons"] == [
        {"id": "pedido_mao_obra_sim", "title": "✅ Sim"},
        {"id": "pedido_mao_obra_nao", "title": "❌ Não"},
    ]


def test_send_whatsapp_message_usa_ids_especificos_para_residuo_contentor(monkeypatch):
    clear_settings(monkeypatch)

    result = send_whatsapp_message(
        "556198266551",
        "Resíduo do contentor 1/3:\n\n1. Entulho Limpo\n2. Entulho Misto",
    )

    assert result["interactive_type"] == "button"
    assert result["buttons"] == [
        {"id": "pedido_residuo_limpo", "title": "Entulho Limpo"},
        {"id": "pedido_residuo_misto", "title": "Entulho Misto"},
    ]


def test_send_whatsapp_message_usa_botoes_para_duas_opcoes_genericas(monkeypatch):
    clear_settings(monkeypatch)

    result = send_whatsapp_message("556198266551", "Qual a forma?\n\n1. MBWay\n2. Transferencia")

    assert result == {
        "to": "556198266551",
        "body": "Qual a forma?",
        "status": "mocked",
        "type": "interactive",
        "interactive_type": "button",
        "buttons": [{"id": "option_1", "title": "MBWay"}, {"id": "option_2", "title": "Transferencia"}],
    }


def test_send_whatsapp_message_envia_payload_interactive_para_duas_opcoes(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    get_settings.cache_clear()
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"messages": [{"id": "wamid.button"}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("556198266551", "Deseja adicionar mais uma foto?\n\n1. Sim\n2. Nao")

    assert result["status"] == "sent"
    assert result["message_id"] == "wamid.button"
    assert result["type"] == "interactive"
    assert result["interactive_type"] == "button"
    assert calls[0]["json"]["type"] == "interactive"
    assert calls[0]["json"]["interactive"]["type"] == "button"
    assert calls[0]["json"]["interactive"]["body"] == {"text": "Deseja adicionar mais uma foto?"}
    assert calls[0]["json"]["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "option_1", "title": "Sim"}},
        {"type": "reply", "reply": {"id": "option_2", "title": "Nao"}},
    ]


def test_send_whatsapp_message_usa_botoes_quando_tiver_tres_opcoes(monkeypatch):
    clear_settings(monkeypatch)

    body = "Quando sera a entrega?\n\n1. Hoje\n2. Amanha\n3. Outra data"
    result = send_whatsapp_message("556198266551", body)

    assert result == {
        "to": "556198266551",
        "body": "Quando sera a entrega?",
        "status": "mocked",
        "type": "interactive",
        "interactive_type": "button",
        "buttons": [
            {"id": "option_1", "title": "Hoje"},
            {"id": "option_2", "title": "Amanha"},
            {"id": "option_3", "title": "Outra data"},
        ],
    }


def test_send_whatsapp_message_trunca_titulo_longo_de_botao(monkeypatch):
    clear_settings(monkeypatch)

    body = "Escolha uma opcao\n\n1. Botao com titulo muito grande\n2. Opcao curta"
    result = send_whatsapp_message("556198266551", body)

    assert result == {
        "to": "556198266551",
        "body": "Escolha uma opcao",
        "status": "mocked",
        "type": "interactive",
        "interactive_type": "button",
        "buttons": [
            {"id": "option_1", "title": "Botao com titulo..."},
            {"id": "option_2", "title": "Opcao curta"},
        ],
    }


def test_send_whatsapp_message_faz_fallback_para_texto_quando_interativo_falha(monkeypatch):
    clear_settings(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    get_settings.cache_clear()
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "interactive failed"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.text"}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    original_body = "Deseja continuar?\n\n1. Sim\n2. Nao"
    result = send_whatsapp_message("556198266551", original_body)

    assert calls[0]["type"] == "interactive"
    assert calls[1] == {
        "messaging_product": "whatsapp",
        "to": "556198266551",
        "type": "text",
        "text": {"body": original_body},
    }
    assert result["status"] == "sent"
    assert result["message_id"] == "wamid.text"
    assert result["fallback_from"] == "button"
