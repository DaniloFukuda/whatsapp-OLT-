from datetime import datetime
from decimal import Decimal, InvalidOperation
import unicodedata

from sqlalchemy.orm import Session

from app.agents.localizacao_agent import LocalizacaoAgent
from app.core.phone import normalize_portugal_phone, whatsapp_link
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService


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
INVALID_VALUE_MESSAGE = (
    "⚠️ Valor invalido.\n\n"
    "Envie um valor realista, por exemplo:\n"
    "120\n"
    "120,50\n"
    "120.50"
)


class GestaoAluguerAgent:
    ALTER_START_STATE = "alteracao_aguardando_item"
    DELETE_START_STATE = "exclusao_aguardando_item"
    ACTIVE_STATES = {
        "alteracao_aguardando_item",
        "alteracao_aguardando_campo",
        "alteracao_aguardando_valor",
        "exclusao_aguardando_item",
        "exclusao_aguardando_confirmacao",
        "exclusao_aguardando_justificativa",
    }
    EDITABLE_FIELDS = [
        ("nome_cliente", "nome do cliente"),
        ("telefone_cliente", "telefone do cliente"),
        ("email_cliente", "e-mail"),
        ("localizacao", "localizacao"),
        ("tipo_residuo", "tipo do residuo"),
        ("valor", "valor"),
        ("forma_pagamento", "forma de pagamento"),
        ("pago", "status de pagamento"),
        ("data_vencimento", "data de retirada"),
    ]

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.localizacao_agent = LocalizacaoAgent()

    def start_alteracao(self, conversa: ConversaWhatsApp) -> str:
        alugueres = self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7)
        if not alugueres:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao encontrei registros cadastrados nos ultimos 7 dias."
        conversa.estado_atual = self.ALTER_START_STATE
        conversa.contexto_json = {"aluguer_ids": [aluguer.id for aluguer in alugueres]}
        self.db.commit()
        return "Escolha o registro para alterar:\n" + self._format_list(alugueres) + "\n0 - Cancelar"

    def start_exclusao(self, conversa: ConversaWhatsApp) -> str:
        alugueres = self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7)
        if not alugueres:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao encontrei registros cadastrados nos ultimos 7 dias."
        conversa.estado_atual = self.DELETE_START_STATE
        conversa.contexto_json = {"aluguer_ids": [aluguer.id for aluguer in alugueres]}
        self.db.commit()
        return "Escolha o registro para excluir:\n" + self._format_list(alugueres) + "\n0 - Cancelar"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})

        if state == "alteracao_aguardando_item":
            aluguer = self._select_aluguer(context, message.texto)
            if not aluguer:
                return "Escolha um numero valido da lista."
            context["aluguer_id"] = aluguer.id
            conversa.estado_atual = "alteracao_aguardando_campo"
            conversa.contexto_json = context
            self.db.commit()
            return self._format_details(aluguer) + "\n\nCampos alteraveis:\n" + self._format_fields() + "\n0 - Cancelar"

        if state == "alteracao_aguardando_campo":
            field = self._select_field(message.texto)
            if not field:
                return "Escolha um numero valido de campo."
            context["field"] = field
            conversa.estado_atual = "alteracao_aguardando_valor"
            conversa.contexto_json = context
            self.db.commit()
            aluguer = self.aluguer_service._get_or_raise(context["aluguer_id"])
            return self._prompt_for_field(field, aluguer)

        if state == "alteracao_aguardando_valor":
            aluguer = self.aluguer_service._get_or_raise(context["aluguer_id"])
            error = self._apply_field(aluguer, context["field"], message)
            if error:
                return error
            aluguer.alterado_por_operador = normalize_portugal_phone(message.telefone)
            self.aluguer_service.salvar(aluguer)
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "✅ Registro alterado.\n" + self._format_summary(aluguer) + "\n\n" + MAIN_MENU

        if state == "exclusao_aguardando_item":
            aluguer = self._select_aluguer(context, message.texto)
            if not aluguer:
                return "Escolha um numero valido da lista."
            context["aluguer_id"] = aluguer.id
            conversa.estado_atual = "exclusao_aguardando_confirmacao"
            conversa.contexto_json = context
            self.db.commit()
            return (
                self._format_details(aluguer)
                + "\n\n🗑️ Tem certeza que deseja apagar este registro?\n\n1️⃣ Sim, apagar registro\n0️⃣ Cancelar"
            )

        if state == "exclusao_aguardando_confirmacao":
            aluguer_id = context["aluguer_id"]
            confirmation = self._parse_confirmacao_exclusao(message.texto)
            if confirmation is True:
                conversa.estado_atual = "exclusao_aguardando_justificativa"
                conversa.contexto_json = context
                self.db.commit()
                return "Informe a justificativa da exclusao com pelo menos 10 caracteres."
            if confirmation is None:
                return "Opcao invalida. Responda 1 para apagar ou 0 para cancelar."
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Exclusao cancelada. Nenhum registro foi apagado.\n\n" + MAIN_MENU

        if state == "exclusao_aguardando_justificativa":
            justificativa = (message.texto or "").strip()
            if len(justificativa) < 10:
                return "A justificativa deve ter pelo menos 10 caracteres."
            aluguer_id = context["aluguer_id"]
            self.aluguer_service.excluir(
                aluguer_id,
                operador_telefone=normalize_portugal_phone(message.telefone),
                justificativa=justificativa,
            )
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return f"✅ Registro #{aluguer_id} excluido.\n\n" + MAIN_MENU

        return "Comando nao reconhecido. Envie 'alterar' ou 'excluir' para iniciar."

    def _select_aluguer(self, context: dict, value: str | None) -> AluguerContentor | None:
        index = self._parse_positive_int(value)
        ids = context.get("aluguer_ids") or []
        if index is None or index > len(ids):
            return None
        return self.aluguer_service._get_or_raise(ids[index - 1])

    def _select_field(self, value: str | None) -> str | None:
        index = self._parse_positive_int(value)
        if index is None or index > len(self.EDITABLE_FIELDS):
            return None
        return self.EDITABLE_FIELDS[index - 1][0]

    def _apply_field(self, aluguer: AluguerContentor, field: str, message: NormalizedWhatsAppMessage) -> str | None:
        value = (message.texto or "").strip()
        if field == "nome_cliente":
            if not value:
                return "Envie o nome do cliente."
            aluguer.nome_cliente = value
            aluguer.cliente.nome = value
        elif field == "telefone_cliente":
            telefone = normalize_portugal_phone(value)
            if not telefone:
                return "Envie um telefone valido do cliente."
            aluguer.telefone_cliente = telefone
            aluguer.cliente.telefone = telefone
        elif field == "email_cliente":
            aluguer.email_cliente = None if value.lower() in {"", "pular", "sem email", "sem e-mail"} else value
        elif field == "localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "Envie uma localizacao do WhatsApp para atualizar."
            aluguer.latitude = latitude
            aluguer.longitude = longitude
        elif field == "tipo_residuo":
            tipo_residuo = self._parse_tipo_residuo(value)
            if tipo_residuo is None:
                return "Opcao invalida. Responda com o numero da opcao."
            aluguer.tipo_residuo = tipo_residuo
        elif field == "valor":
            valor = self._parse_money(value)
            if valor is None:
                return INVALID_VALUE_MESSAGE
            aluguer.valor = valor
        elif field == "forma_pagamento":
            if not value:
                return "Envie a forma de pagamento."
            aluguer.forma_pagamento = value
        elif field == "pago":
            pago = self._parse_payment_status(value)
            if pago is None:
                return "Opcao invalida. Responda com o numero da opcao."
            aluguer.pago = pago
        elif field == "data_vencimento":
            data = self._parse_date(value)
            if data is None:
                return "Envie a data de retirada no formato DD/MM/AAAA."
            aluguer.data_vencimento = data
        return None

    def _format_list(self, alugueres: list[AluguerContentor]) -> str:
        return "\n".join(f"{index}. {self._format_list_item(aluguer)}" for index, aluguer in enumerate(alugueres, start=1))

    def _format_list_item(self, aluguer: AluguerContentor) -> str:
        pagamento = "pago" if aluguer.pago else "pendente"
        contentor = aluguer.contentor.codigo if aluguer.contentor else f"#{aluguer.contentor_id}"
        return (
            f"#{aluguer.id} / {contentor} - {aluguer.nome_cliente} - "
            f"entrega {aluguer.data_entrega:%d/%m/%Y} - retirada {aluguer.data_vencimento:%d/%m/%Y} - {pagamento}"
        )

    def _format_details(self, aluguer: AluguerContentor) -> str:
        return "Registro selecionado:\n" + self._format_summary(aluguer)

    def _format_fields(self) -> str:
        return "\n".join(f"{index}. {label}" for index, (_, label) in enumerate(self.EDITABLE_FIELDS, start=1))

    def _prompt_for_field(self, field: str, aluguer: AluguerContentor) -> str:
        labels = dict(self.EDITABLE_FIELDS)
        atual = self._current_field_value(field, aluguer)
        if field == "tipo_residuo":
            return f"🧱 Residuo atual: {atual}\n\nEnvie o novo tipo do residuo:\n\n1 - Entulho limpo\n2 - Entulho misto\n0 - Cancelar"
        if field == "pago":
            return f"✅ Status de pagamento atual: {atual}\n\nEnvie o novo status de pagamento:\n\n1 - Pago\n2 - Pendente\n0 - Cancelar"
        if field == "localizacao":
            return f"📍 Localizacao atual: {atual}\n\nEnvie a nova localizacao pelo WhatsApp."
        if field == "data_vencimento":
            return f"📅 Data de retirada atual: {atual}\n\nEnvie a nova data de retirada no formato DD/MM/AAAA."
        return f"{self._field_icon(field)} {labels[field].capitalize()} atual: {atual}\n\nEnvie o novo valor:"

    def _current_field_value(self, field: str, aluguer: AluguerContentor) -> str:
        if field == "nome_cliente":
            return aluguer.nome_cliente
        if field == "telefone_cliente":
            return aluguer.telefone_cliente
        if field == "email_cliente":
            return aluguer.email_cliente or "nao informado"
        if field == "localizacao":
            if aluguer.latitude is not None and aluguer.longitude is not None:
                return f"{aluguer.latitude},{aluguer.longitude}"
            return "nao informada"
        if field == "tipo_residuo":
            return aluguer.tipo_residuo or "nao informado"
        if field == "valor":
            return f"{Decimal(str(aluguer.valor or 0)):.2f} EUR"
        if field == "forma_pagamento":
            return aluguer.forma_pagamento or "nao informada"
        if field == "pago":
            return "pago" if aluguer.pago else "pendente"
        if field == "data_vencimento":
            return f"{aluguer.data_vencimento:%d/%m/%Y}"
        return "nao informado"

    def _field_icon(self, field: str) -> str:
        return {
            "nome_cliente": "👤",
            "telefone_cliente": "📞",
            "email_cliente": "✉️",
            "valor": "💰",
            "forma_pagamento": "💳",
        }.get(field, "✏️")

    def _format_summary(self, aluguer: AluguerContentor) -> str:
        pagamento = "pago" if aluguer.pago else "pendente"
        contentor = aluguer.contentor.codigo if aluguer.contentor else f"#{aluguer.contentor_id}"
        return "\n".join(
            [
                f"ID/referencia: #{aluguer.id}",
                f"Contentor: {contentor}",
                f"Cliente: {aluguer.nome_cliente}",
                f"Telefone: {aluguer.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(aluguer.telefone_cliente)}",
                f"E-mail: {aluguer.email_cliente or 'nao informado'}",
                f"Data entrega: {aluguer.data_entrega:%d/%m/%Y}",
                f"Data retirada: {aluguer.data_vencimento:%d/%m/%Y}",
                f"Tipo residuo: {aluguer.tipo_residuo or 'nao informado'}",
                f"Valor: {aluguer.valor}",
                f"Forma pagamento: {aluguer.forma_pagamento or 'nao informada'}",
                f"Status pagamento: {pagamento}",
                f"Operador: {aluguer.operador_telefone or 'nao informado'}",
            ]
        )

    def _parse_positive_int(self, value: str | None) -> int | None:
        try:
            parsed = int((value or "").strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    def _parse_tipo_residuo(self, value: str | None) -> str | None:
        normalized = (value or "").strip().lower()
        if normalized in {"entulho limpo", "limpo", "1"}:
            return "Entulho limpo"
        if normalized in {"entulho misto", "misto", "2"}:
            return "Entulho misto"
        return None

    def _parse_money(self, value: str | None) -> Decimal | None:
        if not value:
            return None
        normalized = value.replace("€", "").replace("EUR", "").replace("eur", "").replace(",", ".").strip()
        try:
            valor = Decimal(normalized).quantize(Decimal("0.01"))
        except (InvalidOperation, AttributeError):
            return None
        if valor <= 0 or valor > Decimal("999.99"):
            return None
        return valor

    def _parse_payment_status(self, value: str | None) -> bool | None:
        normalized = (value or "").strip().lower()
        if normalized in {"sim", "s", "yes", "y", "pago", "paga"}:
            return True
        if normalized in {"nao", "não", "n", "no", "pendente", "nao pago", "não pago"}:
            return False
        return None

    def _parse_payment_status(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "sim", "s", "yes", "y", "pago", "paga"}:
            return True
        if normalized in {"2", "nao", "n", "no", "pendente", "nao pago"}:
            return False
        return None

    def _parse_confirmacao_exclusao(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "sim", "s", "confirmar", "apagar"}:
            return True
        if normalized in {"0", "2", "nao", "n", "cancelar", "sair", "menu"}:
            return False
        return None

    def _normalize_option(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()

    def _parse_date(self, value: str | None) -> datetime | None:
        try:
            return datetime.strptime((value or "").strip(), "%d/%m/%Y")
        except ValueError:
            return None
