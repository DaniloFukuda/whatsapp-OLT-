from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.time import utcnow


class WhatsAppProcessedMessage(Base):
    __tablename__ = "whatsapp_processed_messages"
    __table_args__ = (UniqueConstraint("message_id", name="uq_whatsapp_processed_messages_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    concluido_em: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
