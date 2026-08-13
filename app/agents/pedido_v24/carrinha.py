"""Decisões puras da Chegada moderna de Carrinha."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition


@dataclass(frozen=True)
class ConfirmarChegadaCarrinha:
    context: dict[str, Any]


class CarrinhaOperationalAgent:
    """Decide a Chegada de Carrinha sem acessar infraestrutura persistente."""

    def select_entrega_pedido(self, conversa, selection):
        if selection is None or not selection["pedido_exists"]:
            return "Selecione um pedido da lista."
        if not selection["carrinha_ids"]:
            return IdleTransition("Esse pedido já não possui ativos pendentes de entrega.")
        ctx = dict(conversa.contexto_json or {})
        ctx.update({
            "pedido_id": selection["pedido_id"],
            "contentores": list(selection["carrinha_ids"]),
            "indice": 0,
            "entregas": [],
        })
        return AdvanceTransition("v24_entrega_adesivo", ctx, selection["prompt"])

    def decide_frota(self, conversa, message, snapshot):
        ctx = dict(conversa.contexto_json or {})
        if message.tipo == "interactive":
            return "Digite o número físico do equipamento para continuar."
        number = (message.texto or "").strip()
        if snapshot["ativo_exists"] and not snapshot["is_carrinha"]:
            return None
        if not snapshot["ativo_exists"] or snapshot["status_operacional"] != "AGUARDANDO_CHEGADA":
            return IdleTransition("Esse ativo já não está pendente. Reinicie a entrega.")
        if not re.fullmatch(r"\d{1,6}", number):
            return "Informe o número da frota da carrinha ou 0 se não houver."
        if number != "0" and number in [str(item.get("numero_adesivo")) for item in ctx.get("entregas") or []]:
            return "Esse adesivo ja foi informado neste lote."
        entregas = list(ctx.get("entregas") or [])
        entregas.append({
            "contentor_id": ctx["contentores"][ctx["indice"]],
            "numero_adesivo": number,
            "fotos": [],
        })
        ctx["entregas"] = entregas
        return AdvanceTransition(
            "v24_entrega_foto",
            ctx,
            f"Envie a foto do Contentor {number} posicionado no local.",
        )

    def decide_foto(self, conversa, message):
        photo = message.media_id or message.filename or message.message_id if message.tipo == "image" else None
        if not photo:
            return "Envie uma imagem para continuar."
        ctx = dict(conversa.contexto_json or {})
        entregas = list(ctx.get("entregas") or [])
        atual = dict(entregas[-1])
        fotos = list(atual.get("fotos") or [])
        if photo not in fotos:
            fotos.append(photo)
        atual["fotos"] = fotos
        entregas[-1] = atual
        ctx["entregas"] = entregas
        return AdvanceTransition("v24_entrega_foto_acao", ctx, "Foto guardada. O que deseja fazer?\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo")

    def decide_foto_acao(self, conversa, message):
        choice = self._normalize(message.texto)
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "outra foto", "➕ outra foto"}:
            return AdvanceTransition("v24_entrega_foto", ctx, "Envie a próxima foto deste contentor.")
        if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
            return "Selecione Outra Foto ou Próximo Passo."
        registrado = ctx["indice"] + 1
        total = len(ctx["contentores"])
        ctx["indice"] += 1
        if ctx["indice"] < total:
            return AdvanceTransition(
                "v24_entrega_adesivo", ctx,
                f"Contentor {registrado} de {total} registrado.\n\nVamos registrar o próximo.\n\n"
                "Confirme o número da frota da carrinha alocada (ou digite 0 se não houver):",
            )
        return AdvanceTransition(
            "v24_entrega_gps", ctx,
            f"Contentor {registrado} de {total} registrado.\n\nCompartilhe a localização GPS da obra.",
        )

    def decide_gps(self, conversa, message):
        coords = None
        if message.tipo == "location":
            if message.latitude is not None and message.longitude is not None:
                coords = float(message.latitude), float(message.longitude)
            else:
                match = re.search(r"(?:q=|@|!3d)(-?\d{1,2}\.\d+)[,!3d]*[,\s!4d]+(-?\d{1,3}\.\d+)", message.texto or "")
                if match:
                    coords = float(match.group(1)), float(match.group(2))
        if not coords:
            return "Compartilhe a localização nativa do WhatsApp para confirmar a entrega."
        ctx = dict(conversa.contexto_json or {})
        ctx["latitude"], ctx["longitude"] = coords
        return AdvanceTransition("v24_entrega_referencia_opcao", ctx, "Deseja informar algum ponto de referência para a entrega?\n\n1. Sim\n2. Não")

    def decide_referencia_opcao(self, conversa, message, confirm_prompt):
        choice = self._normalize(message.texto)
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "sim", "entrega_referencia:sim"}:
            return AdvanceTransition("v24_entrega_referencia", ctx, "Digite o ponto de referência.")
        if choice in {"2", "nao", "entrega_referencia:nao"}:
            ctx["referencia_entrega"] = None
            return AdvanceTransition("v24_entrega_confirmacao", ctx, confirm_prompt)
        return "Selecione Sim ou Não."

    def decide_referencia(self, conversa, message, confirm_prompt):
        raw = (message.texto or "").strip()
        if not 1 <= len(raw) <= 50:
            return "O ponto de referência deve ter no máximo 50 caracteres."
        ctx = dict(conversa.contexto_json or {})
        ctx["referencia_entrega"] = raw
        return AdvanceTransition("v24_entrega_confirmacao", ctx, confirm_prompt)

    def decide_confirmacao(self, conversa, message):
        choice = self._normalize(message.texto)
        if choice in {"1", "confirmar entrega", "✅ confirmar entrega"}:
            return ConfirmarChegadaCarrinha(dict(conversa.contexto_json or {}))
        if choice in {"2", "cancelar", "❌ cancelar"}:
            return IdleTransition("Entrega cancelada. Nenhum ativo foi marcado como entregue.")
        return "Escolha Confirmar entrega ou Cancelar."

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(c for c in normalized if not unicodedata.combining(c)).strip().lower()
