import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.agents.pedido_v24.transitions import AdvanceTransition
from app.agents.pedido_v24.contentor_cadastro import (
    CadastroModality,
    classify_cadastro_modality,
)
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.core.config import get_settings
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusOperacionalCarrinha,
    StatusPagamento,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


class PedidoV24Agent:
    PREFIX = "v24_"
    _CONFIRMATION_STATE = "v24_cadastro_confirmacao"
    _CONFIRMING_STATE = "v24_confirmando"

    def __init__(self, db: Session):
        self.db = db
        self.service = PedidoService(db)

    def start_cadastro(self, conversa: ConversaWhatsApp) -> str:
        contentores_enabled, carrinhas_enabled = self._modalidades_habilitadas()
        if not contentores_enabled and not carrinhas_enabled:
            return self._idle(conversa, "Não há modalidade habilitada para criar um novo pedido.")
        if contentores_enabled and carrinhas_enabled:
            return self._advance(
                conversa,
                "v24_cadastro_tipo_solicitacao",
                {},
                self._tipo_solicitacao_prompt(),
            )
        tipo = (
            TipoEquipamentoPedido.CONTENTOR.value
            if contentores_enabled
            else TipoEquipamentoPedido.CARRINHA.value
        )
        return self._selecionar_tipo_solicitacao(conversa, {}, tipo)

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        settings = get_settings()
        if not settings.feature_contentores_enabled and not settings.feature_carrinhas_enabled:
            return self._idle(
                conversa,
                "Não há modalidade operacional habilitada para chegada ou entrega.",
            )
        pedidos_contentor = (
            self.service.pedidos_pendentes_entrega()
            if settings.feature_contentores_enabled
            else []
        )
        return self._start_entrega_com_pedidos_contentor(conversa, pedidos_contentor)

    def _start_entrega_com_pedidos_contentor(
        self,
        conversa: ConversaWhatsApp,
        pedidos_contentor,
    ) -> str:
        settings = get_settings()
        pedidos_carrinha = (
            self.service.pedidos_carrinha_aguardando_chegada()
            if settings.feature_carrinhas_enabled
            else []
        )
        pedidos = self._merge_pedidos(
            pedidos_contentor,
            pedidos_carrinha,
        )
        if not pedidos:
            return self._idle(conversa, "Não existem pedidos pendentes de entrega.")
        return self._advance(
            conversa,
            "v24_entrega_pedido",
            {
                "ids": [p.id for p in pedidos],
                "operational_options": [
                    {
                        "pedido_id": pedido.id,
                        "tipos_equipamento": sorted(
                            self._pedido_entrega_tipos(pedido)
                        ),
                    }
                    for pedido in pedidos
                ],
            },
            "Selecione o cliente para confirmar a chegada / entrega.\n\n"
            + "\n".join(self._pedido_entrega_option(p, i) for i, p in enumerate(pedidos, 1)),
        )

    def start_recolha(self, conversa: ConversaWhatsApp) -> str:
        settings = get_settings()
        if not settings.feature_contentores_enabled and not settings.feature_carrinhas_enabled:
            return self._idle(
                conversa,
                "Não há modalidade operacional habilitada para recolha ou partida.",
            )
        pedidos_contentor = (
            self.service.pedidos_para_recolha()
            if settings.feature_contentores_enabled
            else []
        )
        pedidos_carrinha = (
            self.service.pedidos_carrinha_aguardando_partida()
            if settings.feature_carrinhas_enabled
            else []
        )
        pedidos = self._merge_pedidos(
            pedidos_contentor,
            pedidos_carrinha,
        )
        if not pedidos:
            return self._idle(conversa, "Não existem equipamentos aguardando recolha / partida.")
        return self._advance(
            conversa,
            "v24_recolha_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido para confirmar recolha / partida:\n\n"
            + "\n".join(f"{i}. {self._pedido_recolha_label(p)}" for i, p in enumerate(pedidos, 1)),
        )

    def start_despejo(self, conversa: ConversaWhatsApp) -> str:
        pedidos_contentor = (
            self.service.pedidos_para_despejo()
            if get_settings().feature_contentores_enabled
            else []
        )
        pedidos_carrinha = (
            self.service.pedidos_carrinha_aguardando_despejo()
            if get_settings().feature_carrinhas_enabled
            else []
        )
        pedidos = self._merge_pedidos(
            pedidos_contentor,
            pedidos_carrinha,
        )
        if not pedidos:
            return self._idle(conversa, "Não existem contentores recolhidos aguardando despejo.")
        return self._advance(
            conversa,
            "v24_despejo_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido para despejo no vazadouro:\n\n"
            + "\n".join(f"{i}. {self._pedido_despejo_label(p)}" for i, p in enumerate(pedidos, 1)),
        )
    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        ctx = dict(conversa.contexto_json or {})
        raw = (message.texto or "").strip()
        choice = self._norm(raw)

        if (
            state.startswith("v24_entrega_")
            and state != "v24_entrega_pedido"
            and not get_settings().feature_contentores_enabled
            and self._contexto_entrega_tem_tipo(ctx, TipoEquipamentoPedido.CONTENTOR.value)
        ):
            return self._idle(conversa, self._contentor_entrega_desabilitada_message())
        if (
            state.startswith("v24_entrega_")
            and state != "v24_entrega_pedido"
            and not get_settings().feature_carrinhas_enabled
            and self._contexto_entrega_tem_tipo(ctx, TipoEquipamentoPedido.CARRINHA.value)
        ):
            return self._idle(conversa, self._carrinha_chegada_desabilitada_message())
        if (
            state.startswith("v24_recolha_")
            and state not in {"v24_recolha_pedido", "v24_recolha_ativo"}
            and not get_settings().feature_contentores_enabled
            and self._contexto_recolha_tem_tipo(ctx, TipoEquipamentoPedido.CONTENTOR.value)
        ):
            return self._idle(conversa, self._contentor_recolha_desabilitada_message())
        if (
            state.startswith("v24_recolha_")
            and state not in {"v24_recolha_pedido", "v24_recolha_ativo"}
            and not get_settings().feature_carrinhas_enabled
            and self._contexto_recolha_tem_tipo(ctx, TipoEquipamentoPedido.CARRINHA.value)
        ):
            return self._idle(conversa, self._carrinha_partida_desabilitada_message())
        if (
            state.startswith("v24_despejo_")
            and state not in {"v24_despejo_pedido", "v24_despejo_ativo", "v24_despejo_contentor"}
            and not get_settings().feature_contentores_enabled
            and self._contexto_despejo_tem_tipo(ctx, TipoEquipamentoPedido.CONTENTOR.value)
        ):
            return self._idle(conversa, self._contentor_despejo_desabilitado_message())
        if (
            state.startswith("v24_despejo_")
            and state not in {"v24_despejo_pedido", "v24_despejo_ativo", "v24_despejo_contentor"}
            and not get_settings().feature_carrinhas_enabled
            and self._contexto_despejo_tem_tipo(ctx, TipoEquipamentoPedido.CARRINHA.value)
        ):
            return self._idle(conversa, self._carrinha_despejo_desabilitada_message())

        if state == "v24_cadastro_nome" and message.contact_name:
            name = message.contact_name.strip()
            if len(name) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = name
            phone = self._phone_from_message(message, "")
            if phone:
                ctx["telefone"] = phone
                return self._after_cliente(conversa, ctx)
            return self._advance(conversa, "v24_cadastro_telefone", ctx, "Qual é o telefone do cliente?")
        if state == "v24_cadastro_telefone" and message.contact_phone:
            phone = self._phone_from_message(message, raw)
            if not phone:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
            return self._after_cliente(conversa, ctx)

        if state == "v24_cadastro_nome":
            if len(raw) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = raw
            return self._advance(conversa, "v24_cadastro_telefone", ctx, "Qual é o telefone do cliente?")
        if state == "v24_cadastro_telefone":
            phone = re.sub(r"\D", "", raw)
            if len(phone) < 9:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
            return self._after_cliente(conversa, ctx)
        if state == "v24_cadastro_tipo_solicitacao":
            tipo = self._parse_tipo_solicitacao(choice)
            if not tipo:
                return self._tipo_solicitacao_prompt()
            contentores_enabled, carrinhas_enabled = self._modalidades_habilitadas()
            if not contentores_enabled and not carrinhas_enabled:
                return self._idle(conversa, "Não há modalidade habilitada para criar um novo pedido.")
            if tipo == TipoEquipamentoPedido.CONTENTOR.value and not contentores_enabled:
                return "A modalidade Contentor não está habilitada. Selecione Carrinha."
            if tipo == TipoEquipamentoPedido.CARRINHA.value and not carrinhas_enabled:
                return "A modalidade Carrinha não está habilitada. Selecione Contentor."
            return self._selecionar_tipo_solicitacao(conversa, ctx, tipo)
        if state == "v24_cadastro_data":
            now = datetime.now(self._lisbon_timezone())
            if choice in {"1", "hoje"}:
                planned = now
            elif choice in {"2", "amanha"}:
                planned = now + timedelta(days=1)
            elif choice in {"3", "outra data"}:
                return self._advance(
                    conversa, "v24_cadastro_data_manual", ctx, "Informe a data no formato DD/MM/AAAA."
                )
            else:
                return "Selecione Hoje, Amanhã ou Outra data."
            ctx["data"] = planned.isoformat()
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
                return self._advance(conversa, "v24_cadastro_horario_carrinha", ctx, self._horario_carrinha_prompt())
            return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor global do pedido?")
        if state == "v24_cadastro_data_manual":
            try:
                planned = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=self._lisbon_timezone())
            except ValueError:
                return "Data inválida. Use o formato DD/MM/AAAA."
            ctx["data"] = planned.isoformat()
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
                return self._advance(conversa, "v24_cadastro_horario_carrinha", ctx, self._horario_carrinha_prompt())
            return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor global do pedido?")
        if state == "v24_cadastro_quantidade":
            if not raw.isdigit() or not 1 <= int(raw) <= 50:
                return "Informe uma quantidade entre 1 e 50."
            ctx["quantidade"] = int(raw)
            ctx["itens"] = []
            ctx["residuos"] = []
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
                return self._advance(conversa, "v24_cadastro_nome", ctx, "Qual é o nome do cliente?")
            return self._advance(conversa, "v24_cadastro_tipo_equipamento", ctx, self._tipo_equipamento_prompt(ctx))
        if state == "v24_cadastro_tipo_equipamento":
            legacy_residue = None if choice in {"1", "2"} else self._parse_residue(choice)
            if legacy_residue:
                ctx["item_atual"] = {
                    "tipo_equipamento": TipoEquipamentoPedido.CONTENTOR.value,
                    "horario_agendado": None,
                    "precisa_mao_de_obra": False,
                }
                return self._registrar_item_cadastro(conversa, ctx, legacy_residue)
            equipamento = {
                "1": TipoEquipamentoPedido.CONTENTOR.value,
                "contentor": TipoEquipamentoPedido.CONTENTOR.value,
                "2": TipoEquipamentoPedido.CARRINHA.value,
                "carrinha": TipoEquipamentoPedido.CARRINHA.value,
            }.get(choice)
            if not equipamento:
                return "Selecione Contentor ou Carrinha."
            ctx["item_atual"] = {"tipo_equipamento": equipamento, "horario_agendado": None}
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_horario_carrinha":
            if not self._horario_valido(raw):
                return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
            item_atual = dict(ctx.get("item_atual") or {})
            item_atual["horario_agendado"] = raw
            ctx["item_atual"] = item_atual
            ctx["horario_agendado"] = raw
            if ctx.get("editing_field") == "hora_entrega":
                return self._apply_atomic_edit(conversa, ctx, "hora_entrega", raw)
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value and not ctx.get("residuos"):
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            if "precisa_mao_de_obra" in ctx:
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_mao_obra":
            mao_obra = self._parse_mao_obra(choice)
            if mao_obra is not None:
                if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value and ctx.get("itens"):
                    ctx["precisa_mao_de_obra"] = mao_obra
                    ctx.pop("item_atual", None)
                    return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor comercial total?")
                item_atual = dict(ctx.get("item_atual") or {})
                tipo_item = item_atual.get("tipo_equipamento") or ctx.get("tipo_solicitacao")
                if tipo_item:
                    item_atual["tipo_equipamento"] = tipo_item
                item_atual["precisa_mao_de_obra"] = mao_obra
                ctx["item_atual"] = item_atual
                ctx["precisa_mao_de_obra"] = mao_obra
                if tipo_item == TipoEquipamentoPedido.CARRINHA.value and not item_atual.get("horario_agendado"):
                    return self._advance(conversa, "v24_cadastro_horario_carrinha", ctx, self._horario_carrinha_prompt())
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return self._mao_obra_prompt()
        if state == "v24_cadastro_residuo":
            residue = self._parse_residue(choice)
            if not residue:
                return self._residuo_prompt(ctx)
            return self._registrar_item_cadastro(conversa, ctx, residue)
        if state == "v24_cadastro_valor":
            try:
                ctx["valor"] = str(float(raw.replace(",", ".")))
            except ValueError:
                return "Valor inválido."
            return self._advance(
                conversa,
                "v24_cadastro_pago",
                ctx,
                "O pedido já está pago?\n\n1. Sim, já está pago\n2. Não, pendente",
            )
        if state == "v24_cadastro_pago":
            if choice in {"1", "sim", "sim, ja esta pago"}:
                ctx["pago"] = True
                return self._advance(
                    conversa,
                    "v24_cadastro_forma",
                    ctx,
                    "Selecione a forma de pagamento:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
                )
            if choice in {"2", "nao", "nao, pendente"}:
                ctx["pago"] = False
                ctx["forma"] = None
                return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
            return "Selecione uma das opções de pagamento."
        if state == "v24_cadastro_forma":
            forma = {"1": "MBWay", "mbway": "MBWay", "2": "Transferência",
                     "transferencia": "Transferência", "3": "Dinheiro",
                     "dinheiro": "Dinheiro", "4": "Outro", "outro": "Outro"}.get(choice)
            if not forma:
                return "Selecione uma forma de pagamento."
            if forma == "Outro":
                return self._advance(conversa, "v24_cadastro_forma_outro", ctx, "Qual foi a forma de pagamento?")
            ctx["forma"] = forma
            return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
        if state == "v24_cadastro_forma_outro":
            if not raw:
                return "Informe a forma de pagamento."
            ctx["forma"] = raw[:80]
            return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
        if state == "v24_cadastro_endereco":
            if message.tipo == "location" and not self._coordinates(message, raw):
                return "Não foi possível ler a localização. Reenvie a localização nativa ou digite o endereço."
            if not raw or len(raw) > 300:
                return "O endereço precisa ter entre 1 e 300 caracteres."
            ctx["endereco"] = raw
            coords = self._coordinates(message, raw)
            if coords:
                ctx["endereco_latitude"], ctx["endereco_longitude"] = coords
            return self._advance(
                conversa, "v24_cadastro_referencia_opcao", ctx,
                "Deseja informar um ponto de referência?\n\n1. Sim\n2. Não",
            )
        if state == "v24_cadastro_referencia_opcao":
            if choice in {"1", "sim"}:
                return self._advance(conversa, "v24_cadastro_referencia", ctx, "Qual é o ponto de referência?")
            if choice in {"2", "nao"}:
                ctx["referencia"] = None
                return self._finish_cadastro(conversa, ctx)
            return "Selecione Sim ou Não."
        if state == "v24_cadastro_referencia":
            if not 1 <= len(raw) <= 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia"] = raw
            return self._finish_cadastro(conversa, ctx)

        if state == "v24_cadastro_confirmacao":
            if choice in {"1", "sim", "confirmar", "confirmar e salvar"}:
                ctx["_confirmado"] = True
                return self._finish_cadastro(conversa, ctx)
            if choice in {"2", "corrigir"}:
                return self._advance(conversa, "v24_cadastro_corrigir", ctx, self._corrigir_prompt(ctx))
            if choice in {"3", "cancelar"}:
                return self._idle(conversa, "Pedido cancelado. Nenhum pedido foi criado.")
            return "Escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar."
        if state == "v24_cadastro_corrigir":
            field = self._parse_corrigir_field(choice, ctx)
            if not field:
                return self._corrigir_prompt(ctx)
            if field == "forma_pagamento" and not ctx.get("pago"):
                return self._advance(
                    conversa,
                    "v24_cadastro_confirmacao",
                    ctx,
                    "A forma de pagamento só pode ser corrigida quando o pedido estiver pago.\n\n"
                    + self._format_confirmacao_cadastro(ctx),
                )
            ctx["editing_field"] = field
            return self._advance(conversa, self._edit_state_for(field), ctx, self._edit_prompt(field, ctx))
        if state == "v24_cadastro_edicao_texto":
            field = ctx.get("editing_field")
            return self._apply_atomic_edit(conversa, ctx, field, raw, message)
        if state == "v24_cadastro_edicao_opcao":
            field = ctx.get("editing_field")
            return self._apply_atomic_edit(conversa, ctx, field, choice, message)
        if state == "v24_cadastro_edicao_data":
            field = ctx.get("editing_field")
            if choice in {"1", "hoje"}:
                value = datetime.now(self._lisbon_timezone()).isoformat()
            elif choice in {"2", "amanha"}:
                value = (datetime.now(self._lisbon_timezone()) + timedelta(days=1)).isoformat()
            else:
                try:
                    value = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=self._lisbon_timezone()).isoformat()
                except ValueError:
                    return "Data inválida. Use Hoje, Amanhã ou DD/MM/AAAA."
            return self._apply_atomic_edit(conversa, ctx, field, value)
        if state == "v24_cadastro_edicao_referencia_opcao":
            if choice in {"1", "sim"}:
                ctx["editing_field"] = "ponto_referencia_texto"
                return self._advance(conversa, "v24_cadastro_edicao_texto", ctx, "Qual é o novo ponto de referência?")
            if choice in {"2", "nao"}:
                return self._apply_atomic_edit(conversa, ctx, "ponto_referencia", None)
            return "Selecione Sim ou Não."

        if state == "v24_entrega_pedido":
            pedido_id = self._selected_entrega_pedido_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = [
                contentor.id
                for contentor in pedido.contentores
                if (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                    and contentor.status_entrega == "PENDENTE"
                )
                or (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                    and contentor.status_operacional_carrinha
                    == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
                )
            ]
            tipos = {
                self.db.get(PedidoContentor, item_id).tipo_equipamento
                for item_id in pendentes
            }
            if (
                TipoEquipamentoPedido.CONTENTOR.value in tipos
                and not get_settings().feature_contentores_enabled
            ):
                return self._idle(conversa, self._contentor_entrega_desabilitada_message())
            if (
                TipoEquipamentoPedido.CARRINHA.value in tipos
                and not get_settings().feature_carrinhas_enabled
            ):
                return self._idle(conversa, self._carrinha_chegada_desabilitada_message())
            if len(tipos) > 1:
                return self._idle(
                    conversa,
                    "Pedido legado mistura contentores e carrinhas. Separe os itens antes da operação.",
                )
            if not pendentes:
                return self._idle(conversa, "Esse pedido já não possui ativos pendentes de entrega.")
            ctx.update({"pedido_id": pedido.id, "contentores": pendentes, "indice": 0, "entregas": []})
            return self._advance(conversa, "v24_entrega_adesivo", ctx, self._entrega_numero_prompt(ctx))
        if state == "v24_entrega_adesivo":
            if message.tipo == "interactive":
                return "Digite o número físico do equipamento para continuar."
            number = raw.strip()
            contentor = self.db.get(PedidoContentor, ctx["contentores"][ctx["indice"]])
            if not contentor or not (
                (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                    and contentor.status_entrega == "PENDENTE"
                )
                or (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                    and contentor.status_operacional_carrinha
                    == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
                )
            ):
                return self._idle(conversa, "Esse ativo já não está pendente. Reinicie a entrega.")
            is_carrinha = contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            if is_carrinha and not re.fullmatch(r"\d{1,6}", number):
                return "Informe o número da frota da carrinha ou 0 se não houver."
            if not is_carrinha and (not re.fullmatch(r"\d{1,6}", number) or number == "0"):
                return "Informe somente o número visível no contentor."
            if number != "0" and number in [str(item.get("numero_adesivo")) for item in ctx.get("entregas") or []]:
                return "Esse adesivo ja foi informado neste lote."
            if not is_carrinha:
                duplicate = self.db.query(PedidoContentor).filter(
                    PedidoContentor.numero_adesivo_contentor == number,
                    PedidoContentor.status_ciclo == "EM_ANDAMENTO",
                ).first()
                if duplicate:
                    return "Esse adesivo já está em um ciclo ativo."
            entregas = list(ctx.get("entregas") or [])
            entregas.append(
                {
                    "contentor_id": ctx["contentores"][ctx["indice"]],
                    "numero_adesivo": number,
                    "fotos": [],
                }
            )
            ctx["entregas"] = entregas
            return self._advance(
                conversa,
                "v24_entrega_foto",
                ctx,
                f"Envie a foto do Contentor {number} posicionado no local.",
            )
        if state == "v24_entrega_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            entregas = list(ctx.get("entregas") or [])
            entrega_atual = dict(entregas[-1])
            fotos = list(entrega_atual.get("fotos") or [])
            if photo not in fotos:
                fotos.append(photo)
            entrega_atual["fotos"] = fotos
            entregas[-1] = entrega_atual
            ctx["entregas"] = entregas
            return self._advance(
                conversa, "v24_entrega_foto_acao", ctx,
                "Foto guardada. O que deseja fazer?\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_entrega_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_entrega_foto", ctx, "Envie a próxima foto deste contentor.")
            if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
                return "Selecione Outra Foto ou Próximo Passo."
            registrado = ctx["indice"] + 1
            total = len(ctx["contentores"])
            ctx["indice"] += 1
            if ctx["indice"] < total:
                progresso = f"Contentor {registrado} de {total} registrado."
                return self._advance(
                    conversa,
                    "v24_entrega_adesivo",
                    ctx,
                    f"{progresso}\n\nVamos registrar o próximo.\n\n{self._entrega_numero_prompt(ctx)}",
                )
            return self._advance(
                conversa,
                "v24_entrega_gps",
                ctx,
                f"Contentor {registrado} de {total} registrado.\n\nCompartilhe a localização GPS da obra.",
            )
        if state == "v24_entrega_gps":
            coords = self._coordinates(message, raw) if message.tipo == "location" else None
            if not coords:
                return "Compartilhe a localização nativa do WhatsApp para confirmar a entrega."
            ctx["latitude"], ctx["longitude"] = coords
            return self._advance(
                conversa,
                "v24_entrega_referencia_opcao",
                ctx,
                "Deseja informar algum ponto de referência para a entrega?\n\n1. Sim\n2. Não",
            )
        if state == "v24_entrega_referencia_opcao":
            if choice in {"1", "sim", "entrega_referencia:sim"}:
                return self._advance(conversa, "v24_entrega_referencia", ctx, "Digite o ponto de referência.")
            if choice in {"2", "nao", "entrega_referencia:nao"}:
                ctx["referencia_entrega"] = None
                return self._advance(conversa, "v24_entrega_confirmacao", ctx, self._entrega_confirmacao_prompt(ctx))
            return "Selecione Sim ou Não."
        if state == "v24_entrega_referencia":
            if not 1 <= len(raw) <= 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia_entrega"] = raw
            return self._advance(conversa, "v24_entrega_confirmacao", ctx, self._entrega_confirmacao_prompt(ctx))
        if state == "v24_entrega_confirmacao":
            if choice in {"1", "confirmar entrega", "✅ confirmar entrega"}:
                return self._confirmar_entrega_preparada(conversa, ctx)
            if choice in {"2", "cancelar", "❌ cancelar"}:
                return self._idle(conversa, "Entrega cancelada. Nenhum ativo foi marcado como entregue.")
            return "Escolha Confirmar entrega ou Cancelar."
        if state == "v24_entrega_pagou":
            if choice in {"2", "nao", "nao, continua pendente", "🕒 nao, continua pendente"}:
                return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento permanece pendente.")
            if choice in {"1", "sim", "sim, foi pago", "✅ sim, foi pago"}:
                return self._advance(
                    conversa, "v24_entrega_forma", ctx,
                    "Selecione a forma recebida:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
                )
            return "Selecione Sim ou Não."
        if state == "v24_entrega_forma":
            forms = {"1": "MBWay", "2": "Transferência", "3": "Dinheiro", "4": "Outro",
                     "mbway": "MBWay", "transferencia": "Transferência", "dinheiro": "Dinheiro", "outro": "Outro"}
            form = forms.get(choice)
            if not form:
                return "Selecione uma forma de pagamento."
            if form == "Outro":
                return self._advance(conversa, "v24_entrega_forma_outro", ctx, "Qual foi a forma recebida?")
            self.service.registrar_pagamento(ctx["pedido_id"], form)
            return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado.")
        if state == "v24_entrega_forma_outro":
            if not raw:
                return "Informe a forma recebida."
            self.service.registrar_pagamento(ctx["pedido_id"], raw[:80])
            return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado.")

        if state == "v24_recolha_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = self._recolha_pendentes(pedido)
            if not pendentes:
                return self._idle(conversa, "Esse pedido ja nao possui ativos pendentes de recolha.")
            ctx.update({"pedido_id": pedido.id, "recolhas": []})
            return self._advance(conversa, "v24_recolha_ativo", ctx, self._recolha_selecao_prompt(ctx, pendentes))
        if state == "v24_recolha_ativo":
            if self._is_recolha_terminar(message, choice, ctx):
                return self._idle(conversa, "✅ Recolhas deste cliente encerradas. Os ativos restantes continuam pendentes.")
            contentor_id = self._selected_recolha_contentor_id(raw, ctx)
            if not contentor_id:
                return "Selecione um ativo da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and not get_settings().feature_contentores_enabled
            ):
                return self._idle(conversa, self._contentor_recolha_desabilitada_message())
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and not get_settings().feature_carrinhas_enabled
            ):
                return self._idle(conversa, self._carrinha_partida_desabilitada_message())
            if not self._is_recolha_pendente_do_pedido(contentor, ctx["pedido_id"]):
                pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
                if pendentes:
                    return self._advance(
                        conversa,
                        "v24_recolha_ativo",
                        ctx,
                        "Esse ativo foi atualizado por outro operador.\n\n"
                        + self._recolha_selecao_prompt(ctx, pendentes),
                    )
                return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de recolha.")
            ctx.update({"contentor_id": contentor_id, "fotos_recolha": [], "avariado": None, "relato_avaria": None})
            return self._advance(conversa, "v24_recolha_foto", ctx, self._recolha_foto_prompt(ctx))
        if state == "v24_recolha_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and not get_settings().feature_contentores_enabled
            ):
                return self._idle(conversa, self._contentor_recolha_desabilitada_message())
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and not get_settings().feature_carrinhas_enabled
            ):
                return self._idle(conversa, self._carrinha_partida_desabilitada_message())
            ctx.update({"contentor_id": contentor_id, "fotos_recolha": [], "avariado": None, "relato_avaria": None})
            return self._advance(conversa, "v24_recolha_foto", ctx, "Envie a foto do equipamento cheio antes do içamento.")
        if state == "v24_recolha_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            fotos = list(ctx.get("fotos_recolha") or [])
            if photo not in fotos:
                fotos.append(photo)
            ctx["fotos_recolha"] = fotos
            return self._advance(
                conversa, "v24_recolha_foto_acao", ctx,
                "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_recolha_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_recolha_foto", ctx, "Envie a próxima foto.")
            if choice in {"2", "proximo passo", "➡️ proximo passo"}:
                if not get_settings().feature_avarias_enabled:
                    ctx.pop("avariado", None)
                    ctx.pop("relato_avaria", None)
                    return self._advance(conversa, "v24_recolha_confirmacao", ctx, self._recolha_confirmacao_prompt(ctx))
                return self._advance(
                    conversa, "v24_recolha_avaria", ctx,
                    "O equipamento sofreu algum estrago ou avaria na obra?\n\n1. ✅ Não, está perfeito\n2. 💥 Sim, está estragado",
                )
            return "Selecione Outra Foto ou Próximo Passo."
        if state == "v24_recolha_avaria":
            if not get_settings().feature_avarias_enabled:
                return self._recover_disabled_avaria(conversa, ctx)
            if choice in {"1", "nao, esta perfeito", "✅ nao, esta perfeito"}:
                ctx["avariado"] = False
                ctx["relato_avaria"] = None
                return self._advance(conversa, "v24_recolha_confirmacao", ctx, self._recolha_confirmacao_prompt(ctx))
            if choice in {"2", "sim, esta estragado", "💥 sim, esta estragado"}:
                return self._advance(conversa, "v24_recolha_relato", ctx, "Descreva a avaria com pelo menos 10 caracteres.")
            return "Selecione uma das opções de avaria."
        if state == "v24_recolha_relato":
            if not get_settings().feature_avarias_enabled:
                return self._recover_disabled_avaria(conversa, ctx)
            relato = raw.strip()
            if len(relato) < 10:
                return "O relato da avaria precisa ter pelo menos 10 caracteres."
            ctx["avariado"] = True
            ctx["relato_avaria"] = relato
            return self._advance(conversa, "v24_recolha_confirmacao", ctx, self._recolha_confirmacao_prompt(ctx))

        if state == "v24_recolha_confirmacao":
            if (
                not get_settings().feature_avarias_enabled
                and (ctx.get("avariado") or ctx.get("relato_avaria"))
            ):
                return self._recover_disabled_avaria(conversa, ctx)
            if choice in {"1", "confirmar recolha", "confirmar"}:
                return self._confirmar_recolha_atual(conversa, ctx)
            if choice in {"2", "cancelar ativo", "cancelar"}:
                return self._cancelar_recolha_atual(conversa, ctx)
            return "Escolha Confirmar recolha ou Cancelar ativo."

        if state == "v24_despejo_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = self._despejo_pendentes(pedido)
            if not pendentes:
                return self._idle(conversa, "Esse pedido ja nao possui ativos pendentes de despejo.")
            ctx.update({"pedido_id": pedido.id, "despejos": []})
            return self._advance(conversa, "v24_despejo_ativo", ctx, self._despejo_selecao_prompt(ctx, pendentes))
        if state == "v24_despejo_ativo":
            if self._is_despejo_terminar(message, choice, ctx):
                return self._idle(conversa, "✅ Despejos deste cliente encerrados. Os ativos restantes continuam em andamento.")
            contentor_id = self._selected_despejo_contentor_id(raw, ctx)
            if not contentor_id:
                return "Selecione um ativo da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and not get_settings().feature_contentores_enabled
            ):
                return self._idle(conversa, self._contentor_despejo_desabilitado_message())
            if (
                contentor
                and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and not get_settings().feature_carrinhas_enabled
            ):
                return self._idle(conversa, self._carrinha_despejo_desabilitada_message())
            if not self._is_despejo_pendente_do_pedido(contentor, ctx["pedido_id"]):
                pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
                if pendentes:
                    return self._advance(
                        conversa,
                        "v24_despejo_ativo",
                        ctx,
                        "Esse ativo foi atualizado por outro operador.\n\n"
                        + self._despejo_selecao_prompt(ctx, pendentes),
                    )
                return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de despejo.")
            ctx.update(
                {
                    "contentor_id": contentor_id,
                    "fotos_despejo": [],
                    "residuo_contratado": contentor.residuo_contratado,
                    "residuo_efetivo": None,
                    "residuo_assumido": None,
                    "carga_errada": None,
                    "relato_carga": None,
                }
            )
            return self._despejo_decisao_residuo(conversa, ctx)
        if state == "v24_despejo_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if not contentor:
                return "Selecione um contentor da lista."
            if (
                contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and not get_settings().feature_contentores_enabled
            ):
                return self._idle(conversa, self._contentor_despejo_desabilitado_message())
            if (
                contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and not get_settings().feature_carrinhas_enabled
            ):
                return self._idle(conversa, self._carrinha_despejo_desabilitada_message())
            ctx.update(
                {
                    "contentor_id": contentor_id,
                    "pedido_id": contentor.pedido_id,
                    "fotos_despejo": [],
                    "residuo_contratado": contentor.residuo_contratado,
                    "residuo_efetivo": None,
                    "residuo_assumido": None,
                    "carga_errada": None,
                    "relato_carga": None,
                }
            )
            return self._despejo_decisao_residuo(conversa, ctx)
        if state == "v24_despejo_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            fotos = list(ctx.get("fotos_despejo") or [])
            if photo not in fotos:
                fotos.append(photo)
            ctx["fotos_despejo"] = fotos
            return self._advance(
                conversa, "v24_despejo_foto_acao", ctx,
                "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_despejo_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_despejo_foto", ctx, "Envie a próxima foto do despejo.")
            if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
                return "Selecione Outra Foto ou Próximo Passo."
            if not ctx.get("fotos_despejo"):
                return "Envie pelo menos uma imagem para continuar."
            return self._advance(conversa, "v24_despejo_confirmacao", ctx, self._despejo_confirmacao_prompt(ctx))
        if state in {"v24_despejo_residuo", "v24_despejo_conformidade", "v24_despejo_relato"}:
            self._hydrate_legacy_despejo_context(ctx)
        if state == "v24_despejo_residuo":
            available = ctx.get("residuos_disponiveis") or []
            residue = None
            if choice == "despejo_residuo:limpo":
                residue = "Entulho Limpo"
            elif choice == "despejo_residuo:misto":
                residue = "Entulho Misto"
            if choice.isdigit() and 1 <= int(choice) <= len(available):
                residue = available[int(choice) - 1]
            if not residue:
                residue = next((r for r in available if self._norm(r) == choice), None)
            if residue and "pedido_id" in ctx:
                ctx["residuo_efetivo"] = residue
                ctx["carga_errada"] = False
                ctx["relato_carga"] = None
                return self._advance(conversa, "v24_despejo_foto", ctx, self._despejo_foto_prompt(ctx))
            if not residue:
                return "Selecione um tipo de resíduo com cota em aberto."
            self.service.confirmar_despejo(ctx["contentor_id"], residue, operador=conversa.telefone)
            return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
        if state == "v24_despejo_conformidade":
            if "pedido_id" in ctx:
                if choice == "despejo_conformidade:sim":
                    choice = "1"
                elif choice == "despejo_conformidade:nao":
                    choice = "2"
                if choice in {"1", "sim", "sim, corresponde", "âœ… sim, corresponde", "sim, tudo certo", "âœ… sim, tudo certo"}:
                    ctx["residuo_efetivo"] = ctx.get("residuo_assumido") or ctx["residuo_contratado"]
                    ctx["carga_errada"] = False
                    ctx["relato_carga"] = None
                    return self._advance(conversa, "v24_despejo_foto", ctx, self._despejo_foto_prompt(ctx))
                if choice in {"2", "nao", "nao, existe divergencia", "âŒ nao, existe divergencia", "nao, esta misturado/errado", "ðŸš¨ nao, esta misturado/errado"}:
                    ctx["carga_errada"] = True
                    return self._advance(conversa, "v24_despejo_relato", ctx, "Descreva a divergencia com pelo menos 10 caracteres.")
                return "Selecione se o material corresponde ao residuo contratado."
            residue = ctx["residuo_assumido"]
            if choice in {"1", "sim, tudo certo", "✅ sim, tudo certo"}:
                self.service.confirmar_despejo(ctx["contentor_id"], residue, operador=conversa.telefone)
                return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
            if choice in {"2", "nao, esta misturado/errado", "🚨 nao, esta misturado/errado"}:
                return self._advance(conversa, "v24_despejo_relato", ctx, "Justifique a carga errada com pelo menos 10 caracteres.")
            return "Selecione Sim, tudo certo ou Não, está misturado/errado."
        if state == "v24_despejo_relato":
            if "pedido_id" in ctx:
                relato = raw.strip()
                if len(relato) < 10:
                    return "O relato da carga precisa ter pelo menos 10 caracteres."
                ctx["relato_carga"] = relato
                ctx["carga_errada"] = True
                ctx["residuo_efetivo"] = (
                    ctx.get("residuo_efetivo")
                    or ctx.get("residuo_assumido")
                    or ctx.get("residuo_contratado")
                )
                return self._advance(
                    conversa,
                    "v24_despejo_foto",
                    ctx,
                    self._despejo_foto_prompt(ctx),
                )
            if len(raw) < 10:
                return "O relato da carga precisa ter pelo menos 10 caracteres."
            self.service.confirmar_despejo(
                ctx["contentor_id"], ctx["residuo_assumido"], True, raw, operador=conversa.telefone
            )
            return self._idle(conversa, "✅ Ciclo concluído com pendência de carga.")
        if state == "v24_despejo_confirmacao":
            if choice in {"1", "confirmar despejo", "confirmar", "âœ… confirmar despejo"}:
                return self._confirmar_despejo_atual(conversa, ctx)
            if choice in {"2", "voltar", "â†©ï¸ voltar"}:
                return self._advance(conversa, "v24_despejo_conformidade", ctx, self._despejo_conformidade_prompt(ctx))
            if choice in {"3", "cancelar", "âŒ cancelar"}:
                return self._idle(conversa, "Despejo cancelado. Nenhuma foto foi salva e o ativo permanece em andamento.")
            return "Escolha Confirmar despejo, Voltar ou Cancelar."
        return self._idle(conversa, "Fluxo reiniciado. Abra o menu para continuar.")

    def _confirmar_despejo_atual(self, conversa, ctx):
        contentor_id = ctx["contentor_id"]
        fotos = list(ctx.get("fotos_despejo") or [])
        tem_divergencia = self._despejo_tem_divergencia(ctx)
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            contentor
            and contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
            and not get_settings().feature_contentores_enabled
        ):
            return self._idle(conversa, self._contentor_despejo_desabilitado_message())
        if (
            contentor
            and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            and not get_settings().feature_carrinhas_enabled
        ):
            return self._idle(conversa, self._carrinha_despejo_desabilitada_message())
        try:
            if contentor and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
                contentor = self.service.confirmar_despejo_carrinha(
                    contentor_id,
                    ctx.get("residuo_efetivo"),
                    tem_divergencia,
                    ctx.get("relato_carga"),
                    conversa.telefone,
                    fotos,
                )
            else:
                contentor = self.service.confirmar_despejo(
                    contentor_id,
                    ctx.get("residuo_efetivo"),
                    tem_divergencia,
                    ctx.get("relato_carga"),
                    operador=conversa.telefone,
                    pedido_id=ctx.get("pedido_id"),
                    fotos=fotos,
                )
        except ValueError as exc:
            pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
            self._limpar_despejo_atual(ctx)
            if pendentes:
                return self._advance(
                    conversa,
                    "v24_despejo_ativo",
                    ctx,
                    f"Esse ativo foi atualizado por outro operador. {exc}\n\n"
                    + self._despejo_selecao_prompt(ctx, pendentes),
                )
            return self._idle(conversa, str(exc))

        despejos = list(ctx.get("despejos") or [])
        despejos.append({"contentor_id": contentor_id, "fotos": len(fotos), "carga_errada": tem_divergencia})
        ctx["despejos"] = despejos
        self._limpar_despejo_atual(ctx)
        pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
        label = self._equipamento_label(contentor)
        if pendentes:
            return self._advance(
                conversa,
                "v24_despejo_ativo",
                ctx,
                f"✅ {label} processado no vazadouro.\nRestam {len(pendentes)} contentores pendentes neste pedido.\n\n"
                "Selecione a proxima unidade deste cliente:\n\n"
                + self._despejo_selecao_prompt(ctx, pendentes),
            )
        pedido = self.service.get(ctx["pedido_id"])
        cliente = pedido.nome_cliente if pedido else ctx["pedido_id"]
        numero = contentor.numero_adesivo_contentor or contentor.id
        return self._idle(
            conversa,
            f"✅ Contentor {numero} processado no vazadouro. Pedido do cliente {cliente} concluído. Nenhum contentor pendente.",
        )

    def _limpar_despejo_atual(self, ctx):
        for key in (
            "contentor_id",
            "fotos_despejo",
            "residuo_contratado",
            "residuo_efetivo",
            "residuo_assumido",
            "carga_errada",
            "relato_carga",
            "residuos_disponiveis",
            "saldo_cotas_visualizado",
        ):
            ctx.pop(key, None)

    def _selected_despejo_contentor_id(self, raw, ctx) -> int | None:
        return self._selected_id(raw, ctx.get("contentores") or [])

    def _is_despejo_terminar(self, message, choice, ctx) -> bool:
        if choice in {"terminar", "terminar despejos deste cliente", "🏁 terminar despejos deste cliente"}:
            return True
        if not choice.isdigit() or int(choice) != ctx.get("terminar_indice"):
            return False
        if message.tipo == "interactive":
            return True
        return self.db.get(PedidoContentor, int(choice)) is None

    def _is_despejo_pendente_do_pedido(self, contentor, pedido_id: int) -> bool:
        return bool(
            contentor
            and contentor.pedido_id == pedido_id
            and (
                (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                    and get_settings().feature_contentores_enabled
                    and contentor.status_recolha == "RECOLHIDO"
                    and contentor.status_ciclo == "EM_ANDAMENTO"
                )
                or (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                    and get_settings().feature_carrinhas_enabled
                    and contentor.status_operacional_carrinha
                    == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
                )
            )
        )

    def _despejo_decisao_residuo(self, conversa, ctx) -> str:
        try:
            transition = self._prepare_despejo_decisao_residuo(ctx)
        except ValueError as exc:
            self._limpar_despejo_atual(ctx)
            return self._idle(conversa, str(exc))
        return self.apply_operational_transition(conversa, transition)

    def _prepare_despejo_decisao_residuo(self, ctx):
        """Prepara a decisão de cotas sem persistir conversa ou operação."""
        cotas = self.service.cotas_residuos(ctx["pedido_id"])
        saldo_limpo = cotas["Entulho Limpo"]["saldo"]
        saldo_misto = cotas["Entulho Misto"]["saldo"]
        ctx["saldo_cotas_visualizado"] = {
            "limpo": saldo_limpo,
            "misto": saldo_misto,
        }
        if saldo_limpo <= 0 and saldo_misto <= 0:
            raise ValueError("Não existem cotas de resíduo pendentes para este pedido.")
        if saldo_limpo > 0 and saldo_misto > 0:
            ctx["residuos_disponiveis"] = ["Entulho Limpo", "Entulho Misto"]
            return AdvanceTransition(
                "v24_despejo_residuo",
                ctx,
                "Qual resíduo caiu no chão?\n\n1. 🟢 Entulho Limpo\n2. 🟠 Entulho Misto",
            )
        residuo = "Entulho Limpo" if saldo_limpo > 0 else "Entulho Misto"
        ctx["residuo_assumido"] = residuo
        ctx["residuo_efetivo"] = None
        return AdvanceTransition(
            "v24_despejo_conformidade",
            ctx,
            self._despejo_conformidade_prompt(ctx),
        )

    def resolve_despejo_ativo_selection(self, message, ctx):
        """Resolve e comprova a seleção de despejo sem alterar estado ou banco."""
        if not isinstance(ctx, Mapping):
            return None
        raw = (message.texto or "").strip()
        choice = self._norm(raw)
        if self._is_despejo_terminar(message, choice, ctx):
            return None
        contentor_id = self._selected_despejo_contentor_id(raw, ctx)
        pedido_id = ctx.get("pedido_id")
        if contentor_id is None or not isinstance(pedido_id, int):
            return None
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not self._is_despejo_pendente_do_pedido(contentor, pedido_id):
            return None
        tipos = {
            TipoEquipamentoPedido.CONTENTOR.value: TipoEquipamentoPedido.CONTENTOR,
            TipoEquipamentoPedido.CARRINHA.value: TipoEquipamentoPedido.CARRINHA,
        }
        modality = tipos.get(contentor.tipo_equipamento)
        if modality is None:
            return None
        if (
            modality is TipoEquipamentoPedido.CONTENTOR
            and not get_settings().feature_contentores_enabled
        ) or (
            modality is TipoEquipamentoPedido.CARRINHA
            and not get_settings().feature_carrinhas_enabled
        ):
            return None

        prepared_context = dict(ctx)
        prepared_context.update(
            {
                "contentor_id": contentor.id,
                "fotos_despejo": [],
                "residuo_contratado": contentor.residuo_contratado,
                "residuo_efetivo": None,
                "residuo_assumido": None,
                "carga_errada": None,
                "relato_carga": None,
            }
        )
        try:
            transition = self._prepare_despejo_decisao_residuo(prepared_context)
        except ValueError:
            return None
        return {
            "contentor_id": contentor.id,
            "modality": modality,
            "context_updates": {
                key: value
                for key, value in transition.context.items()
                if key not in ctx or ctx.get(key) != value
            },
            "next_state": transition.next_state,
            "response": transition.response,
        }

    def resolve_despejo_carrinha_selection(self, message, ctx):
        """Resolve pedido Carrinha elegível para Despejo sem alterar estado."""
        if not isinstance(ctx, Mapping):
            return None
        raw = (message.texto or "").strip()
        pedido_id = self._selected_id(raw, ctx.get("ids", []))
        if pedido_id is None:
            return None
        pedido = self.db.get(Pedido, pedido_id)
        if not pedido:
            return {
                "pedido_id": pedido_id,
                "pedido_exists": False,
                "carrinha_ids": (),
                "selection_context": {},
                "prompt": "",
            }
        pendentes = self._despejo_pendentes(pedido)
        tipos = {item.tipo_equipamento for item in pendentes}
        if tipos != {TipoEquipamentoPedido.CARRINHA.value}:
            return None
        context_snapshot = {}
        prompt = self._despejo_selecao_prompt(context_snapshot, pendentes)
        return {
            "pedido_id": pedido.id,
            "pedido_exists": True,
            "carrinha_ids": tuple(item.id for item in pendentes),
            "selection_context": context_snapshot,
            "prompt": prompt,
        }

    def resolve_despejo_carrinha_ativo_selection(self, message, ctx):
        """Prepara apenas o ativo Carrinha elegível, sem persistir alterações."""
        if not isinstance(ctx, Mapping):
            return None
        raw = (message.texto or "").strip()
        choice = self._norm(raw)
        if self._is_despejo_terminar(message, choice, ctx):
            return None
        contentor_id = self._selected_despejo_contentor_id(raw, ctx)
        pedido_id = ctx.get("pedido_id")
        if contentor_id is None or not isinstance(pedido_id, int):
            return None
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            not self._is_despejo_pendente_do_pedido(contentor, pedido_id)
            or contentor.tipo_equipamento != TipoEquipamentoPedido.CARRINHA.value
        ):
            return None
        prepared_context = dict(ctx)
        prepared_context.update({
            "contentor_id": contentor.id,
            "fotos_despejo": [],
            "residuo_contratado": contentor.residuo_contratado,
            "residuo_efetivo": None,
            "residuo_assumido": None,
            "carga_errada": None,
            "relato_carga": None,
        })
        try:
            transition = self._prepare_despejo_decisao_residuo(prepared_context)
        except ValueError:
            return None
        return {
            "contentor_id": contentor.id,
            "modality": TipoEquipamentoPedido.CARRINHA,
            "context_updates": {
                key: value
                for key, value in transition.context.items()
                if key not in ctx or ctx.get(key) != value
            },
            "next_state": transition.next_state,
            "response": transition.response,
        }

    def resolve_despejo_context_modality(self, ctx):
        """Comprova a modalidade do despejo atual sem alterar estado ou banco."""
        if not isinstance(ctx, Mapping):
            return None
        contentor_id = ctx.get("contentor_id")
        pedido_id = ctx.get("pedido_id")
        if not isinstance(contentor_id, int) or not isinstance(pedido_id, int):
            return None
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not self._is_despejo_pendente_do_pedido(contentor, pedido_id):
            return None
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value:
            return TipoEquipamentoPedido.CONTENTOR
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            return TipoEquipamentoPedido.CARRINHA
        return None

    @staticmethod
    def despejo_context_is_modern(ctx, state):
        """Distingue contexto preparado do contexto que exige hidratação legada."""
        if not isinstance(ctx, Mapping):
            return False
        common = {
            "pedido_id", "contentor_id", "fotos_despejo",
            "residuo_contratado", "residuo_efetivo", "residuo_assumido",
            "carga_errada", "relato_carga",
        }
        required = {
            "v24_despejo_residuo": common | {"residuos_disponiveis"},
            "v24_despejo_conformidade": common,
            "v24_despejo_relato": common,
            "v24_despejo_confirmacao": common,
        }.get(state)
        return bool(required and required.issubset(ctx))

    def despejo_foto_prompt(self, ctx):
        """Expõe o prompt legado da foto sem persistir estado."""
        return self._despejo_foto_prompt(ctx)

    def despejo_conformidade_prompt(self, ctx):
        """Expõe o prompt legado de conformidade sem persistir estado."""
        return self._despejo_conformidade_prompt(ctx)

    def confirm_despejo_contentor(self, conversa, ctx):
        """Delega a confirmação ao boundary legado já existente."""
        if (
            self.resolve_despejo_context_modality(ctx)
            is not TipoEquipamentoPedido.CONTENTOR
        ):
            raise ValueError("Esta operação aceita apenas contentores.")
        return self._confirmar_despejo_atual(conversa, ctx)

    def confirmar_despejo_carrinha(self, conversa, ctx):
        """Recomprova o Despejo Carrinha e delega ao fluxo persistente existente."""
        if conversa.estado_atual != "v24_despejo_confirmacao":
            return None
        if self.resolve_despejo_context_modality(ctx) is not TipoEquipamentoPedido.CARRINHA:
            return None
        return self._confirmar_despejo_atual(conversa, ctx)

    def despejo_confirmacao_prompt(self, ctx):
        """Expõe o prompt legado da confirmação sem persistir estado."""
        return self._despejo_confirmacao_prompt(ctx)

    def _despejo_foto_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else "equipamento"
        return f"Envie a foto do despejo do {label} no vazadouro."

    def _despejo_conformidade_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        numero = contentor.numero_adesivo_contentor if contentor and contentor.numero_adesivo_contentor else ctx["contentor_id"]
        residuo = ctx.get("residuo_assumido") or ctx["residuo_contratado"]
        return (
            f"O entulho do Contentor {numero} corresponde a {residuo}?\n\n"
            "1. ✅ Sim, tudo certo\n"
            "2. 🚨 Não, está misturado/errado"
        )

    def _despejo_residuo_efetivo_prompt(self, conversa, ctx):
        ctx["residuos_disponiveis"] = ["Entulho Limpo", "Entulho Misto"]
        return self._advance(
            conversa,
            "v24_despejo_residuo",
            ctx,
            "Qual residuo foi efetivamente encontrado?\n\n"
            + "\n".join(f"{index}. {residuo}" for index, residuo in enumerate(ctx["residuos_disponiveis"], 1)),
        )

    def _despejo_tem_divergencia(self, ctx) -> bool:
        """Determina se há divergência no despejo.

        Considera divergência quando:
        - O fluxo de conformidade marcou explicitamente carga_errada=True; OU
        - O resíduo efetivo difere do resíduo contratado.
        """
        carga_errada = bool(ctx.get("carga_errada"))
        residuo_contratado = ctx.get("residuo_contratado")
        residuo_efetivo = ctx.get("residuo_efetivo")

        residuos_diferentes = bool(
            residuo_contratado
            and residuo_efetivo
            and residuo_contratado != residuo_efetivo
        )

        return carga_errada or residuos_diferentes

    def _despejo_confirmacao_prompt(self, ctx) -> str:
        pedido = self.service.get(ctx["pedido_id"])
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else f"Ativo #{ctx['contentor_id']}"
        tem_divergencia = self._despejo_tem_divergencia(ctx)
        linhas = [
            "Confirme o despejo deste ativo:",
            "",
            f"Cliente: {pedido.nome_cliente if pedido else ctx.get('pedido_id')}",
            f"Ativo: {label}",
            f"Fotos: {len(ctx.get('fotos_despejo') or [])}",
            f"Residuo contratado: {ctx.get('residuo_contratado')}",
            f"Residuo efetivo: {ctx.get('residuo_efetivo')}",
            f"Divergencia: {'Sim' if tem_divergencia else 'Nao'}",
        ]
        if ctx.get("carga_errada"):
            linhas.append(f"Relato: {ctx.get('relato_carga')}")
        linhas.extend(["", "1. ✅ Confirmar despejo", "2. ↩️ Voltar", "3. ❌ Cancelar"])
        return "\n".join(linhas)

    def _despejo_selecao_prompt(self, ctx, pendentes) -> str:
        ctx["contentores"] = [item.id for item in pendentes]
        ctx["terminar_indice"] = len(pendentes) + 1
        linhas = ["Selecione o ativo descarregado:"]
        linhas.extend(f"{index}. {self._equipamento_label(item)}" for index, item in enumerate(pendentes, 1))
        linhas.append(f"{ctx['terminar_indice']}. 🏁 Terminar despejos deste cliente")
        return "\n".join(linhas)

    def _despejo_pendentes_por_pedido(self, pedido_id: int):
        pedido = self.service.get(pedido_id)
        return self._despejo_pendentes(pedido) if pedido else []

    def _despejo_pendentes(self, pedido):
        return [
            item
            for item in sorted(pedido.contentores, key=lambda item: (item.numero_adesivo_contentor or "", item.id))
            if (
                item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and get_settings().feature_contentores_enabled
                and item.status_recolha == "RECOLHIDO"
                and item.status_ciclo == "EM_ANDAMENTO"
            )
            or (
                item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and get_settings().feature_carrinhas_enabled
                and item.status_operacional_carrinha
                == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
            )
        ]

    def _hydrate_legacy_despejo_context(self, ctx) -> None:
        if "pedido_id" in ctx or not ctx.get("contentor_id"):
            return
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        if not contentor:
            return
        ctx["pedido_id"] = contentor.pedido_id
        ctx.setdefault("residuo_contratado", contentor.residuo_contratado)
        ctx.setdefault("residuo_assumido", ctx.get("residuo_assumido") or ctx.get("residuo_contratado"))
        ctx.setdefault("residuo_efetivo", ctx.get("residuo_assumido"))
        ctx.setdefault("carga_errada", None)
        ctx.setdefault("relato_carga", None)
        ctx.setdefault("fotos_despejo", [])

    def _contexto_despejo_tem_tipo(self, ctx, tipo_equipamento: str) -> bool:
        contentor_id = ctx.get("contentor_id")
        contentor = self.db.get(PedidoContentor, contentor_id) if contentor_id else None
        return bool(contentor and contentor.tipo_equipamento == tipo_equipamento)

    @staticmethod
    def _contentor_despejo_desabilitado_message() -> str:
        return "O despejo de Contentor não está habilitado. A operação foi cancelada com segurança."

    @staticmethod
    def _carrinha_despejo_desabilitada_message() -> str:
        return "O despejo de Carrinha não está habilitado. A operação foi cancelada com segurança."

    def _recover_disabled_avaria(self, conversa: ConversaWhatsApp, ctx: dict) -> str:
        ctx.pop("avariado", None)
        ctx.pop("relato_avaria", None)
        prompt = self._recolha_confirmacao_prompt(ctx)
        return self._advance(
            conversa,
            "v24_recolha_confirmacao",
            ctx,
            "A funcionalidade de avarias não está disponível nesta empresa. O subfluxo foi cancelado com segurança.\n\n" + prompt,
        )

    def _confirmar_recolha_atual(self, conversa, ctx):
        contentor_id = ctx["contentor_id"]
        avariado = bool(ctx.get("avariado"))
        relato = ctx.get("relato_avaria")
        fotos = list(ctx.get("fotos_recolha") or [])
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            contentor
            and contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
            and not get_settings().feature_contentores_enabled
        ):
            return self._idle(conversa, self._contentor_recolha_desabilitada_message())
        if (
            contentor
            and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            and not get_settings().feature_carrinhas_enabled
        ):
            return self._idle(conversa, self._carrinha_partida_desabilitada_message())
        is_carrinha = bool(
            contentor
            and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
        )
        try:
            if is_carrinha:
                self.service.confirmar_partida_carrinha(
                    contentor_id, conversa.telefone, avariado, relato, fotos
                )
            else:
                self.service.confirmar_recolha(
                    contentor_id, conversa.telefone, avariado, relato, fotos
                )
        except ValueError:
            pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
            self._limpar_recolha_atual(ctx)
            if pendentes:
                return self._advance(
                    conversa,
                    "v24_recolha_ativo",
                    ctx,
                    "Esse ativo foi atualizado por outro operador.\n\n"
                    + self._recolha_selecao_prompt(ctx, pendentes),
                )
            return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de recolha.")

        recolhas = list(ctx.get("recolhas") or [])
        recolhas.append({
            "contentor_id": contentor_id,
            "avariado": avariado,
            "fotos": len(fotos),
            "operacao": "PARTIDA" if is_carrinha else "RECOLHA",
        })
        ctx["recolhas"] = recolhas
        self._limpar_recolha_atual(ctx)

        if "pedido_id" not in ctx:
            if avariado:
                return self._idle(conversa, "✅ Recolha registrada com pendencia de avaria.")
            return self._idle(conversa, "✅ Recolha registrada. O contentor aguarda despejo.")

        pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
        if pendentes:
            return self._advance(
                conversa,
                "v24_recolha_ativo",
                ctx,
                "Ativo recolhido. Escolha o proximo ou termine as recolhas deste cliente.\n\n"
                + self._recolha_selecao_prompt(ctx, pendentes),
            )

        if any(item.get("avariado") for item in recolhas):
            return self._idle(conversa, "✅ Recolha do pedido concluida com pendencia de avaria. Ativos aguardam despejo.")
        return self._idle(conversa, "✅ Recolha do pedido concluida. Ativos aguardam despejo.")

    def _cancelar_recolha_atual(self, conversa, ctx):
        self._limpar_recolha_atual(ctx)
        pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
        if pendentes:
            return self._advance(
                conversa,
                "v24_recolha_ativo",
                ctx,
                "Recolha do ativo cancelada. Nenhuma foto foi salva.\n\n"
                + self._recolha_selecao_prompt(ctx, pendentes),
            )
        return self._idle(conversa, "✅ Nao existem mais ativos pendentes de recolha neste pedido.")

    def _limpar_recolha_atual(self, ctx):
        for key in ("contentor_id", "fotos_recolha", "avariado", "relato_avaria"):
            ctx.pop(key, None)

    def _selected_recolha_contentor_id(self, raw, ctx) -> int | None:
        return self._selected_id(raw, ctx.get("contentores") or [])

    def resolve_recolha_ativo_selection(self, message, ctx):
        """Resolve e comprova a selecao de recolha sem alterar estado ou banco."""
        if not isinstance(ctx, Mapping):
            return None
        raw = (message.texto or "").strip()
        choice = self._norm(raw)
        if self._is_recolha_terminar(message, choice, ctx):
            return None
        contentor_id = self._selected_recolha_contentor_id(raw, ctx)
        pedido_id = ctx.get("pedido_id")
        if contentor_id is None or not isinstance(pedido_id, int):
            return None
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not self._is_recolha_pendente_do_pedido(contentor, pedido_id):
            return None
        tipos = {
            TipoEquipamentoPedido.CONTENTOR.value: TipoEquipamentoPedido.CONTENTOR,
            TipoEquipamentoPedido.CARRINHA.value: TipoEquipamentoPedido.CARRINHA,
        }
        modality = tipos.get(contentor.tipo_equipamento)
        if modality is None:
            return None
        return {
            "contentor_id": contentor.id,
            "modality": modality,
            "foto_prompt": self._recolha_foto_prompt_for(contentor),
        }

    def resolve_partida_carrinha_selection(self, message, ctx):
        """Resolve pedido Carrinha elegível para Partida sem alterar estado."""
        raw = (message.texto or "").strip()
        pedido_id = self._selected_id(raw, ctx.get("ids", []))
        if pedido_id is None:
            return None
        pedido = self.db.get(Pedido, pedido_id)
        if not pedido:
            return {"pedido_id": pedido_id, "pedido_exists": False, "carrinha_ids": (), "selection_context": {}, "prompt": ""}
        pendentes = self._recolha_pendentes(pedido)
        tipos = {item.tipo_equipamento for item in pendentes}
        if tipos != {TipoEquipamentoPedido.CARRINHA.value}:
            return None
        context_snapshot = {}
        prompt = self._recolha_selecao_prompt(context_snapshot, pendentes)
        return {
            "pedido_id": pedido.id,
            "pedido_exists": True,
            "carrinha_ids": tuple(item.id for item in pendentes),
            "selection_context": context_snapshot,
            "prompt": prompt,
        }

    def resolve_recolha_context_modality(self, ctx):
        """Comprova a modalidade do ativo atual sem alterar estado ou banco."""
        if not isinstance(ctx, Mapping):
            return None
        contentor_id = ctx.get("contentor_id")
        pedido_id = ctx.get("pedido_id")
        if not isinstance(contentor_id, int) or not isinstance(pedido_id, int):
            return None
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not self._is_recolha_pendente_do_pedido(contentor, pedido_id):
            return None
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value:
            return TipoEquipamentoPedido.CONTENTOR
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            return TipoEquipamentoPedido.CARRINHA
        return None

    def recolha_confirmacao_prompt(self, ctx):
        """Expoe o prompt legado de confirmacao sem persistir estado."""
        return self._recolha_confirmacao_prompt(ctx)

    def confirm_recolha_contentor(self, conversa, ctx):
        """Recomprova Contentor e delega a confirmacao ao fluxo legado."""
        if (
            self.resolve_recolha_context_modality(ctx)
            is not TipoEquipamentoPedido.CONTENTOR
        ):
            raise ValueError("Esta operação aceita apenas contentores.")
        return self._confirmar_recolha_atual(conversa, ctx)

    def confirmar_partida_carrinha(self, conversa, ctx):
        """Boundary específico da Partida que reutiliza a confirmação existente."""
        if conversa.estado_atual != "v24_recolha_confirmacao":
            return None
        if self.resolve_recolha_context_modality(ctx) is not TipoEquipamentoPedido.CARRINHA:
            return None
        return self._confirmar_recolha_atual(conversa, ctx)

    def cancelar_partida_carrinha(self, conversa, ctx):
        """Recomprova Carrinha antes de reutilizar o cancelamento existente."""
        if self.resolve_recolha_context_modality(ctx) is not TipoEquipamentoPedido.CARRINHA:
            return None
        return self._cancelar_recolha_atual(conversa, ctx)

    def cancel_recolha_contentor(self, conversa, ctx):
        """Recomprova Contentor e delega o cancelamento ao fluxo legado."""
        if (
            self.resolve_recolha_context_modality(ctx)
            is not TipoEquipamentoPedido.CONTENTOR
        ):
            raise ValueError("Esta operação aceita apenas contentores.")
        return self._cancelar_recolha_atual(conversa, ctx)

    def _is_recolha_terminar(self, message, choice, ctx) -> bool:
        if choice in {"terminar", "terminar recolhas deste cliente", "🏁 terminar recolhas deste cliente"}:
            return True
        if not choice.isdigit() or int(choice) != ctx.get("terminar_indice"):
            return False
        if message.tipo == "interactive":
            return True
        return self.db.get(PedidoContentor, int(choice)) is None

    def _is_recolha_pendente_do_pedido(self, contentor, pedido_id: int) -> bool:
        return bool(
            contentor
            and contentor.pedido_id == pedido_id
            and (
                (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                    and contentor.status_entrega == "ENTREGUE"
                    and contentor.status_recolha == "PENDENTE"
                )
                or (
                    contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                    and contentor.status_operacional_carrinha
                    == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
                )
            )
        )

    def _recolha_foto_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        return self._recolha_foto_prompt_for(contentor)

    def _recolha_foto_prompt_for(self, contentor) -> str:
        label = self._equipamento_label(contentor) if contentor else "equipamento"
        return f"Envie a foto de recolha do {label} cheio antes do icamento."

    def _recolha_confirmacao_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else f"Ativo #{ctx['contentor_id']}"
        linhas = [
            "Confirme a recolha deste ativo:",
            "",
            f"Ativo: {label}",
            f"Fotos: {len(ctx.get('fotos_recolha') or [])}",
        ]
        if get_settings().feature_avarias_enabled:
            avaria = "Sim" if ctx.get("avariado") else "Nao"
            linhas.append(f"Avaria: {avaria}")
            if ctx.get("avariado"):
                linhas.append(f"Relato: {ctx.get('relato_avaria')}")
        linhas.extend(["", "1. Confirmar recolha", "2. Cancelar ativo"])
        return "\n".join(linhas)

    def _recolha_selecao_prompt(self, ctx, pendentes) -> str:
        ctx["contentores"] = [item.id for item in pendentes]
        ctx["terminar_indice"] = len(pendentes) + 1
        linhas = ["Selecione o ativo que esta recolhendo:"]
        linhas.extend(f"{index}. {self._equipamento_label(item)}" for index, item in enumerate(pendentes, 1))
        linhas.append(f"{ctx['terminar_indice']}. 🏁 Terminar recolhas deste cliente")
        return "\n".join(linhas)

    def _recolha_pendentes_por_pedido(self, pedido_id: int):
        pedido = self.service.get(pedido_id)
        return self._recolha_pendentes(pedido) if pedido else []

    def _recolha_pendentes(self, pedido):
        return [
            item
            for item in sorted(pedido.contentores, key=lambda item: (item.numero_adesivo_contentor or "", item.id))
            if (
                item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and get_settings().feature_contentores_enabled
                and item.status_entrega == "ENTREGUE"
                and item.status_recolha == "PENDENTE"
            )
            or (
                item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and get_settings().feature_carrinhas_enabled
                and item.status_operacional_carrinha
                == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
            )
        ]

    def _contexto_recolha_tem_tipo(self, ctx, tipo_equipamento: str) -> bool:
        contentor_id = ctx.get("contentor_id")
        contentor = self.db.get(PedidoContentor, contentor_id) if contentor_id else None
        return bool(
            contentor
            and contentor.tipo_equipamento == tipo_equipamento
        )

    @staticmethod
    def _contentor_recolha_desabilitada_message() -> str:
        return "A recolha de Contentor não está habilitada. A operação foi cancelada com segurança."

    @staticmethod
    def _carrinha_partida_desabilitada_message() -> str:
        return "A partida de Carrinha não está habilitada. A operação foi cancelada com segurança."

    @staticmethod
    def _merge_pedidos(*grupos):
        por_id = {}
        for grupo in grupos:
            for pedido in grupo:
                por_id[pedido.id] = pedido
        return sorted(
            por_id.values(),
            key=lambda pedido: (pedido.data_planejada, pedido.id),
        )

    def _despejo_residuo_prompt(self, conversa, ctx):
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        cotas = self.service.cotas_restantes(contentor.pedido_id)
        available = [name for name, amount in cotas.items() if amount > 0]
        if len(available) == 1:
            ctx["residuo_assumido"] = available[0]
            return self._advance(
                conversa, "v24_despejo_conformidade", ctx,
                f"O entulho está correto com o contratado ({available[0]})?\n\n"
                "1. ✅ Sim, tudo certo\n2. 🚨 Não, está misturado/errado",
            )
        ctx["residuos_disponiveis"] = available
        return self._advance(
            conversa, "v24_despejo_residuo", ctx,
            "Qual resíduo caiu no chão?\n\n"
            + "\n".join(f"{i}. 🟢 {item}" for i, item in enumerate(available, 1)),
        )

    def confirm_entrega_contentor(self, conversa, ctx):
        return self._confirmar_entrega_preparada(
            conversa,
            ctx,
            expected_tipo=TipoEquipamentoPedido.CONTENTOR.value,
        )

    def resolve_entrega_contentor_selection(self, message, context):
        """Resolve a seleção em snapshot simples, sem persistir conversa ou operação."""
        raw = (message.texto or "").strip()
        pedido_id = self._selected_entrega_pedido_id(raw, context.get("ids", []))
        if pedido_id is None:
            return None
        pedido = self.db.get(Pedido, pedido_id)
        if not pedido:
            return {
                "pedido_id": pedido_id,
                "pedido_exists": False,
                "contentor_ids": (),
                "prompt": "",
            }
        pendentes = tuple(
            item.id
            for item in pedido.contentores
            if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
            and item.status_entrega == "PENDENTE"
        )
        prompt = (
            "Digite o número do contentor que está a descarregar agora:"
            if pendentes
            else ""
        )
        return {
            "pedido_id": pedido.id,
            "pedido_exists": True,
            "contentor_ids": pendentes,
            "prompt": prompt,
        }

    def resolve_entrega_contentor_adesivo(self, message, context):
        """Fornece ao agente um snapshot simples das leituras do adesivo."""
        if message.tipo == "interactive":
            return None
        item_id = context["contentores"][context["indice"]]
        contentor = self.db.get(PedidoContentor, item_id)
        number = (message.texto or "").strip()
        duplicate = self.db.query(PedidoContentor).filter(
            PedidoContentor.numero_adesivo_contentor == number,
            PedidoContentor.status_ciclo == "EM_ANDAMENTO",
        ).first()
        return {
            "ativo_exists": contentor is not None,
            "is_contentor": (
                contentor is not None
                and contentor.tipo_equipamento
                == TipoEquipamentoPedido.CONTENTOR.value
            ),
            "status_entrega": contentor.status_entrega if contentor else None,
            "adesivo_em_ciclo_ativo": duplicate is not None,
        }

    def resolve_chegada_carrinha_selection(self, message, context):
        """Resolve a seleção moderna de Carrinha em snapshot read-only."""
        raw = (message.texto or "").strip()
        pedido_id = self._selected_entrega_pedido_id(raw, context.get("ids", []))
        if pedido_id is None:
            return None
        pedido = self.db.get(Pedido, pedido_id)
        if not pedido:
            return {"pedido_id": pedido_id, "pedido_exists": False, "carrinha_ids": (), "prompt": ""}
        pendentes = tuple(
            item.id for item in pedido.contentores
            if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            and item.status_operacional_carrinha
            == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
        )
        return {
            "pedido_id": pedido.id,
            "pedido_exists": True,
            "carrinha_ids": pendentes,
            "prompt": (
                "Confirme o número da frota da carrinha alocada (ou digite 0 se não houver):"
                if pendentes else ""
            ),
        }

    def resolve_chegada_carrinha_frota(self, message, context):
        """Expõe somente existência, modalidade e status do ativo atual."""
        if message.tipo == "interactive":
            return None
        item_id = context["contentores"][context["indice"]]
        item = self.db.get(PedidoContentor, item_id)
        return {
            "ativo_exists": item is not None,
            "is_carrinha": (
                item is not None
                and item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            ),
            "status_operacional": (
                item.status_operacional_carrinha if item else None
            ),
        }

    def confirmar_chegada_carrinha(self, conversa, ctx):
        """Boundary específico que delega à confirmação transacional existente."""
        return self._confirmar_entrega_preparada(
            conversa,
            ctx,
            expected_tipo=TipoEquipamentoPedido.CARRINHA.value,
        )

    def resolve_entrega_pagamento_modality(self, ctx):
        """Resolve a modalidade pelos itens persistidos, sem alterar o pedido."""
        if not isinstance(ctx, Mapping):
            return None
        pedido_id = ctx.get("pedido_id")
        if not isinstance(pedido_id, int) or isinstance(pedido_id, bool):
            return None
        pedido = self.service.get(pedido_id)
        if not pedido or not pedido.contentores:
            return None
        tipos = {item.tipo_equipamento for item in pedido.contentores}
        if tipos == {TipoEquipamentoPedido.CONTENTOR.value}:
            return TipoEquipamentoPedido.CONTENTOR
        if tipos == {TipoEquipamentoPedido.CARRINHA.value}:
            return TipoEquipamentoPedido.CARRINHA
        return None

    def registrar_pagamento_entrega_contentor(self, conversa, ctx, forma):
        """Persiste o pagamento de Contentor mantendo os dois commits legados."""
        if (
            self.resolve_entrega_pagamento_modality(ctx)
            is not TipoEquipamentoPedido.CONTENTOR
        ):
            raise ValueError("Esta operação aceita apenas contentores.")
        self.service.registrar_pagamento(ctx["pedido_id"], forma)
        return self._idle(
            conversa,
            "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado.",
        )

    def _confirmar_entrega_preparada(self, conversa, ctx, expected_tipo=None):
        try:
            if (
                not get_settings().feature_contentores_enabled
                and self._contexto_entrega_tem_tipo(ctx, TipoEquipamentoPedido.CONTENTOR.value)
            ):
                return self._idle(conversa, self._contentor_entrega_desabilitada_message())
            if (
                not get_settings().feature_carrinhas_enabled
                and self._contexto_entrega_tem_tipo(ctx, TipoEquipamentoPedido.CARRINHA.value)
            ):
                return self._idle(conversa, self._carrinha_chegada_desabilitada_message())
            if conversa.estado_atual != "v24_entrega_confirmacao":
                raise ValueError("A entrega não está pronta para confirmação.")
            campos_obrigatorios = {"pedido_id", "latitude", "longitude", "referencia_entrega", "entregas"}
            if not campos_obrigatorios.issubset(ctx):
                raise ValueError("Os dados da entrega estão incompletos. Reinicie a entrega.")
            entregas = ctx.get("entregas") or []
            ativos = [
                self.db.get(PedidoContentor, item["contentor_id"])
                for item in entregas
            ]
            tipos = {item.tipo_equipamento for item in ativos if item}
            if len(tipos) != 1:
                raise ValueError(
                    "Não é permitido misturar contentores e carrinhas na mesma operação."
                )
            if expected_tipo is not None and tipos != {expected_tipo}:
                raise ValueError("Esta operação aceita apenas contentores.")
            is_carrinha = tipos == {TipoEquipamentoPedido.CARRINHA.value}
            confirmar = (
                self.service.confirmar_chegada_carrinha_lote_transacional
                if is_carrinha
                else self.service.confirmar_entrega_lote_transacional
            )
            pedido = confirmar(
                ctx["pedido_id"],
                conversa.telefone,
                ctx["latitude"],
                ctx["longitude"],
                ctx["referencia_entrega"],
                entregas,
            )
            if pedido.status_pagamento == StatusPagamento.PENDENTE.value and not is_carrinha:
                conversa.estado_atual = "v24_entrega_pagou"
                conversa.contexto_json = {"pedido_id": ctx["pedido_id"]}
                response = (
                    "O cliente realizou o pagamento no local?\n\n"
                    "1. ✅ Sim, foi pago\n"
                    "2. 🕒 Não, continua pendente"
                )
            else:
                self._aplicar_idle(conversa)
                response = (
                    "✅ Chegada da carrinha confirmada. O pagamento não foi alterado."
                    if is_carrinha
                    else "✅ Entrega confirmada com sucesso para todos os ativos processados."
                )
            self.db.commit()
        except ValueError as exc:
            self.db.rollback()
            return f"⚠️ {exc}"
        except Exception:
            self.db.rollback()
            raise
        return response

    def entrega_confirmacao_prompt(self, ctx):
        return self._entrega_confirmacao_prompt(ctx)

    def _entrega_confirmacao_prompt(self, ctx):
        pedido = self.service.get(ctx["pedido_id"])
        entregas = ctx.get("entregas") or []
        ativos = [
            self.db.get(PedidoContentor, entrega["contentor_id"])
            for entrega in entregas
        ]
        is_carrinha = bool(ativos) and all(
            ativo
            and ativo.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            for ativo in ativos
        )
        linhas = [
            (
                "Confirme a chegada preparada:"
                if is_carrinha
                else "Confirme a entrega preparada:"
            ),
            "",
            f"Cliente: {pedido.nome_cliente if pedido else ctx.get('pedido_id')}",
            f"Pedido: #{ctx['pedido_id']}",
            f"Quantidade de ativos: {len(entregas)}",
        ]
        for index, entrega in enumerate(entregas, 1):
            contentor = self.db.get(PedidoContentor, entrega["contentor_id"])
            label = self._equipamento_label(contentor) if contentor else f"Ativo #{entrega['contentor_id']}"
            numero = entrega.get("numero_adesivo") or "sem identificação"
            if contentor and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value and numero == "0":
                numero = "sem frota"
            linhas.append(
                f"{index}. {label} | identificação: {numero} | fotos: {len(entrega.get('fotos') or [])}"
            )
        referencia = ctx.get("referencia_entrega") or "sem referência"
        pagamento = "pendente" if pedido and pedido.status_pagamento == StatusPagamento.PENDENTE.value else "pago"
        linhas.extend(
            [
                f"Ponto de referência: {referencia}",
                f"Pagamento: {pagamento}",
                "",
                (
                    "1. ✅ Confirmar chegada"
                    if is_carrinha
                    else "1. ✅ Confirmar entrega"
                ),
                "2. ❌ Cancelar",
            ]
        )
        return "\n".join(linhas)

    def _finish_cadastro(self, conversa, ctx):
        if not ctx.get("_confirmado"):
            return self._advance(
                conversa,
                self._CONFIRMATION_STATE,
                ctx,
                self._format_confirmacao_cadastro(ctx),
            )
        try:
            if not self._reservar_confirmacao(conversa):
                self.db.rollback()
                return "Este pedido já foi confirmado ou está sendo processado."
            pedido = self.service.criar_transacional(
                nome_cliente=ctx["nome"], telefone_cliente=ctx["telefone"],
                data_planejada=datetime.fromisoformat(ctx["data"]), valor_global=ctx["valor"],
                pago=ctx["pago"], forma_pagamento=ctx.get("forma"),
                pedido_feito_por=conversa.telefone, endereco_aproximado=ctx["endereco"],
                ponto_referencia=ctx.get("referencia"), itens=ctx.get("itens") or [],
                endereco_latitude=ctx.get("endereco_latitude"),
                endereco_longitude=ctx.get("endereco_longitude"),
                precisa_mao_de_obra=ctx.get("precisa_mao_de_obra"),
            )
            self._aplicar_idle(conversa)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        tipo_label = self._tipo_label(ctx.get("tipo_solicitacao"))
        return f"✅ Pedido #{pedido.id} criado com {len(pedido.contentores)} {tipo_label.lower()}(es)."

    def confirmar_cadastro_contentor(self, conversa, context):
        """Recomprova Contentor e delega ao boundary persistente legado."""
        if conversa.estado_atual != self._CONFIRMATION_STATE:
            return None
        if (
            classify_cadastro_modality(context)
            is not CadastroModality.CONTENTOR_PROVEN
        ):
            return None
        ctx = dict(context)
        ctx["_confirmado"] = True
        return self._finish_cadastro(conversa, ctx)

    def confirmar_cadastro_carrinha(self, conversa, context):
        """Recomprova Carrinha e delega ao boundary persistente existente."""
        from app.agents.pedido_v24.carrinha_cadastro import (
            CarrinhaCadastroModality,
            classify_carrinha_cadastro,
        )

        if conversa.estado_atual != self._CONFIRMATION_STATE:
            return None
        if classify_carrinha_cadastro(context) is not CarrinhaCadastroModality.CARRINHA_PROVEN:
            return None
        ctx = dict(context)
        ctx["_confirmado"] = True
        return self._finish_cadastro(conversa, ctx)

    def _reservar_confirmacao(self, conversa) -> bool:
        atualizados = (
            self.db.query(ConversaWhatsApp)
            .filter(ConversaWhatsApp.id == conversa.id)
            .filter(ConversaWhatsApp.estado_atual == self._CONFIRMATION_STATE)
            .update(
                {ConversaWhatsApp.estado_atual: self._CONFIRMING_STATE},
                synchronize_session="evaluate",
            )
        )
        return atualizados == 1

    def _after_cliente(self, conversa, ctx):
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            return self._advance(conversa, "v24_cadastro_data", ctx, self._data_prompt())
        return self._advance(conversa, "v24_cadastro_quantidade", ctx, self._quantidade_prompt(ctx))

    def _registrar_item_cadastro(self, conversa, ctx, residue):
        tipo = ctx.get("tipo_solicitacao") or TipoEquipamentoPedido.CONTENTOR.value
        item = {
            "tipo_equipamento": tipo,
            "horario_agendado": ctx.get("horario_agendado") if tipo == TipoEquipamentoPedido.CARRINHA.value else None,
            "precisa_mao_de_obra": False,
        }
        item_atual = dict(ctx.get("item_atual") or {})
        if item_atual.get("tipo_equipamento"):
            item["tipo_equipamento"] = item_atual["tipo_equipamento"]
        if item_atual.get("horario_agendado"):
            item["horario_agendado"] = item_atual["horario_agendado"]
        item["residuo_contratado"] = residue
        ctx["itens"] = [*(ctx.get("itens") or []), item]
        ctx["residuos"] = [*(ctx.get("residuos") or []), residue]
        ctx.pop("item_atual", None)
        if len(ctx["itens"]) < ctx["quantidade"]:
            if tipo == TipoEquipamentoPedido.CARRINHA.value:
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return self._advance(conversa, "v24_cadastro_tipo_equipamento", ctx, self._tipo_equipamento_prompt(ctx))
        if tipo == TipoEquipamentoPedido.CARRINHA.value:
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        return self._advance(
            conversa,
            "v24_cadastro_data",
            ctx,
            self._data_prompt(),
        )

    def _corrigir_prompt(self, ctx):
        fields = self._corrigir_fields(ctx)
        linhas = ["Qual campo deseja corrigir?"]
        linhas.extend(f"{index}. {title}" for index, (field, title) in enumerate(fields, 1))
        return "\n".join(linhas)

    def _corrigir_fields(self, ctx):
        is_carrinha = ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value
        fields = [
            ("quantidade", "Quantidade de carrinhas" if is_carrinha else "Quantidade de contentores"),
            ("nome_cliente", "Nome do cliente"),
            ("telefone", "Telefone"),
            ("data_entrega", "Dia da entrega"),
        ]
        if is_carrinha:
            fields.append(("hora_entrega", "Hora da entrega"))
        fields.extend(
            [
                ("tipo_residuo", "Tipo de resíduo"),
                ("mao_de_obra", "Pessoal para carregamento"),
                ("valor_total", "Valor total"),
                ("status_pagamento", "Status do pagamento"),
            ]
        )
        if ctx.get("pago"):
            fields.append(("forma_pagamento", "Forma de pagamento"))
        fields.extend([("endereco", "Endereço"), ("ponto_referencia", "Ponto de referência")])
        return fields

    def _parse_corrigir_field(self, choice, ctx=None):
        if choice.startswith("corrigir_pedido:"):
            return choice.split(":", 1)[1]
        if choice.startswith("option_") and choice.removeprefix("option_").isdigit() or choice.isdigit():
            number = choice.removeprefix("option_")
            index = int(number) - 1
            fields = [field for field, _title in self._corrigir_fields(ctx or {})]
            return fields[index] if 0 <= index < len(fields) else None
        return {
            "quantidade": "quantidade",
            "nome do cliente": "nome_cliente",
            "telefone": "telefone",
            "dia da entrega": "data_entrega",
            "hora da entrega": "hora_entrega",
            "tipo de residuo": "tipo_residuo",
            "pessoal para carregamento": "mao_de_obra",
            "valor total": "valor_total",
            "status do pagamento": "status_pagamento",
            "forma de pagamento": "forma_pagamento",
            "endereco": "endereco",
            "ponto de referencia": "ponto_referencia",
        }.get(choice)

    def _edit_state_for(self, field):
        if field == "data_entrega":
            return "v24_cadastro_edicao_data"
        if field == "ponto_referencia":
            return "v24_cadastro_edicao_referencia_opcao"
        if field in {"tipo_residuo", "mao_de_obra", "status_pagamento", "forma_pagamento"}:
            return "v24_cadastro_edicao_opcao"
        if field == "hora_entrega":
            return "v24_cadastro_horario_carrinha"
        return "v24_cadastro_edicao_texto"

    def _edit_prompt(self, field, ctx):
        return {
            "quantidade": self._quantidade_prompt(ctx),
            "nome_cliente": "Qual é o nome do cliente?",
            "telefone": "Qual é o telefone do cliente?",
            "data_entrega": self._data_prompt().replace("3. Outra data", "ou envie DD/MM/AAAA"),
            "hora_entrega": self._horario_carrinha_prompt(),
            "tipo_residuo": self._residuo_prompt({"residuos": [], "quantidade": 1, "tipo_solicitacao": ctx.get("tipo_solicitacao")}),
            "mao_de_obra": self._mao_obra_prompt(),
            "valor_total": "Qual é o valor comercial total?",
            "status_pagamento": "O pedido já está pago?\n\n1. Sim, já está pago\n2. Não, pendente",
            "forma_pagamento": "Selecione a forma de pagamento:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
            "endereco": self._endereco_prompt(),
            "ponto_referencia": "Deseja informar algum ponto de referência?\n\n1. Sim\n2. Não",
        }[field]

    def _apply_atomic_edit(self, conversa, ctx, field, value, message=None):
        if field == "quantidade":
            if not str(value).isdigit() or not 1 <= int(value) <= 50:
                return "Informe uma quantidade entre 1 e 50."
            ctx["quantidade"] = int(value)
            self._ajustar_itens_quantidade(ctx)
        elif field == "nome_cliente":
            if message and message.contact_name:
                ctx["nome"] = message.contact_name.strip()
                phone = self._phone_from_message(message, "")
                if phone:
                    ctx["telefone"] = phone
            elif len(str(value).strip()) >= 2:
                ctx["nome"] = str(value).strip()
            else:
                return "Informe o nome completo do cliente."
        elif field == "telefone":
            phone = self._phone_from_message(message, value) if message else re.sub(r"\D", "", str(value))
            if not phone:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
        elif field == "data_entrega":
            ctx["data"] = value
        elif field == "hora_entrega":
            if not self._horario_valido(value):
                return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
            ctx["horario_agendado"] = value
            for item in ctx.get("itens") or []:
                item["horario_agendado"] = value
        elif field == "tipo_residuo":
            residue = self._parse_residue(value)
            if not residue:
                return self._residuo_prompt({"residuos": [], "quantidade": 1, "tipo_solicitacao": ctx.get("tipo_solicitacao")})
            ctx["residuos"] = [residue for _ in range(ctx.get("quantidade") or 1)]
            for item in ctx.get("itens") or []:
                item["residuo_contratado"] = residue
        elif field == "mao_de_obra":
            mao_obra = self._parse_mao_obra(value)
            if mao_obra is None:
                return self._mao_obra_prompt()
            ctx["precisa_mao_de_obra"] = mao_obra
        elif field == "valor_total":
            try:
                ctx["valor"] = str(float(str(value).replace(",", ".")))
            except ValueError:
                return "Valor inválido."
        elif field == "status_pagamento":
            if value in {"1", "sim", "sim, ja esta pago"}:
                ctx["pago"] = True
                ctx["editing_field"] = "forma_pagamento"
                return self._advance(conversa, "v24_cadastro_edicao_opcao", ctx, self._edit_prompt("forma_pagamento", ctx))
            if value in {"2", "nao", "nao, pendente"}:
                ctx["pago"] = False
                ctx["forma"] = None
            else:
                return "Selecione uma das opções de pagamento."
        elif field == "forma_pagamento":
            forma = self._parse_forma_pagamento(value)
            if not forma:
                return "Selecione uma forma de pagamento."
            ctx["forma"] = forma
        elif field == "endereco":
            if message and message.tipo == "location" and not self._coordinates(message, str(value)):
                return "Não foi possível ler a localização. Reenvie a localização nativa ou digite o endereço."
            if not str(value).strip() or len(str(value).strip()) > 300:
                return "O endereço precisa ter entre 1 e 300 caracteres."
            ctx["endereco"] = str(value).strip()
            coords = self._coordinates(message, str(value)) if message else self._coordinates(None, str(value))
            if coords:
                ctx["endereco_latitude"], ctx["endereco_longitude"] = coords
        elif field == "ponto_referencia":
            ctx["referencia"] = None
        elif field == "ponto_referencia_texto":
            if not 1 <= len(str(value).strip()) <= 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia"] = str(value).strip()
        else:
            return self._corrigir_prompt(ctx)
        ctx.pop("editing_field", None)
        ctx.pop("_confirmado", None)
        return self._advance(conversa, "v24_cadastro_confirmacao", ctx, self._format_confirmacao_cadastro(ctx))

    def _ajustar_itens_quantidade(self, ctx):
        itens = list(ctx.get("itens") or [])
        quantidade = ctx.get("quantidade") or len(itens)
        if not itens:
            return
        if len(itens) > quantidade:
            ctx["itens"] = itens[:quantidade]
            ctx["residuos"] = [item.get("residuo_contratado") for item in ctx["itens"]]
            return
        while len(itens) < quantidade:
            itens.append(dict(itens[-1]))
        ctx["itens"] = itens
        ctx["residuos"] = [item.get("residuo_contratado") for item in itens]

    def _parse_residue(self, choice):
        return {
            "1": "Entulho Limpo",
            "option_1": "Entulho Limpo",
            "pedido_residuo_limpo": "Entulho Limpo",
            "entulho limpo": "Entulho Limpo",
            "limpo": "Entulho Limpo",
            "2": "Entulho Misto",
            "option_2": "Entulho Misto",
            "pedido_residuo_misto": "Entulho Misto",
            "entulho misto": "Entulho Misto",
            "misto": "Entulho Misto",
        }.get(choice)

    def _parse_forma_pagamento(self, choice):
        return {
            "1": "MBWay",
            "mbway": "MBWay",
            "2": "Transferência",
            "transferencia": "Transferência",
            "3": "Dinheiro",
            "dinheiro": "Dinheiro",
            "4": "Outro",
            "outro": "Outro",
        }.get(choice)

    def _parse_mao_obra(self, choice):
        return {
            "1": True,
            "option_1": True,
            "pedido_mao_obra_sim": True,
            "sim": True,
            "✅ sim": True,
            "sim, com pessoal": True,
            "com pessoal": True,
            "2": False,
            "option_2": False,
            "pedido_mao_obra_nao": False,
            "nao": False,
            "não": False,
            "❌ nao": False,
            "nao, apenas equipamento": False,
            "não, apenas equipamento": False,
            "apenas equipamento": False,
        }.get(choice)

    def _pedido_entrega_label(self, pedido) -> str:
        pendentes = self._itens_aguardando_chegada_ou_entrega(pedido)
        tipos = self._pedido_entrega_tipos(pedido)
        if tipos == {TipoEquipamentoPedido.CARRINHA.value}:
            tipo = "Carrinha"
        elif tipos == {TipoEquipamentoPedido.CONTENTOR.value}:
            tipo = "Contentor"
        else:
            tipo = "Equipamento"
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        return f"#{pedido.id} — {pedido.nome_cliente} — {tipo} x{len(pendentes)} — {data}"

    def _pedido_entrega_option(self, pedido, index: int) -> str:
        pendentes = self._itens_aguardando_chegada_ou_entrega(pedido)
        tipos = self._pedido_entrega_tipos(pedido)
        if tipos == {TipoEquipamentoPedido.CARRINHA.value}:
            tipo = "Carrinha"
        elif tipos == {TipoEquipamentoPedido.CONTENTOR.value}:
            tipo = "Contentor"
        else:
            tipo = "Equipamento"
        quantidade = len(pendentes)
        plural = "equipamento" if quantidade == 1 else "equipamentos"
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        return (
            f"{index}. {pedido.nome_cliente}\n"
            f"   Quantidade: {quantidade} {plural}\n"
            f"   Tipo: {tipo} x{quantidade} - {data}\n"
            f"   ID: entrega_pedido:{pedido.id}"
        )

    def _pedido_entrega_tipos(self, pedido) -> set[str]:
        return {
            item.tipo_equipamento
            for item in self._itens_aguardando_chegada_ou_entrega(pedido)
        }

    def _pedido_recolha_label(self, pedido) -> str:
        pendentes = self._recolha_pendentes(pedido)
        contentores = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value]
        carrinhas = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value]
        partes = []
        if contentores:
            partes.append(", ".join(self._equipamento_label(item) for item in contentores))
        if carrinhas:
            partes.append(", ".join(self._equipamento_label(item) for item in carrinhas))
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        resumo = " | ".join(partes) if partes else "sem ativos pendentes"
        return f"#{pedido.id} - {pedido.nome_cliente} - {resumo} - {data}"

    def _pedido_despejo_label(self, pedido) -> str:
        pendentes = self._despejo_pendentes(pedido)
        contentores = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value]
        carrinhas = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value]
        partes = []
        if contentores:
            partes.append(f"{len(contentores)} Contentor(es)")
        if carrinhas:
            partes.append(f"{len(carrinhas)} Carrinha(s)")
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        resumo = " e ".join(partes) if partes else "sem ativos"
        return f"#{pedido.id} - {pedido.nome_cliente} - {resumo} aguardando despejo - {data}"

    def _equipamento_label(self, contentor: PedidoContentor) -> str:
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            horario = contentor.horario_agendado or "sem horario"
            frota = contentor.frota_carrinha
            detalhe = f"frota {frota}" if frota else horario
            return f"🚛 Carrinha ({detalhe})"
        return f"📦 Contentor {contentor.numero_adesivo_contentor or contentor.id}"

    @staticmethod
    def _itens_aguardando_chegada_ou_entrega(pedido):
        return [
            item
            for item in pedido.contentores
            if (
                item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
                and item.status_entrega == "PENDENTE"
            )
            or (
                item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and item.status_operacional_carrinha
                == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
            )
        ]

    def _entrega_numero_prompt(self, ctx):
        contentor = self.db.get(PedidoContentor, ctx["contentores"][ctx["indice"]])
        if contentor and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            return "Confirme o número da frota da carrinha alocada (ou digite 0 se não houver):"
        return "Digite o número do contentor que está a descarregar agora:"

    def _contexto_entrega_tem_tipo(self, ctx, tipo_equipamento: str) -> bool:
        item_ids = list(ctx.get("contentores") or [])
        item_ids.extend(
            entrega.get("contentor_id")
            for entrega in (ctx.get("entregas") or [])
            if entrega.get("contentor_id")
        )
        for item_id in set(item_ids):
            item = self.db.get(PedidoContentor, item_id)
            if item and item.tipo_equipamento == tipo_equipamento:
                return True
        pedido_id = ctx.get("pedido_id")
        pedido = self.service.get(pedido_id) if pedido_id else None
        return bool(
            pedido
            and any(
                item.tipo_equipamento == tipo_equipamento
                for item in pedido.contentores
            )
        )

    @staticmethod
    def _contentor_entrega_desabilitada_message() -> str:
        return "A entrega de Contentor não está habilitada. A operação foi cancelada com segurança."

    @staticmethod
    def _carrinha_chegada_desabilitada_message() -> str:
        return "A chegada de Carrinha não está habilitada. A operação foi cancelada com segurança."

    def _tipo_equipamento_prompt(self, ctx):
        index = len(ctx.get("itens") or []) + 1
        return f"Tipo de equipamento do item {index}/{ctx['quantidade']}:\n\n1. 📦 Contentor\n2. 🚛 Carrinha"

    def _parse_tipo_solicitacao(self, choice):
        return {
            "1": TipoEquipamentoPedido.CONTENTOR.value,
            "contentor": TipoEquipamentoPedido.CONTENTOR.value,
            "contentores": TipoEquipamentoPedido.CONTENTOR.value,
            "2": TipoEquipamentoPedido.CARRINHA.value,
            "carrinha": TipoEquipamentoPedido.CARRINHA.value,
            "carrinhas": TipoEquipamentoPedido.CARRINHA.value,
        }.get(choice)

    def _modalidades_habilitadas(self):
        settings = get_settings()
        return (
            settings.feature_contentores_enabled,
            settings.feature_carrinhas_enabled,
        )

    def _selecionar_tipo_solicitacao(self, conversa, ctx, tipo):
        ctx["tipo_solicitacao"] = tipo
        if tipo == TipoEquipamentoPedido.CARRINHA.value:
            return self._advance(
                conversa,
                "v24_cadastro_quantidade",
                ctx,
                self._quantidade_prompt(ctx),
            )
        return self._advance(
            conversa,
            "v24_cadastro_nome",
            ctx,
            "Qual é o nome do cliente?",
        )

    def _tipo_solicitacao_prompt(self):
        return "🚛 Qual é o tipo de solicitação?\n\n1️⃣ Contentor\n2️⃣ Carrinha"

    def _horario_carrinha_prompt(self):
        return "Qual o horário agendado da carrinha? Envie no formato HH:MM. Ex: 14:00"

    def _data_prompt(self):
        return "Quando está planejada a entrega?\n\n1. Hoje\n2. Amanhã\n3. Outra data"

    def _quantidade_prompt(self, ctx):
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            return "🔢 Quantas carrinhas são necessárias para este pedido?"
        return "🔢 Quantos contentores são necessários para este pedido?"

    def _mao_obra_prompt(self):
        return (
            "O cliente solicitou pessoal para carregamento do resíduo?\n\n"
            "1. Sim, com pessoal\n"
            "2. Não, apenas equipamento"
        )

    def _residuo_prompt(self, ctx):
        index = len(ctx["residuos"]) + 1
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            equipamento = f"da carrinha {index}/{ctx['quantidade']}"
        else:
            equipamento = f"do contentor {index}/{ctx['quantidade']}"
        return f"Resíduo {equipamento}:\n\n1. 🟢 Entulho Limpo\n2. 🟠 Entulho Misto"

    def _format_confirmacao_cadastro(self, ctx):
        tipo = self._tipo_label(ctx.get("tipo_solicitacao"))
        mao_obra = "Sim" if ctx.get("precisa_mao_de_obra") else "Não"
        quantidade_label = (
            "Quantidade de carrinhas"
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value
            else "Quantidade de contentores"
        )
        data = datetime.fromisoformat(ctx["data"]).strftime("%d/%m/%Y") if ctx.get("data") else "Não informada"
        residuos = ", ".join(ctx.get("residuos") or []) or "Não informado"
        referencia = ctx.get("referencia") or "Não informado"
        linhas = [
            "Confirme os dados do pedido:",
            "",
            f"Tipo da solicitação: {tipo}",
            f"{quantidade_label}: {ctx.get('quantidade')}",
            f"Cliente: {ctx.get('nome')}",
            f"Telefone: {ctx.get('telefone')}",
            f"Dia da entrega: {data}",
        ]
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            linhas.append(f"Hora da entrega: {ctx.get('horario_agendado') or 'sem horário'}")
        linhas.extend(
            [
                f"Tipo de resíduo: {residuos}",
                f"Pessoal para carregamento: {mao_obra}",
                f"Valor total: {self._format_money(ctx.get('valor'))}",
                f"Status do pagamento: {'Pago' if ctx.get('pago') else 'Pendente'}",
            ]
        )
        if ctx.get("pago"):
            linhas.append(f"Forma de pagamento: {ctx.get('forma') or 'Não informada'}")
        linhas.extend(
            [
                f"Endereço: {ctx.get('endereco')}",
                f"Ponto de referência: {referencia}",
                "",
                "1. Confirmar e salvar",
                "2. Corrigir",
                "3. Cancelar",
            ]
        )
        return "\n".join(linhas)

    def cadastro_confirmacao_prompt(self, context):
        """Reutiliza o resumo legado sem persistir ou alterar o contexto."""
        return self._format_confirmacao_cadastro(context)

    def cadastro_corrigir_decision(self, choice, context):
        """Expõe somente a leitura legada do menu de correções."""
        field = self._parse_corrigir_field(choice, context)
        if not field:
            return None, None, self._corrigir_prompt(context)
        return field, self._edit_state_for(field), self._edit_prompt(field, context)

    def _tipo_label(self, tipo):
        return "Carrinha" if tipo == TipoEquipamentoPedido.CARRINHA.value else "Contentor"

    def _endereco_prompt(self):
        return "Informe o endereço aproximado (até 300 caracteres) ou envie um link do Google Maps."

    def _format_money(self, value):
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0.00")
        formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        return f"{formatted} €"

    def apply_operational_transition(
        self,
        conversa: ConversaWhatsApp,
        transition: object,
    ) -> str:
        from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition

        if isinstance(transition, AdvanceTransition):
            return self._advance(
                conversa,
                transition.next_state,
                transition.context,
                transition.response,
            )
        if isinstance(transition, IdleTransition):
            return self._idle(conversa, transition.response)
        raise TypeError("Transição operacional inválida.")

    def _advance(self, conversa, state, ctx, response):
        if state == "v24_cadastro_tipo_solicitacao":
            response = self._tipo_solicitacao_prompt()
        elif state == "v24_cadastro_quantidade":
            response = self._quantidade_prompt(ctx)
        elif state == "v24_cadastro_tipo_equipamento" and ctx.get("tipo_solicitacao"):
            if (
                "precisa_mao_de_obra" in ctx
                and ctx["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value
                and not ctx.get("horario_agendado")
            ):
                state = "v24_cadastro_horario_carrinha"
                response = self._horario_carrinha_prompt()
            elif "precisa_mao_de_obra" in ctx:
                state = "v24_cadastro_residuo"
                response = self._residuo_prompt(ctx)
            elif ctx["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value:
                state = "v24_cadastro_mao_obra"
                response = self._mao_obra_prompt()
            else:
                state = "v24_cadastro_mao_obra"
                response = self._mao_obra_prompt()
        elif state == "v24_cadastro_residuo" and ctx.get("tipo_solicitacao"):
            response = self._residuo_prompt(ctx)
        conversa.estado_atual = state
        conversa.contexto_json = ctx
        self.db.commit()
        return response

    def _idle(self, conversa, response):
        self._aplicar_idle(conversa)
        self.db.commit()
        return response

    def _aplicar_idle(self, conversa):
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}

    def _selected_id(self, raw, ids):
        value = self._norm(raw)
        if value.isdigit():
            number = int(value)
            if 1 <= number <= len(ids):
                return ids[number - 1]
            if number in ids:
                return number
        match = re.search(r"#?(\d+)", value)
        return int(match.group(1)) if match and int(match.group(1)) in ids else None

    def _selected_entrega_pedido_id(self, raw, ids):
        value = self._norm(raw)
        match = re.fullmatch(r"entrega_pedido:(\d+)", value)
        if match:
            pedido_id = int(match.group(1))
            return pedido_id if pedido_id in ids else None
        return self._selected_id(raw, ids)

    def _photo(self, message):
        return message.media_id or message.filename or message.message_id if message.tipo == "image" else None

    def _phone_from_message(self, message, raw):
        value = message.contact_phone or raw
        phone = re.sub(r"\D", "", value or "")
        return phone if len(phone) >= 9 else None

    def _coordinates(self, message, raw):
        if message and message.tipo == "location" and message.latitude is not None and message.longitude is not None:
            return float(message.latitude), float(message.longitude)
        match = re.search(r"(?:q=|@|!3d)(-?\d{1,2}\.\d+)[,!3d]*[,\s!4d]+(-?\d{1,3}\.\d+)", raw)
        return (float(match.group(1)), float(match.group(2))) if match else None

    def _norm(self, value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(c for c in normalized if not unicodedata.combining(c)).strip().lower()

    def _horario_valido(self, value):
        if not re.fullmatch(r"\d{2}:\d{2}", (value or "").strip()):
            return False
        hour, minute = [int(part) for part in value.split(":")]
        return 0 <= hour <= 23 and 0 <= minute <= 59

    def _lisbon_timezone(self):
        try:
            return ZoneInfo("Europe/Lisbon")
        except ZoneInfoNotFoundError:
            return timezone.utc
