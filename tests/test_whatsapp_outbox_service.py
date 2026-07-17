from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.core.time import utcnow
from app.integrations.whatsapp.errors import WhatsAppErrorCategory
from app.models.whatsapp_outbox import WhatsAppOutboxMessage
from app.services.whatsapp_outbox_service import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SENT,
    OutboxConfig,
    WhatsAppOutboxService,
)
from app.services.whatsapp_outbox_worker import WhatsAppOutboxWorker


PHONE_A = "351900001001"
PHONE_B = "351900001002"


@pytest.fixture()
def outbox_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    now = {"value": utcnow().replace(tzinfo=None)}
    config = OutboxConfig(
        min_recipient_interval=timedelta(seconds=1),
        max_attempts=3,
        backoff_base=timedelta(seconds=1),
        backoff_max=timedelta(seconds=5),
        lease_duration=timedelta(seconds=10),
        worker_interval=timedelta(milliseconds=1),
        sent_retention=timedelta(days=30),
    )

    def now_provider():
        return now["value"]

    def service():
        session = SessionLocal()
        return session, WhatsAppOutboxService(session, config=config, now_provider=now_provider)

    return SessionLocal, service, now, config, engine


def _row(SessionLocal, item_id):
    with SessionLocal() as session:
        return session.get(WhatsAppOutboxMessage, item_id)


def _ok(message_id="wamid.ok"):
    return {"status": "sent", "message_id": message_id}


def _error(category, *, retryable, meta_code=None, status_code=400):
    return {
        "status": "error",
        "status_code": status_code,
        "meta_code": meta_code,
        "error_class": category,
        "retryable": retryable,
        "fallback_allowed": False,
        "recipient_scoped": category == WhatsAppErrorCategory.PAIR_RATE_LIMIT.value,
    }


def test_enqueue_cria_pending(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "Mensagem A1", dedup_key="a1")
    finally:
        session.close()

    item = _row(SessionLocal, item_id)
    assert item.status == STATUS_PENDING
    assert item.attempts == 0
    assert item.recipient_key != PHONE_A
    assert item.payload_json == '{"body":"Mensagem A1"}'


def test_dedup_key_impede_duplicacao(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        first = service.enqueue_body(PHONE_A, "Mensagem A1", dedup_key="dup")
        second = service.enqueue_body(PHONE_A, "Mensagem A1", dedup_key="dup")
    finally:
        session.close()

    assert first == second
    with SessionLocal() as session:
        assert session.query(WhatsAppOutboxMessage).count() == 1


def test_payload_invalido_e_rejeitado(outbox_env):
    _SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        with pytest.raises(ValueError):
            service.enqueue(PHONE_A, message_type="body", payload={"bad": object()})
    finally:
        session.close()


def test_claim_muda_para_processing(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "Mensagem A1")
        lease = service.claim_next()
    finally:
        session.close()

    assert lease is not None
    assert lease.item_id == item_id
    item = _row(SessionLocal, item_id)
    assert item.status == STATUS_PROCESSING
    assert item.lease_owner == lease.lease_owner


def test_claim_respeita_available_at(outbox_env):
    _SessionLocal, service_factory, now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        service.enqueue(
            PHONE_A,
            message_type="body",
            payload={"body": "futura"},
            available_at=now["value"] + timedelta(seconds=30),
        )
        assert service.claim_next() is None
        now["value"] += timedelta(seconds=31)
        assert service.claim_next() is not None
    finally:
        session.close()


def test_fifo_do_mesmo_destinatario(outbox_env):
    _SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        first = service.enqueue_body(PHONE_A, "A1")
        second = service.enqueue_body(PHONE_A, "A2")
        first_lease = service.claim_next()
        assert first_lease.item_id == first
        assert service.claim_next() is None
        service.mark_sent(first_lease.item_id, first_lease.lease_owner)
        second_lease = service.claim_next()
    finally:
        session.close()

    assert second_lease.item_id == second


def test_destinatarios_diferentes_sao_independentes(outbox_env):
    _SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        first = service.enqueue_body(PHONE_A, "A1")
        second = service.enqueue_body(PHONE_B, "B1")
        leases = [service.claim_next(), service.claim_next()]
    finally:
        session.close()

    assert {lease.item_id for lease in leases if lease} == {first, second}


def test_lease_impede_duplo_claim(outbox_env):
    _SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        service.enqueue_body(PHONE_A, "A1")
        assert service.claim_next() is not None
        assert service.claim_next() is None
    finally:
        session.close()


def test_lease_expirada_e_recuperada(outbox_env):
    SessionLocal, service_factory, now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "A1")
        assert service.claim_next() is not None
        now["value"] += timedelta(seconds=11)
        assert service.release_expired_leases() == 1
    finally:
        session.close()

    assert _row(SessionLocal, item_id).status == STATUS_PENDING


def test_sucesso_muda_para_sent(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "A1")
        lease = service.claim_next()
        assert service.mark_sent(lease.item_id, lease.lease_owner) is True
    finally:
        session.close()

    assert _row(SessionLocal, item_id).status == STATUS_SENT


def test_falha_permanente_muda_para_failed(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "A1")
        lease = service.claim_next()
        service.mark_failed(
            lease.item_id,
            lease.lease_owner,
            error_result=_error(WhatsAppErrorCategory.AUTHENTICATION_OR_PERMISSION.value, retryable=False, meta_code=190),
        )
    finally:
        session.close()

    item = _row(SessionLocal, item_id)
    assert item.status == STATUS_FAILED
    assert item.attempts == 1
    assert item.last_meta_code == 190


