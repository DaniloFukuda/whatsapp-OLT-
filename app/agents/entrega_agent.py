import unicodedata

from sqlalchemy.orm import Session

from app.core.phone import normalize_portugal_phone
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


MAIN_MENU = (
    "🤖 Menu principal - OLT Entulhos\n\n"
    "1️⃣ 📝 Novo pedido\n"
    "2️⃣ 🚛 Entrega de contentor\n"
    "3️⃣ 📦 Recolha de contentor\n"
    "4️⃣ ✏️ Alterar registro\n"
    "5️⃣ 🗑️ Apagar registro\n"
    "6️⃣ 📊 Resumo dos contentores\n"
    "7️⃣ 🛠️ Manutencao / avarias\n"
    "0️⃣ ❌ Sair\n\n"
    "Digite o numero da opcao desejada."
)


class EntregaAgent:
    START_STATE = "entrega_aguardando_selecao"
    ACTIVE_STATES = {
        "entrega_aguardando_selecao",
        "entrega_aguardando_contentor",
        "entrega_aguardando_confirmacao",
    }

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)

    def start(self, conversa: ConversaWhatsApp) -> str:
        pendentes = self.aluguer_service.listar_pendentes_entrega()
        if not pendentes:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao existem pedidos pendentes de entrega."
        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "pendentes": [aluguer.id for aluguer in pendentes],
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        return "🚛 Entrega de contentor. Escolha o pedido:\n" + self._format_pedidos(pendentes) + "\n0 - Cancelar"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if state == "entrega_aguardando_selecao":
            pendentes = self._load_pendentes(context)
            aluguer = self._resolve_selection(message.texto, pendentes)
            if not aluguer:
                return "Opcao invalida. Escolha o numero da lista ou o ID do pedido."
            context["aluguer_id"] = aluguer.id
            return self._advance(
                conversa,
                "entrega_aguardando_contentor",
                context,
                "Informe o contentor entregue. Ex: C02\n\n" + self._format_contentores_disponiveis(),
            )

        if state == "entrega_aguardando_contentor":
            codigo = (message.texto or "").strip().upper()
            contentor = self.contentor_service.repository.get_by_codigo(codigo)
            if not contentor or contentor.status != StatusContentor.DISPONIVEL:
                return "Contentor invalido ou indisponivel. Informe um contentor disponivel. Ex: C02"
            context["contentor_codigo"] = contentor.codigo
            return self._advance(
                conversa,
                "entrega_aguardando_confirmacao",
                context,
                f"Confirmar entrega do contentor {contentor.codigo} para o pedido #{context['aluguer_id']}?\n\n"
                "1 - Sim, confirmar entrega\n"
                "0 - Cancelar",
            )

        if state == "entrega_aguardando_confirmacao":
            choice = self._normalize_option(message.texto)
            if choice in {"1", "sim", "s", "confirmar"}:
                aluguer = self.aluguer_service.confirmar_entrega(
                    aluguer_id=int(context["aluguer_id"]),
                    contentor_codigo=context["contentor_codigo"],
                    operador_telefone=normalize_portugal_phone(message.telefone),
                )
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return (
                    f"✅ Entrega registrada. Contentor {aluguer.numero_contentor} vinculado ao pedido #{aluguer.id}.\n\n"
                    + MAIN_MENU
                )
            if choice in {"0", "cancelar", "sair", "menu", "nao", "n"}:
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return "Entrega cancelada. Nenhum contentor foi vinculado.\n\n" + MAIN_MENU
            return "Opcao invalida. Responda 1 para confirmar ou 0 para cancelar."

        return self.start(conversa)

    def _format_pedidos(self, alugueres: list[AluguerContentor]) -> str:
        return "\n".join(
            f"{index}. #{aluguer.id} - {aluguer.nome_cliente} - entrega {aluguer.data_entrega:%d/%m/%Y}"
            for index, aluguer in enumerate(alugueres, start=1)
        )

    def _format_contentores_disponiveis(self) -> str:
        disponiveis = [
            contentor.codigo
            for contentor in self.contentor_service.listar_contentores()
            if contentor.status == StatusContentor.DISPONIVEL
        ]
        if not disponiveis:
            return "Nenhum contentor disponivel no momento."
        return "Disponiveis: " + ", ".join(disponiveis)

    def _load_pendentes(self, context: dict) -> list[AluguerContentor]:
        ids = set(context.get("pendentes") or [])
        pendentes = self.aluguer_service.listar_pendentes_entrega()
        if not ids:
            return pendentes
        return [aluguer for aluguer in pendentes if aluguer.id in ids]

    def _resolve_selection(self, raw: str | None, pendentes: list[AluguerContentor]) -> AluguerContentor | None:
        option = self._normalize_option(raw)
        if not option.isdigit():
            return None
        number = int(option)
        if 1 <= number <= len(pendentes):
            return pendentes[number - 1]
        return next((aluguer for aluguer in pendentes if aluguer.id == number), None)

    def _advance(self, conversa: ConversaWhatsApp, next_state: str, context: dict, response: str) -> str:
        conversa.estado_atual = next_state
        conversa.contexto_json = context
        self.db.commit()
        return response

    def _normalize_option(self, raw: str | None) -> str:
        text = (raw or "").strip().lower()
        normalized = unicodedata.normalize("NFKD", text)
        return "".join(char for char in normalized if not unicodedata.combining(char))
