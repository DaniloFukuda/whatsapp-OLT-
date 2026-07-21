from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.mensagem_webhook import MensagemWebhook, StatusMensagemWebhook
from app.routes import webhook
from app.services.webhook_dedup_service import WebhookDedupService


def normalized(message_id="wamid.test", *, raw=None, kind="text"):
    payload = raw if raw is not None else {"id": message_id, "type": kind, "text": {"body": "menu"}}
    return NormalizedWhatsAppMessage(
        telefone="351900000000",
        tipo=kind,
        message_id=message_id,
        texto="menu",
        raw=payload,
    )


def meta_payload(message_id="wamid.endpoint", *, kind="text", body=None, interactive_kind="button_reply"):
    message = {"from": "351900000000", "id": message_id, "type": kind}
    if kind == "text":
        message["text"] = {"body": body or "menu"}
    elif kind == "interactive":
        reply = {"id": "option_1", "title": "Opção 1"}
        if interactive_kind == "list_reply":
            reply["description"] = "Descrição sintética"
        message["interactive"] = {"type": interactive_kind, interactive_kind: reply}
    elif kind == "location":
        message["location"] = {"latitude": 38.7, "longitude": -9.1}
    elif kind in {"image", "document", "audio"}:
        message[kind] = {"id": f"media-{kind}"}
    elif kind == "contacts":
        message["contacts"] = [{"name": {"formatted_name": "Cliente"}, "phones": [{"wa_id": "351911000000"}]}]
    return messages_payload(message)


def messages_payload(*messages, statuses=None):
    value = {"messages": list(messages)}
    if statuses is not None:
        value["statuses"] = statuses
    return {"entry": [{"changes": [{"value": value}]}]}


def inbound_message(message_id, body="menu"):
    return {"from": "351900000000", "id": message_id, "type": "text", "text": {"body": body}}


class FakeRouter:
    calls = 0

    def __init__(self, db):
        self.db = db

    def handle(self, message):
        type(self).calls += 1
        return "resposta segura"

    def pop_pending_messages(self):
        return []


@pytest.fixture
def endpoint_spies(monkeypatch):
    FakeRouter.calls = 0
    sends = []
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    monkeypatch.setattr(
        webhook,
        "send_whatsapp_message",
        lambda telefone, response, force_mock=False: sends.append((telefone, response))
        or {"status": "mocked", "body": response},
    )
    return sends


def test_first_text_claim(db_session):
    """OLT-DEDUP-001"""
    decision = WebhookDedupService(db_session).adquirir(normalized("wamid.first"))
    assert decision.deve_processar is True
    assert decision.registro.status == StatusMensagemWebhook.PROCESSANDO.value


def test_completed_duplicate_is_suppressed(db_session):
    """OLT-DEDUP-002"""
    service = WebhookDedupService(db_session)
    first = service.adquirir(normalized("wamid.completed"))
    service.marcar_concluida(first.registro, True)
    second = service.adquirir(normalized("wamid.completed"))
    assert second.duplicada is True
    assert second.deve_processar is False


@pytest.mark.parametrize(
    "contract_id,message_id",
    [
        ("OLT-DEDUP-003", "wamid.concurrent.003"),
        ("OLT-DEDUP-036", "wamid.concurrent.036"),
        ("OLT-DEDUP-037", "wamid.concurrent.037"),
    ],
)
def test_concurrent_claim_has_one_winner(contract_id, message_id, tmp_path):
    db_path = tmp_path / f"{message_id}.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    barrier = threading.Barrier(2)

    def claim():
        with factory() as session:
            barrier.wait(timeout=5)
            decision = WebhookDedupService(session).adquirir(normalized(message_id))
            session_usable = session.execute(text("SELECT 1")).scalar_one() == 1
            winner_visible = session.get(MensagemWebhook, message_id) is not None
            return decision.deve_processar, session_usable, winner_visible

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert contract_id
    decisions = [result[0] for result in results]
    assert sorted(decisions) == [False, True]
    loser = next(result for result in results if result[0] is False)
    assert loser[1:] == (True, True)
    with factory() as session:
        assert session.query(MensagemWebhook).filter_by(message_id=message_id).count() == 1
    engine.dispose()