def test_falha_temporaria_volta_para_pending_e_incrementa_attempts(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue_body(PHONE_A, "A1")
        lease = service.claim_next()
        service.reschedule(
            lease.item_id,
            lease.lease_owner,
            delay=timedelta(seconds=1),
            error_result=_error(WhatsAppErrorCategory.TEMPORARY_PLATFORM_ERROR.value, retryable=True, meta_code=2),
        )
    finally:
        session.close()

    item = _row(SessionLocal, item_id)
    assert item.status == STATUS_PENDING
    assert item.attempts == 1
    assert item.last_error_category == WhatsAppErrorCategory.TEMPORARY_PLATFORM_ERROR.value


def test_max_attempts_encerra_em_failed(outbox_env):
    SessionLocal, service_factory, _now, _config, _engine = outbox_env
    session, service = service_factory()
    try:
        item_id = service.enqueue(PHONE_A, message_type="body", payload={"body": "A1"}, max_attempts=1)
        lease = service.claim_next()
        service.reschedule(
            lease.item_id,
            lease.lease_owner,
            delay=timedelta(seconds=1),
            error_result=_error(WhatsAppErrorCategory.TEMPORARY_PLATFORM_ERROR.value, retryable=True),
        )
    finally:
        session.close()

    item = _row(SessionLocal, item_id)
    assert item.status == STATUS_FAILED
    assert item.attempts == 1


def test_pragma_quick_check_ok(outbox_env):
    _SessionLocal, _service_factory, _now, _config, engine = outbox_env

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA quick_check")).scalar_one() == "ok"


def test_131056_reagenda_a1_nao_ultrapassa_a2_e_nao_bloqueia_b(outbox_env):
    SessionLocal, service_factory, now, config, _engine = outbox_env
    session, service = service_factory()
    calls = []

    try:
        a1 = service.enqueue_body(PHONE_A, "A1")
        a2 = service.enqueue_body(PHONE_A, "A2")
        b1 = service.enqueue_body(PHONE_B, "B1")

        def sender(to, body):
            calls.append((to, body))
            if body == "A1" and calls.count((to, body)) == 1:
                return _error(WhatsAppErrorCategory.PAIR_RATE_LIMIT.value, retryable=True, meta_code=131056)
            return _ok(f"wamid.{body}")

        worker = WhatsAppOutboxWorker(service, sender=sender, config=config, now_provider=lambda: now["value"])
        assert worker.process_available(max_items=10) == 2
        assert calls == [(PHONE_A, "A1"), (PHONE_B, "B1")]
        assert _row(SessionLocal, a1).status == STATUS_PENDING
        assert _row(SessionLocal, a1).attempts == 1
        assert _row(SessionLocal, a2).status == STATUS_PENDING
        assert _row(SessionLocal, b1).status == STATUS_SENT

        now["value"] += timedelta(seconds=2)
        assert worker.process_available(max_items=10) == 2
    finally:
        session.close()

    assert calls == [(PHONE_A, "A1"), (PHONE_B, "B1"), (PHONE_A, "A1"), (PHONE_A, "A2")]
    assert _row(SessionLocal, a1).status == STATUS_SENT
    assert _row(SessionLocal, a2).status == STATUS_SENT
    with SessionLocal() as check:
        assert check.query(WhatsAppOutboxMessage).filter_by(status=STATUS_PROCESSING).count() == 0


def test_restart_recupera_processing_expirado_e_envia_uma_vez(outbox_env):
    SessionLocal, service_factory, now, config, _engine = outbox_env
    session, service = service_factory()
    calls = []
    try:
        item_id = service.enqueue_body(PHONE_A, "A1")
        assert service.claim_next() is not None
    finally:
        session.close()

    now["value"] += timedelta(seconds=11)
    with SessionLocal() as restarted_session:
        restarted = WhatsAppOutboxService(restarted_session, config=config, now_provider=lambda: now["value"])
        worker = WhatsAppOutboxWorker(
            restarted,
            sender=lambda to, body: calls.append((to, body)) or _ok("wamid.restart"),
            config=config,
            now_provider=lambda: now["value"],
        )
        assert worker.process_available(max_items=10) == 1

    assert calls == [(PHONE_A, "A1")]
    assert _row(SessionLocal, item_id).status == STATUS_SENT


def test_throttle_global_pausa_b_e_retoma_depois(outbox_env):
    SessionLocal, service_factory, now, config, _engine = outbox_env
    session, service = service_factory()
    calls = []
    try:
        a1 = service.enqueue_body(PHONE_A, "A1")
        b1 = service.enqueue_body(PHONE_B, "B1")

        def sender(to, body):
            calls.append((to, body))
            if body == "A1" and calls.count((to, body)) == 1:
                return _error(WhatsAppErrorCategory.GLOBAL_THROTTLE.value, retryable=True, meta_code=4)
            return _ok(f"wamid.{body}")

        worker = WhatsAppOutboxWorker(service, sender=sender, config=config, now_provider=lambda: now["value"])
        assert worker.process_available(max_items=10) == 1
        assert calls == [(PHONE_A, "A1")]
        assert _row(SessionLocal, a1).status == STATUS_PENDING
        assert _row(SessionLocal, b1).status == STATUS_PENDING

        assert worker.process_available(max_items=10) == 0
        now["value"] += timedelta(seconds=2)
        assert worker.process_available(max_items=10) == 2
    finally:
        session.close()

    assert calls == [(PHONE_A, "A1"), (PHONE_A, "A1"), (PHONE_B, "B1")]
    assert _row(SessionLocal, a1).status == STATUS_SENT
    assert _row(SessionLocal, b1).status == STATUS_SENT
