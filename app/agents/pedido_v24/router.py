"""Seam de delegação para o backend legado do Pedido V24."""

from sqlalchemy.orm import Session

from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp


class PedidoV24OperationalRouter:
    """Delega operações V24 sem interpretar estado, contexto ou modalidade."""

    PREFIX = PedidoV24Agent.PREFIX
    _START_METHODS = {
        "cadastro": "start_cadastro",
        "entrega": "start_entrega",
        "recolha": "start_recolha",
        "despejo": "start_despejo",
    }

    def __init__(self, db: Session | None = None, *, backend=None):
        if backend is None:
            if db is None:
                raise TypeError("db é obrigatório quando backend não é fornecido")
            backend = PedidoV24Agent(db)
        self._backend = backend

    @property
    def backend(self):
        return self._backend

    @staticmethod
    def resolve_modality(context):
        return resolve_operational_modality(context)

    def start(self, operation: str, conversa: ConversaWhatsApp) -> str:
        method_name = self._START_METHODS.get(operation)
        if method_name is None:
            raise ValueError(f"Operação operacional inválida: {operation}")
        return getattr(self._backend, method_name)(conversa)

    def start_cadastro(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_cadastro(conversa)

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_entrega(conversa)

    def start_recolha(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_recolha(conversa)

    def start_despejo(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_despejo(conversa)

    def handle(
        self,
        conversa: ConversaWhatsApp,
        message: NormalizedWhatsAppMessage,
    ) -> str:
        return self._backend.handle(conversa, message)
