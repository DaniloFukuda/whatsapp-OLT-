"""Fronteira de roteamento operacional do Pedido V24."""

from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24.router import PedidoV24OperationalRouter

__all__ = ["PedidoV24OperationalRouter", "resolve_operational_modality"]
