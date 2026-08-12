"""Entrada operacional de entrega de Contentor no fluxo V2.4."""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import PedidoContentor, TipoEquipamentoPedido


@dataclass(frozen=True)
class ConfirmEntregaContentor:
    """Comando específico para o boundary legado de confirmação."""

    context: dict[str, Any]


@dataclass(frozen=True)
class RegistrarPagamentoEntregaContentor:
    """Solicita ao boundary legado o registro financeiro da entrega."""

    context: dict[str, Any]
    forma: str


@dataclass(frozen=True)
class PrepararConfirmacaoRecolhaContentor:
    """Solicita ao backend somente o prompt legado de confirmacao."""

    context: dict[str, Any]
    response_prefix: str = ""


@dataclass(frozen=True)
class ConfirmarRecolhaContentor:
    """Solicita ao boundary legado a confirmacao da recolha."""

    context: dict[str, Any]


@dataclass(frozen=True)
class CancelarRecolhaContentor:
    """Solicita ao boundary legado o cancelamento da recolha atual."""

    context: dict[str, Any]


@dataclass(frozen=True)
class PrepararConfirmacaoDespejoContentor:
    """Solicita ao backend somente o prompt legado de confirmação do despejo."""

    context: dict[str, Any]


@dataclass(frozen=True)
class PrepararFotoDespejoContentor:
    """Solicita ao backend somente o prompt legado da foto do despejo."""

    context: dict[str, Any]


@dataclass(frozen=True)
class ConfirmarDespejoContentor:
    """Solicita ao boundary legado a confirmação persistente do despejo."""

    context: dict[str, Any]


@dataclass(frozen=True)
class PrepararConformidadeDespejoContentor:
    """Solicita ao backend o prompt legado ao voltar da confirmação."""

    context: dict[str, Any]


