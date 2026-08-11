"""Resolução conservadora e somente leitura da modalidade operacional."""

from collections.abc import Mapping

from app.models.pedido import TipoEquipamentoPedido


_KNOWN_MODALITIES = {
    TipoEquipamentoPedido.CONTENTOR.value: TipoEquipamentoPedido.CONTENTOR,
    TipoEquipamentoPedido.CARRINHA.value: TipoEquipamentoPedido.CARRINHA,
}


def resolve_operational_modality(
    context: Mapping | None,
) -> TipoEquipamentoPedido | None:
    """Retorna a modalidade somente quando todos os sinais explícitos concordam."""
    if not isinstance(context, Mapping):
        return None

    evidence: list[TipoEquipamentoPedido] = []
    invalid_explicit_type = False

    def collect(value) -> None:
        nonlocal invalid_explicit_type
        if value is None:
            return
        if not isinstance(value, str):
            invalid_explicit_type = True
            return
        modality = _KNOWN_MODALITIES.get(value)
        if modality is None:
            invalid_explicit_type = True
            return
        evidence.append(modality)

    collect(context.get("tipo_solicitacao"))
    collect(context.get("tipo_equipamento"))

    current_item = context.get("item_atual")
    if isinstance(current_item, Mapping) and "tipo_equipamento" in current_item:
        collect(current_item.get("tipo_equipamento"))

    items = context.get("itens")
    if isinstance(items, (list, tuple)):
        for item in items:
            if isinstance(item, Mapping) and "tipo_equipamento" in item:
                collect(item.get("tipo_equipamento"))

    if invalid_explicit_type or not evidence or len(set(evidence)) != 1:
        return None
    return evidence[0]