def test_same_id_same_canonical_payload(db_session):
    """OLT-DEDUP-004"""
    service = WebhookDedupService(db_session)
    first = normalized("wamid.canonical", raw={"id": "wamid.canonical", "a": 1, "b": 2})
    second = normalized("wamid.canonical", raw={"b": 2, "a": 1, "id": "wamid.canonical"})
    assert service.adquirir(first).deve_processar is True
    assert service.adquirir(second).duplicada is True


def test_same_id_different_payload_conflicts(db_session):
    """OLT-DEDUP-005"""
    service = WebhookDedupService(db_session)
    service.adquirir(normalized("wamid.conflict", raw={"id": "wamid.conflict", "text": "a"}))
    conflict = service.adquirir(normalized("wamid.conflict", raw={"id": "wamid.conflict", "text": "b"}))
    assert conflict.conflito_payload is True
    assert conflict.deve_processar is False


def test_different_ids_same_content_are_independent(db_session):
    """OLT-DEDUP-006"""
    service = WebhookDedupService(db_session)
    one = normalized("wamid.independent.1", raw={"text": "igual"})
    two = normalized("wamid.independent.2", raw={"text": "igual"})
    assert service.adquirir(one).deve_processar is True
    assert service.adquirir(two).deve_processar is True


@pytest.mark.parametrize(
    "contract_id,message_id,reason",
    [
        ("OLT-DEDUP-007", None, "MESSAGE_ID_AUSENTE"),
        ("OLT-DEDUP-008", "", "MESSAGE_ID_AUSENTE"),
        ("OLT-DEDUP-009", "x" * 513, "MESSAGE_ID_LONGO"),
    ],
)
def test_invalid_ids_are_ignored(contract_id, message_id, reason, db_session):
    decision = WebhookDedupService(db_session).adquirir(normalized(message_id))
    assert contract_id
    assert (decision.deve_processar, decision.motivo_interno) == (False, reason)
    assert db_session.query(MensagemWebhook).count() == 0


def test_meta_id_characters_are_accepted(db_session):
    """OLT-DEDUP-010"""
    decision = WebhookDedupService(db_session).adquirir(normalized("wamid.HBgLMzUxOTAxMjM0NTY3FQIAERgSQUJDREVGRw=="))
    assert decision.deve_processar is True


@pytest.mark.parametrize(
    "contract_id,kind,interactive_kind",
    [
        ("OLT-DEDUP-011", "interactive", "button_reply"),
        ("OLT-DEDUP-012", "interactive", "list_reply"),
        ("OLT-DEDUP-013", "location", None),
        ("OLT-DEDUP-014", "image", None),
        ("OLT-DEDUP-015", "audio", None),
        ("OLT-DEDUP-016", "contacts", None),
        ("OLT-DEDUP-017", "unknown", None),
    ],
)
def test_message_types_are_suppressed_on_second_delivery(
    contract_id, kind, interactive_kind, client, endpoint_spies
):
    payload = meta_payload(
        f"wamid.type.{contract_id[-3:]}",
        kind=kind,
        interactive_kind=interactive_kind or "button_reply",
    )
    http = TestClient(client)
    first = http.post("/webhook/whatsapp", json=payload, headers={"x-olt-mock-whatsapp": "true"})
    second = http.post("/webhook/whatsapp", json=payload, headers={"x-olt-mock-whatsapp": "true"})
    assert contract_id
    assert first.status_code == second.status_code == 200
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_status_is_not_an_inbound_message(client, db_session):
    """OLT-DEDUP-018"""
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.outbound", "status": "delivered"}]}}]}]}
    response = TestClient(client).post("/webhook/whatsapp", json=payload)
    assert response.status_code == 200
    assert db_session.query(MensagemWebhook).count() == 0