class ContentorOperationalAgent:
    """Carrega Contentores pendentes e entrega a composição ao legado."""

    def __init__(
        self,
        pedidos_pendentes_entrega: Callable[[], list[Any]],
        legacy_backend: Any,
    ):
        self._pedidos_pendentes_entrega = pedidos_pendentes_entrega
        self._legacy_backend = legacy_backend

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        pedidos_contentor = self._pedidos_pendentes_entrega()
        return self._legacy_backend._start_entrega_com_pedidos_contentor(
            conversa,
            pedidos_contentor,
        )

    def select_entrega_pedido(self, conversa, message) -> str:
        """Executa somente a selecao de Contentor; os estados seguintes seguem legados."""
        return self._legacy_backend.handle(conversa, message)

    def decide_entrega_adesivo(self, conversa, message):
        """Produz a decisão da identificação do Contentor sem persistir estado."""
        ctx = dict(conversa.contexto_json or {})
        if message.tipo == "interactive":
            return "Digite o número físico do equipamento para continuar."

        number = (message.texto or "").strip()
        contentor = self._legacy_backend.db.get(
            PedidoContentor,
            ctx["contentores"][ctx["indice"]],
        )
        if contentor and contentor.tipo_equipamento != TipoEquipamentoPedido.CONTENTOR.value:
            return None
        if not contentor or contentor.status_entrega != "PENDENTE":
            return IdleTransition("Esse ativo já não está pendente. Reinicie a entrega.")
        if not re.fullmatch(r"\d{1,6}", number) or number == "0":
            return "Informe somente o número visível no contentor."
        if number in [
            str(item.get("numero_adesivo"))
            for item in ctx.get("entregas") or []
        ]:
            return "Esse adesivo ja foi informado neste lote."

        duplicate = self._legacy_backend.db.query(PedidoContentor).filter(
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
        return AdvanceTransition(
            "v24_entrega_foto",
            ctx,
            f"Envie a foto do Contentor {number} posicionado no local.",
        )

    def decide_entrega_foto(self, conversa, message):
        """Produz a decisão de registro da foto sem persistir estado."""
        photo = (
            message.media_id or message.filename or message.message_id
            if message.tipo == "image"
            else None
        )
        if not photo:
            return "Envie uma imagem para continuar."

        ctx = dict(conversa.contexto_json or {})
        entregas = list(ctx.get("entregas") or [])
        entrega_atual = dict(entregas[-1])
        fotos = list(entrega_atual.get("fotos") or [])
        if photo not in fotos:
            fotos.append(photo)
        entrega_atual["fotos"] = fotos
        entregas[-1] = entrega_atual
        ctx["entregas"] = entregas
        return AdvanceTransition(
            "v24_entrega_foto_acao",
            ctx,
            "Foto guardada. O que deseja fazer?\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
        )

    def decide_entrega_foto_acao(self, conversa, message):
        """Produz a decisão após a foto sem persistir estado."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "outra foto", "➕ outra foto"}:
            return AdvanceTransition(
                "v24_entrega_foto",
                ctx,
                "Envie a próxima foto deste contentor.",
            )
        if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
            return "Selecione Outra Foto ou Próximo Passo."

        registrado = ctx["indice"] + 1
        total = len(ctx["contentores"])
        ctx["indice"] += 1
        if ctx["indice"] < total:
            progresso = f"Contentor {registrado} de {total} registrado."
            return AdvanceTransition(
                "v24_entrega_adesivo",
                ctx,
                f"{progresso}\n\nVamos registrar o próximo.\n\n"
                "Digite o número do contentor que está a descarregar agora:",
            )
        return AdvanceTransition(
            "v24_entrega_gps",
            ctx,
            f"Contentor {registrado} de {total} registrado.\n\n"
            "Compartilhe a localização GPS da obra.",
        )

    def decide_entrega_gps(self, conversa, message):
        """Produz a decisão de localização sem persistir estado."""
        coords = None
        if message.tipo == "location":
            if message.latitude is not None and message.longitude is not None:
                coords = float(message.latitude), float(message.longitude)
            else:
                match = re.search(
                    r"(?:q=|@|!3d)(-?\d{1,2}\.\d+)[,!3d]*[,\s!4d]+(-?\d{1,3}\.\d+)",
                    message.texto or "",
                )
                if match:
                    coords = float(match.group(1)), float(match.group(2))
        if not coords:
            return "Compartilhe a localização nativa do WhatsApp para confirmar a entrega."

        ctx = dict(conversa.contexto_json or {})
        ctx["latitude"], ctx["longitude"] = coords
        return AdvanceTransition(
            "v24_entrega_referencia_opcao",
            ctx,
            "Deseja informar algum ponto de referência para a entrega?\n\n1. Sim\n2. Não",
        )

    def decide_entrega_referencia_opcao(self, conversa, message):
        """Decide a referência da entrega sem persistir estado."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "sim", "entrega_referencia:sim"}:
            return AdvanceTransition(
                "v24_entrega_referencia",
                ctx,
                "Digite o ponto de referência.",
            )
        if choice in {"2", "nao", "entrega_referencia:nao"}:
            ctx["referencia_entrega"] = None
            prompt = self._legacy_backend.entrega_confirmacao_prompt(ctx)
            return AdvanceTransition(
                "v24_entrega_confirmacao",
                ctx,
                prompt,
            )
        return "Selecione Sim ou Não."

    def decide_entrega_referencia(self, conversa, message):
        """Decide o texto da referência sem persistir estado."""
        raw = (message.texto or "").strip()
        if not 1 <= len(raw) <= 50:
            return "O ponto de referência deve ter no máximo 50 caracteres."

        ctx = dict(conversa.contexto_json or {})
        ctx["referencia_entrega"] = raw
        prompt = self._legacy_backend.entrega_confirmacao_prompt(ctx)
        return AdvanceTransition(
            "v24_entrega_confirmacao",
            ctx,
            prompt,
        )

    def decide_entrega_confirmacao(self, conversa, message):
        """Decide confirmar, cancelar ou rejeitar a entrada sem persistir."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        if choice in {"1", "confirmar entrega", "✅ confirmar entrega"}:
            return ConfirmEntregaContentor(dict(conversa.contexto_json or {}))
        if choice in {"2", "cancelar", "❌ cancelar"}:
            return IdleTransition(
                "Entrega cancelada. Nenhum ativo foi marcado como entregue."
            )
        return "Escolha Confirmar entrega ou Cancelar."

    def decide_entrega_pagou(self, conversa, message):
        """Decide o pagamento informado sem executar qualquer mutacao financeira."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        ctx = dict(conversa.contexto_json or {})
        if choice in {"2", "nao", "nao, continua pendente", "🕒 nao, continua pendente"}:
            return IdleTransition(
                "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento permanece pendente."
            )
        if choice in {"1", "sim", "sim, foi pago", "✅ sim, foi pago"}:
            return AdvanceTransition(
                "v24_entrega_forma",
                ctx,
                "Selecione a forma recebida:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
            )
        return "Selecione Sim ou Não."

    def decide_entrega_forma(self, conversa, message):
        """Valida a forma recebida sem executar persistencia financeira."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        forms = {
            "1": "MBWay",
            "2": "Transferência",
            "3": "Dinheiro",
            "4": "Outro",
            "mbway": "MBWay",
            "transferencia": "Transferência",
            "dinheiro": "Dinheiro",
            "outro": "Outro",
        }
        form = forms.get(choice)
        if not form:
            return "Selecione uma forma de pagamento."
        ctx = dict(conversa.contexto_json or {})
        if form == "Outro":
            return AdvanceTransition(
                "v24_entrega_forma_outro",
                ctx,
                "Qual foi a forma recebida?",
            )
        return RegistrarPagamentoEntregaContentor(ctx, form)

    def decide_entrega_forma_outro(self, conversa, message):
        """Valida e limita a forma livre sem executar persistencia financeira."""
        raw = (message.texto or "").strip()
        if not raw:
            return "Informe a forma recebida."
        return RegistrarPagamentoEntregaContentor(
            dict(conversa.contexto_json or {}),
            raw[:80],
        )

    def decide_recolha_ativo(self, conversa, selection):
        """Prepara a recolha do Contentor selecionado sem acessar persistencia."""
        ctx = dict(conversa.contexto_json or {})
        ctx.update(
            {
                "contentor_id": selection["contentor_id"],
                "fotos_recolha": [],
                "avariado": None,
                "relato_avaria": None,
            }
        )
        return AdvanceTransition(
            "v24_recolha_foto",
            ctx,
            selection["foto_prompt"],
        )

    def decide_despejo_ativo(self, conversa, selection):
        """Prepara o despejo do Contentor selecionado sem acessar persistencia."""
        ctx = dict(conversa.contexto_json or {})
        ctx.update(selection["context_updates"])
        return AdvanceTransition(
            selection["next_state"],
            ctx,
            selection["response"],
        )

    def decide_despejo_foto(self, conversa, message):
        """Registra a foto no contexto sem acessar persistência operacional."""
        photo = (
            message.media_id or message.filename or message.message_id
            if message.tipo == "image"
            else None
        )
        if not photo:
            return "Envie uma imagem para continuar."
        ctx = dict(conversa.contexto_json or {})
        fotos = list(ctx.get("fotos_despejo") or [])
        if photo not in fotos:
            fotos.append(photo)
        ctx["fotos_despejo"] = fotos
        return AdvanceTransition(
            "v24_despejo_foto_acao",
            ctx,
            "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
        )

    def decide_despejo_foto_acao(self, conversa, message):
        """Decide o passo seguinte da foto sem acessar persistência."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "outra foto", "➕ outra foto"}:
            return AdvanceTransition(
                "v24_despejo_foto",
                ctx,
                "Envie a próxima foto do despejo.",
            )
        if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
            return "Selecione Outra Foto ou Próximo Passo."
        if not ctx.get("fotos_despejo"):
            return "Envie pelo menos uma imagem para continuar."
        return PrepararConfirmacaoDespejoContentor(ctx)

    def decide_despejo_residuo(self, conversa, message):
        """Decide o resíduo efetivo sem acessar banco ou serviço."""
        ctx = dict(conversa.contexto_json or {})
        raw = (message.texto or "").strip()
        choice = self._normalize(raw)
        available = ctx.get("residuos_disponiveis") or []
        residue = None
        if choice == "despejo_residuo:limpo":
            residue = "Entulho Limpo"
        elif choice == "despejo_residuo:misto":
            residue = "Entulho Misto"
        if choice.isdigit() and 1 <= int(choice) <= len(available):
            residue = available[int(choice) - 1]
        if not residue:
            residue = next(
                (item for item in available if self._normalize(item) == choice),
                None,
            )
        if not residue:
            return "Selecione um tipo de resíduo com cota em aberto."
        ctx["residuo_efetivo"] = residue
        ctx["carga_errada"] = False
        ctx["relato_carga"] = None
        return PrepararFotoDespejoContentor(ctx)

    def decide_despejo_conformidade(self, conversa, message):
        """Decide conformidade ou divergência sem acessar persistência."""
        ctx = dict(conversa.contexto_json or {})
        choice = self._normalize(message.texto or "")
        if choice == "despejo_conformidade:sim":
            choice = "1"
        elif choice == "despejo_conformidade:nao":
            choice = "2"
        if choice in {
            "1", "sim", "sim, corresponde", "✅ sim, corresponde",
            "sim, tudo certo", "✅ sim, tudo certo",
        }:
            ctx["residuo_efetivo"] = (
                ctx.get("residuo_assumido") or ctx["residuo_contratado"]
            )
            ctx["carga_errada"] = False
            ctx["relato_carga"] = None
            return PrepararFotoDespejoContentor(ctx)
        if choice in {
            "2", "nao", "nao, existe divergencia",
            "❌ nao, existe divergencia", "nao, esta misturado/errado",
            "🚨 nao, esta misturado/errado",
        }:
            ctx["carga_errada"] = True
            return AdvanceTransition(
                "v24_despejo_relato",
                ctx,
                "Descreva a divergencia com pelo menos 10 caracteres.",
            )
        return "Selecione se o material corresponde ao residuo contratado."

    def decide_despejo_relato(self, conversa, message):
        """Valida o relato moderno sem acessar persistência."""
        ctx = dict(conversa.contexto_json or {})
        relato = (message.texto or "").strip()
        if len(relato) < 10:
            return "O relato da carga precisa ter pelo menos 10 caracteres."
        ctx["relato_carga"] = relato
        ctx["carga_errada"] = True
        ctx["residuo_efetivo"] = (
            ctx.get("residuo_efetivo")
            or ctx.get("residuo_assumido")
            or ctx.get("residuo_contratado")
        )
        return PrepararFotoDespejoContentor(ctx)

    def decide_despejo_confirmacao(self, conversa, message):
        """Decide confirmar, voltar ou cancelar sem acessar persistência."""
        ctx = dict(conversa.contexto_json or {})
        choice = self._normalize(message.texto or "")
        if choice in {"1", "confirmar despejo", "confirmar", "✅ confirmar despejo"}:
            return ConfirmarDespejoContentor(ctx)
        if choice in {"2", "voltar", "↩️ voltar"}:
            return PrepararConformidadeDespejoContentor(ctx)
        if choice in {"3", "cancelar", "❌ cancelar"}:
            return IdleTransition(
                "Despejo cancelado. Nenhuma foto foi salva e o ativo permanece em andamento."
            )
        return "Escolha Confirmar despejo, Voltar ou Cancelar."

    @staticmethod
    def _normalize(value):
        normalized = unicodedata.normalize("NFKD", value)
        return "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()

    def decide_recolha_foto(self, conversa, message):
        """Registra a decisao de foto sem persistir estado ou acessar banco."""
        photo = (
            message.media_id or message.filename or message.message_id
            if message.tipo == "image"
            else None
        )
        if not photo:
            return "Envie uma imagem para continuar."
        ctx = dict(conversa.contexto_json or {})
        fotos = list(ctx.get("fotos_recolha") or [])
        if photo not in fotos:
            fotos.append(photo)
        ctx["fotos_recolha"] = fotos
        return AdvanceTransition(
            "v24_recolha_foto_acao",
            ctx,
            "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
        )

    def decide_recolha_foto_acao(
        self,
        conversa,
        message,
        *,
        avarias_enabled,
    ):
        """Decide a acao seguinte da foto sem persistencia operacional."""
        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        ctx = dict(conversa.contexto_json or {})
        if choice in {"1", "outra foto", "➕ outra foto"}:
            return AdvanceTransition(
                "v24_recolha_foto",
                ctx,
                "Envie a próxima foto.",
            )
        if choice in {"2", "proximo passo", "➡️ proximo passo"}:
            if not avarias_enabled:
                ctx.pop("avariado", None)
                ctx.pop("relato_avaria", None)
                return PrepararConfirmacaoRecolhaContentor(ctx)
            return AdvanceTransition(
                "v24_recolha_avaria",
                ctx,
                "O equipamento sofreu algum estrago ou avaria na obra?\n\n"
                "1. ✅ Não, está perfeito\n2. 💥 Sim, está estragado",
            )
        return "Selecione Outra Foto ou Próximo Passo."

    def decide_recolha_avaria(self, conversa, message, *, avarias_enabled):
        """Decide o subfluxo de avaria sem acessar persistencia."""
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled:
            return self._recover_disabled_recolha_avaria(ctx)

        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        if choice in {"1", "nao, esta perfeito", "✅ nao, esta perfeito"}:
            ctx["avariado"] = False
            ctx["relato_avaria"] = None
            return PrepararConfirmacaoRecolhaContentor(ctx)
        if choice in {"2", "sim, esta estragado", "💥 sim, esta estragado"}:
            ctx["avariado"] = True
            return AdvanceTransition(
                "v24_recolha_relato",
                ctx,
                "Descreva a avaria com pelo menos 10 caracteres.",
            )
        return "Selecione uma das opções de avaria."

    def decide_recolha_relato(self, conversa, message, *, avarias_enabled):
        """Valida o relato de avaria sem acessar persistencia."""
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled:
            return self._recover_disabled_recolha_avaria(ctx)

        relato = (message.texto or "").strip()
        if len(relato) < 10:
            return "O relato da avaria precisa ter pelo menos 10 caracteres."
        ctx["avariado"] = True
        ctx["relato_avaria"] = relato
        return PrepararConfirmacaoRecolhaContentor(ctx)

    def decide_recolha_confirmacao(
        self,
        conversa,
        message,
        *,
        avarias_enabled,
    ):
        """Decide confirmar ou cancelar sem executar o fluxo persistente."""
        ctx = dict(conversa.contexto_json or {})
        if not avarias_enabled and (
            ctx.get("avariado") or ctx.get("relato_avaria")
        ):
            return self._recover_disabled_recolha_avaria(ctx)

        normalized = unicodedata.normalize("NFKD", message.texto or "")
        choice = "".join(
            char for char in normalized if not unicodedata.combining(char)
        ).strip().lower()
        if choice in {"1", "confirmar recolha", "confirmar"}:
            return ConfirmarRecolhaContentor(ctx)
        if choice in {"2", "cancelar ativo", "cancelar"}:
            return CancelarRecolhaContentor(ctx)
        return "Escolha Confirmar recolha ou Cancelar ativo."

    @staticmethod
    def _recover_disabled_recolha_avaria(ctx):
        ctx.pop("avariado", None)
        ctx.pop("relato_avaria", None)
        return PrepararConfirmacaoRecolhaContentor(
            ctx,
            "A funcionalidade de avarias não está disponível nesta empresa. "
            "O subfluxo foi cancelado com segurança.\n\n",
        )
