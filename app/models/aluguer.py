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


class StatusEntrega(StrEnum):
    PENDENTE = "PENDENTE"
    ENTREGUE = "ENTREGUE"


class AluguerContentor(Base):
    __tablename__ = "alugueres_contentor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    contentor_id: Mapped[int] = mapped_column(ForeignKey("contentores.id"), nullable=False)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("clientes.id"), nullable=False)
    telefone_cliente: Mapped[str] = mapped_column(String(50), nullable=False)
    nome_cliente: Mapped[str] = mapped_column(String(255), nullable=False)
    email_cliente: Mapped[str | None] = mapped_column(String(255), nullable=True)
    numero_contentor: Mapped[str] = mapped_column(String(20), nullable=False)
    data_entrega: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    data_vencimento: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    tipo_residuo: Mapped[str | None] = mapped_column(String(80), nullable=True)
    valor: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    forma_pagamento: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pago: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    operador_telefone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    criado_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pedido_feito_por: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entrega_feita_por: Mapped[str | None] = mapped_column(String(50), nullable=True)
    alterado_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    excluido_por_operador: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    justificativa_exclusao: Mapped[str | None] = mapped_column(Text, nullable=True)
    id_fatura_fiscal: Mapped[str | None] = mapped_column(String(120), nullable=True)
    google_event_entrega_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_event_retirada_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_ciclo_cliente: Mapped[str] = mapped_column(String(80), default="EM_ANDAMENTO", nullable=False)
    status_entrega: Mapped[str] = mapped_column(String(20), default=StatusEntrega.ENTREGUE.value, nullable=False)
    status: Mapped[StatusAluguer] = mapped_column(Enum(StatusAluguer), default=StatusAluguer.ATIVO, nullable=False)
    foto_entrega_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pedido_endereco_tipo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pedido_endereco_texto: Mapped[str | None] = mapped_column(Text, nullable=True)
    pedido_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pedido_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pedido_ponto_referencia: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entrega_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    entrega_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    entrega_ponto_referencia: Mapped[str | None] = mapped_column(String(50), nullable=True)
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
    fotos = relationship("ContentorFoto", back_populates="aluguer")


class EventoAluguer(Base):
    __tablename__ = "eventos_aluguer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    aluguer_id: Mapped[int] = mapped_column(ForeignKey("alugueres_contentor.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(80), nullable=False)
    descricao: Mapped[str] = mapped_column(Text, nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    aluguer = relationship("AluguerContentor", back_populates="eventos")


class ContentorFoto(Base):
    __tablename__ = "contentor_fotos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    aluguer_id: Mapped[int] = mapped_column(ForeignKey("alugueres_contentor.id"), nullable=False)
    url_foto: Mapped[str] = mapped_column(String(500), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), default="entrega", nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    aluguer = relationship("AluguerContentor", back_populates="fotos")
