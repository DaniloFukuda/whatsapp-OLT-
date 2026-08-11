"""Seam de delegação para o backend legado do Pedido V24."""

from sqlalchemy.orm import Session

from app.agents.pedido_v24.contentor import ContentorOperationalAgent
from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import TipoEquipamentoPedido


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
        self._contentor = None
        if isinstance(backend, PedidoV24Agent):
            self._contentor = ContentorOperationalAgent(
                backend.service.pedidos_pendentes_entrega,
                backend,
            )

    @property
    def backend(self):
        return self._backend

    @staticmethod
    def resolve_modality(context, selected_pedido_id: int | None = None):
        return resolve_operational_modality(context, selected_pedido_id)

    def start(self, operation: str, conversa: ConversaWhatsApp) -> str:
        method_name = self._START_METHODS.get(operation)
        if method_name is None:
            raise ValueError(f"Operação operacional inválida: {operation}")
        return getattr(self._backend, method_name)(conversa)

    def start_cadastro(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_cadastro(conversa)

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        if self._contentor is not None and get_settings().feature_contentores_enabled:
            return self._contentor.start_entrega(conversa)
        return self._backend.start_entrega(conversa)

    def start_recolha(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_recolha(conversa)

    def start_despejo(self, conversa: ConversaWhatsApp) -> str:
        return self._backend.start_despejo(conversa)

    def entrega_confirmacao_prompt(self, context) -> str:
        return self._backend.entrega_confirmacao_prompt(context)

    def handle(
        self,
        conversa: ConversaWhatsApp,
        message: NormalizedWhatsAppMessage,
    ) -> str:
        if getattr(conversa, "estado_atual", None) == "v24_entrega_pedido" and self._contentor is not None:
            context = conversa.contexto_json or {}
            raw = (message.texto or "").strip()
            pedido_id = self._backend._selected_entrega_pedido_id(
                raw,
                context.get("ids", []),
            )
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                return self._contentor.select_entrega_pedido(conversa, message)
        if getattr(conversa, "estado_atual", None) == "v24_entrega_adesivo" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_adesivo(conversa, message)
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                if decision is not None:
                    return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_foto" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_foto(conversa, message)
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_foto_acao" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_foto_acao(
                    conversa,
                    message,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_gps" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_gps(conversa, message)
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        return self._backend.handle(conversa, message)
