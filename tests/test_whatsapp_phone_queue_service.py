from datetime import timedelta
import re
import threading
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.phone import normalize_phone
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.core.time import utcnow
from app.models.whatsapp_phone_queue import WhatsAppPhoneQueueItem
from app.services.whatsapp_phone_queue_service import (
    QueueAcquireTimeout,
    STATUS_PENDING,
    STATUS_PROCESSING,
    WhatsAppPhoneQueueService,
)


PHONE_A = "351900009900"
PHONE_B = "351900009901"


def _session_local(tmp_path, name="queue.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def _service(session, *, lease=timedelta(seconds=1), poll=0.005, timeout=0.5):
    return WhatsAppPhoneQueueService(
        session,
        lease_duration=lease,
        poll_interval=poll,
        wait_timeout=timeout,
    )


def _row(session, queue_id):
    return session.get(WhatsAppPhoneQueueItem, queue_id)


def test_phone_key_hash_deterministico_sem_telefone_original(db_session):
    service = _service(db_session)

    plain = service.phone_key("912 345 678")
    prefixed = service.phone_key("+351 912 345 678")
    normalized = normalize_phone("912 345 678")

    assert plain == prefixed
    assert re.fullmatch(r"[0-9a-f]{64}", plain)
    assert "912345678" not in plain
    assert normalized not in plain


def test_enqueue_preserva_ordem_e_message_id_opcional(db_session):
    service = _service(db_session)

    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, None)

    assert first_id < second_id
    first = _row(db_session, first_id)
    second = _row(db_session, second_id)
    assert first.message_id == "wamid.a"
    assert second.message_id is None
    assert first.phone_key == second.phone_key
    assert first.status == second.status == STATUS_PENDING


def test_primeiro_item_adquire_owner_token_e_lease(db_session):
    service = _service(db_session)
    queue_id = service.enqueue(PHONE_A, "wamid.a")

    lease = service.try_acquire(queue_id)
    db_session.expire_all()
    item = _row(db_session, queue_id)

    assert lease is not None
    assert lease.owner_token
    assert lease.lease_ate is not None
    assert item.status == STATUS_PROCESSING
    assert item.owner_token == lease.owner_token
    assert item.lease_ate is not None


def test_segundo_item_nao_ultrapassa_processing_recente(db_session):
    service = _service(db_session, lease=timedelta(seconds=30))
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")

    assert service.try_acquire(first_id) is not None
    assert service.try_acquire(second_id) is None
    db_session.expire_all()

    assert _row(db_session, second_id).status == STATUS_PENDING


def test_conclusao_libera_proximo(db_session):
    service = _service(db_session)
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")
    first_lease = service.try_acquire(first_id)

    assert first_lease is not None
    assert service.complete(first_id, first_lease.owner_token) is True
    second_lease = service.try_acquire(second_id)

    assert _row(db_session, first_id) is None
    assert second_lease is not None
    assert second_lease.queue_id == second_id


def test_falha_libera_proximo_sem_prender_fila(db_session):
    service = _service(db_session)
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")
    first_lease = service.try_acquire(first_id)

    assert first_lease is not None
    assert service.fail(first_id, first_lease.owner_token) is True
    second_lease = service.try_acquire(second_id)

    assert _row(db_session, first_id) is None
    assert second_lease is not None


def test_cancelamento_de_pending_libera_proximo(db_session):
    service = _service(db_session)
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")

    assert service.cancel_pending(first_id) is True
    second_lease = service.try_acquire(second_id)

    assert _row(db_session, first_id) is None
    assert second_lease is not None


def test_processing_expirado_e_recuperado_de_forma_idempotente(db_session):
    service = _service(db_session, lease=timedelta(milliseconds=1))
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")
    assert service.try_acquire(first_id) is not None
    time.sleep(0.02)

    assert service.recover_expired_leases() == 1
    assert service.recover_expired_leases() == 0
    second_lease = service.try_acquire(second_id)

    assert _row(db_session, first_id) is None
    assert second_lease is not None


