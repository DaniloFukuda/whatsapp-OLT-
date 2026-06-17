from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp


class WhatsappRouterAgent:
    def __init__(self, db: Session):
        self.db = db
        self.aluguer_agent = AluguerAgent(db)
        self.contentor_agent = ContentorAgent(db)

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        conversa = self._get_or_create_conversa(message.telefone)
        text = (message.texto or "").strip().lower()

        if text == "novo":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."
            return self.aluguer_agent.start(conversa)
        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self.aluguer_agent.handle(conversa, message)
        if text in {"contentores", "status"}:
            return self.contentor_agent.listar_status()
        return "Comando nao reconhecido. Envie 'novo' para registrar um aluguer."

    def _get_or_create_conversa(self, telefone: str) -> ConversaWhatsApp:
        conversa = self.db.query(ConversaWhatsApp).filter(ConversaWhatsApp.telefone == telefone).first()
        if conversa:
            return conversa
        conversa = ConversaWhatsApp(telefone=telefone, estado_atual="idle", contexto_json={})
        self.db.add(conversa)
        self.db.commit()
        self.db.refresh(conversa)
        return conversa

    def _is_authorized(self, telefone: str) -> bool:
        authorized_phones = self._authorized_phones()
        return not authorized_phones or normalize_phone(telefone) in authorized_phones

    def _authorized_phones(self) -> set[str]:
        settings = get_settings()
        raw_values = [
            settings.authorized_operator_phone,
            settings.whatsapp_owner_phone,
            settings.owner_whatsapp,
        ]
        raw_values.extend(settings.authorized_operator_phones.split(","))
        return {normalized for value in raw_values if (normalized := normalize_phone(value))}