@pytest.mark.parametrize(
    "contract_id,operation",
    [
        ("OLT-DEDUP-019", "criar_pedido"),
        ("OLT-DEDUP-020", "confirmar_pedido"),
        ("OLT-DEDUP-021", "entrega"),
        ("OLT-DEDUP-022", "recolha"),
        ("OLT-DEDUP-023", "despejo"),
        ("OLT-DEDUP-024", "renovacao"),
        ("OLT-DEDUP-025", "alteracao"),
        ("OLT-DEDUP-026", "exclusao"),
        ("OLT-DEDUP-027", "resolver_carga"),
        ("OLT-DEDUP-028", "resolver_avaria"),
        ("OLT-DEDUP-029", "avancar_conversa"),
    ],
)
def test_business_entry_is_called_once_per_message_id(contract_id, operation, client, endpoint_spies):
    payload = meta_payload(f"wamid.business.{operation}", body=operation)
    http = TestClient(client)
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert contract_id
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_completed_record_survives_new_engine(tmp_path):
    """OLT-DEDUP-030"""
    db_path = tmp_path / "restart.db"
    url = f"sqlite:///{db_path}"
    first_engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(first_engine)
    first_factory = sessionmaker(bind=first_engine)
    with first_factory() as session:
        service = WebhookDedupService(session)
        decision = service.adquirir(normalized("wamid.restart"))
        service.marcar_concluida(decision.registro, True)
    first_engine.dispose()
    second_engine = create_engine(url, connect_args={"check_same_thread": False})
    second_factory = sessionmaker(bind=second_engine)
    with second_factory() as session:
        assert WebhookDedupService(session).adquirir(normalized("wamid.restart")).deve_processar is False
    second_engine.dispose()


def test_failure_before_router_is_retryable(client, monkeypatch, db_session, endpoint_spies):
    """OLT-DEDUP-031"""
    class BrokenConstructor:
        def __init__(self, db):
            raise RuntimeError("transient")

    monkeypatch.setattr(webhook, "WhatsappRouterAgent", BrokenConstructor)
    payload = meta_payload("wamid.before-router")
    response = TestClient(client).post("/webhook/whatsapp", json=payload)
    assert response.status_code == 503
    record = db_session.get(MensagemWebhook, "wamid.before-router")
    assert record.status == StatusMensagemWebhook.FALHOU_REPROCESSAVEL.value
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    assert TestClient(client).post("/webhook/whatsapp", json=payload).status_code == 200
    assert record.tentativas == 2


def test_router_failure_is_definitive(client, monkeypatch, db_session, endpoint_spies):
    """OLT-DEDUP-032"""
    class BrokenRouter(FakeRouter):
        def handle(self, message):
            type(self).calls += 1
            raise RuntimeError("after-router")

    BrokenRouter.calls = 0
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", BrokenRouter)
    payload = meta_payload("wamid.router-failure")
    http = TestClient(client)
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert BrokenRouter.calls == 1
    assert db_session.get(MensagemWebhook, "wamid.router-failure").status == StatusMensagemWebhook.FALHOU_DEFINITIVA.value


def test_failure_after_business_effect_is_not_retried(client, monkeypatch, endpoint_spies):
    """OLT-DEDUP-033"""
    effects = []

    class EffectThenFailure(FakeRouter):
        def handle(self, message):
            effects.append("efeito")
            raise RuntimeError("post-effect")

    monkeypatch.setattr(webhook, "WhatsappRouterAgent", EffectThenFailure)
    payload = meta_payload("wamid.post-effect")
    http = TestClient(client)
    http.post("/webhook/whatsapp", json=payload)
    http.post("/webhook/whatsapp", json=payload)
    assert effects == ["efeito"]


def test_sender_failure_concludes_without_retry(client, monkeypatch, db_session):
    """OLT-DEDUP-034"""
    FakeRouter.calls = 0
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    monkeypatch.setattr(webhook, "send_whatsapp_message", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("send")))
    payload = meta_payload("wamid.sender-failure")
    http = TestClient(client)
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    record = db_session.get(MensagemWebhook, "wamid.sender-failure")
    assert record.status == StatusMensagemWebhook.CONCLUIDA.value
    assert record.resposta_enviada is False
    assert FakeRouter.calls == 1