def test_owner_incorreto_nao_conclui_nem_falha_item(db_session):
    service = _service(db_session)
    queue_id = service.enqueue(PHONE_A, "wamid.a")
    lease = service.try_acquire(queue_id)

    assert lease is not None
    assert service.complete(queue_id, "owner-errado") is False
    assert service.fail(queue_id, "owner-errado") is False
    db_session.expire_all()
    assert _row(db_session, queue_id).status == STATUS_PROCESSING
    assert service.complete(queue_id, lease.owner_token) is True


def test_timeout_de_espera_cancela_pending_e_libera_proximo(db_session):
    service = _service(db_session, lease=timedelta(seconds=30), poll=0.005, timeout=0.03)
    first_id = service.enqueue(PHONE_A, "wamid.a")
    second_id = service.enqueue(PHONE_A, "wamid.b")
    third_id = service.enqueue(PHONE_A, "wamid.c")
    first_lease = service.try_acquire(first_id)
    assert first_lease is not None

    try:
        service.wait_turn(second_id)
        raise AssertionError("timeout esperado")
    except QueueAcquireTimeout:
        pass

    db_session.expire_all()
    assert _row(db_session, second_id) is None
    assert _row(db_session, third_id).status == STATUS_PENDING
    assert service.complete(first_id, first_lease.owner_token) is True
    assert service.try_acquire(third_id) is not None


def test_schema_idempotente_cria_fila_sem_apagar_tabelas_antigas(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-queue.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tabela_antiga (id INTEGER PRIMARY KEY, valor TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO tabela_antiga (id, valor) VALUES (1, 'preservado')"))

    ensure_alugueres_contentor_schema(engine)
    ensure_alugueres_contentor_schema(engine)

    with engine.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(whatsapp_phone_queue)")).mappings()
        }
        indexes = {
            row["name"]
            for row in connection.execute(text("PRAGMA index_list(whatsapp_phone_queue)")).mappings()
        }
        old_value = connection.execute(text("SELECT valor FROM tabela_antiga WHERE id = 1")).scalar_one()
        sqlite_sequence_exists = connection.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")
        ).scalar_one()

    assert {
        "id",
        "phone_key",
        "message_id",
        "status",
        "owner_token",
        "criado_em",
        "atualizado_em",
        "lease_ate",
    } <= columns
    assert "ix_whatsapp_phone_queue_phone_key" in indexes
    assert "ix_whatsapp_phone_queue_phone_status_id" in indexes
    assert old_value == "preservado"
    assert sqlite_sequence_exists == 1


