"""Decisoes puras do cadastro de novos pedidos de Contentor."""

import re
import unicodedata
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.pedido import TipoEquipamentoPedido


class CadastroModality(Enum):
    """Classificacao conservadora da modalidade no contexto de cadastro."""

    CONTENTOR_INTENT = "CONTENTOR_INTENT"
    CONTENTOR_PROVEN = "CONTENTOR_PROVEN"
    CARRINHA = "CARRINHA"
    LEGACY_INDETERMINATE = "LEGACY_INDETERMINATE"
    DIVERGENT = "DIVERGENT"


@dataclass(frozen=True)
class ConfirmarCadastroContentor:
    """Solicita confirmação final do cadastro moderno de Contentor."""

    context: dict[str, Any]


def classify_cadastro_modality(context: dict[str, Any] | None) -> CadastroModality:
    """Classifica somente evidencias explicitas, sem consultar ou alterar estado."""
    ctx = context or {}
    tipo = ctx.get("tipo_solicitacao")
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        return CadastroModality.CARRINHA
    if tipo != TipoEquipamentoPedido.CONTENTOR.value:
        return CadastroModality.LEGACY_INDETERMINATE

    item_atual = ctx.get("item_atual")
    if item_atual is not None and not isinstance(item_atual, dict):
        return CadastroModality.LEGACY_INDETERMINATE
    item_atual_tipo = item_atual.get("tipo_equipamento") if item_atual else None
    if item_atual and item_atual_tipo is None:
        return CadastroModality.LEGACY_INDETERMINATE

    itens_value = ctx.get("itens")
    if itens_value is not None and not isinstance(itens_value, list):
        return CadastroModality.LEGACY_INDETERMINATE
    itens = itens_value or []
    if any(
        not isinstance(item, dict) or item.get("tipo_equipamento") is None
        for item in itens
    ):
        return CadastroModality.LEGACY_INDETERMINATE
    tipos_itens = [item["tipo_equipamento"] for item in itens]
    sinais = [sinal for sinal in [item_atual_tipo, *tipos_itens] if sinal is not None]
    tipos_validos = {
        TipoEquipamentoPedido.CONTENTOR.value,
        TipoEquipamentoPedido.CARRINHA.value,
    }
    if any(sinal not in tipos_validos for sinal in sinais):
        return CadastroModality.DIVERGENT
    if TipoEquipamentoPedido.CARRINHA.value in sinais:
        return CadastroModality.DIVERGENT

    quantidade = ctx.get("quantidade")
    if (
        isinstance(quantidade, int)
        and not isinstance(quantidade, bool)
        and quantidade > 0
        and len(itens) == quantidade
        and item_atual is None
        and all(
            isinstance(item, dict)
            and item.get("tipo_equipamento") == TipoEquipamentoPedido.CONTENTOR.value
            for item in itens
        )
    ):
        return CadastroModality.CONTENTOR_PROVEN
    return CadastroModality.CONTENTOR_INTENT


