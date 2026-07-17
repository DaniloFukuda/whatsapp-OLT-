from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.time import utcnow


class WhatsAppOutboxMessage(Base):
    __tablename__ = "whatsapp_outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_whatsapp_outbox_messages_dedup_key"),
        Index("ix_whatsapp_outbox_status_available_at", "status", "available_at"),
        Index("ix_whatsapp_outbox_recipient_key", "recipient_key"),
        Index("ix_whatsapp_outbox_recipient_status_id", "recipient_key", "status", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient_key: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient: Mapped[str] = mapped_column(String(50), nullable=False)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_meta_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_error_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    sent_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppOutboxControl(Base):
    __tablename__ = "whatsapp_outbox_control"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_datetime: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
