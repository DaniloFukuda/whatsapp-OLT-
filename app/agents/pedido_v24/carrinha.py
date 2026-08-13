"""Decisões puras da Chegada moderna de Carrinha."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition


@dataclass(frozen=True)
class ConfirmarChegadaCarrinha:
    context: dict[str, Any]


@dataclass(frozen=True)
class ConfirmarPartidaCarrinha:
    context: dict[str, Any]


@dataclass(frozen=True)
class CancelarPartidaCarrinha:
    context: dict[str, Any]


@dataclass(frozen=True)
class PrepararConfirmacaoPartidaCarrinha:
    context: dict[str, Any]
    response_prefix: str = ""


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

    def select_partida_pedido(self, conversa, selection):
        if selection is None or not selection["pedido_exists"]:
            return "Selecione um pedido da lista."
        if not selection["carrinha_ids"]:
            return IdleTransition("Esse pedido ja nao possui ativos pendentes de recolha.")
        ctx = dict(conversa.contexto_json or {})
        ctx.update({"pedido_id": selection["pedido_id"], "recolhas": []})
        ctx.update(selection["selection_context"])
        return AdvanceTransition("v24_recolha_ativo", ctx, selection["prompt"])

    def select_partida_ativo(self, conversa, selection):
        if selection is None:
            return None
        ctx = dict(conversa.contexto_json or {})
        ctx.update({
            "contentor_id": selection["contentor_id"],
            "fotos_recolha": [], "avariado": None, "relato_avaria": None,
        })
        return AdvanceTransition("v24_recolha_foto", ctx, selection["foto_prompt"])

    def decide_partida_foto(self, conversa, message):
        photo = message.media_id or message.filename or message.message_id if message.tipo == "image" else None
        if not photo:
            return "Envie uma imagem para continuar."
        ctx = dict(conversa.contexto_json or {})
        fotos = list(ctx.get("fotos_recolha") or [])
        if photo not in fotos:
            fotos.append(photo)
        ctx["fotos_recolha"] = fotos
        return AdvanceTransition("v24_recolha_foto_acao", ctx, "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo")

    def decide_partida_foto_acao(self, conversa, message, *, avarias_enabled):
        choice = self._normalize(message.texto)
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "outra foto", "➕ outra foto"}:
            return AdvanceTransition("v24_recolha_foto", ctx, "Envie a próxima foto.")
        if choice in {"2", "proximo passo", "➡️ proximo passo"}:
            if not avarias_enabled:
                ctx.pop("avariado", None); ctx.pop("relato_avaria", None)
                return PrepararConfirmacaoPartidaCarrinha(ctx)
            return AdvanceTransition("v24_recolha_avaria", ctx, "O equipamento sofreu algum estrago ou avaria na obra?\n\n1. ✅ Não, está perfeito\n2. 💥 Sim, está estragado")
        return "Selecione Outra Foto ou Próximo Passo."

    def decide_partida_avaria(self, conversa, message, *, avarias_enabled):
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled:
            return self._recover_disabled_partida_avaria(ctx)
        choice = self._normalize(message.texto)
        if choice in {"1", "nao, esta perfeito", "✅ nao, esta perfeito"}:
            ctx.update({"avariado": False, "relato_avaria": None})
            return PrepararConfirmacaoPartidaCarrinha(ctx)
        if choice in {"2", "sim, esta estragado", "💥 sim, esta estragado"}:
            ctx["avariado"] = True
            return AdvanceTransition("v24_recolha_relato", ctx, "Descreva a avaria com pelo menos 10 caracteres.")
        return "Selecione uma das opções de avaria."

    def decide_partida_relato(self, conversa, message, *, avarias_enabled):
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled:
            return self._recover_disabled_partida_avaria(ctx)
        relato = (message.texto or "").strip()
        if len(relato) < 10:
            return "O relato da avaria precisa ter pelo menos 10 caracteres."
        ctx.update({"avariado": True, "relato_avaria": relato})
        return PrepararConfirmacaoPartidaCarrinha(ctx)

    def decide_partida_confirmacao(self, conversa, message, *, avarias_enabled):
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled and (ctx.get("avariado") or ctx.get("relato_avaria")):
            return self._recover_disabled_partida_avaria(ctx)
        choice = self._normalize(message.texto)
        if choice in {"1", "confirmar recolha", "confirmar"}:
            return ConfirmarPartidaCarrinha(ctx)
        if choice in {"2", "cancelar ativo", "cancelar"}:
            return CancelarPartidaCarrinha(ctx)
        return "Escolha Confirmar recolha ou Cancelar ativo."

    @staticmethod
    def _recover_disabled_partida_avaria(ctx):
        ctx.pop("avariado", None); ctx.pop("relato_avaria", None)
        return PrepararConfirmacaoPartidaCarrinha(
            ctx,
            "A funcionalidade de avarias não está disponível nesta empresa. O subfluxo foi cancelado com segurança.\n\n",
        )

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(c for c in normalized if not unicodedata.combining(c)).strip().lower()
