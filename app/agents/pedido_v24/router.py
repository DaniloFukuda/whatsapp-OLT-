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
    ConfirmarCadastroContentor,
    ContentorCadastroAgent,
    classify_cadastro_modality,
)
from app.agents.pedido_v24.carrinha_cadastro import (
    CarrinhaCadastroAgent,
    CarrinhaCadastroModality,
    ConfirmarCadastroCarrinha,
    classify_carrinha_cadastro,
)
from app.agents.pedido_v24.carrinha import (
    CancelarPartidaCarrinha,
    CarrinhaOperationalAgent,
    ConfirmarChegadaCarrinha,
    ConfirmarPartidaCarrinha,
    PrepararConfirmacaoPartidaCarrinha,
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
        self._carrinha_cadastro = CarrinhaCadastroAgent()
        self._carrinha = CarrinhaOperationalAgent()
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
        carrinha_response = self._handle_carrinha_cadastro(conversa, message)
        if carrinha_response is not None:
            return carrinha_response
        carrinha_response = self._handle_carrinha_chegada(conversa, message)
        if carrinha_response is not None:
            return carrinha_response
        carrinha_response = self._handle_carrinha_partida(conversa, message)
        if carrinha_response is not None:
            return carrinha_response
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
            cadastro_state == "v24_cadastro_confirmacao"
            and cadastro_modality is CadastroModality.CONTENTOR_PROVEN
        ):
            decision = self._contentor_cadastro.decide_confirmacao(
                cadastro_context,
                message,
            )
            if isinstance(decision, ConfirmarCadastroContentor):
                response = self._backend.confirmar_cadastro_contentor(
                    conversa,
                    decision.context,
                )
                if response is not None:
                    return response
                return self._backend.handle(conversa, message)
            if (
                isinstance(decision, AdvanceTransition)
                and decision.next_state == "v24_cadastro_corrigir"
            ):
                decision = AdvanceTransition(
                    decision.next_state,
                    decision.context,
                    self._backend._corrigir_prompt(decision.context),
                )
            if isinstance(decision, (AdvanceTransition, IdleTransition)):
                return self._backend.apply_operational_transition(
                    conversa,
                    decision,
                )
            return decision
        if (
            cadastro_state == "v24_cadastro_corrigir"
            and cadastro_modality is CadastroModality.CONTENTOR_PROVEN
        ):
            choice = self._backend._norm(message.texto or "")
            field, next_state, prompt = self._backend.cadastro_corrigir_decision(
                choice,
                cadastro_context,
            )
            decision = self._contentor_cadastro.decide_corrigir(
                cadastro_context,
                field,
                next_state,
                prompt,
            )
            if (
                isinstance(decision, AdvanceTransition)
                and decision.next_state == "v24_cadastro_confirmacao"
            ):
                decision = AdvanceTransition(
                    decision.next_state,
                    decision.context,
                    "A forma de pagamento só pode ser corrigida quando o pedido estiver pago.\n\n"
                    + self._backend.cadastro_confirmacao_prompt(decision.context),
                )
            if isinstance(decision, AdvanceTransition):
                return self._backend.apply_operational_transition(conversa, decision)
            return decision
        if (
            cadastro_state in {
                "v24_cadastro_edicao_texto",
                "v24_cadastro_edicao_opcao",
                "v24_cadastro_edicao_data",
                "v24_cadastro_edicao_referencia_opcao",
            }
            and cadastro_modality is CadastroModality.CONTENTOR_PROVEN
            and cadastro_context.get("editing_field") != "hora_entrega"
        ):
            if cadastro_state == "v24_cadastro_edicao_referencia_opcao":
                decision = self._contentor_cadastro.decide_edicao_referencia_opcao(
                    cadastro_context,
                    message,
                )
            else:
                timezone = self._backend._lisbon_timezone()
                decision = self._contentor_cadastro.decide_edicao(
                    cadastro_context,
                    message,
                    coordinates=self._backend._coordinates(
                        message,
                        (message.texto or "").strip(),
                    ),
                    now=datetime.now(timezone),
                )
            if decision is None:
                return self._backend.handle(conversa, message)
            if isinstance(decision, AdvanceTransition) and not decision.response:
                response = (
                    self._backend._edit_prompt("forma_pagamento", decision.context)
                    if decision.next_state == "v24_cadastro_edicao_opcao"
                    else self._backend.cadastro_confirmacao_prompt(decision.context)
                )
                decision = AdvanceTransition(
                    decision.next_state,
                    decision.context,
                    response,
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
                and get_settings().feature_contentores_enabled
            ):
                selection = self._backend.resolve_entrega_contentor_selection(
                    message,
                    context,
                )
                decision = self._contentor.select_entrega_pedido(
                    conversa,
                    selection,
                )
                if isinstance(decision, (AdvanceTransition, IdleTransition)):
                    return self._backend.apply_operational_transition(
                        conversa,
                        decision,
                    )
                return decision
        if getattr(conversa, "estado_atual", None) == "v24_entrega_adesivo" and self._contentor is not None:
            context = conversa.contexto_json or {}
            pedido_id = context.get("pedido_id")
            if (
                pedido_id is not None
                and self.resolve_modality(context, pedido_id)
                is TipoEquipamentoPedido.CONTENTOR
            ):
                snapshot = self._backend.resolve_entrega_contentor_adesivo(
                    message,
                    context,
                )
                decision = self._contentor.decide_entrega_adesivo(
                    conversa,
                    message,
                    snapshot,
                )
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

    def _handle_carrinha_cadastro(self, conversa, message):
        state = getattr(conversa, "estado_atual", None)
        ctx = getattr(conversa, "contexto_json", None) or {}
        if (
            state == "v24_cadastro_tipo_solicitacao"
            and self._carrinha_cadastro.is_carrinha_selection(message.texto)
            and get_settings().feature_carrinhas_enabled
        ):
            decision = self._carrinha_cadastro.decide_tipo_solicitacao(ctx)
            return self._backend.apply_operational_transition(conversa, decision)

        modality = classify_carrinha_cadastro(ctx)
        if modality not in {
            CarrinhaCadastroModality.CARRINHA_INTENT,
            CarrinhaCadastroModality.CARRINHA_PROVEN,
        }:
            return None
        intent_states = {
            "v24_cadastro_quantidade": "decide_quantidade",
            "v24_cadastro_nome": "decide_nome",
            "v24_cadastro_telefone": "decide_telefone",
            "v24_cadastro_horario_carrinha": "decide_horario",
            "v24_cadastro_residuo": "decide_residuo",
        }
        intent_ready = {
            "v24_cadastro_quantidade": True,
            "v24_cadastro_nome": isinstance(ctx.get("quantidade"), int),
            "v24_cadastro_telefone": isinstance(ctx.get("quantidade"), int) and bool(ctx.get("nome")),
            "v24_cadastro_horario_carrinha": bool(ctx.get("data")),
            "v24_cadastro_residuo": bool(ctx.get("horario_agendado")) and isinstance(ctx.get("quantidade"), int),
        }
        proven_states = {
            "v24_cadastro_mao_obra": "decide_mao_obra",
            "v24_cadastro_valor": "decide_valor",
            "v24_cadastro_pago": "decide_pago",
            "v24_cadastro_forma": "decide_forma",
            "v24_cadastro_forma_outro": "decide_forma_outro",
            "v24_cadastro_referencia_opcao": "decide_referencia_opcao",
            "v24_cadastro_referencia": "decide_referencia",
        }
        decision = None
        if (
            state in intent_states
            and modality is CarrinhaCadastroModality.CARRINHA_INTENT
            and intent_ready[state]
        ):
            decision = getattr(self._carrinha_cadastro, intent_states[state])(ctx, message)
        elif (
            state in {"v24_cadastro_data", "v24_cadastro_data_manual"}
            and isinstance(ctx.get("quantidade"), int)
            and bool(ctx.get("nome"))
            and bool(ctx.get("telefone"))
        ):
            timezone = self._backend._lisbon_timezone()
            decision = (
                self._carrinha_cadastro.decide_data(ctx, message, datetime.now(timezone))
                if state == "v24_cadastro_data"
                else self._carrinha_cadastro.decide_data_manual(ctx, message, timezone)
            )
        elif state in proven_states and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            decision = getattr(self._carrinha_cadastro, proven_states[state])(ctx, message)
        elif state == "v24_cadastro_endereco" and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            decision = self._carrinha_cadastro.decide_endereco(
                ctx, message, self._backend._coordinates(message, message.texto or "")
            )
        elif state == "v24_cadastro_confirmacao" and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            decision = self._carrinha_cadastro.decide_confirmacao(ctx, message)
            if isinstance(decision, ConfirmarCadastroCarrinha):
                response = self._backend.confirmar_cadastro_carrinha(conversa, decision.context)
                return response if response is not None else self._backend.handle(conversa, message)
            if isinstance(decision, AdvanceTransition) and decision.next_state == "v24_cadastro_corrigir":
                decision = AdvanceTransition(decision.next_state, decision.context, self._backend._corrigir_prompt(decision.context))
        elif state == "v24_cadastro_corrigir" and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            choice = self._backend._norm(message.texto or "")
            field, next_state, prompt = self._backend.cadastro_corrigir_decision(choice, ctx)
            decision = self._carrinha_cadastro.decide_corrigir(ctx, field, next_state, prompt)
        elif state in {"v24_cadastro_edicao_texto", "v24_cadastro_edicao_opcao", "v24_cadastro_edicao_data", "v24_cadastro_horario_carrinha"} and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            timezone = self._backend._lisbon_timezone()
            decision = self._carrinha_cadastro.decide_edicao(
                ctx,
                message,
                coordinates=self._backend._coordinates(message, message.texto or ""),
                now=datetime.now(timezone),
            )
        elif state == "v24_cadastro_edicao_referencia_opcao" and modality is CarrinhaCadastroModality.CARRINHA_PROVEN:
            decision = self._carrinha_cadastro.decide_edicao_referencia_opcao(ctx, message)
        if decision is None:
            return None
        if isinstance(decision, AdvanceTransition) and decision.next_state == "v24_cadastro_confirmacao":
            prefix = ""
            if ctx.get("editing_field") == "forma_pagamento" and not ctx.get("pago"):
                prefix = "A forma de pagamento só pode ser corrigida quando o pedido estiver pago.\n\n"
            decision = AdvanceTransition(
                decision.next_state,
                decision.context,
                prefix + self._backend.cadastro_confirmacao_prompt(decision.context),
            )
        if isinstance(decision, (AdvanceTransition, IdleTransition)):
            return self._backend.apply_operational_transition(conversa, decision)
        return decision

    def _handle_carrinha_chegada(self, conversa, message):
        state = getattr(conversa, "estado_atual", None)
        ctx = getattr(conversa, "contexto_json", None) or {}
        states = {
            "v24_entrega_pedido", "v24_entrega_adesivo", "v24_entrega_foto",
            "v24_entrega_foto_acao", "v24_entrega_gps",
            "v24_entrega_referencia_opcao", "v24_entrega_referencia",
            "v24_entrega_confirmacao",
        }
        if state not in states:
            return None
        if not getattr(get_settings(), "feature_carrinhas_enabled", True):
            return None
        pedido_id = ctx.get("pedido_id")
        selected_id = None
        if state == "v24_entrega_pedido":
            selected_id = self._backend._selected_entrega_pedido_id(
                (message.texto or "").strip(), ctx.get("ids", [])
            )
            pedido_id = selected_id
        ready = {
            "v24_entrega_pedido": bool(ctx.get("ids")) and bool(ctx.get("operational_options")),
            "v24_entrega_adesivo": bool(ctx.get("contentores")) and isinstance(ctx.get("indice"), int) and isinstance(ctx.get("entregas"), list),
            "v24_entrega_foto": bool(ctx.get("contentores")) and isinstance(ctx.get("entregas"), list) and bool(ctx.get("entregas")),
            "v24_entrega_foto_acao": bool(ctx.get("contentores")) and isinstance(ctx.get("indice"), int) and bool(ctx.get("entregas")),
            "v24_entrega_gps": bool(ctx.get("contentores")) and bool(ctx.get("entregas")),
            "v24_entrega_referencia_opcao": "latitude" in ctx and "longitude" in ctx and bool(ctx.get("entregas")),
            "v24_entrega_referencia": "latitude" in ctx and "longitude" in ctx and bool(ctx.get("entregas")),
            "v24_entrega_confirmacao": {"pedido_id", "latitude", "longitude", "referencia_entrega", "entregas"}.issubset(ctx),
        }
        if not ready.get(state, False):
            return None
        if (
            pedido_id is None
            or self.resolve_modality(ctx, pedido_id) is not TipoEquipamentoPedido.CARRINHA
        ):
            return None
        decision = None
        if state == "v24_entrega_pedido":
            snapshot = self._backend.resolve_chegada_carrinha_selection(message, ctx)
            decision = self._carrinha.select_entrega_pedido(conversa, snapshot)
        elif state == "v24_entrega_adesivo":
            snapshot = self._backend.resolve_chegada_carrinha_frota(message, ctx)
            decision = self._carrinha.decide_frota(conversa, message, snapshot)
        elif state == "v24_entrega_foto":
            decision = self._carrinha.decide_foto(conversa, message)
        elif state == "v24_entrega_foto_acao":
            decision = self._carrinha.decide_foto_acao(conversa, message)
        elif state == "v24_entrega_gps":
            decision = self._carrinha.decide_gps(conversa, message)
        elif state == "v24_entrega_referencia_opcao":
            decision = self._carrinha.decide_referencia_opcao(conversa, message, "")
        elif state == "v24_entrega_referencia":
            decision = self._carrinha.decide_referencia(conversa, message, "")
        elif state == "v24_entrega_confirmacao":
            decision = self._carrinha.decide_confirmacao(conversa, message)
            if isinstance(decision, ConfirmarChegadaCarrinha):
                return self._backend.confirmar_chegada_carrinha(
                    conversa, decision.context
                )
        if decision is None:
            return None
        if (
            isinstance(decision, AdvanceTransition)
            and decision.next_state == "v24_entrega_confirmacao"
        ):
            decision = AdvanceTransition(
                decision.next_state,
                decision.context,
                self._backend.entrega_confirmacao_prompt(decision.context),
            )
        if isinstance(decision, (AdvanceTransition, IdleTransition)):
            return self._backend.apply_operational_transition(conversa, decision)
        return decision

    def _handle_carrinha_partida(self, conversa, message):
        state = getattr(conversa, "estado_atual", None)
        ctx = getattr(conversa, "contexto_json", None) or {}
        states = {
            "v24_recolha_pedido", "v24_recolha_ativo", "v24_recolha_foto",
            "v24_recolha_foto_acao", "v24_recolha_avaria",
            "v24_recolha_relato", "v24_recolha_confirmacao",
        }
        if state not in states or not getattr(get_settings(), "feature_carrinhas_enabled", False):
            return None
        settings = get_settings()
        ready = {
            "v24_recolha_pedido": bool(ctx.get("ids")),
            "v24_recolha_ativo": isinstance(ctx.get("pedido_id"), int) and bool(ctx.get("contentores")),
            "v24_recolha_foto": isinstance(ctx.get("pedido_id"), int) and isinstance(ctx.get("contentor_id"), int) and isinstance(ctx.get("fotos_recolha"), list),
            "v24_recolha_foto_acao": isinstance(ctx.get("contentor_id"), int) and bool(ctx.get("fotos_recolha")),
            "v24_recolha_avaria": isinstance(ctx.get("contentor_id"), int) and bool(ctx.get("fotos_recolha")),
            "v24_recolha_relato": isinstance(ctx.get("contentor_id"), int) and ctx.get("avariado") is True,
            "v24_recolha_confirmacao": isinstance(ctx.get("pedido_id"), int) and isinstance(ctx.get("contentor_id"), int) and bool(ctx.get("fotos_recolha")),
        }
        if not ready[state]:
            return None
        decision = None
        if state == "v24_recolha_pedido":
            selection = self._backend.resolve_partida_carrinha_selection(message, ctx)
            if selection is None:
                return None
            decision = self._carrinha.select_partida_pedido(conversa, selection)
        elif state == "v24_recolha_ativo":
            selection = self._backend.resolve_recolha_ativo_selection(message, ctx)
            if selection is None or selection["modality"] is not TipoEquipamentoPedido.CARRINHA:
                return None
            decision = self._carrinha.select_partida_ativo(conversa, selection)
        else:
            if self._backend.resolve_recolha_context_modality(ctx) is not TipoEquipamentoPedido.CARRINHA:
                return None
            avarias_enabled = getattr(settings, "feature_avarias_enabled", False)
            if state == "v24_recolha_foto":
                decision = self._carrinha.decide_partida_foto(conversa, message)
            elif state == "v24_recolha_foto_acao":
                decision = self._carrinha.decide_partida_foto_acao(conversa, message, avarias_enabled=avarias_enabled)
            elif state == "v24_recolha_avaria":
                decision = self._carrinha.decide_partida_avaria(conversa, message, avarias_enabled=avarias_enabled)
            elif state == "v24_recolha_relato":
                decision = self._carrinha.decide_partida_relato(conversa, message, avarias_enabled=avarias_enabled)
            elif state == "v24_recolha_confirmacao":
                decision = self._carrinha.decide_partida_confirmacao(conversa, message, avarias_enabled=avarias_enabled)
                if isinstance(decision, ConfirmarPartidaCarrinha):
                    return self._backend.confirmar_partida_carrinha(conversa, decision.context)
                if isinstance(decision, CancelarPartidaCarrinha):
                    return self._backend.cancelar_partida_carrinha(conversa, decision.context)
        if isinstance(decision, PrepararConfirmacaoPartidaCarrinha):
            decision = AdvanceTransition(
                "v24_recolha_confirmacao", decision.context,
                decision.response_prefix + self._backend.recolha_confirmacao_prompt(decision.context),
            )
        if isinstance(decision, (AdvanceTransition, IdleTransition)):
            return self._backend.apply_operational_transition(conversa, decision)
        return decision
