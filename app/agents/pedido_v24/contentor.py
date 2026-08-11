"""Entrada operacional de entrega de Contentor no fluxo V2.4."""

from collections.abc import Callable
from typing import Any

from app.models.conversa import ConversaWhatsApp


class ContentorOperationalAgent:
    """Carrega Contentores pendentes e entrega a composição ao legado."""

    def __init__(
        self,
        pedidos_pendentes_entrega: Callable[[], list[Any]],
        legacy_backend: Any,
    ):
        self._pedidos_pendentes_entrega = pedidos_pendentes_entrega
        self._legacy_backend = legacy_backend

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        pedidos_contentor = self._pedidos_pendentes_entrega()
        return self._legacy_backend._start_entrega_com_pedidos_contentor(
            conversa,
            pedidos_contentor,
        )

    def select_entrega_pedido(self, conversa, message) -> str:
        """Executa somente a selecao de Contentor; os estados seguintes seguem legados."""
        return self._legacy_backend.handle(conversa, message)
