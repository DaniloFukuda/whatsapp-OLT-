"""Seam de delegação para o backend legado do Pedido V24."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.pedido_v24.contentor import (
    CancelarRecolhaContentor,
    ConfirmEntregaContentor,
    ConfirmarDespejoContentor,
    ConfirmarRecolhaContentor,
    ContentorOperationalAgent,
    PrepararConfirmacaoDespejoContentor,
    PrepararConformidadeDespejoContentor,
    PrepararFotoDespejoContentor,
    PrepararConfirmacaoRecolhaContentor,
    RegistrarPagamentoEntregaContentor,
)
from app.agents.pedido_v24.contentor_cadastro import (
    CadastroModality,
    ContentorCadastroAgent,
    classify_cadastro_modality,
)
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
        self._contentor_cadastro = ContentorCadastroAgent()
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
        cadastro_decisions = {
            "v24_cadastro_nome": self._contentor_cadastro.decide_nome,
            "v24_cadastro_telefone": self._contentor_cadastro.decide_telefone,
            "v24_cadastro_quantidade": self._contentor_cadastro.decide_quantidade,
        }
        cadastro_decision = cadastro_decisions.get(
            getattr(conversa, "estado_atual", None)
        )
        if (
            cadastro_decision is not None
            and classify_cadastro_modality(conversa.contexto_json or {})
            is CadastroModality.CONTENTOR_INTENT
        ):
            decision = cadastro_decision(conversa.contexto_json or {}, message)
            if isinstance(decision, AdvanceTransition):
                return self._backend.apply_operational_transition(conversa, decision)
            return decision
        cadastro_state = getattr(conversa, "estado_atual", None)
        cadastro_context = getattr(conversa, "contexto_json", None) or {}
        cadastro_modality = classify_cadastro_modality(cadastro_context)
        if (
            cadastro_state == "v24_cadastro_tipo_equipamento"
            and cadastro_modality is CadastroModality.CONTENTOR_INTENT
            and self._contentor_cadastro.is_contentor_item_selection(message.texto)
        ):
            decision = self._contentor_cadastro.decide_tipo_equipamento(
                cadastro_context
            )
            return self._backend.apply_operational_transition(conversa, decision)
        item_atual = cadastro_context.get("item_atual")
        item_atual_contentor = (
            isinstance(item_atual, dict)
            and item_atual.get("tipo_equipamento")
            == TipoEquipamentoPedido.CONTENTOR.value
        )
        if (
            cadastro_state in {"v24_cadastro_mao_obra", "v24_cadastro_residuo"}
            and cadastro_modality is CadastroModality.CONTENTOR_INTENT
            and item_atual_contentor
        ):
            decide = (
                self._contentor_cadastro.decide_mao_obra
                if cadastro_state == "v24_cadastro_mao_obra"
                else self._contentor_cadastro.decide_residuo
            )
            decision = decide(cadastro_context, message)
            if isinstance(decision, AdvanceTransition):
                return self._backend.apply_operational_transition(conversa, decision)
            return decision
        if (
            cadastro_state in {
                "v24_cadastro_data",
                "v24_cadastro_data_manual",
                "v24_cadastro_valor",
            }
            and cadastro_modality is CadastroModality.CONTENTOR_PROVEN
        ):
            timezone = self._backend._lisbon_timezone()
            if cadastro_state == "v24_cadastro_data":
                decision = self._contentor_cadastro.decide_data(
                    cadastro_context,
                    message,
                    datetime.now(timezone),
                )
            elif cadastro_state == "v24_cadastro_data_manual":
                decision = self._contentor_cadastro.decide_data_manual(
                    cadastro_context,
                    message,
                    timezone,
                )
            else:
                decision = self._contentor_cadastro.decide_valor(
                    cadastro_context,
                    message,
                )
            if isinstance(decision, AdvanceTransition):
                return self._backend.apply_operational_transition(conversa, decision)
            return decision
        if (
            cadastro_state in {
                "v24_cadastro_pago",
                "v24_cadastro_forma",
                "v24_cadastro_forma_outro",
                "v24_cadastro_endereco",
                "v24_cadastro_referencia_opcao",
                "v24_cadastro_referencia",
            }
            and cadastro_modality is CadastroModality.CONTENTOR_PROVEN
        ):
            if cadastro_state == "v24_cadastro_endereco":
                decision = self._contentor_cadastro.decide_endereco(
                    cadastro_context,
                    message,
                    self._backend._coordinates(message, (message.texto or "").strip()),
                )
            else:
                decide = {
                    "v24_cadastro_pago": self._contentor_cadastro.decide_pago,
                    "v24_cadastro_forma": self._contentor_cadastro.decide_forma,
                    "v24_cadastro_forma_outro": self._contentor_cadastro.decide_forma_outro,
                    "v24_cadastro_referencia_opcao": self._contentor_cadastro.decide_referencia_opcao,
                    "v24_cadastro_referencia": self._contentor_cadastro.decide_referencia,
                }[cadastro_state]
                decision = decide(cadastro_context, message)
            if (
                isinstance(decision, AdvanceTransition)
                and decision.next_state == "v24_cadastro_confirmacao"
            ):
                decision = AdvanceTransition(
                    decision.next_state,
                    decision.context,
                    self._backend.cadastro_confirmacao_prompt(decision.context),
                )
            if isinstance(decision, AdvanceTransition):
                return self._backend.apply_operational_transition(conversa, decision)
            return decision
        if (
            getattr(conversa, "estado_atual", None)
            == "v24_cadastro_tipo_solicitacao"
            and self._contentor_cadastro.is_contentor_selection(message.texto)
            and get_settings().feature_contentores_enabled
        ):
            decision = self._contentor_cadastro.decide_tipo_solicitacao(
                conversa.contexto_json or {}
            )
            return self._backend.apply_operational_transition(conversa, decision)
        if (
            getattr(conversa, "estado_atual", None) == "v24_despejo_ativo"
            and self._contentor is not None
            and get_settings().feature_contentores_enabled
        ):
            context = conversa.contexto_json or {}
            selection = self._backend.resolve_despejo_ativo_selection(
                message,
                context,
            )
            if (
                selection is not None
                and selection["modality"] is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_despejo_ativo(
                    conversa,
                    selection,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) in {
            "v24_despejo_foto",
            "v24_despejo_foto_acao",
        } and self._contentor is not None:
            context = conversa.contexto_json or {}
            if (
                get_settings().feature_contentores_enabled
                and self._backend.resolve_despejo_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decide = (
                    self._contentor.decide_despejo_foto
                    if conversa.estado_atual == "v24_despejo_foto"
                    else self._contentor.decide_despejo_foto_acao
                )
                decision = decide(conversa, message)
                if isinstance(decision, PrepararConfirmacaoDespejoContentor):
                    decision = AdvanceTransition(
                        "v24_despejo_confirmacao",
                        decision.context,
                        self._backend.despejo_confirmacao_prompt(
                            decision.context
                        ),
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) in {
            "v24_despejo_residuo",
            "v24_despejo_conformidade",
            "v24_despejo_relato",
        } and self._contentor is not None:
            context = conversa.contexto_json or {}
            state = conversa.estado_atual
            if (
                get_settings().feature_contentores_enabled
                and self._backend.despejo_context_is_modern(context, state)
                and self._backend.resolve_despejo_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decide = {
                    "v24_despejo_residuo": self._contentor.decide_despejo_residuo,
                    "v24_despejo_conformidade": self._contentor.decide_despejo_conformidade,
                    "v24_despejo_relato": self._contentor.decide_despejo_relato,
                }[state]
                decision = decide(conversa, message)
                if isinstance(decision, PrepararFotoDespejoContentor):
                    decision = AdvanceTransition(
                        "v24_despejo_foto",
                        decision.context,
                        self._backend.despejo_foto_prompt(decision.context),
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if (
            getattr(conversa, "estado_atual", None) == "v24_despejo_confirmacao"
            and self._contentor is not None
        ):
            context = conversa.contexto_json or {}
            if (
                get_settings().feature_contentores_enabled
                and self._backend.despejo_context_is_modern(
                    context,
                    "v24_despejo_confirmacao",
                )
                and self._backend.resolve_despejo_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_despejo_confirmacao(
                    conversa,
                    message,
                )
                if isinstance(decision, ConfirmarDespejoContentor):
                    return self._backend.confirm_despejo_contentor(
                        conversa,
                        decision.context,
                    )
                if isinstance(decision, PrepararConformidadeDespejoContentor):
                    decision = AdvanceTransition(
                        "v24_despejo_conformidade",
                        decision.context,
                        self._backend.despejo_conformidade_prompt(
                            decision.context
                        ),
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if (
            getattr(conversa, "estado_atual", None) == "v24_recolha_ativo"
            and self._contentor is not None
            and get_settings().feature_contentores_enabled
        ):
            context = conversa.contexto_json or {}
            selection = self._backend.resolve_recolha_ativo_selection(
                message,
                context,
            )
            if (
                selection is not None
                and selection["modality"] is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_recolha_ativo(
                    conversa,
                    selection,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) in {
            "v24_recolha_foto",
            "v24_recolha_foto_acao",
        } and self._contentor is not None:
            context = conversa.contexto_json or {}
            settings = get_settings()
            if (
                settings.feature_contentores_enabled
                and self._backend.resolve_recolha_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                if conversa.estado_atual == "v24_recolha_foto":
                    decision = self._contentor.decide_recolha_foto(
                        conversa,
                        message,
                    )
                else:
                    avarias_enabled = settings.feature_avarias_enabled
                    decision = self._contentor.decide_recolha_foto_acao(
                        conversa,
                        message,
                        avarias_enabled=avarias_enabled,
                    )
                if isinstance(decision, PrepararConfirmacaoRecolhaContentor):
                    prompt = self._backend.recolha_confirmacao_prompt(
                        decision.context
                    )
                    decision = AdvanceTransition(
                        "v24_recolha_confirmacao",
                        decision.context,
                        decision.response_prefix + prompt,
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) in {
            "v24_recolha_avaria",
            "v24_recolha_relato",
        } and self._contentor is not None:
            context = conversa.contexto_json or {}
            settings = get_settings()
            if (
                settings.feature_contentores_enabled
                and self._backend.resolve_recolha_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decide = (
                    self._contentor.decide_recolha_avaria
                    if conversa.estado_atual == "v24_recolha_avaria"
                    else self._contentor.decide_recolha_relato
                )
                decision = decide(
                    conversa,
                    message,
                    avarias_enabled=settings.feature_avarias_enabled,
                )
                if isinstance(decision, PrepararConfirmacaoRecolhaContentor):
                    prompt = self._backend.recolha_confirmacao_prompt(
                        decision.context
                    )
                    decision = AdvanceTransition(
                        "v24_recolha_confirmacao",
                        decision.context,
                        decision.response_prefix + prompt,
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if (
            getattr(conversa, "estado_atual", None)
            == "v24_recolha_confirmacao"
            and self._contentor is not None
        ):
            context = conversa.contexto_json or {}
            settings = get_settings()
            if (
                settings.feature_contentores_enabled
                and self._backend.resolve_recolha_context_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_recolha_confirmacao(
                    conversa,
                    message,
                    avarias_enabled=settings.feature_avarias_enabled,
                )
                if isinstance(decision, ConfirmarRecolhaContentor):
                    return self._backend.confirm_recolha_contentor(
                        conversa,
                        decision.context,
                    )
                if isinstance(decision, CancelarRecolhaContentor):
                    return self._backend.cancel_recolha_contentor(
                        conversa,
                        decision.context,
                    )
                if isinstance(decision, PrepararConfirmacaoRecolhaContentor):
                    prompt = self._backend.recolha_confirmacao_prompt(
                        decision.context
                    )
                    decision = AdvanceTransition(
                        "v24_recolha_confirmacao",
                        decision.context,
                        decision.response_prefix + prompt,
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
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
        if getattr(conversa, "estado_atual", None) == "v24_entrega_referencia_opcao" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_referencia_opcao(
                    conversa,
                    message,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_referencia" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_referencia(
                    conversa,
                    message,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_confirmacao" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_confirmacao(
                    conversa,
                    message,
                )
                if isinstance(decision, ConfirmEntregaContentor):
                    return self._backend.confirm_entrega_contentor(
                        conversa,
                        decision.context,
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_pagou" and self._contentor is not None:
            context = conversa.contexto_json or {}
            if (
                self._backend.resolve_entrega_pagamento_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decision = self._contentor.decide_entrega_pagou(conversa, message)
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) in {
            "v24_entrega_forma",
            "v24_entrega_forma_outro",
        } and self._contentor is not None:
            context = conversa.contexto_json or {}
            if (
                self._backend.resolve_entrega_pagamento_modality(context)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                decide = (
                    self._contentor.decide_entrega_forma
                    if conversa.estado_atual == "v24_entrega_forma"
                    else self._contentor.decide_entrega_forma_outro
                )
                decision = decide(conversa, message)
                if isinstance(decision, RegistrarPagamentoEntregaContentor):
                    return self._backend.registrar_pagamento_entrega_contentor(
                        conversa,
                        decision.context,
                        decision.forma,
                    )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        return self._backend.handle(conversa, message)