def test_failure_after_router_before_sender_is_definitive(client, monkeypatch, db_session, endpoint_spies):
    FakeRouter.calls = 0
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    monkeypatch.setattr(webhook, "_before_send", lambda: (_ for _ in ()).throw(RuntimeError("pre-send")))
    payload = meta_payload("wamid.pre-sender-failure")
    http = TestClient(client)

    first = http.post("/webhook/whatsapp", json=payload)
    retry = http.post("/webhook/whatsapp", json=payload)

    record = db_session.get(MensagemWebhook, "wamid.pre-sender-failure")
    assert first.status_code == retry.status_code == 200
    assert record.status == StatusMensagemWebhook.FALHOU_DEFINITIVA.value
    assert FakeRouter.calls == 1
    assert endpoint_spies == []


def test_sender_error_result_concludes_without_retry(client, monkeypatch, db_session):
    FakeRouter.calls = 0
    sends = []
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    monkeypatch.setattr(
        webhook,
        "send_whatsapp_message",
        lambda *args, **kwargs: sends.append("send")
        or {"status": "error", "message_id": "wamid.outbound.error", "error": "technical-secret"},
    )
    payload = meta_payload("wamid.sender-error-result")
    http = TestClient(client)

    first = http.post("/webhook/whatsapp", json=payload)
    retry = http.post("/webhook/whatsapp", json=payload)

    record = db_session.get(MensagemWebhook, "wamid.sender-error-result")
    assert first.status_code == retry.status_code == 200
    assert record.status == StatusMensagemWebhook.CONCLUIDA.value
    assert record.resposta_enviada is False
    assert FakeRouter.calls == 1
    assert sends == ["send"]


def test_lost_http_response_retry_is_suppressed(client, endpoint_spies):
    """OLT-DEDUP-035"""
    payload = meta_payload("wamid.http-lost")
    first_response = TestClient(client).post("/webhook/whatsapp", json=payload)
    del first_response
    retry = TestClient(client).post("/webhook/whatsapp", json=payload)
    assert retry.status_code == 200
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_temporal_index_and_no_automatic_cleanup(tmp_path):
    """OLT-DEDUP-038"""
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    ensure_alugueres_contentor_schema(engine)
    ensure_alugueres_contentor_schema(engine)
    indexes = {item["name"] for item in inspect(engine).get_indexes("mensagens_webhook")}
    assert "ix_mensagens_webhook_atualizado_em" in indexes
    assert "mensagens_webhook" in inspect(engine).get_table_names()
    engine.dispose()


def test_duplicate_does_not_disclose_internal_data(client, endpoint_spies):
    """OLT-DEDUP-039"""
    payload = meta_payload("wamid.secret-value")
    http = TestClient(client)
    http.post("/webhook/whatsapp", json=payload)
    response = http.post("/webhook/whatsapp", json=payload)
    body = response.text
    assert "wamid.secret-value" not in body
    assert "duplic" not in body.lower()
    assert len(endpoint_spies) == 1


def test_http_response_is_explicitly_sanitized(client, monkeypatch):
    FakeRouter.calls = 0
    monkeypatch.setattr(webhook, "WhatsappRouterAgent", FakeRouter)
    monkeypatch.setattr(
        webhook,
        "send_whatsapp_message",
        lambda *args, **kwargs: {
            "status": "sent",
            "message_id": "wamid.outbound.synthetic-secret",
            "payload_hash": "hash-secret",
            "traceback": "traceback-secret",
        },
    )
    response = TestClient(client).post(
        "/webhook/whatsapp", json=meta_payload("wamid.inbound.synthetic-secret")
    )
    serialized = response.text.lower()
    assert response.json() == {"status": "ok", "processed": 1, "ignored": 0, "failed": 0}
    for forbidden in (
        "wamid",
        "message_id",
        "payload_hash",
        "traceback",
        "wamid.outbound.synthetic-secret",
    ):
        assert forbidden not in serialized


def test_authorization_response_is_preserved_and_deduplicated(client, endpoint_spies):
    """OLT-DEDUP-040"""
    payload = meta_payload("wamid.modules-1-2")
    http = TestClient(client)
    first = http.post("/webhook/whatsapp", json=payload)
    second = http.post("/webhook/whatsapp", json=payload)
    assert first.status_code == second.status_code == 200
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_retryable_claim_can_only_be_reassumed_once(db_session):
    service = WebhookDedupService(db_session)
    first = service.adquirir(normalized("wamid.retryable"))
    service.marcar_falhou_reprocessavel(first.registro, RuntimeError("before"))
    retry = service.adquirir(normalized("wamid.retryable"))
    duplicate = service.adquirir(normalized("wamid.retryable"))
    assert retry.deve_processar is True
    assert retry.reprocessamento is True
    assert duplicate.deve_processar is False