class ContentorCadastroAgent:
    """Decide somente o ramo Contentor do cadastro, sem persistencia."""

    _CONTENTOR_ALIASES = {"1", "contentor", "contentores"}

    @staticmethod
    def is_contentor_selection(value: str | None) -> bool:
        return ContentorCadastroAgent._normalize(value) in (
            ContentorCadastroAgent._CONTENTOR_ALIASES
        )

    @staticmethod
    def is_contentor_item_selection(value: str | None) -> bool:
        return ContentorCadastroAgent._normalize(value) in {"1", "contentor"}

    def decide_tipo_solicitacao(self, context: dict[str, Any] | None) -> AdvanceTransition:
        ctx = dict(context or {})
        ctx["tipo_solicitacao"] = TipoEquipamentoPedido.CONTENTOR.value
        return AdvanceTransition(
            "v24_cadastro_nome",
            ctx,
            "Qual é o nome do cliente?",
        )

    def decide_nome(self, context, message: NormalizedWhatsAppMessage):
        ctx = dict(context or {})
        raw = (message.texto or "").strip()
        if message.contact_name:
            name = message.contact_name.strip()
            if len(name) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = name
            phone = self._phone_from_message(message, "")
            if phone:
                ctx["telefone"] = phone
                return self._advance_to_quantidade(ctx)
        else:
            if len(raw) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = raw
        return AdvanceTransition(
            "v24_cadastro_telefone",
            ctx,
            "Qual é o telefone do cliente?",
        )

    def decide_telefone(self, context, message: NormalizedWhatsAppMessage):
        ctx = dict(context or {})
        raw = (message.texto or "").strip()
        if message.contact_phone:
            phone = self._phone_from_message(message, raw)
            if not phone:
                return "O telefone informado não é válido."
        else:
            phone = re.sub(r"\D", "", raw)
            if len(phone) < 9:
                return "O telefone informado não é válido."
        ctx["telefone"] = phone
        return self._advance_to_quantidade(ctx)

    def decide_quantidade(self, context, message: NormalizedWhatsAppMessage):
        raw = (message.texto or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 50:
            return "Informe uma quantidade entre 1 e 50."
        ctx = dict(context or {})
        ctx["quantidade"] = int(raw)
        ctx["itens"] = []
        ctx["residuos"] = []
        return AdvanceTransition(
            "v24_cadastro_tipo_equipamento",
            ctx,
            self._tipo_equipamento_prompt(ctx),
        )

    def decide_tipo_equipamento(self, context):
        ctx = dict(context or {})
        ctx["item_atual"] = {
            "tipo_equipamento": TipoEquipamentoPedido.CONTENTOR.value,
            "horario_agendado": None,
        }
        return AdvanceTransition(
            "v24_cadastro_mao_obra",
            ctx,
            self._mao_obra_prompt(),
        )

    def decide_mao_obra(self, context, message: NormalizedWhatsAppMessage):
        choice = self._normalize(message.texto)
        mao_obra = {
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
            "❌ nao": False,
            "nao, apenas equipamento": False,
            "apenas equipamento": False,
        }.get(choice)
        if mao_obra is None:
            return self._mao_obra_prompt()
        ctx = dict(context or {})
        item_atual = dict(ctx.get("item_atual") or {})
        item_atual["tipo_equipamento"] = TipoEquipamentoPedido.CONTENTOR.value
        item_atual["precisa_mao_de_obra"] = mao_obra
        ctx["item_atual"] = item_atual
        ctx["precisa_mao_de_obra"] = mao_obra
        return AdvanceTransition(
            "v24_cadastro_residuo",
            ctx,
            self._residuo_prompt(ctx),
        )

    def decide_residuo(self, context, message: NormalizedWhatsAppMessage):
        residue = {
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
        }.get(self._normalize(message.texto))
        ctx = dict(context or {})
        if not residue:
            return self._residuo_prompt(ctx)
        item_atual = dict(ctx.get("item_atual") or {})
        item = {
            "tipo_equipamento": TipoEquipamentoPedido.CONTENTOR.value,
            "horario_agendado": None,
            "precisa_mao_de_obra": False,
        }
        if item_atual.get("tipo_equipamento"):
            item["tipo_equipamento"] = item_atual["tipo_equipamento"]
        if item_atual.get("horario_agendado"):
            item["horario_agendado"] = item_atual["horario_agendado"]
        item["residuo_contratado"] = residue
        ctx["itens"] = [*(ctx.get("itens") or []), item]
        ctx["residuos"] = [*(ctx.get("residuos") or []), residue]
        ctx.pop("item_atual", None)
        if len(ctx["itens"]) < ctx["quantidade"]:
            return AdvanceTransition(
                "v24_cadastro_tipo_equipamento",
                ctx,
                self._tipo_equipamento_prompt(ctx),
            )
        return AdvanceTransition(
            "v24_cadastro_data",
            ctx,
            self._data_prompt(),
        )

    def decide_data(
        self,
        context,
        message: NormalizedWhatsAppMessage,
        now: datetime,
    ):
        choice = self._normalize(message.texto)
        if choice in {"1", "hoje"}:
            planned = now
        elif choice in {"2", "amanha"}:
            planned = now + timedelta(days=1)
        elif choice in {"3", "outra data"}:
            return AdvanceTransition(
                "v24_cadastro_data_manual",
                dict(context or {}),
                "Informe a data no formato DD/MM/AAAA.",
            )
        else:
            return "Selecione Hoje, Amanhã ou Outra data."
        ctx = dict(context or {})
        ctx["data"] = planned.isoformat()
        return AdvanceTransition(
            "v24_cadastro_valor",
            ctx,
            "Qual é o valor global do pedido?",
        )

    def decide_data_manual(
        self,
        context,
        message: NormalizedWhatsAppMessage,
        timezone,
    ):
        raw = (message.texto or "").strip()
        try:
            planned = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=timezone)
        except ValueError:
            return "Data inválida. Use o formato DD/MM/AAAA."
        ctx = dict(context or {})
        ctx["data"] = planned.isoformat()
        return AdvanceTransition(
            "v24_cadastro_valor",
            ctx,
            "Qual é o valor global do pedido?",
        )

    def decide_valor(self, context, message: NormalizedWhatsAppMessage):
        raw = (message.texto or "").strip()
        try:
            valor = str(float(raw.replace(",", ".")))
        except ValueError:
            return "Valor inválido."
        ctx = dict(context or {})
        ctx["valor"] = valor
        return AdvanceTransition(
            "v24_cadastro_pago",
            ctx,
            "O pedido já está pago?\n\n1. Sim, já está pago\n2. Não, pendente",
        )

    def decide_pago(self, context, message: NormalizedWhatsAppMessage):
        choice = self._normalize(message.texto)
        ctx = dict(context or {})
        if choice in {"1", "sim", "sim, ja esta pago"}:
            ctx["pago"] = True
            return AdvanceTransition(
                "v24_cadastro_forma",
                ctx,
                "Selecione a forma de pagamento:\n\n"
                "1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
            )
        if choice in {"2", "nao", "nao, pendente"}:
            ctx["pago"] = False
            ctx["forma"] = None
            return AdvanceTransition(
                "v24_cadastro_endereco",
                ctx,
                self._endereco_prompt(),
            )
        return "Selecione uma das opções de pagamento."

    def decide_forma(self, context, message: NormalizedWhatsAppMessage):
        choice = self._normalize(message.texto)
        forma = {
            "1": "MBWay",
            "mbway": "MBWay",
            "2": "Transferência",
            "transferencia": "Transferência",
            "3": "Dinheiro",
            "dinheiro": "Dinheiro",
            "4": "Outro",
            "outro": "Outro",
        }.get(choice)
        if not forma:
            return "Selecione uma forma de pagamento."
        ctx = dict(context or {})
        if forma == "Outro":
            return AdvanceTransition(
                "v24_cadastro_forma_outro",
                ctx,
                "Qual foi a forma de pagamento?",
            )
        ctx["forma"] = forma
        return AdvanceTransition(
            "v24_cadastro_endereco",
            ctx,
            self._endereco_prompt(),
        )

    def decide_forma_outro(self, context, message: NormalizedWhatsAppMessage):
        raw = (message.texto or "").strip()
        if not raw:
            return "Informe a forma de pagamento."
        ctx = dict(context or {})
        ctx["forma"] = raw[:80]
        return AdvanceTransition(
            "v24_cadastro_endereco",
            ctx,
            self._endereco_prompt(),
        )

    def decide_endereco(self, context, message, coordinates):
        raw = (message.texto or "").strip()
        if message.tipo == "location" and not coordinates:
            return (
                "Não foi possível ler a localização. Reenvie a localização "
                "nativa ou digite o endereço."
            )
        if not raw or len(raw) > 300:
            return "O endereço precisa ter entre 1 e 300 caracteres."
        ctx = dict(context or {})
        ctx["endereco"] = raw
        if coordinates:
            ctx["endereco_latitude"], ctx["endereco_longitude"] = coordinates
        return AdvanceTransition(
            "v24_cadastro_referencia_opcao",
            ctx,
            "Deseja informar um ponto de referência?\n\n1. Sim\n2. Não",
        )

    def decide_referencia_opcao(self, context, message):
        choice = self._normalize(message.texto)
        ctx = dict(context or {})
        if choice in {"1", "sim"}:
            return AdvanceTransition(
                "v24_cadastro_referencia",
                ctx,
                "Qual é o ponto de referência?",
            )
        if choice in {"2", "nao"}:
            ctx["referencia"] = None
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        return "Selecione Sim ou Não."

    def decide_referencia(self, context, message):
        raw = (message.texto or "").strip()
        if not 1 <= len(raw) <= 50:
            return "O ponto de referência deve ter no máximo 50 caracteres."
        ctx = dict(context or {})
        ctx["referencia"] = raw
        return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")

    def decide_confirmacao(self, context, message):
        choice = self._normalize(message.texto)
        if choice in {"1", "sim", "confirmar", "confirmar e salvar"}:
            return ConfirmarCadastroContentor(dict(context or {}))
        if choice in {"2", "corrigir"}:
            return AdvanceTransition("v24_cadastro_corrigir", dict(context or {}), "")
        if choice in {"3", "cancelar"}:
            return IdleTransition("Pedido cancelado. Nenhum pedido foi criado.")
        return "Escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar."

    def decide_corrigir(self, context, field, next_state, prompt):
        if not field:
            return prompt
        ctx = dict(context or {})
        if field == "forma_pagamento" and not ctx.get("pago"):
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        ctx["editing_field"] = field
        return AdvanceTransition(next_state, ctx, prompt)

    def decide_edicao(self, context, message, *, coordinates=None, now=None):
        ctx = dict(context or {})
        field = ctx.get("editing_field")
        raw = (message.texto or "").strip()
        choice = self._normalize(raw)
        if field == "quantidade":
            if not raw.isdigit() or not 1 <= int(raw) <= 50:
                return "Informe uma quantidade entre 1 e 50."
            ctx["quantidade"] = int(raw)
            self._ajustar_itens_quantidade(ctx)
        elif field == "nome_cliente":
            if message.contact_name:
                ctx["nome"] = message.contact_name.strip()
                phone = self._phone_from_message(message, "")
                if phone:
                    ctx["telefone"] = phone
            elif len(raw) >= 2:
                ctx["nome"] = raw
            else:
                return "Informe o nome completo do cliente."
        elif field == "telefone":
            phone = self._phone_from_message(message, raw)
            if not phone:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
        elif field == "data_entrega":
            if choice in {"1", "hoje"}:
                value = now.isoformat()
            elif choice in {"2", "amanha"}:
                value = (now + timedelta(days=1)).isoformat()
            else:
                try:
                    value = datetime.strptime(raw, "%d/%m/%Y").replace(
                        tzinfo=now.tzinfo
                    ).isoformat()
                except ValueError:
                    return "Data inválida. Use Hoje, Amanhã ou DD/MM/AAAA."
            ctx["data"] = value
        elif field == "tipo_residuo":
            residue = self._parse_residue(choice)
            if not residue:
                return self._residuo_prompt(
                    {"residuos": [], "quantidade": 1}
                )
            ctx["residuos"] = [residue for _ in range(ctx.get("quantidade") or 1)]
            for item in ctx.get("itens") or []:
                item["residuo_contratado"] = residue
        elif field == "mao_de_obra":
            mao_obra = self._parse_mao_obra(choice)
            if mao_obra is None:
                return self._mao_obra_prompt()
            ctx["precisa_mao_de_obra"] = mao_obra
        elif field == "valor_total":
            try:
                ctx["valor"] = str(float(raw.replace(",", ".")))
            except ValueError:
                return "Valor inválido."
        elif field == "status_pagamento":
            if choice in {"1", "sim", "sim, ja esta pago"}:
                ctx["pago"] = True
                ctx["editing_field"] = "forma_pagamento"
                return AdvanceTransition("v24_cadastro_edicao_opcao", ctx, "")
            if choice in {"2", "nao", "nao, pendente"}:
                ctx["pago"] = False
                ctx["forma"] = None
            else:
                return "Selecione uma das opções de pagamento."
        elif field == "forma_pagamento":
            forma = self._parse_forma(choice)
            if not forma:
                return "Selecione uma forma de pagamento."
            ctx["forma"] = forma
        elif field == "endereco":
            if message.tipo == "location" and not coordinates:
                return (
                    "Não foi possível ler a localização. Reenvie a localização "
                    "nativa ou digite o endereço."
                )
            if not raw or len(raw) > 300:
                return "O endereço precisa ter entre 1 e 300 caracteres."
            ctx["endereco"] = raw
            if coordinates:
                ctx["endereco_latitude"], ctx["endereco_longitude"] = coordinates
        elif field == "ponto_referencia":
            ctx["referencia"] = None
        elif field == "ponto_referencia_texto":
            if not 1 <= len(raw) <= 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia"] = raw
        else:
            return None
        ctx.pop("editing_field", None)
        ctx.pop("_confirmado", None)
        return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")

    def decide_edicao_referencia_opcao(self, context, message):
        choice = self._normalize(message.texto)
        ctx = dict(context or {})
        if choice in {"1", "sim"}:
            ctx["editing_field"] = "ponto_referencia_texto"
            return AdvanceTransition(
                "v24_cadastro_edicao_texto",
                ctx,
                "Qual é o novo ponto de referência?",
            )
        if choice in {"2", "nao"}:
            ctx["referencia"] = None
            ctx.pop("editing_field", None)
            ctx.pop("_confirmado", None)
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        return "Selecione Sim ou Não."

    @staticmethod
    def _ajustar_itens_quantidade(context):
        itens = list(context.get("itens") or [])
        quantidade = context.get("quantidade") or len(itens)
        if not itens:
            return
        if len(itens) > quantidade:
            context["itens"] = itens[:quantidade]
        else:
            while len(itens) < quantidade:
                itens.append(dict(itens[-1]))
            context["itens"] = itens
        context["residuos"] = [
            item.get("residuo_contratado") for item in context["itens"]
        ]

    @staticmethod
    def _parse_residue(choice):
        return {
            "1": "Entulho Limpo", "option_1": "Entulho Limpo",
            "pedido_residuo_limpo": "Entulho Limpo", "entulho limpo": "Entulho Limpo",
            "limpo": "Entulho Limpo", "2": "Entulho Misto",
            "option_2": "Entulho Misto", "pedido_residuo_misto": "Entulho Misto",
            "entulho misto": "Entulho Misto", "misto": "Entulho Misto",
        }.get(choice)

    @staticmethod
    def _parse_mao_obra(choice):
        if choice in {"1", "option_1", "pedido_mao_obra_sim", "sim", "✅ sim", "sim, com pessoal", "com pessoal"}:
            return True
        if choice in {"2", "option_2", "pedido_mao_obra_nao", "nao", "❌ nao", "nao, apenas equipamento", "apenas equipamento"}:
            return False
        return None

    @staticmethod
    def _parse_forma(choice):
        return {"1": "MBWay", "mbway": "MBWay", "2": "Transferência", "transferencia": "Transferência", "3": "Dinheiro", "dinheiro": "Dinheiro", "4": "Outro", "outro": "Outro"}.get(choice)

    @staticmethod
    def _phone_from_message(message, raw):
        value = message.contact_phone or raw
        phone = re.sub(r"\D", "", value or "")
        return phone if len(phone) >= 9 else None

    @staticmethod
    def _advance_to_quantidade(context):
        return AdvanceTransition(
            "v24_cadastro_quantidade",
            context,
            "🔢 Quantos contentores são necessários para este pedido?",
        )

    @staticmethod
    def _tipo_equipamento_prompt(context):
        index = len(context.get("itens") or []) + 1
        return (
            f"Tipo de equipamento do item {index}/{context['quantidade']}:\n\n"
            "1. 📦 Contentor\n2. 🚛 Carrinha"
        )

    @staticmethod
    def _mao_obra_prompt():
        return (
            "O cliente solicitou pessoal para carregamento do resíduo?\n\n"
            "1. Sim, com pessoal\n"
            "2. Não, apenas equipamento"
        )

    @staticmethod
    def _residuo_prompt(context):
        index = len(context["residuos"]) + 1
        return (
            f"Resíduo do contentor {index}/{context['quantidade']}:\n\n"
            "1. 🟢 Entulho Limpo\n2. 🟠 Entulho Misto"
        )

    @staticmethod
    def _data_prompt():
        return "Quando está planejada a entrega?\n\n1. Hoje\n2. Amanhã\n3. Outra data"

    @staticmethod
    def _endereco_prompt():
        return (
            "Informe o endereço aproximado (até 300 caracteres) ou envie um "
            "link do Google Maps."
        )

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        ).strip().lower()
