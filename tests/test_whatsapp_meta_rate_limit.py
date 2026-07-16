from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.db import Base, get_db
from app.integrations.whatsapp.client import send_whatsapp_message
from app.models import Operador, PerfilOperador, WhatsAppPhoneQueueItem, WhatsAppProcessedMessage
from app.routes import webhook


BODY_WITH_FOUR_OPTIONS = "Escolha uma opcao:\n\n1. Azul\n2. Verde\n3. Amarelo\n4. Vermelho"
BODY_WITH_TWO_OPTIONS = "Deseja continuar?\n\n1. Sim\n2. Nao"


def meta_error(code: int, message: str, *, status_code: int = 400) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "error": {
                "message": message,
                "type": "OAuthException",
                "code": code,
                "error_data": {"details": message},
                "fbtrace_id": f"TRACE_{code}",
            }
        },
    )


def meta_success(message_id: str) -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": message_id}]})


def assert_structured_error(
    result: dict[str, Any],
    *,
    category: str,
    retryable: bool,
    fallback_allowed: bool,
    recipient_scoped: bool,
) -> None:
    assert result["status"] == "error"
    assert result["error_class"] == category
    assert result["retryable"] is retryable
    assert result["fallback_allowed"] is fallback_allowed
    assert result["recipient_scoped"] is recipient_scoped
    assert result["response"]["error"]["category"] == category
    assert result["response"]["error"]["fallback_allowed"] is fallback_allowed


@pytest.fixture(autouse=True)
def whatsapp_cloud_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "fake-phone-number-id")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v25.0")
    yield
    get_settings.cache_clear()


