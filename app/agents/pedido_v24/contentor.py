"""Entrada operacional de entrega de Contentor no fluxo V2.4."""

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import PedidoContentor, TipoEquipamentoPedido


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
