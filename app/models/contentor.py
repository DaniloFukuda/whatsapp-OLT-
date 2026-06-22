from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.time import utcnow


class StatusContentor(StrEnum):
    DISPONIVEL = "disponivel"
    ALUGADO = "alugado"
    AGUARDANDO_RECOLHA = "aguardando_recolha"
    MANUTENCAO = "manutencao"


class Contentor(Base):
    __tablename__ = "contentores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    codigo: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    status: Mapped[StatusContentor] = mapped_column(
        Enum(StatusContentor),
        default=StatusContentor.DISPONIVEL,
        nullable=False,
    )
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    criado_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    alterado_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    excluido_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    justificativa_exclusao: Mapped[str | None] = mapped_column(Text, nullable=True)

    alugueres = relationship("AluguerContentor", back_populates="contentor")
