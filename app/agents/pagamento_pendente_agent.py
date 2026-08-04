from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador
from app.models.pedido import Pedido, StatusPagamento
from app.services.pedido_service import PedidoService


class PagamentoPendenteAgent:
    PREFIX = "pagamento_pendente_"
    SELECAO = PREFIX + "selecao"
    FORMA = PREFIX + "forma"
    CONFIRMACAO = PREFIX + "confirmacao"
    ACTIVE_STATES = {SELECAO, FORMA, CONFIRMACAO}
    TIMEOUT = timedelta(minutes=30)

    def __init__(self, db: Session):
        self.db = db
        self.service = PedidoService(db)

    def start(self, conversa: ConversaWhatsApp) -> str:
        pedidos = self.service.pedidos_pagamento_pendente()
        if not pedidos:
            return "Não existem pagamentos pendentes para registrar."
        conversa.estado_atual = self.SELECAO
        conversa.contexto_json = {
            "pedido_ids": [pedido.id for pedido in pedidos],
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        linhas = ["💶 Registrar pagamento pendente", "", "Selecione o pedido:"]
        for indice, pedido in enumerate(pedidos, 1):
            tipo = self.service.tipo_equipamento_pagamento(pedido)
            valor = self._money(pedido.valor_global)
            data = pedido.data_planejada.strftime("%d/%m/%Y")
            linhas.append(
                f"{indice}. Pedido #{pedido.id} • {pedido.nome_cliente} • {tipo} • "
                f"Total: {valor} • Pendente: {valor} • {data}"
            )
        linhas.append("\nEnvie o número da lista. Use voltar, cancelar ou menu para sair.")
        return "\n".join(linhas)

    def handle(
        self,
        conversa: ConversaWhatsApp,
        message: NormalizedWhatsAppMessage,
        operador_id: str,
    ) -> str:
        if self._expired(conversa):
            self._clear(conversa)
            self.db.commit()
            return "A sessão de pagamento expirou. Nenhuma alteração foi salva."

        text = (message.texto or "").strip()
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if conversa.estado_atual == self.SELECAO:
            pedido = self._selecionar_pedido(text, context)
            if not pedido:
                return "Seleção inválida. Escolha um dos pedidos pendentes exibidos."
            context["pedido_id"] = pedido.id
            conversa.estado_atual = self.FORMA
            conversa.contexto_json = context
            self.db.commit()
            return self._revisao(pedido) + "\n\nEscolha a forma:\n1. Dinheiro\n2. MBWay\n3. Transferência\n4. Multibanco"

        pedido = self._pedido_pendente(context.get("pedido_id"))
        if not pedido:
            self._clear(conversa)
            self.db.commit()
            return "Este pedido já está marcado como pago. Nenhuma alteração adicional foi feita."

        if conversa.estado_atual == self.FORMA:
            forma = self._parse_forma(text)
            if not forma:
                return "Forma de pagamento inválida. Escolha Dinheiro, MBWay, Transferência ou Multibanco."
            context["forma_pagamento"] = forma
            conversa.estado_atual = self.CONFIRMACAO
            conversa.contexto_json = context
            self.db.commit()
            return self._confirmacao(pedido, forma)

        if conversa.estado_atual == self.CONFIRMACAO:
            normalized = text.lower()
            if normalized in {"2", "trocar", "trocar forma", "alterar forma"}:
                context.pop("forma_pagamento", None)
                conversa.estado_atual = self.FORMA
                conversa.contexto_json = context
                self.db.commit()
                return "Escolha a nova forma:\n1. Dinheiro\n2. MBWay\n3. Transferência\n4. Multibanco"
            if normalized not in {"1", "confirmar", "confirmo", "sim"}:
                return "Confirmação inválida. Envie 1 para confirmar, 2 para trocar a forma ou cancelar."

            forma = context.get("forma_pagamento")
            self._clear(conversa)
            try:
                pago = self.service.registrar_pagamento_pendente(
                    pedido.id, forma, operador_id, utcnow()
                )
            except (ValueError, PermissionError) as exc:
                self._clear(conversa)
                self.db.commit()
                return str(exc)
            operador = self.db.get(Operador, operador_id)
            momento = self._to_local_datetime(pago.pagamento_recebido_em).strftime(
                "%d/%m/%Y %H:%M"
            )
            return "\n".join(
                [
                    "✅ Pagamento registrado",
                    f"Pedido: #{pago.id}",
                    f"Cliente: {pago.nome_cliente}",
                    f"Valor recebido: {self._money(pago.valor_global)}",
                    f"Forma: {pago.forma_pagamento}",
                    f"Data/hora: {momento}",
                    f"Operador: {operador.nome_operador}",
                    "Novo status: PAGO",
                ]
            )
        return "Fluxo de pagamento inválido. Nenhuma alteração foi salva."

    def _selecionar_pedido(self, text: str, context: dict) -> Pedido | None:
        ids = [int(value) for value in context.get("pedido_ids", [])]
        clean = text.strip().lstrip("#")
        if not clean.isdigit():
            return None
        value = int(clean)
        pedido_id = ids[value - 1] if 1 <= value <= len(ids) else value if value in ids else None
        return self._pedido_pendente(pedido_id)

    def _pedido_pendente(self, pedido_id: int | None) -> Pedido | None:
        if not pedido_id:
            return None
        pedido = self.service.get(int(pedido_id))
        if (
            not pedido
            or pedido.status_pagamento != StatusPagamento.PENDENTE.value
            or Decimal(str(pedido.valor_global or 0)) <= 0
        ):
            return None
        return pedido

    def _revisao(self, pedido: Pedido) -> str:
        valor = self._money(pedido.valor_global)
        return "\n".join(
            [
                "Revisão do recebimento",
                f"Pedido: #{pedido.id}",
                f"Cliente: {pedido.nome_cliente}",
                f"Tipo: {self.service.tipo_equipamento_pagamento(pedido)}",
                f"Valor total: {valor}",
                "Valor já pago: € 0,00",
                f"Saldo pendente: {valor}",
                "Status atual: PENDENTE",
                "O saldo integral será registrado nesta primeira versão.",
            ]
        )

    def _confirmacao(self, pedido: Pedido, forma: str) -> str:
        return "\n".join(
            [
                self._revisao(pedido),
                f"Forma selecionada: {forma}",
                "",
                "1. Confirmar pagamento integral",
                "2. Trocar forma de pagamento",
                "0. Cancelar",
            ]
        )

    def _parse_forma(self, text: str) -> str | None:
        numbered = {"1": "Dinheiro", "2": "MBWay", "3": "Transferência", "4": "Multibanco"}
        return numbered.get(text.strip()) or self.service.normalizar_forma_pagamento(text)

    def _expired(self, conversa: ConversaWhatsApp) -> bool:
        raw = (conversa.contexto_json or {}).get("updated_at")
        if not raw:
            return False
        try:
            return utcnow() - datetime.fromisoformat(raw) > self.TIMEOUT
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _to_local_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            local_timezone = ZoneInfo(get_settings().timezone)
        except ZoneInfoNotFoundError:
            local_timezone = timezone.utc
        return value.astimezone(local_timezone)

    @staticmethod
    def _clear(conversa: ConversaWhatsApp) -> None:
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}

    @staticmethod
    def _money(value) -> str:
        return f"€ {Decimal(str(value or 0)):.2f}".replace(".", ",")
