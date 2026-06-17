from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, StatusAluguer
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


ACTIVE_ALUGUER_STATUSES = {StatusAluguer.ATIVO, StatusAluguer.VENCENDO, StatusAluguer.RENOVADO}
COMMANDS = {"resumo", "lista", "disponiveis", "alugados", "vencendo", "atrasados"}


class WhatsappRouterAgent:
    def __init__(self, db: Session):
        self.db = db
        self.aluguer_agent = AluguerAgent(db)
        self.contentor_agent = ContentorAgent(db)
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        conversa = self._get_or_create_conversa(message.telefone)
        text = (message.texto or "").strip().lower()

        if text in COMMANDS:
            return self._handle_operational_command(text)
        if text == "novo":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."
            return self.aluguer_agent.start(conversa)
        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self.aluguer_agent.handle(conversa, message)
        if text in {"contentores", "status"}:
            return self.contentor_agent.listar_status()
        return "Comando nao reconhecido. Envie 'novo' para registar um aluguer."

    def _handle_operational_command(self, command: str) -> str:
        if command == "resumo":
            return self._resumo()
        if command == "lista":
            return self._lista()
        if command == "disponiveis":
            return self._disponiveis()
        if command == "alugados":
            return self._alugados()
        if command == "vencendo":
            return self._vencendo()
        if command == "atrasados":
            return self._atrasados()
        return "Comando nao reconhecido. Envie 'novo' para registar um aluguer."

    def _resumo(self) -> str:
        contentores = self.contentor_service.listar_contentores()
        alugueres_ativos = self._active_alugueres()
        vencendo_amanha = self.aluguer_service.listar_vencendo_amanha()
        atrasados = self.aluguer_service.listar_atrasados()
        counts = {status: 0 for status in StatusContentor}
        for contentor in contentores:
            counts[contentor.status] += 1

        return "\n".join(
            [
                "📦 Resumo dos contentores",
                f"Total: {len(contentores)}",
                f"Disponíveis: {counts[StatusContentor.DISPONIVEL]}",
                f"Alugados: {counts[StatusContentor.ALUGADO]}",
                f"Aguardando recolha: {counts[StatusContentor.AGUARDANDO_RECOLHA]}",
                f"Manutenção: {counts[StatusContentor.MANUTENCAO]}",
                f"Alugueres ativos: {len(alugueres_ativos)}",
                f"Vencem amanhã: {len(vencendo_amanha)}",
                f"Em atraso: {len(atrasados)}",
            ]
        )

    def _lista(self) -> str:
        contentores = self.contentor_service.listar_contentores()
        if not contentores:
            return "Ainda não existem contentores registados."
        return "\n".join(f"{contentor.codigo} - {self._format_contentor_status(contentor.status)}" for contentor in contentores)

    def _disponiveis(self) -> str:
        contentores = [
            contentor
            for contentor in self.contentor_service.listar_contentores()
            if contentor.status == StatusContentor.DISPONIVEL
        ]
        if not contentores:
            return "Não existem contentores disponíveis."
        return "Contentores disponíveis:\n" + "\n".join(contentor.codigo for contentor in contentores)

    def _alugados(self) -> str:
        alugueres = self._active_alugueres()
        if not alugueres:
            return "Não existem contentores alugados."
        return "Contentores alugados:\n" + "\n".join(self._format_aluguer(aluguer) for aluguer in alugueres)

    def _vencendo(self) -> str:
        alugueres = self.aluguer_service.listar_vencendo_amanha()
        if not alugueres:
            return "Não existem alugueres com vencimento amanhã."
        return "Alugueres que vencem amanhã:\n" + "\n".join(self._format_aluguer(aluguer) for aluguer in alugueres)

    def _atrasados(self) -> str:
        alugueres = self.aluguer_service.listar_atrasados()
        if not alugueres:
            return "Não existem alugueres em atraso."
        return "Alugueres em atraso:\n" + "\n".join(self._format_aluguer(aluguer) for aluguer in alugueres)

    def _active_alugueres(self) -> list[AluguerContentor]:
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.status.in_(ACTIVE_ALUGUER_STATUSES))
            .order_by(AluguerContentor.data_vencimento, AluguerContentor.id)
            .all()
        )

    def _format_aluguer(self, aluguer: AluguerContentor) -> str:
        vencimento = aluguer.data_vencimento.strftime("%d/%m/%Y")
        return (
            f"{aluguer.contentor.codigo} - {aluguer.nome_cliente} - "
            f"vencimento {vencimento} - {self._format_aluguer_status(aluguer.status)}"
        )

    def _format_contentor_status(self, status: StatusContentor) -> str:
        labels = {
            StatusContentor.DISPONIVEL: "disponível",
            StatusContentor.ALUGADO: "alugado",
            StatusContentor.AGUARDANDO_RECOLHA: "aguardando recolha",
            StatusContentor.MANUTENCAO: "manutenção",
        }
        return labels[status]

    def _format_aluguer_status(self, status: StatusAluguer) -> str:
        labels = {
            StatusAluguer.ATIVO: "ativo",
            StatusAluguer.VENCENDO: "vencendo",
            StatusAluguer.RENOVADO: "renovado",
            StatusAluguer.AGUARDANDO_RECOLHA: "aguardando recolha",
            StatusAluguer.RECOLHIDO: "recolhido",
            StatusAluguer.CANCELADO: "cancelado",
        }
        return labels[status]

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
