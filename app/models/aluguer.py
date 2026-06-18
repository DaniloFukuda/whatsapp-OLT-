from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.time import utcnow


class StatusAluguer(StrEnum):
    ATIVO = "ativo"
    VENCENDO = "vencendo"
    RENOVADO = "renovado"
    AGUARDANDO_RECOLHA = "aguardando_recolha"
    RECOLHIDO = "recolhido"
    CANCELADO = "cancelado"


class AluguerContentor(Base):
    __tablename__ = "alugueres_contentor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    contentor_id: Mapped[int] = mapped_column(ForeignKey("contentores.id"), nullable=False)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("clientes.id"), nullable=False)
    telefone_cliente: Mapped[str] = mapped_column(String(50), nullable=False)
    nome_cliente: Mapped[str] = mapped_column(String(255), nullable=False)
    email_cliente: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantidade_contentores: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    data_entrega: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    data_vencimento: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    tipo_residuo: Mapped[str | None] = mapped_column(String(80), nullable=True)
    valor: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    forma_pagamento: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pago: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    operador_telefone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[StatusAluguer] = mapped_column(Enum(StatusAluguer), default=StatusAluguer.ATIVO, nullable=False)
    foto_entrega_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    observacoes: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    cliente = relationship("Cliente", back_populates="alugueres")
    contentor = relationship("Contentor", back_populates="alugueres")
    eventos = relationship("EventoAluguer", back_populates="aluguer")


class EventoAluguer(Base):
    __tablename__ = "eventos_aluguer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    aluguer_id: Mapped[int] = mapped_column(ForeignKey("alugueres_contentor.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(80), nullable=False)
    descricao: Mapped[str] = mapped_column(Text, nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    aluguer = relationship("AluguerContentor", back_populates="eventos")