def test_fifo_concorrente_mesmo_telefone_respeita_id_persistido(tmp_path):
    for _attempt in range(3):
        Session, _engine = _session_local(tmp_path, f"fifo-{_attempt}.db")
        with Session() as session:
            service = _service(session, timeout=2)
            ids = [
                service.enqueue(PHONE_A, "wamid.a"),
                service.enqueue(PHONE_A, "wamid.b"),
                service.enqueue(PHONE_A, "wamid.c"),
            ]
        start = threading.Barrier(3)
        acquired_order = []
        order_lock = threading.Lock()
        errors = []

        def worker(queue_id):
            with Session() as session:
                service = _service(session, timeout=2)
                try:
                    start.wait(timeout=5)
                    lease = service.wait_turn(queue_id)
                    with order_lock:
                        acquired_order.append(queue_id)
                    assert service.complete(queue_id, lease.owner_token) is True
                except Exception as exc:  # pragma: no cover - assertion reports exact exception
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(ids[2],)),
            threading.Thread(target=worker, args=(ids[0],)),
            threading.Thread(target=worker, args=(ids[1],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert acquired_order == ids


def test_concorrente_telefones_diferentes_processam_ao_mesmo_tempo(tmp_path):
    Session, _engine = _session_local(tmp_path, "phones.db")
    with Session() as session:
        service = _service(session, timeout=1)
        first_id = service.enqueue(PHONE_A, "wamid.a")
        second_id = service.enqueue(PHONE_B, "wamid.b")
    barrier = threading.Barrier(2)
    first_processing = threading.Event()
    second_processing = threading.Event()
    release = threading.Event()
    errors = []

    def worker(queue_id, own_event, other_event):
        with Session() as session:
            service = _service(session, timeout=1)
            try:
                barrier.wait(timeout=5)
                lease = service.wait_turn(queue_id)
                own_event.set()
                assert other_event.wait(timeout=1)
                assert release.wait(timeout=1)
                assert service.complete(queue_id, lease.owner_token) is True
            except Exception as exc:  # pragma: no cover - assertion reports exact exception
                errors.append(exc)

    t1 = threading.Thread(target=worker, args=(first_id, first_processing, second_processing))
    t2 = threading.Thread(target=worker, args=(second_id, second_processing, first_processing))
    t1.start()
    t2.start()
    assert first_processing.wait(timeout=5)
    assert second_processing.wait(timeout=5)
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors


def test_claim_concorrente_mesmo_item_apenas_um_owner(tmp_path):
    Session, _engine = _session_local(tmp_path, "same-item.db")
    with Session() as session:
        queue_id = _service(session, timeout=1).enqueue(PHONE_A, "wamid.same")
    barrier = threading.Barrier(2)
    leases = []
    errors = []

    def worker():
        with Session() as session:
            service = _service(session, timeout=1)
            try:
                barrier.wait(timeout=5)
                leases.append(service.try_acquire(queue_id))
            except Exception as exc:  # pragma: no cover - assertion reports exact exception
                errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert len(leases) == 2
    assert sum(lease is not None for lease in leases) == 1


def test_limpeza_concorrente_de_lease_expirada_e_idempotente(tmp_path):
    Session, _engine = _session_local(tmp_path, "expired.db")
    with Session() as session:
        service = _service(session, lease=timedelta(milliseconds=1), timeout=1)
        first_id = service.enqueue(PHONE_A, "wamid.a")
        second_id = service.enqueue(PHONE_A, "wamid.b")
        assert service.try_acquire(first_id) is not None
    time.sleep(0.02)
    barrier = threading.Barrier(2)
    recovered = []
    errors = []

    def recover_worker():
        with Session() as session:
            service = _service(session, timeout=1)
            try:
                barrier.wait(timeout=5)
                recovered.append(service.recover_expired_leases())
            except Exception as exc:  # pragma: no cover - assertion reports exact exception
                errors.append(exc)

    t1 = threading.Thread(target=recover_worker)
    t2 = threading.Thread(target=recover_worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    with Session() as session:
        lease = _service(session, timeout=1).try_acquire(second_id)
        remaining_ids = [row.id for row in session.query(WhatsAppPhoneQueueItem).order_by(WhatsAppPhoneQueueItem.id)]

    assert not errors
    assert sorted(recovered) == [0, 1]
    assert lease is not None
    assert first_id not in remaining_ids


def test_fila_nao_acumula_registros_terminais(db_session):
    service = _service(db_session)
    completed_id = service.enqueue(PHONE_A, "wamid.done")
    failed_id = service.enqueue(PHONE_A, "wamid.fail")
    cancelled_id = service.enqueue(PHONE_A, "wamid.cancel")

    completed_lease = service.try_acquire(completed_id)
    assert completed_lease is not None
    assert service.complete(completed_id, completed_lease.owner_token) is True
    failed_lease = service.try_acquire(failed_id)
    assert failed_lease is not None
    assert service.fail(failed_id, failed_lease.owner_token) is True
    assert service.cancel_pending(cancelled_id) is True

    db_session.expire_all()
    assert db_session.query(WhatsAppPhoneQueueItem).count() == 0
