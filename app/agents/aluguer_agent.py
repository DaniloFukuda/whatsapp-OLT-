from datetime import datetime
from decimal import Decimal, InvalidOperation
import unicodedata

from sqlalchemy.orm import Session

from app.agents.comprovativo_agent import ComprovativoAgent
from app.agents.localizacao_agent import LocalizacaoAgent
from app.core.phone import normalize_portugal_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


class AluguerAgent:
    START_STATE = "aguardando_quantidade_contentores"
    CONFIRMED_STATE = "confirmado"
    ACTIVE_STATES = {
        "aguardando_quantidade_contentores",
        "aguardando_foto_entrega",
        "aguardando_localizacao",
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_email_cliente",
        "aguardando_confirmacao_data_entrega",
        "aguardando_tipo_residuo",
        "aguardando_valor",
        "aguardando_forma_pagamento",
        "aguardando_pago",
    }

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.localizacao_agent = LocalizacaoAgent()
        self.comprovativo_agent = ComprovativoAgent()

    def start(self, conversa: ConversaWhatsApp) -> str:
        contentor = self.contentor_service.repository.first_available()
        if not contentor:
            conversa.estado_atual = self.CONFIRMED_STATE
            conversa.contexto_json = {"erro": "sem_contentor_disponivel"}
            self.db.commit()
            return "Nao ha contentores disponiveis para iniciar um novo aluguer."

        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "contentor_id": contentor.id,
            "contentor_codigo": contentor.codigo,
            "operador_telefone": normalize_portugal_phone(conversa.telefone),
        }
        self.db.commit()
        return f"Vamos registrar um novo aluguer com o contentor {contentor.codigo}. Qual e a quantidade de contentores?"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})

        if state == "aguardando_quantidade_contentores":
            quantidade = self._parse_positive_int(message.texto)
            if quantidade is None:
                return "Envie a quantidade de contentores, por exemplo 1."
            context["quantidade_contentores"] = quantidade
            return self._advance(conversa, "aguardando_foto_entrega", context, "Envie a foto do contentor no local.")

        if state == "aguardando_foto_entrega":
            media_path = self.comprovativo_agent.extract_media_path(message)
            if not media_path:
                return "Envie uma foto do contentor no local para continuar."
            context["foto_entrega_path"] = media_path
            context["media_id"] = message.media_id
            return self._advance(conversa, "aguardando_localizacao", context, "Agora envie a localizacao.")

        if state == "aguardando_localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "Envie a localizacao do contentor para continuar."
            context["latitude"] = latitude
            context["longitude"] = longitude
            return self._advance(conversa, "aguardando_nome_cliente", context, "Qual e o nome do cliente?")

        if state == "aguardando_nome_cliente":
            if not message.texto:
                return "Envie o nome do cliente."
            context["nome_cliente"] = message.texto.strip()
            return self._advance(conversa, "aguardando_telefone_cliente", context, "Qual e o telefone do cliente?")

        if state == "aguardando_telefone_cliente":
            if not message.texto:
                return "Envie o telefone do cliente."
            telefone = normalize_portugal_phone(message.texto)
            if not telefone:
                return "Envie um telefone valido do cliente."
            context["telefone_cliente"] = telefone
            context["whatsapp_cliente_link"] = whatsapp_link(telefone)
            return self._advance(
                conversa,
                "aguardando_email_cliente",
                context,
                "Qual e o e-mail do cliente? Se nao houver, responda pular.",
            )

        if state == "aguardando_email_cliente":
            email = (message.texto or "").strip()
            context["email_cliente"] = None if self._is_optional_skip(email) else email
            entrega = utcnow()
            context["data_entrega"] = entrega.isoformat()
            return self._advance(
                conversa,
                "aguardando_confirmacao_data_entrega",
                context,
                f"Confirma a data de entrega como hoje ({entrega:%d/%m/%Y})?\n\n1 - Sim\n2 - Nao",
            )

        if state == "aguardando_confirmacao_data_entrega":
            confirmado = self._parse_sim_nao_opcao(message.texto)
            if confirmado is not True:
                return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."
            return self._advance(
                conversa,
                "aguardando_tipo_residuo",
                context,
                "Qual e o tipo do residuo?\n\n1 - Entulho limpo\n2 - Entulho misto",
            )

        if state == "aguardando_tipo_residuo":
            tipo_residuo = self._parse_tipo_residuo(message.texto)
            if tipo_residuo is None:
                return "Opcao invalida. Responda com o numero da opcao."
            context["tipo_residuo"] = tipo_residuo
            return self._advance(conversa, "aguardando_valor", context, "Qual e o valor?")

        if state == "aguardando_valor":
            valor = self._parse_money(message.texto)
            if valor is None:
                return "Envie um valor valido, por exemplo 120 ou 120,50."
            context["valor"] = str(valor)
            return self._advance(conversa, "aguardando_forma_pagamento", context, "Qual e a forma de pagamento?")

        if state == "aguardando_forma_pagamento":
            if not message.texto:
                return "Envie a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()
            return self._advance(conversa, "aguardando_pago", context, "Esta pago?\n\n1 - Sim\n2 - Nao")

        if state == "aguardando_pago":
            pago = self._parse_pagamento_opcao(message.texto)
            if pago is None:
                return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."
            context["pago"] = pago
            service_context = {
                key: value
                for key, value in context.items()
                if key not in {"contentor_codigo", "media_id", "whatsapp_cliente_link"}
            }
            service_context["data_entrega"] = datetime.fromisoformat(context["data_entrega"])
            aluguer = self.aluguer_service.registrar_novo_aluguer(**service_context)
            conversa.estado_atual = self.CONFIRMED_STATE
            conversa.contexto_json = {
                **context,
                "aluguer_id": aluguer.id,
                "data_vencimento": aluguer.data_vencimento.isoformat(),
            }
            self.db.commit()
            return self._format_summary(aluguer)

        return self.start(conversa)

    def _advance(self, conversa: ConversaWhatsApp, next_state: str, context: dict, response: str) -> str:
        conversa.estado_atual = next_state
        conversa.contexto_json = context
        self.db.commit()
        return response

    def _parse_money(self, value: str | None) -> Decimal | None:
        if not value:
            return None
        try:
            return Decimal(value.replace("EUR", "").replace(",", ".").strip())
        except (InvalidOperation, AttributeError):
            return None

    def _parse_sim_nao_opcao(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "sim", "s", "yes", "y"}:
            return True
        if normalized in {"2", "nao", "n", "no"}:
            return False
        return self._parse_yes_no(value)

    def _parse_pagamento_opcao(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "sim", "s", "yes", "y", "pago", "paga"}:
            return True
        if normalized in {"2", "nao", "n", "no", "pendente", "nao pago"}:
            return False
        return self._parse_yes_no(value)

    def _normalize_option(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()

    def _parse_yes_no(self, value: str | None) -> bool | None:
        normalized = (value or "").strip().lower()
        if normalized in {"sim", "s", "yes", "y"}:
            return True
        if normalized in {"nao", "não", "n", "no"}:
            return False
        return None

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

    def _is_optional_skip(self, value: str) -> bool:
        return value.strip().lower() in {"", "pular", "saltar", "sem email", "sem e-mail", "nao", "nao tem", "não"}

    def _format_summary(self, aluguer) -> str:
        pagamento = "pago" if aluguer.pago else "pendente"
        return "\n".join(
            [
                f"Cadastro concluido. ID/referencia: #{aluguer.id}",
                f"Quantidade: {aluguer.quantidade_contentores}",
                f"Cliente: {aluguer.nome_cliente}",
                f"Telefone: {aluguer.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(aluguer.telefone_cliente)}",
                f"Data entrega: {aluguer.data_entrega:%d/%m/%Y}",
                f"Data retirada: {aluguer.data_vencimento:%d/%m/%Y}",
                f"Tipo residuo: {aluguer.tipo_residuo}",
                f"Valor: {aluguer.valor}",
                f"Forma pagamento: {aluguer.forma_pagamento}",
                f"Status pagamento: {pagamento}",
                f"Operador: {aluguer.operador_telefone}",
            ]
        )
