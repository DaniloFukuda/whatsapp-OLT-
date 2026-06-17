from enum import StrEnum

from sqlalchemy import DateTime, Enum, Integer, String
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

    alugueres = relationship("AluguerContentor", back_populates="contentor")
