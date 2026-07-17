from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.phone import normalize_phone
from app.core.time import utcnow
from app.models.whatsapp_outbox import WhatsAppOutboxControl, WhatsAppOutboxMessage


logger = get_logger(__name__)

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)
GLOBAL_PAUSE_KEY = "global_pause_until"


@dataclass(frozen=True)
class OutboxConfig:
    min_recipient_interval: timedelta
    max_attempts: int
    backoff_base: timedelta
    backoff_max: timedelta
    lease_duration: timedelta
    worker_interval: timedelta
    sent_retention: timedelta

    @classmethod
    def from_settings(cls) -> "OutboxConfig":
        settings = get_settings()
        return cls(
            min_recipient_interval=timedelta(seconds=settings.whatsapp_outbox_min_recipient_interval_seconds),
            max_attempts=settings.whatsapp_outbox_max_attempts,
            backoff_base=timedelta(seconds=settings.whatsapp_outbox_backoff_base_seconds),
            backoff_max=timedelta(seconds=settings.whatsapp_outbox_backoff_max_seconds),
            lease_duration=timedelta(seconds=settings.whatsapp_outbox_lease_seconds),
            worker_interval=timedelta(seconds=settings.whatsapp_outbox_worker_interval_seconds),
            sent_retention=timedelta(days=settings.whatsapp_outbox_sent_retention_days),
        )


@dataclass(frozen=True)
class OutboxLease:
    item_id: int
    recipient_key: str
    recipient: str
    message_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_until: datetime


