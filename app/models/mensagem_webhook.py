from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.time import utcnow


class StatusMensagemWebhook(StrEnum):
    PROCESSANDO = "PROCESSANDO"
    CONCLUIDA = "CONCLUIDA"
    FALHOU_REPROCESSAVEL = "FALHOU_REPROCESSAVEL"
    FALHOU_DEFINITIVA = "FALHOU_DEFINITIVA"


class MensagemWebhook(Base):
    __tablename__ = "mensagens_webhook"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSANDO', 'CONCLUIDA', 'FALHOU_REPROCESSAVEL', 'FALHOU_DEFINITIVA')",
            name="ck_mensagens_webhook_status",
        ),
    )

    message_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    telefone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tentativas: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    recebido_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False, index=True
    )
    concluido_em: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    codigo_erro: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resposta_enviada: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
