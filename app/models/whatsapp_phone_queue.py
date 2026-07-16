from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.time import utcnow


class WhatsAppPhoneQueueItem(Base):
    __tablename__ = "whatsapp_phone_queue"
    __table_args__ = (
        Index("ix_whatsapp_phone_queue_phone_key", "phone_key"),
        Index("ix_whatsapp_phone_queue_phone_status_id", "phone_key", "status", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_key: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    lease_ate: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