def test_131056_em_lista_nao_faz_fallback_textual_imediato(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(131056, "(#131056) Pair rate limit hit")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900001", BODY_WITH_FOUR_OPTIONS)

    # Regression guard for the production incident: 131056 must not trigger text fallback.
    assert len(calls) == 1
    assert calls[0]["type"] == "interactive"
    assert calls[0]["interactive"]["type"] == "list"
    assert "fallback_from" not in result
    assert result["meta_code"] == 131056
    assert_structured_error(
        result,
        category="PAIR_RATE_LIMIT",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


def test_regra_desejada_131056_nao_faz_fallback_imediato(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(131056, "(#131056) Pair rate limit hit")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900002", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="PAIR_RATE_LIMIT",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


def test_131056_em_botoes_nao_faz_fallback_textual_imediato(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(131056, "(#131056) Pair rate limit hit")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900013", BODY_WITH_TWO_OPTIONS)

    assert len(calls) == 1
    assert calls[0]["type"] == "interactive"
    assert calls[0]["interactive"]["type"] == "button"
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="PAIR_RATE_LIMIT",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


def test_erro_payload_interativo_ainda_caracteriza_fallback_textual(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []
    responses = [
        meta_error(100, "(#100) Invalid parameter"),
        meta_success("wamid.fallback.text"),
    ]

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900003", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 2
    assert calls[0]["type"] == "interactive"
    assert calls[1]["type"] == "text"
    assert result["status"] == "sent"
    assert result["message_id"] == "wamid.fallback.text"
    assert result["fallback_from"] == "list"
    assert result["interactive_error"]["error"]["code"] == 100
    assert result["interactive_error"]["error"]["category"] == "INVALID_INTERACTIVE_PAYLOAD"


def test_falha_do_fallback_textual_nao_tenta_terceiro_envio(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(100, "(#100) Invalid parameter")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900014", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 2
    assert calls[0]["type"] == "interactive"
    assert calls[1]["type"] == "text"
    assert result["status"] == "error"
    assert result["fallback_from"] == "list"
    assert result["interactive_error"]["error"]["category"] == "INVALID_INTERACTIVE_PAYLOAD"


def test_regra_desejada_erro_autenticacao_nao_faz_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(190, "(#190) Invalid OAuth access token")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900004", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="AUTHENTICATION_OR_PERMISSION",
        retryable=False,
        fallback_allowed=False,
        recipient_scoped=False,
    )


def test_regra_desejada_erro_temporario_generico_nao_gera_tempestade(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(2, "(#2) Service temporarily unavailable", status_code=500)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900005", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="TEMPORARY_PLATFORM_ERROR",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


@pytest.mark.parametrize("code", [4, 80007, 130429])
def test_global_throttle_nao_faz_fallback_e_eh_retryable(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(code, f"(#{code}) Throttle")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900015", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="GLOBAL_THROTTLE",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=False,
    )


def test_quality_restriction_131048_nao_faz_fallback_nem_retry_imediato(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return meta_error(131048, "(#131048) Spam rate limit hit")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900016", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="QUALITY_RESTRICTION",
        retryable=False,
        fallback_allowed=False,
        recipient_scoped=False,
    )


def test_json_de_erro_incompleto_nao_quebra_parser(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return httpx.Response(400, json={"error": {"message": "unclassified failure"}})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900017", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert result["status_code"] == 400
    assert result["response"]["error"]["message"] == "unclassified failure"
    assert_structured_error(
        result,
        category="PERMANENT_UNKNOWN_ERROR",
        retryable=False,
        fallback_allowed=False,
        recipient_scoped=False,
    )


def test_corpo_nao_json_nao_quebra_parser_e_preserva_status(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900018", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert result["status_code"] == 502
    assert_structured_error(
        result,
        category="TEMPORARY_PLATFORM_ERROR",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


def test_timeout_ou_conexao_vira_temporario_sem_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = send_whatsapp_message("351999900019", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 1
    assert "fallback_from" not in result
    assert_structured_error(
        result,
        category="TEMPORARY_PLATFORM_ERROR",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )


def test_telefone_a_com_131056_nao_bloqueia_telefone_b(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        if kwargs["json"]["to"] == "351999900020":
            return meta_error(131056, "(#131056) Pair rate limit hit")
        return meta_success("wamid.phone.b")

    monkeypatch.setattr(httpx, "post", fake_post)

    limited = send_whatsapp_message("351999900020", BODY_WITH_FOUR_OPTIONS)
    unaffected = send_whatsapp_message("351999900021", BODY_WITH_FOUR_OPTIONS)

    assert len(calls) == 2
    assert calls[0]["to"] == "351999900020"
    assert calls[1]["to"] == "351999900021"
    assert_structured_error(
        limited,
        category="PAIR_RATE_LIMIT",
        retryable=True,
        fallback_allowed=False,
        recipient_scoped=True,
    )
    assert unaffected["status"] == "sent"
    assert unaffected["message_id"] == "wamid.phone.b"


def test_webhook_local_primeiro_envio_sucesso_segundo_131056_mesmo_evento(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database_url = f"sqlite:///{tmp_path / 'webhook-rate-limit.db'}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(webhook.router)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    phone = "351999900010"
    with TestingSessionLocal() as db:
        db.add(
            Operador(
                telefone_whatsapp=phone,
                nome_operador="Operador Rate Limit",
                perfil=PerfilOperador.GESTOR,
                ativo=True,
            )
        )
        db.commit()

    calls: list[dict[str, Any]] = []
    responses = [
        meta_success("wamid.first.ok"),
        meta_error(131056, "(#131056) Pair rate limit hit"),
    ]

    def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "351000000000",
                                "phone_number_id": "fake-phone-number-id",
                            },
                            "contacts": [{"wa_id": phone, "profile": {"name": "Rate"}}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": "wamid.inbound.rate-limit",
                                    "timestamp": "1720000000",
                                    "type": "text",
                                    "text": {"body": "resumo"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    response = TestClient(app).post("/webhook/whatsapp", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert len(calls) == 2
    assert calls[0]["to"] == calls[1]["to"] == phone
    assert data["messages"][0]["status"] == "sent"
    assert data["messages"][1]["status"] == "error"
    assert data["messages"][1]["response"]["error"]["code"] == 131056

    with TestingSessionLocal() as db:
        processed = (
            db.query(WhatsAppProcessedMessage)
            .filter_by(message_id="wamid.inbound.rate-limit")
            .one()
        )
        assert processed.status == "COMPLETED"
        assert db.query(WhatsAppPhoneQueueItem).count() == 0


def test_arquitetura_desejada_limite_por_telefone_nao_afeta_outro_destinatario():
    class PlannedRecipientRateLimit:
        def __init__(self) -> None:
            self._limited_until_by_phone: dict[str, datetime] = {}

        def mark_limited(self, phone: str, limited_until: datetime) -> None:
            self._limited_until_by_phone[phone] = limited_until

        def can_send(self, phone: str, now: datetime) -> bool:
            limited_until = self._limited_until_by_phone.get(phone)
            return limited_until is None or now >= limited_until

    gate = PlannedRecipientRateLimit()
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    gate.mark_limited("351999900011", datetime(2026, 7, 16, 12, 5, tzinfo=UTC))

    assert gate.can_send("351999900011", now) is False
    assert gate.can_send("351999900012", now) is True
