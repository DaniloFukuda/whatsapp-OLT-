from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.agents.aluguer_agent import AluguerAgent
from app.agents.contentor_agent import ContentorAgent
from app.agents.entrega_agent import EntregaAgent
from app.agents.gestao_aluguer_agent import GestaoAluguerAgent
from app.agents.recolha_agent import RecolhaAgent
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.renovacao_agent import RenovacaoAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, StatusAluguer, StatusEntrega
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.models.operador import PerfilOperador
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusEntregaPedido,
    StatusPagamento,
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
MAIN_MENU = (
    f"🤖 Menu principal - {APP_DISPLAY_NAME}\n\n"
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
    f"🤖 Menu principal - {APP_DISPLAY_NAME}\n\n"
    "1. 🟢 Novo pedido\n"
    "2. 🚛 Entrega de contentor\n"
    "3. 📦 Recolha de contentor\n"
    "4. ♻️ Confirmar Despejo no Vazadouro\n"
    "5. 📊 Resumo dos contentores\n\n"
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
        self.pedido_service = PedidoService(db)
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.operador_service = OperadorService(db)
        self._pending_messages: list[str] = []

    def handle(self, message: NormalizedWhatsAppMessage) -> str:
        self._pending_messages = []
        conversa = self._get_or_create_conversa(message.telefone)
        text = (message.texto or "").strip().lower()

        if text in MENU_COMMANDS:
            if not self._is_authorized(message.telefone):
                self._pending_messages = []
                return UNAUTHORIZED_MESSAGE
            if self._has_active_flow(conversa):
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
            return self._initial_menu(message.telefone)

        if text in CANCEL_COMMANDS and not (text == "0" and conversa.estado_atual == "v24_entrega_adesivo"):
            if not self._is_authorized(message.telefone):
                self._pending_messages = []
                return UNAUTHORIZED_MESSAGE
            if self._has_active_flow(conversa):
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return CANCELLED_MENU_MESSAGE
            return "Nenhuma operação em andamento para cancelar.\n\n" + self._initial_menu(message.telefone)

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

        if conversa.estado_atual.startswith(PedidoV24Agent.PREFIX):
            return self._finalize_response(
                self.pedido_v24_agent.handle(conversa, message),
                conversa,
                message.telefone,
                append_menu_on_success=True,
            )

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
                return UNAUTHORIZED_MESSAGE
            return self._handle_operational_command(text, message.telefone)

        if conversa.estado_atual in AluguerAgent.ACTIVE_STATES:
            return self._finalize_response(self.aluguer_agent.handle(conversa, message), conversa, message.telefone)
        if conversa.estado_atual in GestaoAluguerAgent.ACTIVE_STATES:
            return self._finalize_response(self.gestao_aluguer_agent.handle(conversa, message), conversa, message.telefone)
        if conversa.estado_atual in EntregaAgent.ACTIVE_STATES:
            return self._finalize_response(self.entrega_agent.handle(conversa, message), conversa, message.telefone)
        if conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES:
            return self._finalize_response(self.renovacao_agent.handle(conversa, message), conversa, message.telefone)
        if conversa.estado_atual in RecolhaAgent.ACTIVE_STATES:
            return self._finalize_response(self.recolha_agent.handle(conversa, message), conversa, message.telefone)
        if conversa.estado_atual in ContentorAgent.ACTIVE_STATES:
            return self._finalize_response(self.contentor_agent.handle(conversa, message), conversa, message.telefone)

        if text == "5":
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self._handle_operational_command("resumo", message.telefone)

        if text in {"1", "novo pedido", "cadastrar pedido"}:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if text == "1" and self._is_funcionario(message.telefone):
                return self.entrega_agent.start(conversa)
            if not self._can_create_pedido(message.telefone):
                return "Seu perfil de motorista nÃ£o possui permissÃ£o para cadastrar pedidos."
            return self.pedido_v24_agent.start_cadastro(conversa)
        if text in {"2", "confirmar entrega de contentor", "confirmar entrega do lote"}:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if self.pedido_service.pedidos_pendentes_entrega():
                return self.pedido_v24_agent.start_entrega(conversa)
            return self.entrega_agent.start(conversa)
        if text in {"3", "confirmar recolha de contentor"}:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if self.pedido_service.contentores_para_recolha():
                return self.pedido_v24_agent.start_recolha(conversa)
            return self.recolha_agent.start(conversa)
        if text in {"4", "confirmar despejo no vazadouro", "confirmar despejo"}:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.pedido_v24_agent.start_despejo(conversa)

        if text == "6":
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self._handle_operational_command("resumo", message.telefone)
        if text in ENTREGA_COMMANDS or (text == "2" and not self._is_funcionario(message.telefone)) or (
            text == "1" and self._is_funcionario(message.telefone)
        ):
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.entrega_agent.start(conversa)
        if text in RECOLHA_COMMANDS or text == "3" or (text == "2" and self._is_funcionario(message.telefone)):
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.recolha_agent.start(conversa)
        if text in START_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if not self._can_create_pedido(message.telefone):
                return "Seu perfil de motorista nao possui permissao para cadastrar pedidos. Use a opcao de recolha."
            return self.aluguer_agent.start(conversa)
        if text == "1":
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if not self._can_create_pedido(message.telefone):
                return self.recolha_agent.start(conversa)
            return self.aluguer_agent.start(conversa)
        if text in CONTENTOR_STATUS_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.contentor_agent.start_alteracao_status(conversa)
        if text in ALTER_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text == "4":
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.gestao_aluguer_agent.start_alteracao(conversa)
        if text in DELETE_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            if text == "excluir" and not self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7):
                return self.contentor_agent.start_exclusao(conversa)
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in CONTENTOR_DELETE_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.contentor_agent.start_exclusao(conversa)
        if text == "5":
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.gestao_aluguer_agent.start_exclusao(conversa)
        if text in RENEW_COMMANDS:
            if not self._is_authorized(message.telefone):
                return UNAUTHORIZED_MESSAGE
            return self.renovacao_agent.start(conversa)
        if text in {"contentores", "status"}:
            return self.contentor_agent.listar_status()
        if self._is_authorized(message.telefone):
            return self._initial_menu(message.telefone)
        return UNAUTHORIZED_MESSAGE

    def _handle_operational_command(self, command: str, telefone: str | None = None) -> str:
        if command == "resumo":
            perfil = self.operador_service.obter_perfil(telefone) or PerfilOperador.FUNCIONARIO
            self._queue_initial_menu(telefone or "")
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
        today = self._local_date(utcnow())
        tomorrow = today + timedelta(days=1)
        pedidos = self._pedidos_v32()
        linhas = [
            "PAINEL DE CONTROLE OPERACIONAL OLT",
            f"Data: {today:%d/%m/%Y}",
        ]
        if perfil == PerfilOperador.GESTOR:
            linhas.extend(
                [
                    "",
                    "1. VENCEM AMANHA (ACAO COMERCIAL - EXCLUSIVO GESTOR - APENAS CONTENTORES):",
                    self._painel_v32_vencem_amanha(pedidos, today),
                ]
            )
        linhas.extend(
            [
                "",
                "2. RECOLHER HOJE (URGENTE):",
                self._painel_v32_recolhas(pedidos, today, "hoje"),
                "",
                "3. RECOLHER AMANHA (PLANEAMENTO):",
                self._painel_v32_recolhas(pedidos, tomorrow, "amanha"),
                "",
                "4. PENDENCIAS ATIVAS:",
                self._painel_v32_pendencias(perfil),
            ]
        )
        if perfil == PerfilOperador.GESTOR:
            linhas.extend(
                [
                    "",
                    "5. RESUMO FINANCEIRO DO MES (EXCLUSIVO GESTOR):",
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
        for pedido in pedidos:
            contentores = [
                item for item in pedido.contentores
                if self._is_contentor_para_recolha(item)
                and item.entrega_data_hora
                and self._local_date(item.entrega_data_hora) == today - timedelta(days=4)
            ]
            if not contentores:
                continue
            link = f" | Renovar: {whatsapp_link(pedido.telefone_cliente)}" if pedido.telefone_cliente else ""
            linhas.append(
                f"- {pedido.nome_cliente}: {len(contentores)} contentor(es), "
                f"valor total {self._money(pedido.valor_global)}{link}"
            )
        return "\n".join(linhas) if linhas else "Nenhum contentor vencendo amanha."

    def _painel_v32_recolhas(self, pedidos: list[Pedido], target_date, label: str) -> str:
        linhas = []
        for pedido in pedidos:
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
                numeros = ", ".join(
                    item.numero_adesivo_contentor or f"#{item.id}"
                    for item in sorted(contentores, key=lambda item: item.numero_adesivo_contentor or str(item.id))
                )
                linhas.append(f"- Contentores | {pedido.nome_cliente}: {numeros}{self._rota_gps(pedido, contentores[0])}")
            carrinhas = [
                item for item in pedido.contentores
                if self._is_carrinha_para_recolha(item)
                and self._local_date(pedido.data_planejada) == target_date
            ]
            if len(carrinhas) > 1:
                horarios = ", ".join(
                    item.horario_agendado or "sem horario"
                    for item in sorted(carrinhas, key=lambda item: item.horario_agendado or str(item.id))
                )
                mao_obra = " - Com Pessoal" if self.pedido_service.precisa_mao_de_obra(pedido) else ""
                linhas.append(
                    f"- {len(carrinhas)} Carrinhas | {pedido.nome_cliente}: horario {horarios}"
                    f"{mao_obra}{self._rota_gps(pedido, carrinhas[0])}"
                )
                carrinhas = []
            for carrinha in carrinhas:
                mao_obra = " • ⚠️ Com Pessoal" if self.pedido_service.precisa_mao_de_obra(pedido) else ""
                linhas.append(
                    f"- Carrinha | {pedido.nome_cliente}: horario {carrinha.horario_agendado or 'sem horario'}"
                    f"{mao_obra}{self._rota_gps(pedido, carrinha)}"
                )
        if linhas:
            return "\n".join(linhas)
        return "Nenhum item para recolher hoje." if label == "hoje" else "Nenhum item para recolher amanha."

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
        if itens["avarias"]:
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
        }
        for pedido in pedidos:
            if self._local_date(pedido.data_planejada).year != today.year or self._local_date(pedido.data_planejada).month != today.month:
                continue
            contentores = sum(1 for item in pedido.contentores if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value)
            carrinhas = sum(1 for item in pedido.contentores if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value)
            total_itens = contentores + carrinhas
            if not total_itens:
                continue
            valor = Decimal(str(pedido.valor_global or 0))
            # Pedido misto divide o valor proporcionalmente para nao duplicar receita por equipamento.
            valor_contentores = valor * Decimal(contentores) / Decimal(total_itens)
            valor_carrinhas = valor * Decimal(carrinhas) / Decimal(total_itens)
            status = "pago" if pedido.status_pagamento == StatusPagamento.PAGO.value else "pendente"
            totais[f"contentores_{status}"] += valor_contentores
            totais[f"carrinhas_{status}"] += valor_carrinhas
        faturado = totais["contentores_pago"] + totais["carrinhas_pago"]
        receber = totais["contentores_pendente"] + totais["carrinhas_pendente"]
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
        return f" | Rota: https://www.google.com/maps?q={latitude},{longitude}"

    def _money(self, value) -> str:
        return f"EUR {Decimal(str(value or 0)):.2f}"

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
        pendencias_avaria = self.aluguer_service.listar_pendencias_avaria()

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

    def _resolver_pendencia(self, tipo: str, text: str, telefone: str) -> str:
        if not self._is_authorized(telefone):
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
            or conversa.estado_atual in EntregaAgent.ACTIVE_STATES
            or conversa.estado_atual in RenovacaoAgent.ACTIVE_STATES
            or conversa.estado_atual in RecolhaAgent.ACTIVE_STATES
            or conversa.estado_atual in ContentorAgent.ACTIVE_STATES
            or conversa.estado_atual.startswith(PedidoV24Agent.PREFIX)
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
        append_menu_on_success: bool = False,
    ) -> str:
        response = self._detach_embedded_menu(response, telefone)
        if append_menu_on_success and conversa.estado_atual == "idle" and response.lstrip().startswith("✅"):
            self._queue_initial_menu(telefone)
        return response

    def _detach_embedded_menu(self, response: str, telefone: str) -> str:
        markers = (
            f"🤖 Menu principal - {APP_DISPLAY_NAME}",
            f"Menu principal - {APP_DISPLAY_NAME}",
        )
        for marker in markers:
            index = response.find(marker)
            if index > 0:
                self._queue_initial_menu(telefone)
                return response[:index].rstrip()
        return response

    def _queue_initial_menu(self, telefone: str) -> None:
        if not self._is_authorized(telefone):
            self._pending_messages = []
            return
        menu = self._initial_menu(telefone)
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

    def _initial_menu(self, telefone: str) -> str:
        if self._is_funcionario(telefone):
            return (
                f"Olá, sou o Robô de Gestão de Contentores da {APP_DISPLAY_NAME}. O que vamos fazer agora?\n\n"
                "1. Confirmar entrega de contentor\n"
                "2. Confirmar recolha de contentor\n"
                "3. Confirmar Despejo no Vazadouro"
            )
        return MAIN_MENU
