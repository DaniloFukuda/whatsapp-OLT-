from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.time import utcnow


class ConversaWhatsApp(Base):
    __tablename__ = "conversas_whatsapp"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    telefone: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    estado_atual: Mapped[str] = mapped_column(String(80), default="idle", nullable=False)
    contexto_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
