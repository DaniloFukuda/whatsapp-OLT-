"""Decisoes puras do cadastro de novos pedidos de Contentor."""

import unicodedata
from enum import Enum
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition
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
        normalized = unicodedata.normalize("NFKD", value or "")
        choice = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        ).strip().lower()
        return choice in ContentorCadastroAgent._CONTENTOR_ALIASES

    def decide_tipo_solicitacao(self, context: dict[str, Any] | None) -> AdvanceTransition:
        ctx = dict(context or {})
        ctx["tipo_solicitacao"] = TipoEquipamentoPedido.CONTENTOR.value
        return AdvanceTransition(
            "v24_cadastro_nome",
            ctx,
            "Qual é o nome do cliente?",
        )