def test_migration_preserves_existing_schema_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-dedup.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE legado (id INTEGER PRIMARY KEY, valor TEXT)"))
        connection.execute(text("INSERT INTO legado (id, valor) VALUES (1, 'preservar')"))
    ensure_alugueres_contentor_schema(engine)
    ensure_alugueres_contentor_schema(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT valor FROM legado WHERE id = 1")).scalar_one() == "preservar"
        assert connection.execute(text("SELECT COUNT(*) FROM mensagens_webhook")).scalar_one() == 0
    engine.dispose()


def test_claim_database_failure_returns_503_without_router(client, monkeypatch, endpoint_spies):
    def fail_claim(self, message):
        raise RuntimeError("database-unavailable")

    monkeypatch.setattr(WebhookDedupService, "adquirir", fail_claim)
    response = TestClient(client).post("/webhook/whatsapp", json=meta_payload("wamid.claim-failure"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook temporarily unavailable"}
    assert FakeRouter.calls == 0
    assert endpoint_spies == []


def test_completion_mark_failure_does_not_rerun_router(client, monkeypatch, endpoint_spies):
    original = WebhookDedupService.marcar_concluida

    def fail_completion(self, registro, resposta_enviada):
        raise RuntimeError("completion-unavailable")

    monkeypatch.setattr(WebhookDedupService, "marcar_concluida", fail_completion)
    payload = meta_payload("wamid.completion-failure")
    http = TestClient(client)
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    monkeypatch.setattr(WebhookDedupService, "marcar_concluida", original)
    assert http.post("/webhook/whatsapp", json=payload).status_code == 200
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_batch_new_then_completed_duplicate_processes_only_new(client, endpoint_spies):
    http = TestClient(client)
    duplicate = inbound_message("wamid.batch.completed-a", "duplicada")
    http.post("/webhook/whatsapp", json=messages_payload(duplicate))
    FakeRouter.calls = 0
    endpoint_spies.clear()
    new = inbound_message("wamid.batch.new-a", "nova")

    response = http.post("/webhook/whatsapp", json=messages_payload(new, duplicate))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "processed": 1, "ignored": 1, "failed": 0}
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_batch_completed_duplicate_then_new_continues(client, endpoint_spies):
    http = TestClient(client)
    duplicate = inbound_message("wamid.batch.completed-b", "duplicada")
    http.post("/webhook/whatsapp", json=messages_payload(duplicate))
    FakeRouter.calls = 0
    endpoint_spies.clear()
    new = inbound_message("wamid.batch.new-b", "nova")

    response = http.post("/webhook/whatsapp", json=messages_payload(duplicate, new))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "processed": 1, "ignored": 1, "failed": 0}
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1


def test_batch_two_new_messages_processes_both_independently(client, endpoint_spies, db_session):
    first = inbound_message("wamid.batch.new-c1", "primeira")
    second = inbound_message("wamid.batch.new-c2", "segunda")

    response = TestClient(client).post("/webhook/whatsapp", json=messages_payload(first, second))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "processed": 2, "ignored": 0, "failed": 0}
    assert FakeRouter.calls == 2
    assert len(endpoint_spies) == 2
    assert db_session.query(MensagemWebhook).count() == 2


def test_batch_status_and_message_only_persists_inbound(client, endpoint_spies, db_session):
    message = inbound_message("wamid.batch.inbound-d", "mensagem")
    status = {"id": "wamid.outbound-d", "status": "delivered"}

    response = TestClient(client).post(
        "/webhook/whatsapp", json=messages_payload(message, statuses=[status])
    )

    serialized = response.text.lower()
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "processed": 1, "ignored": 0, "failed": 0}
    assert "wamid" not in serialized
    assert FakeRouter.calls == 1
    assert len(endpoint_spies) == 1
    assert db_session.query(MensagemWebhook).count() == 1
    assert db_session.get(MensagemWebhook, "wamid.batch.inbound-d") is not None
