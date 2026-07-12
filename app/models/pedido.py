from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.time import utcnow


class StatusPagamento(StrEnum):
    PAGO = "PAGO"
    PENDENTE = "PENDENTE"


class StatusEntregaPedido(StrEnum):
    PENDENTE = "PENDENTE"
    ENTREGUE = "ENTREGUE"


class StatusRecolhaPedido(StrEnum):
    PENDENTE = "PENDENTE"
    RECOLHIDO = "RECOLHIDO"


class StatusCicloPedido(StrEnum):
    EM_ANDAMENTO = "EM_ANDAMENTO"
    CONCLUIDO = "CONCLUIDO"


class StatusResolucaoPedido(StrEnum):
    NAO_APLICA = "N/A"
    PENDENTE = "PENDENTE"
    RESOLVIDO = "RESOLVIDO"


class TipoFoto(StrEnum):
    ENTREGA = "ENTREGA"
    RECOLHA = "RECOLHA"
    DESPEJO = "DESPEJO"


class TipoEquipamentoPedido(StrEnum):
    CONTENTOR = "CONTENTOR"
    CARRINHA = "CARRINHA"


class Pedido(Base):
    __tablename__ = "pedidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    nome_cliente: Mapped[str] = mapped_column(String(255), nullable=False)
    telefone_cliente: Mapped[str] = mapped_column(String(50), nullable=False)
    data_planejada: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    valor_global: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status_pagamento: Mapped[str] = mapped_column(
        String(20), default=StatusPagamento.PENDENTE.value, nullable=False
    )
    forma_pagamento: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pedido_feito_por: Mapped[str] = mapped_column(String(50), nullable=False)
    endereco_aproximado: Mapped[str] = mapped_column(Text, nullable=False)
    endereco_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    endereco_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    ponto_referencia: Mapped[str | None] = mapped_column(String(50), nullable=True)
    precisa_mao_de_obra: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    contentores = relationship(
        "PedidoContentor", back_populates="pedido", cascade="all, delete-orphan"
    )


class PedidoContentor(Base):
    __tablename__ = "pedido_contentores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    pedido_id: Mapped[int] = mapped_column(
        ForeignKey("pedidos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    numero_adesivo_contentor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tipo_equipamento: Mapped[str] = mapped_column(
        String(20), default=TipoEquipamentoPedido.CONTENTOR.value, nullable=False
    )
    horario_agendado: Mapped[str | None] = mapped_column(String(5), nullable=True)
    precisa_mao_de_obra: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    residuo_contratado: Mapped[str] = mapped_column(String(80), nullable=False)
    residuo_efetivo_vazadouro: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status_entrega: Mapped[str] = mapped_column(
        String(20), default=StatusEntregaPedido.PENDENTE.value, nullable=False
    )
    entrega_feita_por: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entrega_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    entrega_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    entrega_ponto_referencia: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entrega_data_hora: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_recolha: Mapped[str] = mapped_column(
        String(20), default=StatusRecolhaPedido.PENDENTE.value, nullable=False
    )
    recolha_feita_por: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recolha_data_hora: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    contentor_avariado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    relato_avaria: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_resolucao_avaria: Mapped[str] = mapped_column(
        String(20), default=StatusResolucaoPedido.NAO_APLICA.value, nullable=False
    )
    status_ciclo: Mapped[str] = mapped_column(
        String(20), default=StatusCicloPedido.EM_ANDAMENTO.value, nullable=False
    )
    carga_errada: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    relato_carga: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_resolucao_carga: Mapped[str] = mapped_column(
        String(20), default=StatusResolucaoPedido.NAO_APLICA.value, nullable=False
    )
    despejo_feito_por: Mapped[str | None] = mapped_column(String(50), nullable=True)
    despejo_data_hora: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    criado_em: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    atualizado_em: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    pedido = relationship("Pedido", back_populates="contentores")
    fotos = relationship(
        "ContentorFoto", back_populates="pedido_contentor", cascade="all, delete-orphan"
    )
