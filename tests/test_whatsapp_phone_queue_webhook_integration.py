from datetime import datetime, timedelta, timezone
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.core.db import Base, get_db
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.core.time import utcnow
from app.models.conversa import ConversaWhatsApp
from app.models.whatsapp_dedup import WhatsAppProcessedMessage
from app.models.whatsapp_phone_queue import WhatsAppPhoneQueueItem
from app.routes import webhook
from app.services.pedido_service import PedidoService
from app.services import whatsapp_phone_queue_service as queue_module


PHONE = "351900009900"
OTHER_PHONE = "351900009901"


def _liberar_operadores(monkeypatch):
    for name in (
        "WHATSAPP_OWNER_PHONE",
        "AUTHORIZED_OPERATOR_PHONE",
        "AUTHORIZED_OPERATOR_PHONES",
        "OWNER_WHATSAPP",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()


def _webhook_payload(text, *, phone=PHONE, message_id="wamid.integration"):
    message = {
        "from": phone,
        "type": "text",
        "text": {"body": text},
    }
    if message_id is not None:
        message["id"] = message_id
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def _integration_app(tmp_path):
    db_path = tmp_path / "queue_webhook_integration.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    app = FastAPI(title="olt-queue-webhook-integration-test")
    app.include_router(webhook.router)
    session_ids = []

    def override_get_db():
        session = SessionLocal()
        session_ids.append(id(session))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return app, SessionLocal, session_ids


def _pedido_para_recolha(SessionLocal, *, adesivo_base=100):
    with SessionLocal() as session:
        service = PedidoService(session)
        pedido = service.criar(
            nome_cliente="Cliente Integracao",
            telefone_cliente="351912345678",
            data_planejada=datetime.now(timezone.utc),
            valor_global="100",
            pago=True,
            forma_pagamento="MBWay",
            pedido_feito_por="gestor",
            endereco_aproximado="Rua",
            ponto_referencia=None,
            residuos=["Entulho Limpo"],
        )
        service.confirmar_entrega_lote(
            pedido.id,
            "motorista",
            38.7,
            -9.1,
            None,
            [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": str(adesivo_base), "fotos": ["foto"]}],
        )
        return pedido.id, pedido.contentores[0].id


def _instrument_send(monkeypatch, events, fail_once=False):
    calls = {"count": 0}

    def fake_send(to, body, **_kwargs):
        calls["count"] += 1
        events.append(("send", to, body))
        if fail_once and calls["count"] == 1:
            raise RuntimeError("falha simulada no envio")
        return {"to": to, "body": body, "status": "mocked", "message_id": f"sent-{calls['count']}"}

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    return calls


def _instrument_router(monkeypatch, events, fail_once=False):
    original_handle = WhatsappRouterAgent.handle
    calls = {"count": 0}

    def wrapped_handle(self, message):
        calls["count"] += 1
        events.append(("router", message.message_id, message.telefone, message.texto))
        if fail_once and calls["count"] == 1:
            raise RuntimeError("falha simulada no roteador")
        return original_handle(self, message)

    monkeypatch.setattr(WhatsappRouterAgent, "handle", wrapped_handle)
    return calls


def _instrument_queue(monkeypatch, events, *, wait_timeout=None):
    original = queue_module.WhatsAppPhoneQueueService

    class RecordingQueueService(original):
        def __init__(self, *args, **kwargs):
            if wait_timeout is not None:
                kwargs["wait_timeout"] = wait_timeout
                kwargs["poll_interval"] = 0.005
                kwargs["lease_duration"] = timedelta(seconds=30)
            super().__init__(*args, **kwargs)

        def enqueue(self, phone, message_id=None):
            queue_id = super().enqueue(phone, message_id)
            events.append(("queue_enqueue", queue_id, message_id))
            return queue_id

        def wait_turn(self, queue_id, **kwargs):
            events.append(("queue_wait", queue_id))
            lease = super().wait_turn(queue_id, **kwargs)
            events.append(("queue_acquire", queue_id))
            return lease

        def complete(self, queue_id, owner_token):
            result = super().complete(queue_id, owner_token)
            events.append(("queue_complete", queue_id, result))
            return result

        def fail(self, queue_id, owner_token):
            result = super().fail(queue_id, owner_token)
            events.append(("queue_fail", queue_id, result))
            return result

    monkeypatch.setattr(queue_module, "WhatsAppPhoneQueueService", RecordingQueueService)
    monkeypatch.setattr(webhook, "WhatsAppPhoneQueueService", RecordingQueueService, raising=False)
    return RecordingQueueService


def _pause_first_recolha(monkeypatch):
    first_reached = threading.Event()
    release_first = threading.Event()
    state = {"paused": False}
    original_start = PedidoV24Agent.start_recolha

    def synchronized_start(self, conversa):
        if not state["paused"]:
            state["paused"] = True
            first_reached.set()
            assert release_first.wait(timeout=5), "primeira requisicao nao foi liberada"
        return original_start(self, conversa)

    monkeypatch.setattr(PedidoV24Agent, "start_recolha", synchronized_start)
    return first_reached, release_first


def _post(app, payload, results, label, done=None):
    try:
        response = TestClient(app).post("/webhook/whatsapp", json=payload)
        results[label] = {"status_code": response.status_code, "json": response.json()}
    except Exception as exc:
        results[label] = {"error": exc}
    finally:
        if done is not None:
            done.set()


def _statuses(session):
    return {row.message_id: row.status for row in session.query(WhatsAppProcessedMessage)}


def _active_queue_count(session):
    return session.query(WhatsAppPhoneQueueItem).count()


def _wait_for_event(events, predicate, timeout=5):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        if any(predicate(event) for event in events):
            return True
        threading.Event().wait(0.01)
    return False


def test_mesmo_telefone_3_seguido_de_1_deve_serializar_requisicoes(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, session_ids = _integration_app(tmp_path)
    _pedido_para_recolha(SessionLocal)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_router(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    first_reached, release_first = _pause_first_recolha(monkeypatch)
    results = {}
    second_done = threading.Event()

    t1 = threading.Thread(target=_post, args=(app, _webhook_payload("3", message_id="wamid.ordem.3"), results, "a"))
    t1.start()
    assert first_reached.wait(timeout=5)
    t2 = threading.Thread(
        target=_post,
        args=(app, _webhook_payload("1", message_id="wamid.ordem.1"), results, "b", second_done),
    )
    t2.start()
    assert not second_done.wait(timeout=0.2)
    release_first.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "error" not in results.get("a", {})
    assert "error" not in results.get("b", {})
    bodies = [results[label]["json"]["messages"][0]["body"] for label in ("a", "b")]
    assert "Selecione o pedido para recolha" in bodies[0]
    assert "Selecione o ativo" in bodies[1]
    assert [event[0] for event in events].count("queue_enqueue") == 2
    assert [event[:2] for event in events if event[0] in {"router", "send"}] == [
        ("router", "wamid.ordem.3"),
        ("send", PHONE),
        ("router", "wamid.ordem.1"),
        ("send", PHONE),
    ]
    with SessionLocal() as session:
        conversa = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        assert conversa.estado_atual == "v24_recolha_ativo"
        assert conversa.contexto_json["pedido_id"]
        assert _statuses(session)["wamid.ordem.3"] == "COMPLETED"
        assert _statuses(session)["wamid.ordem.1"] == "COMPLETED"
        assert _active_queue_count(session) == 0
    assert len(set(session_ids)) >= 2


def test_dois_3_mesmo_telefone_preservam_contexto_fifo(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    _pedido_para_recolha(SessionLocal, adesivo_base=200)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_router(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    first_reached, release_first = _pause_first_recolha(monkeypatch)
    results = {}
    second_done = threading.Event()

    t1 = threading.Thread(target=_post, args=(app, _webhook_payload("3", message_id="wamid.duplo.a"), results, "a"))
    t1.start()
    assert first_reached.wait(timeout=5)
    t2 = threading.Thread(target=_post, args=(app, _webhook_payload("3", message_id="wamid.duplo.b"), results, "b", second_done))
    t2.start()
    assert not second_done.wait(timeout=0.2)
    release_first.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert [event[:2] for event in events if event[0] == "router"] == [
        ("router", "wamid.duplo.a"),
        ("router", "wamid.duplo.b"),
    ]
    assert [event[0] for event in events].count("queue_complete") == 2
    with SessionLocal() as session:
        conversa = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        assert conversa.estado_atual == "v24_recolha_pedido"
        assert conversa.contexto_json["ids"]
        assert "pedido_id" not in conversa.contexto_json
        assert _statuses(session)["wamid.duplo.a"] == "COMPLETED"
        assert _statuses(session)["wamid.duplo.b"] == "COMPLETED"
        assert _active_queue_count(session) == 0


def test_message_id_duplicado_nao_entra_duas_vezes_na_fila(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_send(monkeypatch, events)
    router_calls = _instrument_router(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    payload = _webhook_payload("novo pedido", message_id="wamid.duplicado.fila")

    first = TestClient(app).post("/webhook/whatsapp", json=payload)
    second = TestClient(app).post("/webhook/whatsapp", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert router_calls["count"] == 1
    assert [event[0] for event in events].count("queue_enqueue") == 1
    assert [event[0] for event in events].count("send") == 1
    with SessionLocal() as session:
        assert _statuses(session)["wamid.duplicado.fila"] == "COMPLETED"
        assert _active_queue_count(session) == 0


def test_telefones_diferentes_processam_em_paralelo_sem_lock_global(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    _pedido_para_recolha(SessionLocal, adesivo_base=300)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    both_inside = threading.Event()
    inside = set()
    lock = threading.Lock()
    release = threading.Event()
    original_handle = WhatsappRouterAgent.handle

    def wrapped_handle(self, message):
        with lock:
            inside.add(message.telefone)
            if {PHONE, OTHER_PHONE} <= inside:
                both_inside.set()
        if message.telefone == PHONE:
            assert release.wait(timeout=5)
        events.append(("router", message.message_id, message.telefone, message.texto))
        return original_handle(self, message)

    monkeypatch.setattr(WhatsappRouterAgent, "handle", wrapped_handle)
    results = {}
    t1 = threading.Thread(target=_post, args=(app, _webhook_payload("3", phone=PHONE, message_id="wamid.phone.a"), results, "a"))
    t2 = threading.Thread(target=_post, args=(app, _webhook_payload("3", phone=OTHER_PHONE, message_id="wamid.phone.b"), results, "b"))
    t1.start()
    t2.start()
    assert both_inside.wait(timeout=5)
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "error" not in results.get("a", {})
    assert "error" not in results.get("b", {})
    assert [event[0] for event in events].count("queue_acquire") == 2
    with SessionLocal() as session:
        assert session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one().estado_atual == "v24_recolha_pedido"
        assert session.query(ConversaWhatsApp).filter_by(telefone=OTHER_PHONE).one().estado_atual == "v24_recolha_pedido"
        assert _active_queue_count(session) == 0


def test_fifo_tres_mensagens_mesmo_telefone_avanca_fluxo_recolha(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    _pedido_para_recolha(SessionLocal, adesivo_base=400)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_router(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    first_reached, release_first = _pause_first_recolha(monkeypatch)
    payloads = [
        _webhook_payload("3", message_id="wamid.fifo.a"),
        _webhook_payload("1", message_id="wamid.fifo.b"),
        _webhook_payload("1", message_id="wamid.fifo.c"),
    ]
    results = {}
    threads = [threading.Thread(target=_post, args=(app, payload, results, str(index))) for index, payload in enumerate(payloads)]

    threads[0].start()
    assert first_reached.wait(timeout=5)
    threads[1].start()
    assert _wait_for_event(events, lambda event: event[:3] == ("queue_enqueue", 2, "wamid.fifo.b"))
    threads[2].start()
    assert _wait_for_event(events, lambda event: event[:3] == ("queue_enqueue", 3, "wamid.fifo.c"))
    release_first.set()
    for thread in threads:
        thread.join(timeout=10)

    assert [event[1] for event in events if event[0] == "router"] == ["wamid.fifo.a", "wamid.fifo.b", "wamid.fifo.c"]
    assert [event[0] for event in events].count("queue_complete") == 3
    with SessionLocal() as session:
        conversa = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        assert conversa.estado_atual != "v24_cadastro_tipo_solicitacao"
        assert conversa.contexto_json["pedido_id"]
        assert _active_queue_count(session) == 0


def test_falha_do_roteador_libera_fila_e_permite_retry(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_router(monkeypatch, events, fail_once=True)
    _instrument_queue(monkeypatch, events)
    failing_client = TestClient(app, raise_server_exceptions=False)
    payload = _webhook_payload("novo pedido", message_id="wamid.router.retry")

    first = failing_client.post("/webhook/whatsapp", json=payload)
    second = TestClient(app).post("/webhook/whatsapp", json=payload)

    assert first.status_code == 500
    assert second.status_code == 200
    assert [event[0] for event in events].count("queue_fail") == 1
    assert [event[0] for event in events].count("queue_complete") == 1
    with SessionLocal() as session:
        assert _statuses(session)["wamid.router.retry"] == "COMPLETED"
        assert _active_queue_count(session) == 0


def test_falha_no_envio_libera_fila_e_permite_retry(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_router(monkeypatch, events)
    _instrument_send(monkeypatch, events, fail_once=True)
    _instrument_queue(monkeypatch, events)
    failing_client = TestClient(app, raise_server_exceptions=False)
    payload = _webhook_payload("novo pedido", message_id="wamid.send.retry")

    first = failing_client.post("/webhook/whatsapp", json=payload)
    second = TestClient(app).post("/webhook/whatsapp", json=payload)

    assert first.status_code == 500
    assert second.status_code == 200
    assert [event[0] for event in events].count("queue_fail") == 1
    assert [event[0] for event in events].count("queue_complete") == 1
    with SessionLocal() as session:
        assert _statuses(session)["wamid.send.retry"] == "COMPLETED"
        assert _active_queue_count(session) == 0


def test_timeout_de_espera_remove_pending_e_marca_dedup_retryable(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_queue(monkeypatch, events, wait_timeout=0.01)
    phone_key = queue_module.WhatsAppPhoneQueueService.phone_key(PHONE)
    with SessionLocal() as session:
        session.add(
            WhatsAppPhoneQueueItem(
                phone_key=phone_key,
                message_id="wamid.timeout.blocker",
                status="PROCESSING",
                owner_token="owner",
                criado_em=utcnow(),
                atualizado_em=utcnow(),
                lease_ate=utcnow() + timedelta(seconds=30),
            )
        )
        session.commit()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/webhook/whatsapp",
        json=_webhook_payload("novo pedido", message_id="wamid.timeout.waiting"),
    )

    assert response.status_code == 500
    assert [event[0] for event in events].count("send") == 0
    with SessionLocal() as session:
        assert _statuses(session)["wamid.timeout.waiting"] == "FAILED"
        assert session.query(WhatsAppPhoneQueueItem).filter_by(message_id="wamid.timeout.waiting").count() == 0


def test_mensagem_sem_message_id_tambem_respeita_fila(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_router(monkeypatch, events)
    _instrument_queue(monkeypatch, events)

    response = TestClient(app).post("/webhook/whatsapp", json=_webhook_payload("novo pedido", message_id=None))

    assert response.status_code == 200
    assert [event[0] for event in events].count("queue_enqueue") == 1
    with SessionLocal() as session:
        assert session.query(WhatsAppProcessedMessage).count() == 0
        assert _active_queue_count(session) == 0


def test_lease_expirada_abandonada_e_recuperada_na_integracao(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    app, SessionLocal, _session_ids = _integration_app(tmp_path)
    events = []
    _instrument_send(monkeypatch, events)
    _instrument_queue(monkeypatch, events)
    phone_key = queue_module.WhatsAppPhoneQueueService.phone_key(PHONE)
    with SessionLocal() as session:
        session.add(
            WhatsAppPhoneQueueItem(
                phone_key=phone_key,
                message_id="wamid.lease.abandonada",
                status="PROCESSING",
                owner_token="owner",
                criado_em=utcnow() - timedelta(minutes=5),
                atualizado_em=utcnow() - timedelta(minutes=5),
                lease_ate=utcnow() - timedelta(seconds=1),
            )
        )
        session.commit()

    response = TestClient(app).post("/webhook/whatsapp", json=_webhook_payload("novo pedido", message_id="wamid.lease.nova"))

    assert response.status_code == 200
    assert [event[0] for event in events].count("queue_acquire") == 1
    with SessionLocal() as session:
        assert session.query(WhatsAppPhoneQueueItem).count() == 0
        assert _statuses(session)["wamid.lease.nova"] == "COMPLETED"
