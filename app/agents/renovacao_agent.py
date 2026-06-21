from decimal import Decimal, InvalidOperation
import unicodedata

from sqlalchemy.orm import Session

from app.agents.localizacao_agent import LocalizacaoAgent
from app.core.phone import normalize_portugal_phone, whatsapp_link
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService


class RenovacaoAgent:
    START_STATE = "renovacao_aguardando_item"
    ACTIVE_STATES = {
        "renovacao_aguardando_item",
        "renovacao_aguardando_decisao_alterar",
        "renovacao_aguardando_campo",
        "renovacao_aguardando_valor",
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
    ]

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.localizacao_agent = LocalizacaoAgent()

    def start(self, conversa: ConversaWhatsApp) -> str:
        alugueres = self.aluguer_service.listar_cadastrados_nos_ultimos_dias(7)
        if not alugueres:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao encontrei registros cadastrados nos ultimos 7 dias."
        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {"aluguer_ids": [aluguer.id for aluguer in alugueres], "ajustes": {}}
        self.db.commit()
        return "Escolha o registro para renovar:\n" + self._format_list(alugueres) + "\n0 - Cancelar"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})

        if state == "renovacao_aguardando_item":
            aluguer = self._select_aluguer(context, message.texto)
            if not aluguer:
                return "Escolha um numero valido da lista."
            context["aluguer_id"] = aluguer.id
            context["ajustes"] = {}
            conversa.estado_atual = "renovacao_aguardando_decisao_alterar"
            conversa.contexto_json = context
            self.db.commit()
            return (
                self._format_details(aluguer)
                + "\n\nDeseja alterar alguma informacao antes de renovar?\n\n1 - Sim\n2 - Nao, prosseguir\n0 - Cancelar"
            )

        if state == "renovacao_aguardando_decisao_alterar":
            decision = self._parse_yes_no(message.texto)
            if decision is True:
                conversa.estado_atual = "renovacao_aguardando_campo"
                conversa.contexto_json = context
                self.db.commit()
                return "Campos alteraveis antes de renovar:\n" + self._format_fields() + "\n0 - Cancelar"
            if decision is False:
                return self._finish(conversa, context, message.telefone)
            return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."

        if state == "renovacao_aguardando_campo":
            field = self._select_field(message.texto)
            if not field:
                return "Escolha um numero valido de campo."
            context["field"] = field
            conversa.estado_atual = "renovacao_aguardando_valor"
            conversa.contexto_json = context
            self.db.commit()
            return self._prompt_for_field(field)

        if state == "renovacao_aguardando_valor":
            ajustes = dict(context.get("ajustes") or {})
            error = self._apply_adjustment(ajustes, context["field"], message)
            if error:
                return error
            context["ajustes"] = ajustes
            context.pop("field", None)
            conversa.estado_atual = "renovacao_aguardando_decisao_alterar"
            conversa.contexto_json = context
            self.db.commit()
            return "Alteracao registrada. Deseja alterar mais alguma coisa?\n\n1 - Sim\n2 - Nao, prosseguir\n0 - Cancelar"

        return "Comando nao reconhecido. Envie 'renovar' ou 'prorrogar' para iniciar."

    def _finish(self, conversa: ConversaWhatsApp, context: dict, operador_telefone: str) -> str:
        origem = self.aluguer_service._get_or_raise(context["aluguer_id"])
        novo = self.aluguer_service.renovar_criando_novo_registro(
            origem.id,
            operador_telefone=normalize_portugal_phone(operador_telefone),
            ajustes=dict(context.get("ajustes") or {}),
        )
        conversa.estado_atual = "confirmado"
        conversa.contexto_json = {
            "aluguer_origem_id": origem.id,
            "aluguer_id": novo.id,
            "ultima_operacao": "renovacao",
        }
        self.db.commit()
        return self._format_renewal_summary(origem, novo)

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

    def _apply_adjustment(self, ajustes: dict, field: str, message: NormalizedWhatsAppMessage) -> str | None:
        value = (message.texto or "").strip()
        if field == "nome_cliente":
            if not value:
                return "Envie o nome do cliente."
            ajustes["nome_cliente"] = value
        elif field == "telefone_cliente":
            telefone = normalize_portugal_phone(value)
            if not telefone:
                return "Envie um telefone valido do cliente."
            ajustes["telefone_cliente"] = telefone
        elif field == "email_cliente":
            ajustes["email_cliente"] = None if value.lower() in {"", "pular", "sem email", "sem e-mail"} else value
        elif field == "localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "Envie uma localizacao do WhatsApp para atualizar."
            ajustes["latitude"] = latitude
            ajustes["longitude"] = longitude
        elif field == "tipo_residuo":
            tipo_residuo = self._parse_tipo_residuo(value)
            if tipo_residuo is None:
                return "Opcao invalida. Responda com o numero da opcao."
            ajustes["tipo_residuo"] = tipo_residuo
        elif field == "valor":
            valor = self._parse_money(value)
            if valor is None:
                return "Envie um valor valido, por exemplo 150, 150.00, 150,00 ou EUR 150."
            ajustes["valor"] = valor
        elif field == "forma_pagamento":
            if not value:
                return "Envie a forma de pagamento."
            ajustes["forma_pagamento"] = value
        elif field == "pago":
            pago = self._parse_payment_status(value)
            if pago is None:
                return "Opcao invalida. Responda com o numero da opcao."
            ajustes["pago"] = pago
        return None

    def _normalize_option(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()

    def _format_list(self, alugueres: list[AluguerContentor]) -> str:
        return "\n".join(f"{index}. {self._format_list_item(aluguer)}" for index, aluguer in enumerate(alugueres, start=1))

    def _format_list_item(self, aluguer: AluguerContentor) -> str:
        pagamento = "pago" if aluguer.pago else "pendente"
        contentor = aluguer.contentor.codigo if aluguer.contentor else f"#{aluguer.contentor_id}"
        return (
            f"#{aluguer.id} / {contentor} - {aluguer.nome_cliente} - "
            f"entrega {aluguer.data_entrega:%d/%m/%Y} - retirada {aluguer.data_vencimento:%d/%m/%Y} - "
            f"valor {aluguer.valor} - {pagamento}"
        )

    def _format_details(self, aluguer: AluguerContentor) -> str:
        pagamento = "pago" if aluguer.pago else "pendente"
        contentor = aluguer.contentor.codigo if aluguer.contentor else f"#{aluguer.contentor_id}"
        return "\n".join(
            [
                "Registro selecionado:",
                f"ID/referencia: #{aluguer.id}",
                f"Contentor: {contentor}",
                f"Cliente: {aluguer.nome_cliente}",
                f"Telefone: {aluguer.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(aluguer.telefone_cliente)}",
                f"Data entrega: {aluguer.data_entrega:%d/%m/%Y}",
                f"Data retirada: {aluguer.data_vencimento:%d/%m/%Y}",
                f"Valor: {aluguer.valor}",
                f"Status pagamento: {pagamento}",
            ]
        )

    def _format_fields(self) -> str:
        return "\n".join(f"{index}. {label}" for index, (_, label) in enumerate(self.EDITABLE_FIELDS, start=1))

    def _prompt_for_field(self, field: str) -> str:
        labels = dict(self.EDITABLE_FIELDS)
        if field == "tipo_residuo":
            return "Envie o novo tipo do residuo:\n\n1 - Entulho limpo\n2 - Entulho misto\n0 - Cancelar"
        if field == "pago":
            return "Envie o novo status de pagamento:\n\n1 - Pago\n2 - Pendente\n0 - Cancelar"
        if field == "localizacao":
            return "Envie a nova localizacao pelo WhatsApp."
        return f"Envie o novo valor para {labels[field]}."

    def _format_renewal_summary(self, origem: AluguerContentor, novo: AluguerContentor) -> str:
        pagamento = "pago" if novo.pago else "pendente"
        return "\n".join(
            [
                "Renovacao criada.",
                f"Registro antigo: #{origem.id}",
                f"Novo registro: #{novo.id}",
                f"Cliente: {novo.nome_cliente}",
                f"Telefone: {novo.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(novo.telefone_cliente)}",
                f"Nova data entrega: {novo.data_entrega:%d/%m/%Y}",
                f"Nova data retirada: {novo.data_vencimento:%d/%m/%Y}",
                f"Valor: {novo.valor}",
                f"Status pagamento: {pagamento}",
            ]
        )

    def _parse_positive_int(self, value: str | None) -> int | None:
        try:
            parsed = int((value or "").strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    def _parse_yes_no(self, value: str | None) -> bool | None:
        option = self._normalize_option(value)
        if option in {"1", "sim", "s", "yes", "y"}:
            return True
        if option in {"2", "nao", "n", "no", "prosseguir"}:
            return False
        normalized = (value or "").strip().lower()
        if normalized in {"sim", "s", "yes", "y"}:
            return True
        if normalized in {"nao", "não", "n", "no"}:
            return False
        return None

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
        normalized = value.replace("EUR", "").replace("eur", "").replace(",", ".").strip()
        normalized = normalized.replace(chr(8364), "").strip()
        try:
            return Decimal(normalized)
        except (InvalidOperation, AttributeError):
            return None

    def _parse_payment_status(self, value: str | None) -> bool | None:
        option = self._normalize_option(value)
        if option in {"1", "sim", "s", "yes", "y", "pago", "paga"}:
            return True
        if option in {"2", "nao", "n", "no", "pendente", "nao pago"}:
            return False
        normalized = (value or "").strip().lower()
        if normalized in {"sim", "s", "yes", "y", "pago", "paga"}:
            return True
        if normalized in {"nao", "não", "n", "no", "pendente", "nao pago", "não pago"}:
            return False
        return None
