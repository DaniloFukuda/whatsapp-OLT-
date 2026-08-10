from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.agents.entrega_agent import EntregaAgent
from app.agents.gestao_aluguer_agent import GestaoAluguerAgent
from app.agents.recolha_agent import RecolhaAgent
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.pagamento_pendente_agent import PagamentoPendenteAgent
from app.agents.renovacao_agent import RenovacaoAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import (
    AluguerContentor,
    EventoAluguer,
    StatusAluguer,
    StatusCiclo,
    StatusEntrega,
    StatusResolucao,
)
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.models.operador import PerfilOperador
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusOperacionalCarrinha,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
)
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService
from app.services.operador_service import OperadorService
from app.services.pedido_service import PedidoService


ACTIVE_ALUGUER_STATUSES = {StatusAluguer.ATIVO, StatusAluguer.VENCENDO, StatusAluguer.RENOVADO}
APP_DISPLAY_NAME = "OLT Gestão de Resíduos & Demolições"
UNAUTHORIZED_MESSAGE = "Telefone não autorizado."
FORBIDDEN_MESSAGE = "Operação não permitida."
AVARIAS_DISABLED_MESSAGE = "A funcionalidade de avarias não está disponível nesta empresa."
COMMANDS = {"resumo", "lista", "disponiveis", "alugados", "vencendo", "atrasados"}
START_COMMANDS = {"iniciar", "cadastrar", "comecar", "começar", "novo"}
ALTER_COMMANDS = {"alterar", "modificar"}
CONTENTOR_STATUS_COMMANDS = {"alterar contentor", "alterar status", "status contentor"}
DELETE_COMMANDS = {"excluir", "deletar"}
CONTENTOR_DELETE_COMMANDS = {"apagar", "remover", "excluir contentor", "excluir contentores"}
RENEW_COMMANDS = {"renovar", "prorrogar"}
ENTREGA_COMMANDS = {"entrega", "entregar", "entrega de contentor", "confirmar entrega"}
RECOLHA_COMMANDS = {"recolha", "recolher", "confirmar recolha", "confirmar recolha de contentor"}
MENU_COMMANDS = {"menu", "inicio", "início"}
CANCEL_COMMANDS = {"cancelar", "cancela", "sair", "parar", "voltar", "0"}
PAGAMENTO_PENDENTE_COMMANDS = {
    "registrar pagamento",
    "registrar pagamento pendente",
    "receber pagamento",
    "pagamentos pendentes",
    "pagamento pendente",
}
COMMAND_FAMILY_OPERATIONAL_QUERY = "operational_query"
COMMAND_FAMILY_COMMERCIAL_QUERY = "commercial_query"
COMMAND_FAMILY_ADMIN_MUTATION = "admin_mutation"
COMMAND_FAMILY_RESOLUTION = "resolution"
RESOLUCAO_AVARIA_REVISAO_STATE = "resolucao_avaria_revisao"
RESOLUCAO_AVARIA_CONTEXT_KEY = "_resolucao_avaria"
RESOLUCAO_AVARIA_CONFIRMAR = {"1", "confirmar resolucao", "resolucao_avaria:confirmar"}
RESOLUCAO_AVARIA_VOLTAR = {"2", "voltar", "resolucao_avaria:voltar"}
RESOLUCAO_AVARIA_CANCELAR = {"3", "cancelar", "resolucao_avaria:cancelar"}
RESOLUCAO_AVARIA_RELATO_DISPLAY_LIMIT = 1000
RESOLUCAO_AVARIA_RELATO_TRUNCADO = "[relato reduzido para exibição]"
COMMAND_PROFILES = {
    COMMAND_FAMILY_OPERATIONAL_QUERY: {PerfilOperador.FUNCIONARIO, PerfilOperador.GESTOR},
    COMMAND_FAMILY_COMMERCIAL_QUERY: {PerfilOperador.GESTOR},
    COMMAND_FAMILY_ADMIN_MUTATION: {PerfilOperador.GESTOR},
    COMMAND_FAMILY_RESOLUTION: {PerfilOperador.GESTOR},
}
MAIN_MENU = (
    f"🤖 Menu Principal • {APP_DISPLAY_NAME}\n\n"
    "1️⃣ 📝 Novo pedido\n"
    "2️⃣ 🚛 Entrega de contentor\n"
    "3️⃣ 📦 Recolha de contentor\n"
    "4️⃣ ✏️ Alterar registro\n"
    "5️⃣ 🗑️ Apagar registro\n"
    "6️⃣ 📊 Resumo dos contentores\n"
    "7️⃣ 🛠️ Manutencao / avarias\n"
    "0️⃣ ❌ Sair\n\n"
    "Digite o número da opção desejada."
)
# Menu v2.4: cinco caminhos restritos, convertidos em lista interativa pelo cliente.
MAIN_MENU = (
    f"Menu principal - {APP_DISPLAY_NAME}\n\n"
    "1. Novo pedido\n"
    "2. Entrega de contentor\n"
    "3. Recolha de contentor\n"
    "4. Confirmar Despejo no Vazadouro\n"
    "5. Resumo dos contentores"
)
MAIN_MENU = (
    f"🤖 Menu Principal • {APP_DISPLAY_NAME}\n\n"
    "1. 🟢 Novo Pedido\n"
    "2. 🚛 Confirmar Chegada / Entrega\n"
    "3. 📦 Confirmar Recolha / Partida\n"
    "4. ♻️ Confirmar Despejo no Vazadouro\n"
    "5. 📊 Painel de Controle Operacional\n\n"
    "Digite o número da opção desejada."
)
CANCELLED_MENU_MESSAGE = "Operação cancelada. Nenhuma alteração foi salva.\n\n" + MAIN_MENU


