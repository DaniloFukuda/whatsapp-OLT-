from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.agents.gestao_aluguer_agent import GestaoAluguerAgent
from app.agents.renovacao_agent import RenovacaoAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, StatusAluguer
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.models.operador import PerfilOperador
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService
from app.services.operador_service import OperadorService


ACTIVE_ALUGUER_STATUSES = {StatusAluguer.ATIVO, StatusAluguer.VENCENDO, StatusAluguer.RENOVADO}
COMMANDS = {"resumo", "lista", "disponiveis", "alugados", "vencendo", "atrasados"}
START_COMMANDS = {"iniciar", "cadastrar", "comecar", "começar", "novo"}
ALTER_COMMANDS = {"alterar", "modificar"}
DELETE_COMMANDS = {"excluir", "deletar"}
RENEW_COMMANDS = {"renovar", "prorrogar"}
MENU_COMMANDS = {"menu", "ola", "oi", "ajuda"}
MENU_STATE = "menu_principal"


class WhatsappRouterAgent:
    def __init__(self, db: Session):
        self.db = db
        self.aluguer_agent = AluguerAgent(db)
        self.gestao_aluguer_agent = GestaoAluguerAgent(db)
        self.renovacao_agent = RenovacaoAgent(db)
        self.contentor_agent = ContentorAgent(db)
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.operador_service = OperadorService(db)

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        conversa = self._get_or_create_conversa(message.telefone)
        text = (message.texto or "").strip().lower()
        normalized_text = self._normalize_text(message.texto)

        if conversa.estado_atual == MENU_STATE:
            return self._handle_menu_option(conversa, normalized_text, message.telefone)
        if text in COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para consultar dados operacionais. Contacte o administrador do sistema."
            return self._handle_operational_command(text, message.telefone)
        if text in START_COMMANDS or normalized_text in {"comecar"}:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."
            return self.aluguer_agent.start(conversa)
        if text in ALTER_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para alterar registros. Contacte o administrador do sistema."
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text in DELETE_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para excluir registros. Contacte o administrador do sistema."
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in RENEW_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para renovar registros. Contacte o administrador do sistema."
            return self.renovacao_agent.start(conversa)
        if normalized_text in MENU_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para usar o menu operacional. Contacte o administrador do sistema."
            return self._show_menu(conversa)
        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self.aluguer_agent.handle(conversa, message)
        if conversa.estado_atual in GestaoAluguerAgent.ACTIVE_STATES:
            return self.gestao_aluguer_agent.handle(conversa, message)
        if conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES:
            return self.renovacao_agent.handle(conversa, message)
        if text in {"contentores", "status"}:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para consultar dados operacionais. Contacte o administrador do sistema."
            return self.contentor_agent.listar_status()
        if not self._is_authorized(message.telefone):
            return "Telefone nao autorizado para usar o menu operacional. Contacte o administrador do sistema."
        return self._show_menu(conversa)

    def _show_menu(self, conversa: ConversaWhatsApp) -> str:
        conversa.estado_atual = MENU_STATE
        conversa.contexto_json = {}
        self.db.commit()
        return "\n".join(
            [
                "Olá, sou o Robô de Gestão de Contentores da OLT.",
                "O que vamos fazer agora?",
                "",
                "1 - Cadastrar entrega de contentor",
                "2 - Alterar informações",
                "3 - Excluir pedido",
                "4 - Ver resumo",
                "5 - Renovar/prorrogar contentor",
                "",
                "Responda com o número da opção.",
            ]
        )

    def _handle_menu_option(self, conversa: ConversaWhatsApp, option: str, telefone: str) -> str:
        if not self._is_authorized(telefone):
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Telefone nao autorizado para usar o menu operacional. Contacte o administrador do sistema."
        if option == "1":
            return self.aluguer_agent.start(conversa)
        if option == "2":
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if option == "3":
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if option == "4":
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return self._handle_operational_command("resumo", telefone)
        if option == "5":
            return self.renovacao_agent.start(conversa)
        return "Opção inválida. Responda com o número da opção."

    def _handle_operational_command(self, command: str, telefone: str | None = None) -> str:
        if command == "resumo":
            return self._resumo_operacional(self.operador_service.obter_perfil(telefone) or PerfilOperador.GESTOR)
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

    def _resumo_operacional(self, perfil: PerfilOperador = PerfilOperador.GESTOR) -> str:
        contentores = self.contentor_service.listar_contentores()
        alugueres_ativos = self._active_alugueres()
        vencendo_amanha = self.aluguer_service.listar_vencendo_amanha()
        atrasados = self.aluguer_service.listar_atrasados()
        counts = {status: 0 for status in StatusContentor}
        for contentor in contentores:
            counts[contentor.status] += 1

        today = self._local_date(utcnow())
        tomorrow = today + timedelta(days=1)
        retiradas_hoje = self._alugueres_por_data_retirada(today)
        retiradas_amanha = self._alugueres_por_data_retirada(tomorrow)
        linhas = [
            "Resumo dos contentores",
            f"Total: {len(contentores)}",
            f"Disponiveis: {counts[StatusContentor.DISPONIVEL]}",
            f"Alugados: {counts[StatusContentor.ALUGADO]}",
            f"Aguardando recolha: {counts[StatusContentor.AGUARDANDO_RECOLHA]}",
            f"Manutencao: {counts[StatusContentor.MANUTENCAO]}",
            f"Alugueres ativos: {len(alugueres_ativos)}",
            f"Vencem amanha: {len(vencendo_amanha)}",
            f"Em atraso: {len(atrasados)}",
            f"Quantidade de contentores alugados: {counts[StatusContentor.ALUGADO]}",
            "",
            "Retiradas hoje:",
            self._format_retiradas(retiradas_hoje),
            "",
            "Retiradas amanha:",
            self._format_retiradas(retiradas_amanha),
        ]
        if perfil == PerfilOperador.GESTOR:
            faturado_total, recebido_total = self._faturamento_mes_corrente()
            linhas.extend(
                [
                    "",
                    "Faturamento do mes corrente:",
                    f"Faturado total do mes: {faturado_total:.2f}",
                    f"Recebido/pago no mes: {recebido_total:.2f}",
                    "Obs.: faturado total soma todos os alugueres do mes; recebido soma apenas registros pagos.",
                ]
            )
        return "\n".join(linhas)

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
            .filter(AluguerContentor.is_deleted.is_(False))
            .filter(AluguerContentor.status.in_(ACTIVE_ALUGUER_STATUSES))
            .order_by(AluguerContentor.data_vencimento, AluguerContentor.id)
            .all()
        )

    def _alugueres_por_data_retirada(self, date) -> list[AluguerContentor]:
        return [
            aluguer
            for aluguer in self._active_alugueres()
            if self._local_date(aluguer.data_vencimento) == date
        ]

    def _faturamento_mes_corrente(self) -> tuple[Decimal, Decimal]:
        local_now = self._to_local_datetime(utcnow())
        faturado_total = Decimal("0")
        recebido_total = Decimal("0")
        for aluguer in self.db.query(AluguerContentor).filter(AluguerContentor.is_deleted.is_(False)).all():
            entrega = self._to_local_datetime(aluguer.data_entrega)
            if entrega.year == local_now.year and entrega.month == local_now.month:
                valor = Decimal(str(aluguer.valor or 0))
                faturado_total += valor
                if aluguer.pago:
                    recebido_total += valor
        return faturado_total, recebido_total

    def _format_retiradas(self, alugueres: list[AluguerContentor]) -> str:
        if not alugueres:
            return "Nenhum contentor para retirar."
        return "\n".join(self._format_retirada(aluguer) for aluguer in alugueres)

    def _format_retirada(self, aluguer: AluguerContentor) -> str:
        contentor = aluguer.contentor.codigo if aluguer.contentor else f"#{aluguer.contentor_id}"
        telefone_info = "telefone nao informado"
        if aluguer.telefone_cliente:
            telefone_info = f"{normalize_phone(aluguer.telefone_cliente)} / {whatsapp_link(aluguer.telefone_cliente)}"
        localizacao = "localizacao nao informada"
        if aluguer.latitude is not None and aluguer.longitude is not None:
            localizacao = f"https://www.google.com/maps?q={aluguer.latitude},{aluguer.longitude}"
        return (
            f"- {aluguer.nome_cliente} - #{aluguer.id} / {contentor} - "
            f"telefone: {telefone_info} - localizacao: {localizacao} - "
            f"retirada {aluguer.data_vencimento:%d/%m/%Y}"
        )

    def _to_local_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(self._timezone())

    def _local_date(self, value: datetime):
        return self._to_local_datetime(value).date()

    def _timezone(self):
        try:
            return ZoneInfo(get_settings().timezone)
        except ZoneInfoNotFoundError:
            return timezone.utc

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
        return self.operador_service.verificar_autorizacao(telefone)

    def _normalize_text(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()