class WhatsAppOutboxService:
    def __init__(
        self,
        db: Session,
        *,
        config: OutboxConfig | None = None,
        now_provider=utcnow,
    ):
        self._session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
        self._config = config or OutboxConfig.from_settings()
        self._now_provider = now_provider

    @staticmethod
    def recipient_key(recipient: str | None) -> str:
        normalized = normalize_phone(recipient)
        if not normalized:
            raise ValueError("destinatario vazio nao pode entrar na outbox")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def enqueue(
        self,
        recipient: str,
        *,
        message_type: str,
        payload: dict[str, Any],
        dedup_key: str | None = None,
        available_at: datetime | None = None,
        max_attempts: int | None = None,
    ) -> int:
        clean_recipient = normalize_phone(recipient)
        if not clean_recipient:
            raise ValueError("destinatario vazio nao pode entrar na outbox")
        clean_payload = _dump_payload(payload)
        now = self._now()
        item = WhatsAppOutboxMessage(
            recipient_key=self.recipient_key(clean_recipient),
            recipient=clean_recipient,
            message_type=message_type,
            payload_json=clean_payload,
            status=STATUS_PENDING,
            attempts=0,
            max_attempts=max_attempts or self._config.max_attempts,
            available_at=_naive(available_at or now),
            dedup_key=(dedup_key or "").strip() or None,
            created_at=_naive(now),
            updated_at=_naive(now),
        )
        with self._session_factory() as session:
            session.add(item)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if not item.dedup_key:
                    raise
                existing_id = session.scalar(
                    select(WhatsAppOutboxMessage.id).where(WhatsAppOutboxMessage.dedup_key == item.dedup_key)
                )
                if existing_id is None:
                    raise
                return existing_id
            logger.info(
                "WhatsApp outbox enqueued item_id=%s recipient_key_prefix=%s status=%s",
                item.id,
                _key_prefix(item.recipient_key),
                STATUS_PENDING,
            )
            return item.id

    def enqueue_body(self, recipient: str, body: str, *, dedup_key: str | None = None) -> int:
        return self.enqueue(
            recipient,
            message_type="body",
            payload={"body": body},
            dedup_key=dedup_key,
        )

    def claim_next(self, *, lease_owner: str | None = None) -> OutboxLease | None:
        now = self._now()
        lease_owner = lease_owner or uuid.uuid4().hex
        lease_until = _naive(now + self._config.lease_duration)
        with self._session_factory() as session:
            self._release_expired_leases(session, now)
            if self._global_pause_until(session) and self._global_pause_until(session) > _naive(now):
                session.commit()
                return None
            candidate_ids = [
                row[0]
                for row in session.execute(
                    select(WhatsAppOutboxMessage.id)
                    .where(WhatsAppOutboxMessage.status == STATUS_PENDING)
                    .where(WhatsAppOutboxMessage.available_at <= _naive(now))
                    .order_by(WhatsAppOutboxMessage.id)
                    .limit(50)
                )
            ]
            for item_id in candidate_ids:
                item = session.get(WhatsAppOutboxMessage, item_id)
                if item is None or item.status != STATUS_PENDING:
                    continue
                older_active = session.scalar(
                    select(func.count(WhatsAppOutboxMessage.id))
                    .where(WhatsAppOutboxMessage.recipient_key == item.recipient_key)
                    .where(WhatsAppOutboxMessage.status.in_(ACTIVE_STATUSES))
                    .where(WhatsAppOutboxMessage.id < item.id)
                )
                if older_active:
                    continue
                result = session.execute(
                    update(WhatsAppOutboxMessage)
                    .where(WhatsAppOutboxMessage.id == item.id)
                    .where(WhatsAppOutboxMessage.status == STATUS_PENDING)
                    .values(
                        status=STATUS_PROCESSING,
                        lease_owner=lease_owner,
                        lease_until=lease_until,
                        updated_at=_naive(now),
                    )
                )
                if result.rowcount != 1:
                    continue
                session.commit()
                session.refresh(item)
                logger.info(
                    "WhatsApp outbox claimed item_id=%s recipient_key_prefix=%s",
                    item.id,
                    _key_prefix(item.recipient_key),
                )
                return _lease_from_item(item)
            session.commit()
            return None

    def mark_sent(self, item_id: int, lease_owner: str) -> bool:
        now = self._now()
        with self._session_factory() as session:
            result = session.execute(
                update(WhatsAppOutboxMessage)
                .where(WhatsAppOutboxMessage.id == item_id)
                .where(WhatsAppOutboxMessage.status == STATUS_PROCESSING)
                .where(WhatsAppOutboxMessage.lease_owner == lease_owner)
                .values(
                    status=STATUS_SENT,
                    lease_owner=None,
                    lease_until=None,
                    sent_at=_naive(now),
                    updated_at=_naive(now),
                )
            )
            session.commit()
        return result.rowcount == 1

    def reschedule(
        self,
        item_id: int,
        lease_owner: str,
        *,
        delay: timedelta,
        error_result: dict[str, Any],
    ) -> bool:
        now = self._now()
        with self._session_factory() as session:
            item = session.get(WhatsAppOutboxMessage, item_id)
            if item is None or item.status != STATUS_PROCESSING or item.lease_owner != lease_owner:
                return False
            attempts = item.attempts + 1
            terminal = attempts >= item.max_attempts
            item.status = STATUS_FAILED if terminal else STATUS_PENDING
            item.attempts = attempts
            item.available_at = _naive(now if terminal else now + delay)
            item.lease_owner = None
            item.lease_until = None
            item.last_http_status = error_result.get("status_code")
            item.last_meta_code = error_result.get("meta_code")
            item.last_error_category = error_result.get("error_class")
            item.last_error_at = _naive(now)
            item.updated_at = _naive(now)
            session.commit()
        return True

    def mark_failed(self, item_id: int, lease_owner: str, *, error_result: dict[str, Any]) -> bool:
        now = self._now()
        with self._session_factory() as session:
            item = session.get(WhatsAppOutboxMessage, item_id)
            if item is None or item.status != STATUS_PROCESSING or item.lease_owner != lease_owner:
                return False
            item.status = STATUS_FAILED
            item.attempts += 1
            item.lease_owner = None
            item.lease_until = None
            item.last_http_status = error_result.get("status_code")
            item.last_meta_code = error_result.get("meta_code")
            item.last_error_category = error_result.get("error_class")
            item.last_error_at = _naive(now)
            item.updated_at = _naive(now)
            session.commit()
        return True

    def release_expired_leases(self) -> int:
        now = self._now()
        with self._session_factory() as session:
            count = self._release_expired_leases(session, now)
            session.commit()
        return count

    def apply_global_pause(self, until: datetime) -> None:
        now = self._now()
        clean_until = _naive(until)
        with self._session_factory() as session:
            control = session.get(WhatsAppOutboxControl, GLOBAL_PAUSE_KEY)
            if control is None:
                control = WhatsAppOutboxControl(
                    key=GLOBAL_PAUSE_KEY,
                    value_datetime=clean_until,
                    updated_at=_naive(now),
                )
                session.add(control)
            elif control.value_datetime is None or control.value_datetime < clean_until:
                control.value_datetime = clean_until
                control.updated_at = _naive(now)
            session.execute(
                update(WhatsAppOutboxMessage)
                .where(WhatsAppOutboxMessage.status == STATUS_PENDING)
                .where(WhatsAppOutboxMessage.available_at < clean_until)
                .values(available_at=clean_until, updated_at=_naive(now))
            )
            session.commit()

    def get(self, item_id: int) -> WhatsAppOutboxMessage | None:
        with self._session_factory() as session:
            item = session.get(WhatsAppOutboxMessage, item_id)
            if item is not None:
                session.expunge(item)
            return item

    def count_by_status(self, status: str) -> int:
        with self._session_factory() as session:
            return session.scalar(
                select(func.count(WhatsAppOutboxMessage.id)).where(WhatsAppOutboxMessage.status == status)
            ) or 0

    def cleanup_sent_before(self, before: datetime) -> int:
        # Future retention hook; intentionally not scheduled automatically in this module.
        with self._session_factory() as session:
            result = session.execute(
                update(WhatsAppOutboxMessage)
                .where(WhatsAppOutboxMessage.status == STATUS_SENT)
                .where(WhatsAppOutboxMessage.sent_at < _naive(before))
                .values(status=STATUS_CANCELLED)
            )
            session.commit()
        return result.rowcount or 0

    def _release_expired_leases(self, session: Session, now: datetime) -> int:
        result = session.execute(
            update(WhatsAppOutboxMessage)
            .where(WhatsAppOutboxMessage.status == STATUS_PROCESSING)
            .where(WhatsAppOutboxMessage.lease_until <= _naive(now))
            .values(
                status=STATUS_PENDING,
                lease_owner=None,
                lease_until=None,
                updated_at=_naive(now),
            )
        )
        if result.rowcount:
            logger.warning("WhatsApp outbox expired leases recovered count=%s", result.rowcount)
        return result.rowcount or 0

    def _global_pause_until(self, session: Session) -> datetime | None:
        control = session.get(WhatsAppOutboxControl, GLOBAL_PAUSE_KEY)
        return control.value_datetime if control else None

    def _now(self) -> datetime:
        return _naive(self._now_provider())


def _lease_from_item(item: WhatsAppOutboxMessage) -> OutboxLease:
    return OutboxLease(
        item_id=item.id,
        recipient_key=item.recipient_key,
        recipient=item.recipient,
        message_type=item.message_type,
        payload=json.loads(item.payload_json),
        attempts=item.attempts,
        max_attempts=item.max_attempts,
        lease_owner=item.lease_owner or "",
        lease_until=item.lease_until,
    )


def _dump_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload da outbox deve ser objeto JSON")
    try:
        dumped = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        json.loads(dumped)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload da outbox deve ser JSON valido") from exc
    return dumped


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _key_prefix(value: str | None) -> str:
    return (value or "")[:12]