class WhatsappRouterAgent:
    def __init__(self, db: Session):
        self.db = db
        self.aluguer_agent = AluguerAgent(db)
        self.gestao_aluguer_agent = GestaoAluguerAgent(db)
        self.entrega_agent = EntregaAgent(db)
        self.renovacao_agent = RenovacaoAgent(db)
        self.contentor_agent = ContentorAgent(db)
        self.recolha_agent = RecolhaAgent(db)
        self.pedido_v24_agent = PedidoV24Agent(db)
        self.pagamento_pendente_agent = PagamentoPendenteAgent(db)
        self.pedido_service = PedidoService(db)
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.operador_service = OperadorService(db)
        self._pending_messages: list[str] = []

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        self._pending_messages = []
        text = (message.texto or "").strip().lower()
        decisao = self.operador_service.decidir_acesso(message.telefone)
        if not decisao.autorizado:
            conversa_existente = (
                self.db.query(ConversaWhatsApp)
                .filter(ConversaWhatsApp.telefone == message.telefone)
                .first()
            )
            if (
                conversa_existente
                and conversa_existente.estado_atual in PagamentoPendenteAgent.ACTIVE_STATES
            ):
                conversa_existente.estado_atual = "idle"
                conversa_existente.contexto_json = {}
                self.db.commit()
                return FORBIDDEN_MESSAGE
            return UNAUTHORIZED_MESSAGE
        perfil = decisao.perfil
        if perfil is None:
            return UNAUTHORIZED_MESSAGE
        if text in PAGAMENTO_PENDENTE_COMMANDS:
            operador = self.operador_service.buscar_por_telefone(message.telefone)
            if (
                not operador
                or not operador.ativo
                or operador.perfil != PerfilOperador.GESTOR
            ):
                return FORBIDDEN_MESSAGE
        conversa = self._get_or_create_conversa(message.telefone)

        if not self._avarias_enabled():
            recovered = self._recover_disabled_avaria_review(conversa)
            if recovered:
                return AVARIAS_DISABLED_MESSAGE + " A revisão foi cancelada com segurança e o fluxo anterior foi retomado."
            if text.startswith("resolver avaria") or text in {
                "avaria", "avarias", "listar avaria", "listar avarias", "lista avaria", "lista avarias",
                "resolucao_avaria:confirmar", "resolucao_avaria:voltar", "resolucao_avaria:cancelar",
            }:
                return AVARIAS_DISABLED_MESSAGE

        if text.startswith("resolver avaria"):
            if not self._can_execute_command(perfil, COMMAND_FAMILY_RESOLUTION):
                return FORBIDDEN_MESSAGE
            return self._iniciar_revisao_avaria(conversa, text, message.telefone)

        if text == "resolucao_avaria:confirmar" and conversa.estado_atual == "idle":
            if not self._can_execute_command(perfil, COMMAND_FAMILY_RESOLUTION):
                return FORBIDDEN_MESSAGE
            ultima = self._contexto_dict(conversa.contexto_json).get(
                "_ultima_resolucao_avaria"
            )
            ultima = self._contexto_dict(ultima)
            if ultima.get("id") and ultima.get("origem"):
                return self._mensagem_avaria_ja_resolvida(ultima["origem"], int(ultima["id"]))

        if conversa.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_RESOLUTION):
                return FORBIDDEN_MESSAGE
            return self._handle_revisao_avaria(conversa, text, message.telefone)

        if text in MENU_COMMANDS:
            if self._has_active_flow(conversa):
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
            return self._initial_menu(perfil)

        if text in CANCEL_COMMANDS and not (text == "0" and conversa.estado_atual == "v24_entrega_adesivo"):
            if self._has_active_flow(conversa):
                is_v24_flow = conversa.estado_atual.startswith(PedidoV24Agent.PREFIX)
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                if is_v24_flow:
                    self._queue_initial_menu(perfil)
                    return "Operação cancelada. Nenhuma alteração foi salva."
                return CANCELLED_MENU_MESSAGE
            return "Nenhuma operação em andamento para cancelar.\n\n" + self._initial_menu(perfil)

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

        if conversa.estado_atual in PagamentoPendenteAgent.ACTIVE_STATES:
            operador = self.operador_service.buscar_por_telefone(message.telefone)
            if (
                perfil != PerfilOperador.GESTOR
                or not operador
                or not operador.ativo
                or operador.perfil != PerfilOperador.GESTOR
            ):
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return FORBIDDEN_MESSAGE
            return self.pagamento_pendente_agent.handle(
                conversa, message, operador.telefone_whatsapp
            )

        if text in PAGAMENTO_PENDENTE_COMMANDS:
            operador = self.operador_service.buscar_por_telefone(message.telefone)
            if (
                perfil != PerfilOperador.GESTOR
                or not operador
                or not operador.ativo
                or operador.perfil != PerfilOperador.GESTOR
            ):
                return FORBIDDEN_MESSAGE
            return self.pagamento_pendente_agent.start(conversa)

        if conversa.estado_atual.startswith(PedidoV24Agent.PREFIX):
            return self._finalize_response(
                self.pedido_v24_agent.handle(conversa, message),
                conversa,
                message.telefone,
                perfil,
                append_menu_on_success=True,
            )

        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES and self._is_expired(conversa):
            context = dict(conversa.contexto_json or {})
            context["estado_anterior"] = conversa.estado_atual
            conversa.contexto_json = context
            return self.aluguer_agent.timeout_prompt(conversa)

        if text.startswith("resolver carga"):
            if not self._can_execute_command(perfil, COMMAND_FAMILY_RESOLUTION):
                return FORBIDDEN_MESSAGE
            return self._resolver_pendencia("carga", text, message.telefone, perfil)
        if text in COMMANDS:
            family = self._classify_top_level_command(text)
            if family and not self._can_execute_command(perfil, family):
                return FORBIDDEN_MESSAGE
            return self._handle_operational_command(text, message.telefone, perfil)

        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self._finalize_response(self.aluguer_agent.handle(conversa, message), conversa, message.telefone, perfil)
        if conversa.estado_atual in GestaoAluguerAgent.ACTIVE_STATES:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self._finalize_response(self.gestao_aluguer_agent.handle(conversa, message), conversa, message.telefone, perfil)
        if conversa.estado_atual in EntregaAgent.ACTIVE_STATES:
            return self._finalize_response(self.entrega_agent.handle(conversa, message), conversa, message.telefone, perfil)
        if conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self._finalize_response(self.renovacao_agent.handle(conversa, message), conversa, message.telefone, perfil)
        if conversa.estado_atual in RecolhaAgent.ACTIVE_STATES:
            return self._finalize_response(self.recolha_agent.handle(conversa, message), conversa, message.telefone, perfil)
        if conversa.estado_atual in ContentorAgent.ACTIVE_STATES:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self._finalize_response(self.contentor_agent.handle(conversa, message), conversa, message.telefone, perfil)

        if text == "5":
            return self._handle_operational_command("resumo", message.telefone, perfil)

        if text in {"1", "novo pedido", "cadastrar pedido"}:
            if perfil == PerfilOperador.FUNCIONARIO:
                return "Seu perfil de motorista nÃ£o possui permissÃ£o para cadastrar pedidos."
            return self.pedido_v24_agent.start_cadastro(conversa)
        if text in {
            "2", "confirmar entrega de contentor", "confirmar entrega do lote",
            "confirmar chegada", "confirmar chegada / entrega", "chegada",
        }:
            if (
                self.pedido_service.pedidos_pendentes_entrega()
                or self.pedido_service.carrinhas_aguardando_chegada()
            ):
                return self.pedido_v24_agent.start_entrega(conversa)
            return self.entrega_agent.start(conversa)
        if text in {
            "3", "confirmar recolha de contentor", "confirmar partida",
            "confirmar recolha / partida", "partida",
        }:
            if (
                self.pedido_service.contentores_para_recolha()
                or self.pedido_service.carrinhas_aguardando_partida()
            ):
                return self.pedido_v24_agent.start_recolha(conversa)
            return self.recolha_agent.start(conversa)
        if text in {"4", "confirmar despejo no vazadouro", "confirmar despejo"}:
            return self.pedido_v24_agent.start_despejo(conversa)

        if text == "6":
            return self._handle_operational_command("resumo", message.telefone, perfil)
        if text in ENTREGA_COMMANDS or (text == "2" and perfil != PerfilOperador.FUNCIONARIO) or (
            text == "1" and perfil == PerfilOperador.FUNCIONARIO
        ):
            return self.entrega_agent.start(conversa)
        if text in RECOLHA_COMMANDS or text == "3" or (text == "2" and perfil == PerfilOperador.FUNCIONARIO):
            return self.recolha_agent.start(conversa)
        if text in START_COMMANDS:
            if perfil == PerfilOperador.FUNCIONARIO:
                return "Seu perfil de motorista nao possui permissao para cadastrar pedidos. Use a opcao de recolha."
            return self.aluguer_agent.start(conversa)
        if text == "1":
            if perfil == PerfilOperador.FUNCIONARIO:
                return self.recolha_agent.start(conversa)
            return self.aluguer_agent.start(conversa)
        if text in CONTENTOR_STATUS_COMMANDS:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self.contentor_agent.start_alteracao_status(conversa)
        if text in ALTER_COMMANDS:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text == "4":
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text in DELETE_COMMANDS:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            if text == "excluir" and not self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7):
                return self.contentor_agent.start_exclusao(conversa)
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in CONTENTOR_DELETE_COMMANDS:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self.contentor_agent.start_exclusao(conversa)
        if text == "5":
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in RENEW_COMMANDS:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_ADMIN_MUTATION):
                return FORBIDDEN_MESSAGE
            return self.renovacao_agent.start(conversa)
        if text in {"contentores", "status"}:
            if not self._can_execute_command(perfil, COMMAND_FAMILY_OPERATIONAL_QUERY):
                return FORBIDDEN_MESSAGE
            return self.contentor_agent.listar_status()
        return self._initial_menu(perfil)

    def _classify_top_level_command(self, text: str) -> str | None:
        if text in {"lista", "disponiveis", "contentores", "status", "resumo"}:
            return COMMAND_FAMILY_OPERATIONAL_QUERY
        if text in {"alugados", "vencendo", "atrasados"}:
            return COMMAND_FAMILY_COMMERCIAL_QUERY
        if (
            text in ALTER_COMMANDS
            or text in DELETE_COMMANDS
            or text in RENEW_COMMANDS
            or text in CONTENTOR_STATUS_COMMANDS
            or text in CONTENTOR_DELETE_COMMANDS
        ):
            return COMMAND_FAMILY_ADMIN_MUTATION
        if text.startswith("resolver carga") or text.startswith("resolver avaria"):
            return COMMAND_FAMILY_RESOLUTION
        return None

    def _can_execute_command(self, perfil: PerfilOperador, family: str) -> bool:
        return perfil in COMMAND_PROFILES[family]

    @staticmethod
    def _avarias_enabled() -> bool:
        return get_settings().feature_avarias_enabled

    def _recover_disabled_avaria_review(self, conversa: ConversaWhatsApp) -> bool:
        if conversa.estado_atual != RESOLUCAO_AVARIA_REVISAO_STATE:
            return False
        contexto = self._contexto_dict(conversa.contexto_json)
        revisao = self._contexto_dict(contexto.get(RESOLUCAO_AVARIA_CONTEXT_KEY))
        if isinstance(revisao.get("contexto_anterior"), dict):
            conversa.estado_atual = revisao.get("estado_anterior") or "idle"
            conversa.contexto_json = deepcopy(revisao["contexto_anterior"])
        else:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
        self.db.commit()
        return True

    def _handle_operational_command(
        self,
        command: str,
        telefone: str | None = None,
        perfil: PerfilOperador | None = None,
    ) -> str:
        if command == "resumo":
            perfil = perfil or self.operador_service.decidir_acesso(telefone).perfil or PerfilOperador.FUNCIONARIO
            self._queue_initial_menu(perfil)
            return self._painel_v32(perfil)
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

    def _painel_v32(self, perfil: PerfilOperador) -> str:
        return self._painel_v4(perfil)

    def _painel_v4(self, perfil: PerfilOperador) -> str:
        today = datetime.now(self._timezone()).date()
        tomorrow = today + timedelta(days=1)
        pedidos = self._pedidos_v32()
        alugueres = self._alugueres_v4()
        linhas = [
            "📊 *PAINEL DE CONTROLE OPERACIONAL OLT*",
            f"📅 Data: {today:%d/%m/%Y}",
            "",
            self._painel_v4_acoes_hoje(pedidos, alugueres, today, perfil),
            "",
            self._painel_v4_proximos_dias(pedidos, alugueres, tomorrow),
            "",
            self._painel_v4_pendencias(pedidos, alugueres, today, perfil),
        ]
        if perfil == PerfilOperador.GESTOR:
            linhas.extend(["", self._painel_v4_financeiro(pedidos, alugueres, today)])
            if self.pedido_service.pedidos_pagamento_pendente():
                linhas.extend(["", "💶 Registrar pagamento pendente"])
        return "\n".join(linhas)

    def _painel_v4_acoes_hoje(self, pedidos: list[Pedido], alugueres: list[AluguerContentor], today, perfil: PerfilOperador) -> str:
        settings = get_settings()
        blocos = []
        entregas = (
            self._pedidos_por_entrega(pedidos, today, TipoEquipamentoPedido.CONTENTOR.value)
            if settings.feature_contentores_enabled
            else []
        )
        if entregas:
            blocos.append("📦 *Entrega de Contentores:*\n" + "\n".join(
                self._format_entrega_hoje(pedido, itens, incluir_horario=False)
                for pedido, itens in entregas
            ))
        carrinhas = (
            self._pedidos_por_entrega(pedidos, today, TipoEquipamentoPedido.CARRINHA.value)
            if settings.feature_carrinhas_enabled
            else []
        )
        if carrinhas:
            blocos.append("🚛 *Chegada de Carrinhas:*\n" + "\n".join(
                self._format_entrega_hoje(pedido, itens, incluir_horario=True)
                for pedido, itens in carrinhas
            ))
        carrinhas_em_atendimento = (
            self._carrinhas_em_atendimento(pedidos)
            if settings.feature_carrinhas_enabled
            else []
        )
        if carrinhas_em_atendimento:
            blocos.append(
                "⏱️ *Carrinhas em atendimento:*\n"
                + "\n".join(
                    self._format_carrinha_em_atendimento(item)
                    for item in carrinhas_em_atendimento
                )
            )
        if perfil == PerfilOperador.GESTOR and settings.feature_contentores_enabled:
            renovacoes = self._contentores_vencendo_amanha(pedidos, today)
            renovacoes_legadas = self._alugueres_por_vencimento(alugueres, today + timedelta(days=1))
            if renovacoes:
                blocos.append("🔄 *Renovações:*\n" + "\n".join(
                    self._format_renovacao(pedido, itens) for pedido, itens in renovacoes
                ))
            if renovacoes_legadas:
                bloco = "🔄 *Renovações:*" if not renovacoes else ""
                linhas = [bloco] if bloco else []
                linhas.extend(self._format_renovacao_aluguer(aluguer) for aluguer in renovacoes_legadas)
                blocos.append("\n".join(linhas))
        conteudo = "\n\n".join(blocos) if blocos else "• Nenhuma ação para hoje."
        return "🟢 *1. AÇÕES PARA HOJE*\n" + conteudo

    def _painel_v4_proximos_dias(self, pedidos: list[Pedido], alugueres: list[AluguerContentor], tomorrow) -> str:
        settings = get_settings()
        blocos = []
        recolhas = (
            self._contentores_para_recolha_em(pedidos, tomorrow)
            if settings.feature_contentores_enabled
            else []
        )
        recolhas_legadas = (
            self._alugueres_por_vencimento(alugueres, tomorrow)
            if settings.feature_contentores_enabled
            else []
        )
        if recolhas or recolhas_legadas:
            linhas = [f"• {pedido.nome_cliente} ({len(itens)} un)" for pedido, itens in recolhas]
            linhas.extend(f"• {aluguer.nome_cliente} (1 un)" for aluguer in recolhas_legadas)
            blocos.append("📦 *Recolher Amanhã:*\n" + "\n".join(
                linhas
            ))
        carrinhas = (
            self._pedidos_por_entrega(pedidos, tomorrow, TipoEquipamentoPedido.CARRINHA.value)
            if settings.feature_carrinhas_enabled
            else []
        )
        if carrinhas:
            blocos.append("🚛 *Carrinhas para Amanhã:*\n" + "\n".join(
                self._format_carrinha_amanha(pedido, itens) for pedido, itens in carrinhas
            ))
        conteudo = "\n\n".join(blocos) if blocos else "• Nenhuma ação agendada."
        return "🔵 *2. AÇÕES AGENDADAS PARA OS PRÓXIMOS DIAS*\n" + conteudo

    def _painel_v4_pendencias(self, pedidos: list[Pedido], alugueres: list[AluguerContentor], today, perfil: PerfilOperador) -> str:
        settings = get_settings()
        blocos = []
        vencidos = self._contentores_vencidos(pedidos, today) if settings.feature_contentores_enabled else []
        vencidos_legados = self._alugueres_vencidos(alugueres, today) if settings.feature_contentores_enabled else []
        if vencidos or vencidos_legados:
            linhas = [
                self._format_contentor_vencido(pedido, data_entrega, itens, today)
                for pedido, data_entrega, itens in vencidos
            ]
            linhas.extend(self._format_aluguer_vencido(aluguer, today) for aluguer in vencidos_legados)
            blocos.append("⏳ *Contentores com Prazo Vencido:*\n" + "\n".join(
                linhas
            ))
        if perfil == PerfilOperador.GESTOR:
            pendentes = self._pedidos_pagamento_pendente(pedidos)
            pendentes_legados = self._alugueres_pagamento_pendente(alugueres)
            if pendentes or pendentes_legados:
                linhas = [self._format_pagamento_pendente(pedido) for pedido in pendentes]
                linhas.extend(self._format_pagamento_pendente_aluguer(aluguer) for aluguer in pendentes_legados)
                blocos.append("💳 *Pagamentos Pendentes:*\n" + "\n".join(
                    linhas
                ))
        avarias = self._avarias_ativas(pedidos) if self._avarias_enabled() else []
        avarias_legadas = self._alugueres_avarias_ativas(alugueres) if self._avarias_enabled() else []
        if avarias or avarias_legadas:
            linhas = [self._format_avaria(item) for item in avarias]
            linhas.extend(self._format_avaria_aluguer(aluguer) for aluguer in avarias_legadas)
            blocos.append("🛠️ *Avarias em Equipamentos:*\n" + "\n".join(
                linhas
            ))
        conteudo = "\n\n".join(blocos) if blocos else "• Nenhuma pendência ativa."
        return "🚨 *3. PENDÊNCIAS ATIVAS*\n" + conteudo

    def _painel_v4_financeiro(self, pedidos: list[Pedido], alugueres: list[AluguerContentor], today) -> str:
        totais = self._financeiro_mes(pedidos, today)
        for aluguer in alugueres:
            criado_em = self._local_date(aluguer.criado_em)
            if criado_em.year != today.year or criado_em.month != today.month:
                continue
            status = "pago" if aluguer.pago else "pendente"
            totais[f"contentores_{status}"] += Decimal(str(aluguer.valor or 0))
        faturado = totais["contentores_pago"] + totais["carrinhas_pago"]
        receber = totais["contentores_pendente"] + totais["carrinhas_pendente"]
        sep = "════════════════════════"
        return "\n".join(
            [
                "💶 *4. RESUMO FINANCEIRO DO MÊS*",
                sep,
                "📦 *FATURAMENTO CONTENTORES*",
                f"• Pago: {self._money(totais['contentores_pago'])} | Pendente: {self._money(totais['contentores_pendente'])}",
                "",
                "🚛 *FATURAMENTO CARRINHAS*",
                f"• Pago: {self._money(totais['carrinhas_pago'])} | Pendente: {self._money(totais['carrinhas_pendente'])}",
                sep,
                f"🟢 Total faturado — caixa: {self._money(faturado)}",
                f"🟡 Total a receber: {self._money(receber)}",
                f"📊 Total projetado: {self._money(faturado + receber)}",
                sep,
            ]
        )

    def _alugueres_v4(self) -> list[AluguerContentor]:
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.is_deleted.is_(False))
            .order_by(AluguerContentor.data_vencimento, AluguerContentor.nome_cliente, AluguerContentor.id)
            .all()
        )

    def _alugueres_por_vencimento(self, alugueres: list[AluguerContentor], target_date) -> list[AluguerContentor]:
        return [
            aluguer for aluguer in alugueres
            if aluguer.status_entrega == StatusEntrega.ENTREGUE.value
            and aluguer.status_ciclo == StatusCiclo.EM_ANDAMENTO.value
            and aluguer.status not in {StatusAluguer.CANCELADO, StatusAluguer.RECOLHIDO}
            and self._local_date(aluguer.data_vencimento) == target_date
        ]

    def _alugueres_vencidos(self, alugueres: list[AluguerContentor], today) -> list[AluguerContentor]:
        return [
            aluguer for aluguer in alugueres
            if aluguer.status_entrega == StatusEntrega.ENTREGUE.value
            and aluguer.status_ciclo == StatusCiclo.EM_ANDAMENTO.value
            and aluguer.status not in {StatusAluguer.CANCELADO, StatusAluguer.RECOLHIDO}
            and self._local_date(aluguer.data_vencimento) < today
        ]

    def _alugueres_pagamento_pendente(self, alugueres: list[AluguerContentor]) -> list[AluguerContentor]:
        return [
            aluguer for aluguer in alugueres
            if not aluguer.pago
            and aluguer.status not in {StatusAluguer.CANCELADO}
        ]

    def _alugueres_avarias_ativas(self, alugueres: list[AluguerContentor]) -> list[AluguerContentor]:
        return [
            aluguer for aluguer in alugueres
            if aluguer.contentor_avariado
            and aluguer.status_resolucao_avaria == StatusResolucao.PENDENTE.value
        ]

    def _pedidos_por_entrega(self, pedidos: list[Pedido], target_date, tipo: str) -> list[tuple[Pedido, list[PedidoContentor]]]:
        resultado = []
        for pedido in pedidos:
            if self._planned_date(pedido.data_planejada) != target_date:
                continue
            itens = [
                item for item in pedido.contentores
                if item.tipo_equipamento == tipo
                and item.status_entrega == StatusEntregaPedido.PENDENTE.value
                and item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
            ]
            if itens:
                resultado.append((pedido, self._sort_itens(itens)))
        return sorted(resultado, key=lambda entry: (entry[0].data_planejada, self._primeiro_horario(entry[1]), entry[0].nome_cliente, entry[0].id))

    def _contentores_para_recolha_em(self, pedidos: list[Pedido], target_date) -> list[tuple[Pedido, list[PedidoContentor]]]:
        resultado = []
        entrega_alvo = target_date - timedelta(days=5)
        for pedido in pedidos:
            itens = [
                item for item in pedido.contentores
                if self._is_contentor_para_recolha(item)
                and item.entrega_data_hora
                and self._local_date(item.entrega_data_hora) == entrega_alvo
                and item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
            ]
            if itens:
                resultado.append((pedido, self._sort_itens(itens)))
        return sorted(resultado, key=lambda entry: (entry[0].nome_cliente, entry[0].id))

    def _contentores_vencendo_amanha(self, pedidos: list[Pedido], today) -> list[tuple[Pedido, list[PedidoContentor]]]:
        entrega_alvo = today - timedelta(days=4)
        resultado = []
        for pedido in pedidos:
            itens = [
                item for item in pedido.contentores
                if self._is_contentor_para_recolha(item)
                and item.entrega_data_hora
                and self._local_date(item.entrega_data_hora) == entrega_alvo
                and item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
            ]
            if itens:
                resultado.append((pedido, self._sort_itens(itens)))
        return sorted(resultado, key=lambda entry: (entry[0].nome_cliente, entry[0].id))

    def _contentores_vencidos(self, pedidos: list[Pedido], today) -> list[tuple[Pedido, object, list[PedidoContentor]]]:
        grupos = []
        limite = today - timedelta(days=5)
        for pedido in pedidos:
            por_data = {}
            for item in pedido.contentores:
                if not (
                    self._is_contentor_para_recolha(item)
                    and item.entrega_data_hora
                    and self._local_date(item.entrega_data_hora) < limite
                    and item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
                ):
                    continue
                data_entrega = self._local_date(item.entrega_data_hora)
                por_data.setdefault(data_entrega, []).append(item)
            for data_entrega, itens in por_data.items():
                grupos.append((pedido, data_entrega, self._sort_itens(itens)))
        return sorted(grupos, key=lambda entry: (entry[1], entry[0].nome_cliente, entry[0].id))

    def _pedidos_pagamento_pendente(self, pedidos: list[Pedido]) -> list[Pedido]:
        return sorted(
            [pedido for pedido in pedidos if pedido.status_pagamento == StatusPagamento.PENDENTE.value],
            key=lambda pedido: (pedido.nome_cliente, pedido.id),
        )

    def _avarias_ativas(self, pedidos: list[Pedido]) -> list[PedidoContentor]:
        itens = [
            item for pedido in pedidos for item in pedido.contentores
            if item.contentor_avariado
            and item.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
        ]
        return sorted(itens, key=lambda item: (item.pedido.nome_cliente, self._equipamento_numero(item), item.id))

    def _financeiro_mes(self, pedidos: list[Pedido], today) -> dict[str, Decimal]:
        totais = {
            "contentores_pago": Decimal("0"),
            "contentores_pendente": Decimal("0"),
            "carrinhas_pago": Decimal("0"),
            "carrinhas_pendente": Decimal("0"),
        }
        for pedido in pedidos:
            criado_em = self._local_date(pedido.criado_em)
            if criado_em.year != today.year or criado_em.month != today.month:
                continue
            tipos = {
                item.tipo_equipamento for item in pedido.contentores
                if item.tipo_equipamento in {
                    TipoEquipamentoPedido.CONTENTOR.value,
                    TipoEquipamentoPedido.CARRINHA.value,
                }
            }
            if len(tipos) != 1:
                continue
            tipo = "contentores" if tipos == {TipoEquipamentoPedido.CONTENTOR.value} else "carrinhas"
            status = "pago" if pedido.status_pagamento == StatusPagamento.PAGO.value else "pendente"
            totais[f"{tipo}_{status}"] += Decimal(str(pedido.valor_global or 0))
        return totais

    def _format_entrega_hoje(self, pedido: Pedido, itens: list[PedidoContentor], incluir_horario: bool) -> str:
        linhas = [f"• {pedido.nome_cliente} ({len(itens)} un)"]
        if incluir_horario:
            linhas.append(f"  ⏰ Horário: {self._horarios_label(itens)}")
        linhas.append(f"  {self._endereco_linha(pedido)}")
        return "\n".join(linhas)

    def _format_carrinha_amanha(self, pedido: Pedido, itens: list[PedidoContentor]) -> str:
        return f"• {pedido.nome_cliente} ({len(itens)} un) • ⏰ {self._horarios_label(itens)}"

    def _carrinhas_em_atendimento(self, pedidos: list[Pedido]) -> list[PedidoContentor]:
        return sorted(
            [
                item
                for pedido in pedidos
                for item in pedido.contentores
                if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
                and item.status_operacional_carrinha
                == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
            ],
            key=lambda item: (item.partida_prevista_carrinha_data_hora or item.criado_em, item.id),
        )

    def _format_carrinha_em_atendimento(self, item: PedidoContentor) -> str:
        previsao = self.pedido_service.previsao_partida_carrinha(item)
        agora = utcnow()
        if self.pedido_service.carrinha_atrasada(item, agora):
            situacao = "🔴 Atrasada"
        elif previsao and agora >= previsao - timedelta(minutes=30):
            situacao = "🟡 Próxima do prazo"
        else:
            situacao = "🟢 Dentro do prazo"
        hora = previsao.astimezone(self._timezone()).strftime("%H:%M") if previsao else "não informada"
        return f"• {item.pedido.nome_cliente} — {situacao} — partida prevista {hora}"

    def _format_renovacao(self, pedido: Pedido, itens: list[PedidoContentor]) -> str:
        linhas = [f"• {pedido.nome_cliente} ({len(itens)} un) • Valor: {self._money(pedido.valor_global)}"]
        linhas.append(f"  💬 {self._contacto_linha('Entrar em contacto', pedido.telefone_cliente)}")
        return "\n".join(linhas)

    def _format_renovacao_aluguer(self, aluguer: AluguerContentor) -> str:
        linhas = [f"• {aluguer.nome_cliente} (1 un) • Valor: {self._money(aluguer.valor)}"]
        linhas.append(f"  💬 {self._contacto_linha('Entrar em contacto', aluguer.telefone_cliente)}")
        return "\n".join(linhas)

    def _format_pagamento_pendente(self, pedido: Pedido) -> str:
        linhas = [f"• {pedido.nome_cliente} • Em dívida: {self._money(pedido.valor_global)}"]
        linhas.append(f"  👤 {self._contacto_linha('Cobrar cliente', pedido.telefone_cliente)}")
        return "\n".join(linhas)

    def _format_pagamento_pendente_aluguer(self, aluguer: AluguerContentor) -> str:
        linhas = [f"• {aluguer.nome_cliente} • Em dívida: {self._money(aluguer.valor)}"]
        linhas.append(f"  👤 {self._contacto_linha('Cobrar cliente', aluguer.telefone_cliente)}")
        return "\n".join(linhas)

    def _format_contentor_vencido(self, pedido: Pedido, data_entrega, itens: list[PedidoContentor], today) -> str:
        numeros = self._format_lista_numeros(self._unique_sorted(self._equipamento_numero(item) for item in itens))
        dias = (today - (data_entrega + timedelta(days=5))).days
        sufixo = "dia" if dias == 1 else "dias"
        return "\n".join([f"• {pedido.nome_cliente}", f"  Nºs: {numeros}", f"  Vencido há {dias} {sufixo}"])

    def _format_aluguer_vencido(self, aluguer: AluguerContentor, today) -> str:
        dias = (today - self._local_date(aluguer.data_vencimento)).days
        sufixo = "dia" if dias == 1 else "dias"
        return "\n".join([f"• {aluguer.nome_cliente}", f"  Nºs: {aluguer.numero_contentor}", f"  Vencido há {dias} {sufixo}"])

    def _format_avaria(self, item: PedidoContentor) -> str:
        comando = quote(f"resolver avaria {item.id}")
        return "\n".join(
            [
                f"• {item.pedido.nome_cliente}",
                f"  Equipamento Nº {self._equipamento_numero(item)}",
                f"  📝 Dano: {item.relato_avaria or 'sem relato'}",
                f"  💬 Resolver: https://wa.me/?text={comando}",
            ]
        )

    def _format_avaria_aluguer(self, aluguer: AluguerContentor) -> str:
        comando = quote(f"resolver avaria {aluguer.id}")
        return "\n".join(
            [
                f"• {aluguer.nome_cliente}",
                f"  Equipamento Nº {aluguer.numero_contentor}",
                f"  📝 Dano: {aluguer.relato_avaria or 'sem relato'}",
                f"  💬 Resolver: https://wa.me/?text={comando}",
            ]
        )

    def _endereco_linha(self, pedido: Pedido) -> str:
        url = self._endereco_url(pedido)
        if url:
            return f"📍 Abrir endereço: {url}"
        endereco = (pedido.endereco_aproximado or "").strip()
        return f"📍 Endereço: {endereco}" if endereco else "📍 Endereço não informado"

    def _endereco_url(self, pedido: Pedido) -> str:
        endereco = (pedido.endereco_aproximado or "").strip()
        if endereco.startswith(("http://", "https://")):
            return endereco
        if pedido.endereco_latitude is not None and pedido.endereco_longitude is not None:
            return f"https://www.google.com/maps?q={pedido.endereco_latitude},{pedido.endereco_longitude}"
        return ""

    def _contacto_linha(self, label: str, telefone: str | None) -> str:
        telefone_normalizado = normalize_phone(telefone) if telefone else ""
        if telefone_normalizado and len(telefone_normalizado) >= 9:
            return f"{label}: https://wa.me/{telefone_normalizado}"
        return f"{label}: telefone não informado"

    def _horarios_label(self, itens: list[PedidoContentor]) -> str:
        horarios = self._unique_sorted(item.horario_agendado for item in itens if item.horario_agendado)
        return ", ".join(horarios) if horarios else "Horário não informado"

    def _primeiro_horario(self, itens: list[PedidoContentor]) -> str:
        horarios = self._unique_sorted(item.horario_agendado for item in itens if item.horario_agendado)
        return horarios[0] if horarios else ""

    def _sort_itens(self, itens: list[PedidoContentor]) -> list[PedidoContentor]:
        return sorted(itens, key=lambda item: (self._equipamento_numero(item), item.id))

    def _equipamento_numero(self, item: PedidoContentor) -> str:
        return str(item.numero_adesivo_contentor or item.id)

    def _format_lista_numeros(self, numeros: list[str]) -> str:
        if len(numeros) <= 1:
            return numeros[0] if numeros else "não informado"
        return ", ".join(numeros[:-1]) + f" e {numeros[-1]}"

        today = self._local_date(utcnow())
        tomorrow = today + timedelta(days=1)
        pedidos = self._pedidos_v32()
        linhas = [
            "📊 PAINEL DE CONTROLE OPERACIONAL OLT",
            f"📅 Data: {today:%d/%m/%Y}",
        ]
        if perfil == PerfilOperador.GESTOR:
            linhas.extend(
                [
                    "",
                    "🗓️ 1. VENCEM AMANHA",
                    "ACAO COMERCIAL",
                    self._painel_v32_vencem_amanha(pedidos, today),
                ]
            )
        linhas.extend(
            [
                "",
                "🚛 2. RECOLHER HOJE",
                "URGENTE",
                self._painel_v32_recolhas(pedidos, today, "hoje"),
                "",
                "🚚 3. RECOLHER AMANHA",
                "PLANEAMENTO",
                self._painel_v32_recolhas(pedidos, tomorrow, "amanha"),
                "",
                "🚨 4. PENDENCIAS ATIVAS",
                self._painel_v32_pendencias(perfil),
            ]
        )
        if perfil == PerfilOperador.GESTOR:
            linhas.extend(
                [
                    "",
                    "💰 5. RESUMO FINANCEIRO DO MES",
                    self._painel_v32_financeiro(pedidos, today),
                ]
            )
        return "\n".join(linhas)

    def _pedidos_v32(self) -> list[Pedido]:
        return (
            self.db.query(Pedido)
            .order_by(Pedido.data_planejada, Pedido.id)
            .all()
        )

    def _painel_v32_vencem_amanha(self, pedidos: list[Pedido], today) -> str:
        linhas = []
        for pedido in sorted(pedidos, key=lambda pedido: (pedido.data_planejada, pedido.nome_cliente, pedido.id)):
            contentores = [
                item for item in pedido.contentores
                if self._is_contentor_para_recolha(item)
                and item.entrega_data_hora
                and self._local_date(item.entrega_data_hora) == today - timedelta(days=4)
            ]
            if not contentores:
                continue
            linha = [
                f"• {pedido.nome_cliente} ({len(contentores)} Contentores)",
                f"  Valor: {self._money(pedido.valor_global)}",
            ]
            if pedido.telefone_cliente:
                linha.append(f"  💬 Enviar mensagem de renovacao: {whatsapp_link(pedido.telefone_cliente)}")
            linhas.append("\n".join(linha))
        return "\n".join(linhas) if linhas else "Nenhum contentor vencendo amanha."

    def _painel_v32_recolhas(self, pedidos: list[Pedido], target_date, label: str) -> str:
        linhas = []
        for pedido in sorted(pedidos, key=lambda pedido: (pedido.data_planejada, pedido.nome_cliente, pedido.id)):
            contentores = [
                item for item in pedido.contentores
                if self._is_contentor_para_recolha(item)
                and item.entrega_data_hora
                and (
                    self._local_date(item.entrega_data_hora) <= target_date - timedelta(days=5)
                    if label == "hoje"
                    else self._local_date(item.entrega_data_hora) == target_date - timedelta(days=5)
                )
            ]
            if contentores:
                linhas.append(self._render_recolha_contentores(pedido, contentores))
            carrinhas = [
                item for item in pedido.contentores
                if self._is_carrinha_para_recolha(item)
                and self._planned_date(pedido.data_planejada) == target_date
            ]
            if carrinhas:
                linhas.append(self._render_recolha_carrinhas(pedido, carrinhas))
        if linhas:
            return "\n".join(linhas)
        return "Nenhum item para recolher hoje." if label == "hoje" else "Nenhum item para recolher amanha."

    def _render_recolha_contentores(self, pedido: Pedido, contentores: list[PedidoContentor]) -> str:
        numeros = ", ".join(
            self._unique_sorted(
                item.numero_adesivo_contentor or f"Ativo {item.id}"
                for item in contentores
            )
        )
        prefixo = "Contentores" if len(contentores) > 1 else "Contentor"
        return f"- {prefixo} | {pedido.nome_cliente}: {numeros}{self._operational_extras(pedido, contentores)}"

    def _render_recolha_carrinhas(self, pedido: Pedido, carrinhas: list[PedidoContentor]) -> str:
        horarios = ", ".join(
            self._unique_sorted(
                item.horario_agendado or "sem horario"
                for item in carrinhas
            )
        )
        frotas = self._unique_sorted(
            item.numero_adesivo_contentor
            for item in carrinhas
            if item.numero_adesivo_contentor and item.numero_adesivo_contentor != "0"
        )
        frota = f" | frota {', '.join(frotas)}" if frotas else ""
        prefixo = f"{len(carrinhas)} Carrinhas" if len(carrinhas) > 1 else "Carrinha"
        return f"- {prefixo} | {pedido.nome_cliente}: horario {horarios}{frota}{self._operational_extras(pedido, carrinhas)}"

    def _operational_extras(self, pedido: Pedido, itens: list[PedidoContentor]) -> str:
        extras = []
        if self.pedido_service.precisa_mao_de_obra(pedido):
            extras.append("Com Pessoal")
        rota = self._rota_gps(pedido, itens[0]) if itens else ""
        if rota:
            extras.append(f"Rota: {rota}")
        return f" | {' | '.join(extras)}" if extras else ""

    def _unique_sorted(self, values) -> list[str]:
        return sorted({str(value).strip() for value in values if value and str(value).strip()})

    def _painel_v32_pendencias(self, perfil: PerfilOperador) -> str:
        itens = self.pedido_service.pendencias()
        linhas = []
        if perfil == PerfilOperador.GESTOR:
            if itens["financeiras"]:
                linhas.append("Pagamentos pendentes:")
                for pedido in itens["financeiras"]:
                    link = f" | Cobrar: {whatsapp_link(pedido.telefone_cliente)}" if pedido.telefone_cliente else ""
                    linhas.append(f"- #{pedido.id} {pedido.nome_cliente}: {self._money(pedido.valor_global)}{link}")
            if itens["cargas"]:
                linhas.append("Divergencias de residuo:")
                for item in itens["cargas"]:
                    linhas.append(
                        f"- {self._painel_v32_equipamento(item)} | {item.pedido.nome_cliente}: "
                        f"contratado {item.residuo_contratado}; vazadouro {item.residuo_efetivo_vazadouro or 'nao informado'} "
                        f"| resolver carga {item.id}"
                    )
        if self._avarias_enabled() and itens["avarias"]:
            linhas.append("Avarias em equipamentos:")
            for item in itens["avarias"]:
                linhas.append(
                    f"- {self._painel_v32_equipamento(item)} | {item.pedido.nome_cliente}: "
                    f"{item.relato_avaria or 'sem relato'} | resolver avaria {item.id}"
                )
        return "\n".join(linhas) if linhas else "Nenhuma pendencia ativa."

    def _painel_v32_financeiro(self, pedidos: list[Pedido], today) -> str:
        totais = {
            "contentores_pago": Decimal("0"),
            "contentores_pendente": Decimal("0"),
            "carrinhas_pago": Decimal("0"),
            "carrinhas_pendente": Decimal("0"),
            "global_pago": Decimal("0"),
            "global_pendente": Decimal("0"),
        }
        for pedido in pedidos:
            criado_em = self._local_date(pedido.criado_em)
            if criado_em.year != today.year or criado_em.month != today.month:
                continue
            tipos = {
                item.tipo_equipamento
                for item in pedido.contentores
                if item.tipo_equipamento in {
                    TipoEquipamentoPedido.CONTENTOR.value,
                    TipoEquipamentoPedido.CARRINHA.value,
                }
            }
            if not tipos:
                continue
            valor = Decimal(str(pedido.valor_global or 0))
            status = "pago" if pedido.status_pagamento == StatusPagamento.PAGO.value else "pendente"
            totais[f"global_{status}"] += valor
            if tipos == {TipoEquipamentoPedido.CONTENTOR.value}:
                totais[f"contentores_{status}"] += valor
            elif tipos == {TipoEquipamentoPedido.CARRINHA.value}:
                totais[f"carrinhas_{status}"] += valor
        faturado = totais["global_pago"]
        receber = totais["global_pendente"]
        return "\n".join(
            [
                f"- Receita de Contentores ja paga: {self._money(totais['contentores_pago'])}",
                f"- Receita de Contentores pendente: {self._money(totais['contentores_pendente'])}",
                f"- Receita de Carrinhas ja paga: {self._money(totais['carrinhas_pago'])}",
                f"- Receita de Carrinhas pendente: {self._money(totais['carrinhas_pendente'])}",
                f"- Faturado Global: {self._money(faturado)}",
                f"- A receber Global: {self._money(receber)}",
                f"- Total projetado do mes: {self._money(faturado + receber)}",
            ]
        )

    def _is_contentor_para_recolha(self, item: PedidoContentor) -> bool:
        return (
            item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
            and item.status_entrega == StatusEntregaPedido.ENTREGUE.value
            and item.status_recolha == StatusRecolhaPedido.PENDENTE.value
        )

    def _is_carrinha_para_recolha(self, item: PedidoContentor) -> bool:
        return (
            item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            and item.status_entrega == StatusEntregaPedido.ENTREGUE.value
            and item.status_recolha == StatusRecolhaPedido.PENDENTE.value
        )

    def _painel_v32_equipamento(self, item: PedidoContentor) -> str:
        if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            detalhe = (
                item.numero_adesivo_contentor
                if item.numero_adesivo_contentor and item.numero_adesivo_contentor != "0"
                else item.horario_agendado or f"#{item.id}"
            )
            return f"Carrinha {detalhe}"
        return f"Contentor {item.numero_adesivo_contentor or item.id}"

    def _rota_gps(self, pedido: Pedido, item: PedidoContentor) -> str:
        latitude = item.entrega_latitude if item.entrega_latitude is not None else pedido.endereco_latitude
        longitude = item.entrega_longitude if item.entrega_longitude is not None else pedido.endereco_longitude
        if latitude is None or longitude is None:
            return ""
        if Decimal(str(latitude)) == Decimal("0") and Decimal(str(longitude)) == Decimal("0"):
            return ""
        return f"https://www.google.com/maps?q={latitude},{longitude}"

    def _money(self, value) -> str:
        quantized = Decimal(str(value or 0)).quantize(Decimal("0.01"))
        formatted = f"{quantized:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        return f"{formatted} €"

    def _pendencias_v24(self, mostrar_financeiro: bool = True) -> str:
        itens = self.pedido_service.pendencias()
        linhas = [
            "PENDÊNCIAS OPERACIONAIS CRÍTICAS — V2.4",
        ]
        if mostrar_financeiro:
            total = sum(Decimal(str(p.valor_global)) for p in itens["financeiras"])
            linhas.extend(["", f"Financeiras: {len(itens['financeiras'])} pedido(s) — total € {total:.2f}"])
            linhas.extend(
                f"• #{p.id} {p.nome_cliente}: € {Decimal(str(p.valor_global)):.2f}"
                for p in itens["financeiras"]
            )
        linhas.append("")
        linhas.append(f"Cargas erradas: {len(itens['cargas'])}")
        linhas.extend(
            f"• {c.pedido.nome_cliente} — Contentor {c.numero_adesivo_contentor or c.id}: "
            f"{c.relato_carga} | resolver carga {c.id}"
            for c in itens["cargas"]
        )
        if self._avarias_enabled():
            linhas.append("")
            linhas.append(f"Avarias: {len(itens['avarias'])}")
            linhas.extend(
                f"• {c.pedido.nome_cliente} — Contentor {c.numero_adesivo_contentor or c.id}: "
                f"{c.relato_avaria} | resolver avaria {c.id}"
                for c in itens["avarias"]
            )
        return "\n".join(linhas)

    def _resumo_operacional(self, perfil: PerfilOperador = PerfilOperador.GESTOR) -> str:
        contentores = self.contentor_service.listar_contentores()
        counts = {status: 0 for status in StatusContentor}
        for contentor in contentores:
            counts[contentor.status] += 1

        today = self._local_date(utcnow())
        tomorrow = today + timedelta(days=1)
        retiradas_hoje = self._alugueres_por_data_retirada(today)
        retiradas_amanha = self._alugueres_por_data_retirada(tomorrow)
        alugueres_ativos = self._active_alugueres()
        vencendo_amanha = self.aluguer_service.listar_vencendo_amanha()
        atrasados = self.aluguer_service.listar_atrasados()
        linhas = [
            "🤖 Resumo dos contentores",
            "",
            "📦 Contentores",
            f"Total: {len(contentores)}",
            f"🚛 Alugados: {counts[StatusContentor.ALUGADO]}",
            f"Alugados: {counts[StatusContentor.ALUGADO]}",
            f"✅ Disponiveis: {counts[StatusContentor.DISPONIVEL]}",
            f"Disponiveis: {counts[StatusContentor.DISPONIVEL]}",
            f"🕓 Aguardando recolha: {counts[StatusContentor.AGUARDANDO_RECOLHA]}",
            f"Aguardando recolha: {counts[StatusContentor.AGUARDANDO_RECOLHA]}",
            f"🛠️ Manutencao: {counts[StatusContentor.MANUTENCAO]}",
            f"Manutencao: {counts[StatusContentor.MANUTENCAO]}",
            f"Alugueres ativos: {len(alugueres_ativos)}",
            f"Vencem amanha: {len(vencendo_amanha)}",
            f"Em atraso: {len(atrasados)}",
        ]
        if perfil == PerfilOperador.GESTOR:
            faturado_total, recebido_total = self._faturamento_mes_corrente()
            pendente_total = faturado_total - recebido_total
            linhas.extend(
                [
                    "",
                    "💰 Faturamento do mes corrente",
                    "Faturamento do mes corrente:",
                    f"✅ Recebido: {recebido_total:.2f} EUR",
                    f"Recebido/pago no mes: {recebido_total:.2f}",
                    f"⚠️ Pendente: {pendente_total:.2f} EUR",
                    f"📊 Total: {faturado_total:.2f} EUR",
                    f"Faturado total do mes: {faturado_total:.2f}",
                    "Obs.: faturado total soma todos os alugueres do mes; recebido soma apenas registros pagos.",
                ]
            )
        linhas.extend(
            [
                "",
                "📅 Retiradas",
                "🚛 Hoje:",
                "Retiradas hoje:",
                self._format_retiradas(retiradas_hoje),
                "",
                "📆 Amanha:",
                "Retiradas amanha:",
                self._format_retiradas(retiradas_amanha),
                "",
                "🚨 Pendencias Operacionais",
                "PENDENCIAS OPERACIONAIS CRITICAS",
            ]
        )
        linhas.extend(self._pendencias_operacionais(mostrar_financeiro=perfil == PerfilOperador.GESTOR))
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
            .filter(AluguerContentor.status_entrega == StatusEntrega.ENTREGUE.value)
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

    def _pendencias_operacionais(self, mostrar_financeiro: bool = True) -> list[str]:
        pendencias_financeiras = [
            aluguer
            for aluguer in self.db.query(AluguerContentor).filter(AluguerContentor.is_deleted.is_(False)).all()
            if not aluguer.pago
        ]
        pendencias_carga = self.aluguer_service.listar_pendencias_carga()
        pendencias_avaria = (
            self.aluguer_service.listar_pendencias_avaria()
            if self._avarias_enabled()
            else []
        )

        linhas = []
        if mostrar_financeiro:
            total_pendente = sum(Decimal(str(aluguer.valor or 0)) for aluguer in pendencias_financeiras)
            linhas.append(f"💸 Pendencias financeiras: {total_pendente:.2f} EUR")
            linhas.extend(
                [self._format_pendencia_financeira(aluguer) for aluguer in pendencias_financeiras]
                if pendencias_financeiras
                else ["Nenhuma pendencia financeira."]
            )
        else:
            linhas.append("💸 Pendencias financeiras: ocultas para este perfil.")
        linhas.append(f"📦 Pendencias de carga: {len(pendencias_carga)}")
        linhas.extend(
            [self._format_pendencia_carga(aluguer) for aluguer in pendencias_carga]
            if pendencias_carga
            else ["Nenhuma pendencia de carga."]
        )
        if self._avarias_enabled():
            linhas.append(f"🛠️ Pendencias de avarias: {len(pendencias_avaria)}")
            linhas.extend(
                [self._format_pendencia_avaria(aluguer) for aluguer in pendencias_avaria]
                if pendencias_avaria
                else ["Nenhuma pendencia de avaria."]
            )
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

    def _iniciar_revisao_avaria(
        self,
        conversa: ConversaWhatsApp,
        text: str,
        telefone: str,
    ) -> str:
        if conversa.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE:
            revisao_atual = self._contexto_dict(conversa.contexto_json).get(
                RESOLUCAO_AVARIA_CONTEXT_KEY
            )
            revisao_atual = self._contexto_dict(revisao_atual)
            if self._revisao_avaria_valida(revisao_atual, conversa, telefone):
                return (
                    "Já existe uma revisão de avaria em andamento.\n\n"
                    + self._tela_revisao_avaria(
                        revisao_atual["origem"],
                        int(revisao_atual["id"]),
                        fluxo_pausado=True,
                    )
                )
            return (
                "⚠️ Revisão inválida. Use Voltar ou Cancelar para recuperar a "
                "conversa com segurança. Nenhuma avaria foi alterada."
            )
        match = re.fullmatch(r"resolver\s+avaria\s+(\d+)", text)
        if not match:
            return "Informe o ID interno da pendência. Ex: resolver avaria 7"
        alvo_id = int(match.group(1))

        contentor = self.db.get(PedidoContentor, alvo_id)
        if contentor is not None:
            if contentor.status_resolucao_avaria == StatusResolucaoPedido.RESOLVIDO.value:
                return self._mensagem_avaria_ja_resolvida("pedido", alvo_id)
            if (
                not contentor.contentor_avariado
                or contentor.status_resolucao_avaria != StatusResolucaoPedido.PENDENTE.value
            ):
                return "Esse equipamento não possui uma pendência de avaria ativa."
            origem = "pedido"
        else:
            aluguer = self.db.get(AluguerContentor, alvo_id)
            if aluguer is None or aluguer.is_deleted:
                return "Pendência de avaria não encontrada."
            if aluguer.status_resolucao_avaria == StatusResolucao.RESOLVIDO.value:
                return self._mensagem_avaria_ja_resolvida("legado", alvo_id)
            if (
                not aluguer.contentor_avariado
                or aluguer.status_resolucao_avaria != StatusResolucao.PENDENTE.value
            ):
                return "Esse equipamento não possui uma pendência de avaria ativa."
            origem = "legado"

        contexto_anterior = self._contexto_dict(conversa.contexto_json)
        estado_anterior = (
            conversa.estado_atual
            if isinstance(conversa.estado_atual, str) and conversa.estado_atual
            else "idle"
        )
        revisao = {
            "id": alvo_id,
            "origem": origem,
            "estado_anterior": estado_anterior,
            "contexto_anterior": deepcopy(contexto_anterior),
            "iniciada_em": utcnow().isoformat(),
            "gestor": telefone,
        }
        conversa.estado_atual = RESOLUCAO_AVARIA_REVISAO_STATE
        conversa.contexto_json = {RESOLUCAO_AVARIA_CONTEXT_KEY: revisao}
        try:
            # Decisão arquitetural: este commit persiste somente a pausa e o
            # contexto conversacional. A avaria e sua auditoria só mudam após
            # confirmação explícita, em uma transação posterior.
            self.db.commit()
        except Exception:
            self.db.rollback()
            return "⚠️ Não foi possível iniciar a revisão da avaria. Nenhuma alteração foi realizada."
        return self._tela_revisao_avaria(origem, alvo_id, fluxo_pausado=True)

    def _handle_revisao_avaria(
        self,
        conversa: ConversaWhatsApp,
        text: str,
        telefone: str,
    ) -> str:
        contexto = self._contexto_dict(conversa.contexto_json)
        revisao = self._contexto_dict(contexto.get(RESOLUCAO_AVARIA_CONTEXT_KEY))
        revisao_valida = self._revisao_avaria_valida(revisao, conversa, telefone)
        if text in RESOLUCAO_AVARIA_VOLTAR:
            if not revisao_valida:
                return self._cancelar_revisao_invalida(conversa)
            if not self._restaurar_fluxo_anterior(conversa, revisao):
                return (
                    "⚠️ Não foi possível voltar agora. A revisão permanece ativa "
                    "e nenhuma avaria foi alterada."
                )
            return "Revisão encerrada sem alterações. O fluxo anterior foi retomado."
        if text in RESOLUCAO_AVARIA_CANCELAR:
            if not revisao_valida:
                return self._cancelar_revisao_invalida(conversa)
            if not self._restaurar_fluxo_anterior(conversa, revisao):
                return (
                    "⚠️ Não foi possível cancelar agora. A revisão permanece ativa "
                    "e nenhuma avaria foi alterada."
                )
            return "Resolução cancelada. A pendência foi preservada e o fluxo anterior foi retomado."
        if not revisao_valida:
            return (
                "⚠️ Revisão inválida. Use Voltar ou Cancelar para recuperar a "
                "conversa com segurança. Nenhuma avaria foi alterada."
            )

        alvo_id = revisao["id"]
        origem = revisao["origem"]
        if text in RESOLUCAO_AVARIA_CONFIRMAR:
            if not self._alvo_avaria_compativel(origem, alvo_id):
                return (
                    "⚠️ A pendência não está disponível para confirmação. "
                    "Nenhuma avaria foi alterada."
                )
            return self._confirmar_resolucao_avaria(conversa, origem, alvo_id, telefone)
        return self._tela_revisao_avaria(origem, alvo_id, fluxo_pausado=True)

    @staticmethod
    def _contexto_dict(valor: object) -> dict:
        return valor if isinstance(valor, dict) else {}

    def _revisao_avaria_valida(
        self,
        revisao: dict,
        conversa: ConversaWhatsApp,
        telefone: str,
    ) -> bool:
        alvo_id = revisao.get("id")
        return (
            isinstance(alvo_id, int)
            and not isinstance(alvo_id, bool)
            and alvo_id > 0
            and revisao.get("origem") in {"pedido", "legado"}
            and isinstance(revisao.get("estado_anterior"), str)
            and bool(revisao.get("estado_anterior"))
            and isinstance(revisao.get("contexto_anterior"), dict)
            and isinstance(revisao.get("gestor"), str)
            and revisao.get("gestor") == telefone
            and conversa.telefone == telefone
        )

    def _alvo_avaria_compativel(self, origem: str, alvo_id: int) -> bool:
        if origem == "pedido":
            alvo = self.db.get(PedidoContentor, alvo_id)
            return bool(
                alvo
                and alvo.contentor_avariado
                and alvo.status_resolucao_avaria
                in {
                    StatusResolucaoPedido.PENDENTE.value,
                    StatusResolucaoPedido.RESOLVIDO.value,
                }
            )
        alvo = self.db.get(AluguerContentor, alvo_id)
        return bool(
            alvo
            and not alvo.is_deleted
            and alvo.contentor_avariado
            and alvo.status_resolucao_avaria
            in {StatusResolucao.PENDENTE.value, StatusResolucao.RESOLVIDO.value}
        )

    def _cancelar_revisao_invalida(self, conversa: ConversaWhatsApp) -> str:
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            return (
                "⚠️ Não foi possível recuperar a conversa agora. A revisão "
                "permanece ativa e nenhuma avaria foi alterada."
            )
        return (
            "Revisão inválida cancelada com segurança. "
            "Nenhuma avaria foi alterada."
        )

    def _restaurar_fluxo_anterior(
        self,
        conversa: ConversaWhatsApp,
        revisao: dict,
    ) -> bool:
        conversa.estado_atual = revisao.get("estado_anterior") or "idle"
        conversa.contexto_json = deepcopy(revisao["contexto_anterior"])
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            return False
        return True

    def _tela_revisao_avaria(
        self,
        origem: str,
        alvo_id: int,
        *,
        fluxo_pausado: bool,
    ) -> str:
        if origem == "pedido":
            contentor = self.db.get(PedidoContentor, alvo_id)
            if contentor is None:
                return "Pendência de avaria não encontrada."
            cliente = contentor.pedido.nome_cliente
            pedido = str(contentor.pedido_id)
            equipamento = str(contentor.numero_adesivo_contentor or "não informado")
            relato = self._relato_avaria_para_exibicao(contentor.relato_avaria)
            ocorrencia = contentor.recolha_data_hora
            estado = contentor.status_resolucao_avaria
        else:
            aluguer = self.db.get(AluguerContentor, alvo_id)
            if aluguer is None:
                return "Pendência de avaria não encontrada."
            cliente = aluguer.nome_cliente
            pedido = "registro legado"
            equipamento = str(aluguer.numero_contentor or "não informado")
            relato = self._relato_avaria_para_exibicao(aluguer.relato_avaria)
            ocorrencia = aluguer.recolha_data_hora
            estado = aluguer.status_resolucao_avaria

        data_ocorrencia = (
            self._to_local_datetime(ocorrencia).strftime("%d/%m/%Y %H:%M")
            if ocorrencia
            else "não disponível"
        )
        aviso = (
            "O fluxo anterior está pausado. Confirmar encerrará esse fluxo; "
            "Voltar ou Cancelar o retomará sem alterações."
            if fluxo_pausado
            else "Nenhum fluxo operacional anterior estava ativo."
        )
        return "\n".join(
            [
                "Revisão de resolução de avaria",
                "",
                f"Cliente: {cliente}",
                f"Pedido: {pedido}",
                f"Equipamento Nº {equipamento}",
                f"Relato: {relato}",
                f"Ocorrência: {data_ocorrencia}",
                f"Estado atual: {estado}",
                "Ação proposta: marcar a pendência como resolvida.",
                "",
                aviso,
                "",
                "1. Confirmar resolução",
                "2. Voltar",
                "3. Cancelar",
            ]
        )

    @staticmethod
    def _relato_avaria_para_exibicao(relato: object) -> str:
        if not isinstance(relato, str) or not relato.strip():
            return "sem relato"
        if len(relato) <= RESOLUCAO_AVARIA_RELATO_DISPLAY_LIMIT:
            return relato
        marcador = "\n" + RESOLUCAO_AVARIA_RELATO_TRUNCADO
        limite = RESOLUCAO_AVARIA_RELATO_DISPLAY_LIMIT - len(marcador)
        return relato[:limite].rstrip() + marcador

    def _confirmar_resolucao_avaria(
        self,
        conversa: ConversaWhatsApp,
        origem: str,
        alvo_id: int,
        telefone: str,
    ) -> str:
        try:
            if origem == "pedido":
                result = self.db.execute(
                    update(PedidoContentor)
                    .where(PedidoContentor.id == alvo_id)
                    .where(
                        PedidoContentor.status_resolucao_avaria
                        == StatusResolucaoPedido.PENDENTE.value
                    )
                    .values(status_resolucao_avaria=StatusResolucaoPedido.RESOLVIDO.value)
                )
                if result.rowcount == 0:
                    self.db.expire_all()
                    contentor = self.db.get(PedidoContentor, alvo_id)
                    if (
                        contentor
                        and contentor.status_resolucao_avaria
                        == StatusResolucaoPedido.RESOLVIDO.value
                    ):
                        self._finalizar_conversa_resolucao(conversa, origem, alvo_id)
                        self.db.commit()
                        return self._mensagem_avaria_ja_resolvida(origem, alvo_id)
                    self.db.rollback()
                    return "⚠️ A pendência não está mais disponível para resolução."
                self.db.expire_all()
                contentor = self.db.get(PedidoContentor, alvo_id)
                self._registrar_auditoria_avaria_atual(contentor, telefone)
                equipamento = str(contentor.numero_adesivo_contentor or "não informado")
            else:
                result = self.db.execute(
                    update(AluguerContentor)
                    .where(AluguerContentor.id == alvo_id)
                    .where(
                        AluguerContentor.status_resolucao_avaria
                        == StatusResolucao.PENDENTE.value
                    )
                    .values(status_resolucao_avaria=StatusResolucao.RESOLVIDO.value)
                )
                if result.rowcount == 0:
                    self.db.expire_all()
                    aluguer = self.db.get(AluguerContentor, alvo_id)
                    if aluguer and aluguer.status_resolucao_avaria == StatusResolucao.RESOLVIDO.value:
                        self._finalizar_conversa_resolucao(conversa, origem, alvo_id)
                        self.db.commit()
                        return self._mensagem_avaria_ja_resolvida(origem, alvo_id)
                    self.db.rollback()
                    return "⚠️ A pendência não está mais disponível para resolução."
                self.db.expire_all()
                aluguer = self.db.get(AluguerContentor, alvo_id)
                self._registrar_auditoria_avaria_legada(aluguer, telefone)
                equipamento = str(aluguer.numero_contentor or "não informado")

            self._finalizar_conversa_resolucao(conversa, origem, alvo_id)
            self.db.commit()
            return f"Pendência de avaria do Equipamento Nº {equipamento} resolvida."
        except Exception:
            self.db.rollback()
            return "⚠️ Não foi possível confirmar a resolução. Nenhuma alteração foi realizada."

    def _registrar_auditoria_avaria_atual(
        self,
        contentor: PedidoContentor,
        telefone: str,
    ) -> None:
        contentor.avaria_estado_anterior = StatusResolucaoPedido.PENDENTE.value
        contentor.avaria_resolvida_em = utcnow()
        contentor.avaria_resolvida_por = telefone

    def _registrar_auditoria_avaria_legada(
        self,
        aluguer: AluguerContentor,
        telefone: str,
    ) -> None:
        self.db.add(
            EventoAluguer(
                aluguer_id=aluguer.id,
                tipo="pendencia_avaria_resolvida",
                descricao=(
                    f"Avaria do equipamento {aluguer.numero_contentor} resolvida por {telefone}; "
                    "estado anterior PENDENTE; estado final RESOLVIDO"
                ),
            )
        )

    def _finalizar_conversa_resolucao(
        self,
        conversa: ConversaWhatsApp,
        origem: str,
        alvo_id: int,
    ) -> None:
        conversa.estado_atual = "idle"
        conversa.contexto_json = {
            "_ultima_resolucao_avaria": {"origem": origem, "id": alvo_id}
        }

    def _mensagem_avaria_ja_resolvida(self, origem: str, alvo_id: int) -> str:
        if origem == "pedido":
            contentor = self.db.get(PedidoContentor, alvo_id)
            equipamento = (
                str(contentor.numero_adesivo_contentor or "não informado")
                if contentor
                else "não informado"
            )
        else:
            aluguer = self.db.get(AluguerContentor, alvo_id)
            equipamento = str(aluguer.numero_contentor or "não informado") if aluguer else "não informado"
        return f"A avaria do Equipamento Nº {equipamento} já está resolvida."

    def _resolver_pendencia(
        self,
        tipo: str,
        text: str,
        telefone: str,
        perfil: PerfilOperador | None = None,
    ) -> str:
        if perfil is None:
            perfil = self.operador_service.decidir_acesso(telefone).perfil
        if perfil != PerfilOperador.GESTOR:
            return UNAUTHORIZED_MESSAGE
        raw_id = text.split()[-1]
        if not raw_id.isdigit():
            return "Informe o ID do aluguer. Ex: resolver carga 12"
        aluguer_id = int(raw_id)
        try:
            contentor = self.db.get(PedidoContentor, aluguer_id)
            if contentor and (
                (tipo == "carga" and contentor.status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value)
                or (tipo == "avaria" and contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value)
            ):
                self.pedido_service.resolver(tipo, aluguer_id)
                return f"Pendência de {tipo} do contentor #{aluguer_id} resolvida."
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
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self._timezone())

    def _local_date(self, value: datetime):
        return self._to_local_datetime(value).date()

    def _planned_date(self, value: datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(self._timezone()).date()

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
            or conversa.estado_atual in EntregaAgent.ACTIVE_STATES
            or conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES
            or conversa.estado_atual in RecolhaAgent.ACTIVE_STATES
            or conversa.estado_atual in ContentorAgent.ACTIVE_STATES
            or conversa.estado_atual.startswith(PedidoV24Agent.PREFIX)
            or conversa.estado_atual in PagamentoPendenteAgent.ACTIVE_STATES
        )

    def pop_pending_messages(self) -> list[str]:
        messages = list(self._pending_messages)
        self._pending_messages = []
        return messages

    def _finalize_response(
        self,
        response: str,
        conversa: ConversaWhatsApp,
        telefone: str,
        perfil: PerfilOperador,
        append_menu_on_success: bool = False,
    ) -> str:
        response = self._detach_embedded_menu(response, perfil)
        if append_menu_on_success and conversa.estado_atual == "idle" and self._is_success_response(response):
            self._queue_initial_menu(perfil)
        return response

    def _is_success_response(self, response: str) -> bool:
        clean = (response or "").lstrip()
        return clean.startswith("✅") or clean.startswith("âœ…")

    def _detach_embedded_menu(self, response: str, perfil: PerfilOperador) -> str:
        markers = (
            f"🤖 Menu principal - {APP_DISPLAY_NAME}",
            f"Menu principal - {APP_DISPLAY_NAME}",
        )
        for marker in markers:
            index = response.find(marker)
            if index > 0:
                self._queue_initial_menu(perfil)
                return response[:index].rstrip()
        return response

    def _queue_initial_menu(self, perfil: PerfilOperador) -> None:
        menu = self._initial_menu(perfil)
        if menu not in self._pending_messages:
            self._pending_messages.append(menu)

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

    def _initial_menu(self, perfil: PerfilOperador) -> str:
        return MAIN_MENU
