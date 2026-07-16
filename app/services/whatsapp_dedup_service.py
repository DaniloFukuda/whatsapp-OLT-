from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.whatsapp_dedup import WhatsAppProcessedMessage


logger = get_logger(__name__)


STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
PROCESSING_CLAIM_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class MessageClaim:
    should_process: bool
    reason: str


class WhatsAppMessageDedupService:
    def __init__(self, db: Session):
        self._session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)

    def claim(self, message_id: str) -> MessageClaim:
        clean_id = (message_id or "").strip()
        if not clean_id:
            logger.warning("WhatsApp message without message_id; processing without dedup")
            return MessageClaim(True, "missing_message_id")

        now = _dedup_now()
        with self._session_factory() as session:
            try:
                session.add(
                    WhatsAppProcessedMessage(
                        message_id=clean_id,
                        status=STATUS_PROCESSING,
                        criado_em=now,
                        atualizado_em=now,
                    )
                )
                session.commit()
                logger.info("WhatsApp message claimed message_id=%s status=%s", clean_id, STATUS_PROCESSING)
                return MessageClaim(True, "claimed")
            except IntegrityError:
                session.rollback()

            existing = (
                session.query(WhatsAppProcessedMessage)
                .filter(WhatsAppProcessedMessage.message_id == clean_id)
                .first()
            )
            if not existing:
                logger.warning("WhatsApp dedup claim conflict without row message_id=%s", clean_id)
                return MessageClaim(False, "claim_conflict")

            if existing.status == STATUS_COMPLETED:
                logger.info("WhatsApp duplicate ignored message_id=%s status=%s", clean_id, STATUS_COMPLETED)
                return MessageClaim(False, "completed")

            cutoff = now - PROCESSING_CLAIM_TTL
            is_expired_processing = existing.status == STATUS_PROCESSING and existing.atualizado_em < cutoff
            if existing.status == STATUS_PROCESSING and not is_expired_processing:
                logger.info("WhatsApp message already processing message_id=%s", clean_id)
                return MessageClaim(False, "processing")

            statement = (
                update(WhatsAppProcessedMessage)
                .where(WhatsAppProcessedMessage.message_id == clean_id)
                .where(
                    or_(
                        WhatsAppProcessedMessage.status == STATUS_FAILED,
                        (
                            (WhatsAppProcessedMessage.status == STATUS_PROCESSING)
                            & (WhatsAppProcessedMessage.atualizado_em < cutoff)
                        ),
                    )
                )
                .values(status=STATUS_PROCESSING, atualizado_em=now, concluido_em=None)
            )
            result = session.execute(statement)
            session.commit()
            if result.rowcount == 1:
                reason = "expired_processing_reclaimed" if is_expired_processing else "failed_reclaimed"
                logger.info("WhatsApp message reclaimed message_id=%s reason=%s", clean_id, reason)
                return MessageClaim(True, reason)

            logger.info("WhatsApp message claim lost race message_id=%s", clean_id)
            return MessageClaim(False, "lost_race")

    def mark_completed(self, message_id: str) -> None:
        self._mark(message_id, STATUS_COMPLETED, completed=True)

    def mark_failed(self, message_id: str) -> None:
        self._mark(message_id, STATUS_FAILED, completed=False)

    def _mark(self, message_id: str, status: str, *, completed: bool) -> None:
        clean_id = (message_id or "").strip()
        if not clean_id:
            return
        now = _dedup_now()
        values = {"status": status, "atualizado_em": now}
        if completed:
            values["concluido_em"] = now
        with self._session_factory() as session:
            session.execute(
                update(WhatsAppProcessedMessage)
                .where(WhatsAppProcessedMessage.message_id == clean_id)
                .values(**values)
            )
            session.commit()
        logger.info("WhatsApp message marked message_id=%s status=%s", clean_id, status)


def _dedup_now():
    return utcnow().replace(tzinfo=None)
