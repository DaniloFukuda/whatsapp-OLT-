from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.agents.gestao_aluguer_agent import GestaoAluguerAgent
from app.agents.recolha_agent import RecolhaAgent
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
CONTENTOR_STATUS_COMMANDS = {"alterar contentor", "alterar status", "status contentor"}
DELETE_COMMANDS = {"excluir", "deletar"}
CONTENTOR_DELETE_COMMANDS = {"apagar", "remover", "excluir contentor", "excluir contentores"}
RENEW_COMMANDS = {"renovar", "prorrogar"}
RECOLHA_COMMANDS = {"recolha", "recolher", "confirmar recolha", "confirmar recolha de contentor"}
CANCEL_COMMANDS = {"cancelar", "cancela", "sair", "parar", "voltar", "menu", "0"}
CANCELLED_MENU_MESSAGE = (
    "Operação cancelada. Nenhuma alteração foi salva.\n\n"
    "Digite:\n"
    "1 - Novo pedido\n"
    "2 - Alterar registro\n"
    "3 - Excluir registro\n"
    "4 - Ver resumo\n"
    "5 - Confirmar recolha de contentor"
)


class WhatsappRouterAgent:
    def __init__(self, db: Session):
        self.db = db
        self.aluguer_agent = AluguerAgent(db)
        self.gestao_aluguer_agent = GestaoAluguerAgent(db)
        self.renovacao_agent = RenovacaoAgent(db)
        self.contentor_agent = ContentorAgent(db)
        self.recolha_agent = RecolhaAgent(db)
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.operador_service = OperadorService(db)

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        conversa = self._get_or_create_conversa(message.telefone)
        text = (message.texto or "").strip().lower()

        if text in CANCEL_COMMANDS:
            if self._has_active_flow(conversa):
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return CANCELLED_MENU_MESSAGE
            return "Nenhuma operação em andamento para cancelar."

        if conversa.estado_atual == "cadastro_expirado":
            if text in {"1", "sim", "continuar"}:
                context = dict(conversa.contexto_json or {})
                previous_state = context.get("estado_anterior") or AluguerAgent.START_STATE
                conversa.estado_atual = previous_state
                context["updated_at"] = utcnow().isoformat()
                context.pop("estado_anterior", None)
                conversa.contexto_json = context
                self.db.commit()
                return "Vamos continuar de onde parou. " + self._prompt_for_state(previous_state)
            if text in {"2", "nao", "recomecar", "recomeçar"}:
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return self.aluguer_agent.start(conversa)
            return "Opcao invalida. Responda 1 para continuar ou 2 para recomecar."

        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES and self._is_expired(conversa):
            context = dict(conversa.contexto_json or {})
            context["estado_anterior"] = conversa.estado_atual
            conversa.contexto_json = context
            return self.aluguer_agent.timeout_prompt(conversa)

        if text.startswith("resolver carga"):
            return self._resolver_pendencia("carga", text, message.telefone)
        if text.startswith("resolver avaria"):
            return self._resolver_pendencia("avaria", text, message.telefone)

        if text in COMMANDS:
            if text == "resumo" and not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para consultar dados operacionais. Contacte o administrador do sistema."
            return self._handle_operational_command(text, message.telefone)

        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self.aluguer_agent.handle(conversa, message)
        if conversa.estado_atual in GestaoAluguerAgent.ACTIVE_STATES:
            return self.gestao_aluguer_agent.handle(conversa, message)
        if conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES:
            return self.renovacao_agent.handle(conversa, message)
        if conversa.estado_atual in RecolhaAgent.ACTIVE_STATES:
            return self.recolha_agent.handle(conversa, message)
        if conversa.estado_atual in ContentorAgent.ACTIVE_STATES:
            return self.contentor_agent.handle(conversa, message)

        if text == "4":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para consultar dados operacionais. Contacte o administrador do sistema."
            return self._handle_operational_command("resumo", message.telefone)
        if text in RECOLHA_COMMANDS or text == "5" or (text == "1" and self._is_funcionario(message.telefone)):
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para confirmar recolhas. Contacte o administrador do sistema."
            return self.recolha_agent.start(conversa)
        if text in START_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."
            if not self._can_create_pedido(message.telefone):
                return "Seu perfil de motorista nao possui permissao para cadastrar pedidos. Use a opcao de recolha."
            return self.aluguer_agent.start(conversa)
        if text == "1":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."
            if not self._can_create_pedido(message.telefone):
                return self.recolha_agent.start(conversa)
            return self.aluguer_agent.start(conversa)
        if text in CONTENTOR_STATUS_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para alterar registros. Contacte o administrador do sistema."
            return self.contentor_agent.start_alteracao_status(conversa)
        if text in ALTER_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para alterar registros. Contacte o administrador do sistema."
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text == "2":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para alterar registros. Contacte o administrador do sistema."
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text in DELETE_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para excluir registros. Contacte o administrador do sistema."
            if text == "excluir" and not self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7):
                return self.contentor_agent.start_exclusao(conversa)
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in CONTENTOR_DELETE_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para excluir registros. Contacte o administrador do sistema."
            return self.contentor_agent.start_exclusao(conversa)
        if text == "3":
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para excluir registros. Contacte o administrador do sistema."
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in RENEW_COMMANDS:
            if not self._is_authorized(message.telefone):
                return "Telefone nao autorizado para renovar registros. Contacte o administrador do sistema."
            return self.renovacao_agent.start(conversa)
        if text in {"contentores", "status"}:
            return self.contentor_agent.listar_status()
        if self._is_authorized(message.telefone):
            return self._initial_menu(message.telefone)
        return "Comando nao reconhecido. Envie 'novo' para registar um aluguer."

    def _handle_operational_command(self, command: str, telefone: str | None = None) -> str:
        if command == "resumo":
            return self._resumo_operacional(self.operador_service.obter_perfil(telefone) or PerfilOperador.FUNCIONARIO)
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
            f"Contentores com status alugado: {counts[StatusContentor.ALUGADO]}",
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
            linhas.extend(self._pendencias_operacionais())
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

    def _pendencias_operacionais(self) -> list[str]:
        pendencias_financeiras = [
            aluguer
            for aluguer in self.db.query(AluguerContentor).filter(AluguerContentor.is_deleted.is_(False)).all()
            if not aluguer.pago
        ]
        pendencias_carga = self.aluguer_service.listar_pendencias_carga()
        pendencias_avaria = self.aluguer_service.listar_pendencias_avaria()

        linhas = ["", "PENDENCIAS OPERACIONAIS CRITICAS"]
        total_pendente = sum(Decimal(str(aluguer.valor or 0)) for aluguer in pendencias_financeiras)
        linhas.append(f"Pendencias financeiras: {total_pendente:.2f}")
        if pendencias_financeiras:
            linhas.extend(self._format_pendencia_financeira(aluguer) for aluguer in pendencias_financeiras)
        else:
            linhas.append("- Nenhuma pendencia financeira ativa.")

        linhas.append("")
        linhas.append("Pendencias de carga:")
        if pendencias_carga:
            linhas.extend(self._format_pendencia_carga(aluguer) for aluguer in pendencias_carga)
        else:
            linhas.append("- Nenhuma pendencia de carga.")

        linhas.append("")
        linhas.append("Pendencias de avarias:")
        if pendencias_avaria:
            linhas.extend(self._format_pendencia_avaria(aluguer) for aluguer in pendencias_avaria)
        else:
            linhas.append("- Nenhuma pendencia de avaria.")
        return linhas

    def _format_pendencia_financeira(self, aluguer: AluguerContentor) -> str:
        return (
            f"- #{aluguer.id} {aluguer.nome_cliente}: {Decimal(str(aluguer.valor or 0)):.2f} "
            f"| Cobrar: {whatsapp_link(aluguer.telefone_cliente)}"
        )

    def _format_pendencia_carga(self, aluguer: AluguerContentor) -> str:
        return (
            f"- #{aluguer.id} {aluguer.nome_cliente}: {aluguer.relato_carga or 'sem relato'} "
            f"| Resolver: resolver carga {aluguer.id}"
        )

    def _format_pendencia_avaria(self, aluguer: AluguerContentor) -> str:
        return (
            f"- #{aluguer.id} {aluguer.nome_cliente}: {aluguer.relato_avaria or 'sem relato'} "
            f"| Resolver: resolver avaria {aluguer.id}"
        )

    def _resolver_pendencia(self, tipo: str, text: str, telefone: str) -> str:
        if not self._is_authorized(telefone):
            return "Telefone nao autorizado para resolver pendencias."
        raw_id = text.split()[-1]
        if not raw_id.isdigit():
            return "Informe o ID do aluguer. Ex: resolver carga 12"
        aluguer_id = int(raw_id)
        try:
            if tipo == "carga":
                self.aluguer_service.resolver_pendencia_carga(aluguer_id, telefone)
                return f"Pendencia de carga do aluguer #{aluguer_id} resolvida."
            self.aluguer_service.resolver_pendencia_avaria(aluguer_id, telefone)
            return f"Pendencia de avaria do aluguer #{aluguer_id} resolvida."
        except ValueError as exc:
            return f"⚠️ {exc}"

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
        latitude = aluguer.entrega_latitude if aluguer.entrega_latitude is not None else aluguer.latitude
        longitude = aluguer.entrega_longitude if aluguer.entrega_longitude is not None else aluguer.longitude
        if latitude is not None and longitude is not None:
            localizacao = f"https://www.google.com/maps?q={latitude},{longitude}"
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

    def _has_active_flow(self, conversa: ConversaWhatsApp) -> bool:
        return (
            conversa.estado_atual in AluguerAgent.ACTIVE_STATES
            or conversa.estado_atual in GestaoAluguerAgent.ACTIVE_STATES
            or conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES
            or conversa.estado_atual in RecolhaAgent.ACTIVE_STATES
            or conversa.estado_atual in ContentorAgent.ACTIVE_STATES
        )

    def _is_expired(self, conversa: ConversaWhatsApp) -> bool:
        context = conversa.contexto_json or {}
        raw_updated_at = context.get("updated_at")
        if not raw_updated_at:
            return False
        try:
            updated_at = datetime.fromisoformat(raw_updated_at)
        except ValueError:
            return False
        return utcnow() - updated_at > timedelta(minutes=30)

    def _prompt_for_state(self, state: str) -> str:
        prompts = {
            "aguardando_numero_contentor": "🚛 Qual o numero do contentor?",
            "aguardando_foto_entrega": "📷 Por favor, envie a foto do contentor no local.",
            "aguardando_localizacao": "📍 Agora, envie a localizacao GPS do local.",
            "aguardando_nome_cliente": "👤 Qual o nome do cliente?",
            "aguardando_telefone_cliente": "📞 Envie o telefone do cliente.",
            "aguardando_email_cliente": "✉️ Qual o e-mail do cliente? Voce tambem pode responder Pular.",
            "aguardando_confirmacao_data_entrega": "📅 Confirma a entrega para hoje?\n\n1. Sim\n2. Outra data",
            "aguardando_data_entrega_manual": "📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA.",
            "aguardando_tipo_residuo": "🧱 Qual o tipo de residuo?\n\n1. Entulho Limpo\n2. Entulho Misto",
            "aguardando_valor": "💰 Qual o valor do servico?",
            "aguardando_forma_pagamento": "💳 Qual a forma de pagamento?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro",
            "aguardando_forma_pagamento_outro": "💳 Por favor, digite textualmente a forma de pagamento.",
            "aguardando_pago": "✅ O servico ja esta pago?\n\n1. Pago\n2. Pendente",
            "aguardando_confirmacao_final": "✅ Confira os dados e escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar.",
            "recolha_aguardando_selecao": "Escolha o pedido para recolha.",
            "recolha_aguardando_foto": "📷 Envie a foto do contentor para recolha.",
        }
        return prompts.get(state, "Envie a proxima informacao do cadastro.")

    def _is_authorized(self, telefone: str) -> bool:
        return self.operador_service.verificar_autorizacao(telefone)

    def _is_funcionario(self, telefone: str) -> bool:
        return self.operador_service.obter_perfil(telefone) == PerfilOperador.FUNCIONARIO

    def _can_create_pedido(self, telefone: str) -> bool:
        return self.operador_service.obter_perfil(telefone) != PerfilOperador.FUNCIONARIO

    def _initial_menu(self, telefone: str) -> str:
        if self._is_funcionario(telefone):
            return (
                "Ola, sou o Robo de Gestao de Contentores da OLT. O que vamos fazer agora?\n\n"
                "1. Confirmar recolha de contentor\n"
                "Digite recolha para abrir a lista."
            )
        return (
            "Ola, sou o Robo de Gestao de Contentores da OLT. O que vamos fazer agora?\n\n"
            "1. Cadastrar pedido de contentor\n"
            "2. Alterar informacoes\n"
            "3. Excluir pedidos\n"
            "4. Ver resumo\n"
            "5. Confirmar recolha de contentor"
        )
