"""Decisoes puras do cadastro de novos pedidos de Contentor."""

import re
import unicodedata
from enum import Enum
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.pedido import TipoEquipamentoPedido


class CadastroModality(Enum):
    """Classificacao conservadora da modalidade no contexto de cadastro."""

    CONTENTOR_INTENT = "CONTENTOR_INTENT"
    CONTENTOR_PROVEN = "CONTENTOR_PROVEN"
    CARRINHA = "CARRINHA"
    LEGACY_INDETERMINATE = "LEGACY_INDETERMINATE"
    DIVERGENT = "DIVERGENT"


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
    def _normalize(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        ).strip().lower()
