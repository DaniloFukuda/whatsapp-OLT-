from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import time
import uuid

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.core.phone import normalize_phone
from app.core.time import utcnow
from app.models.whatsapp_phone_queue import WhatsAppPhoneQueueItem


logger = get_logger(__name__)


STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_FAILED = "FAILED"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)
DEFAULT_LEASE_DURATION = timedelta(seconds=60)
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_WAIT_TIMEOUT = 10.0


@dataclass(frozen=True)
class QueueLease:
    queue_id: int
    phone_key: str
    owner_token: str
    lease_ate: datetime


class QueueAcquireTimeout(TimeoutError):
    pass


class WhatsAppPhoneQueueService:
    def __init__(
        self,
        db: Session,
        *,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    ):
        self._session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
        self._lease_duration = lease_duration
        self._poll_interval = poll_interval
        self._wait_timeout = wait_timeout

    @staticmethod
    def phone_key(phone: str | None) -> str:
        normalized = normalize_phone(phone)
        if not normalized:
            raise ValueError("telefone vazio nao pode entrar na fila")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def enqueue(self, phone: str | None, message_id: str | None = None) -> int:
        phone_key = self.phone_key(phone)
        now = _queue_now()
        clean_message_id = (message_id or "").strip() or None
        with self._session_factory() as session:
            item = WhatsAppPhoneQueueItem(
                phone_key=phone_key,
                message_id=clean_message_id,
                status=STATUS_PENDING,
                criado_em=now,
                atualizado_em=now,
            )
            session.add(item)
            session.commit()
            queue_id = item.id
        logger.info(
            "WhatsApp phone queue item enqueued queue_id=%s phone_key_prefix=%s message_id=%s status=%s",
            queue_id,
            _key_prefix(phone_key),
            clean_message_id,
            STATUS_PENDING,
        )
        return queue_id

    def try_acquire(self, queue_id: int) -> QueueLease | None:
        now = _queue_now()
        owner_token = uuid.uuid4().hex
        lease_ate = now + self._lease_duration
        with self._session_factory() as session:
            item = session.get(WhatsAppPhoneQueueItem, queue_id)
            if item is None:
                return None
            phone_key = item.phone_key
            self._recover_expired_for_phone(session, phone_key, now)
            first_active_id = (
                select(func.min(WhatsAppPhoneQueueItem.id))
                .where(WhatsAppPhoneQueueItem.phone_key == phone_key)
                .where(WhatsAppPhoneQueueItem.status.in_(ACTIVE_STATUSES))
                .scalar_subquery()
            )
            recent_processing_exists = (
                select(WhatsAppPhoneQueueItem.id)
                .where(WhatsAppPhoneQueueItem.phone_key == phone_key)
                .where(WhatsAppPhoneQueueItem.status == STATUS_PROCESSING)
                .where(WhatsAppPhoneQueueItem.lease_ate > now)
                .exists()
            )
            statement = (
                update(WhatsAppPhoneQueueItem)
                .where(WhatsAppPhoneQueueItem.id == queue_id)
                .where(WhatsAppPhoneQueueItem.status == STATUS_PENDING)
                .where(WhatsAppPhoneQueueItem.id == first_active_id)
                .where(~recent_processing_exists)
                .values(
                    status=STATUS_PROCESSING,
                    owner_token=owner_token,
                    lease_ate=lease_ate,
                    atualizado_em=now,
                )
            )
            result = session.execute(statement)
            session.commit()
            if result.rowcount != 1:
                return None
        logger.info(
            "WhatsApp phone queue item acquired queue_id=%s phone_key_prefix=%s status=%s",
            queue_id,
            _key_prefix(phone_key),
            STATUS_PROCESSING,
        )
        return QueueLease(queue_id=queue_id, phone_key=phone_key, owner_token=owner_token, lease_ate=lease_ate)

    def wait_turn(self, queue_id: int, *, timeout: float | None = None) -> QueueLease:
        deadline = time.monotonic() + (self._wait_timeout if timeout is None else timeout)
        while True:
            lease = self.try_acquire(queue_id)
            if lease is not None:
                return lease
            if time.monotonic() >= deadline:
                self.cancel_pending(queue_id)
                logger.warning("WhatsApp phone queue wait timed out queue_id=%s", queue_id)
                raise QueueAcquireTimeout(f"timeout aguardando vez na fila: {queue_id}")
            logger.info("WhatsApp phone queue item waiting queue_id=%s", queue_id)
            time.sleep(self._poll_interval)

    def complete(self, queue_id: int, owner_token: str) -> bool:
        removed = self._delete_owned(queue_id, owner_token, STATUS_PROCESSING)
        if removed:
            logger.info("WhatsApp phone queue item completed queue_id=%s", queue_id)
        return removed

    def fail(self, queue_id: int, owner_token: str) -> bool:
        removed = self._delete_owned(queue_id, owner_token, STATUS_PROCESSING)
        if removed:
            logger.info("WhatsApp phone queue item failed queue_id=%s status=%s", queue_id, STATUS_FAILED)
        return removed

    def cancel_pending(self, queue_id: int) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                delete(WhatsAppPhoneQueueItem)
                .where(WhatsAppPhoneQueueItem.id == queue_id)
                .where(WhatsAppPhoneQueueItem.status == STATUS_PENDING)
            )
            session.commit()
        cancelled = result.rowcount == 1
        if cancelled:
            logger.info("WhatsApp phone queue item cancelled queue_id=%s", queue_id)
        return cancelled

    def recover_expired_leases(self) -> int:
        now = _queue_now()
        with self._session_factory() as session:
            expired = self._recover_expired_for_phone(session, None, now)
            session.commit()
        if expired:
            logger.warning("WhatsApp phone queue expired leases recovered count=%s", expired)
        return expired

    def _delete_owned(self, queue_id: int, owner_token: str, status: str) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                delete(WhatsAppPhoneQueueItem)
                .where(WhatsAppPhoneQueueItem.id == queue_id)
                .where(WhatsAppPhoneQueueItem.owner_token == owner_token)
                .where(WhatsAppPhoneQueueItem.status == status)
            )
            session.commit()
        return result.rowcount == 1

    def _recover_expired_for_phone(self, session: Session, phone_key: str | None, now: datetime) -> int:
        criteria = [
            WhatsAppPhoneQueueItem.status == STATUS_PROCESSING,
            or_(WhatsAppPhoneQueueItem.lease_ate.is_(None), WhatsAppPhoneQueueItem.lease_ate <= now),
        ]
        if phone_key is not None:
            criteria.append(WhatsAppPhoneQueueItem.phone_key == phone_key)
        result = session.execute(delete(WhatsAppPhoneQueueItem).where(*criteria))
        if result.rowcount:
            logger.warning(
                "WhatsApp phone queue expired lease recovered phone_key_prefix=%s count=%s",
                _key_prefix(phone_key),
                result.rowcount,
            )
        return result.rowcount or 0


def _queue_now():
    return utcnow().replace(tzinfo=None)


def _key_prefix(phone_key: str | None) -> str:
    return (phone_key or "")[:12]
