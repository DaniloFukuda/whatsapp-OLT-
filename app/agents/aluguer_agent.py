from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.agents.comprovativo_agent import ComprovativoAgent
from app.agents.localizacao_agent import LocalizacaoAgent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


class AluguerAgent:
    START_STATE = "aguardando_foto_entrega"
    CONFIRMED_STATE = "confirmado"
    ACTIVE_STATES = {
        "aguardando_foto_entrega",
        "aguardando_localizacao",
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_valor",
        "aguardando_pago",
        "aguardando_forma_pagamento",
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
        conversa.contexto_json = {"contentor_id": contentor.id, "contentor_codigo": contentor.codigo}
        self.db.commit()
        return f"Vamos registrar um novo aluguer com o contentor {contentor.codigo}. Envie a foto do contentor no local."

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})

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
            context["telefone_cliente"] = message.texto.strip()
            return self._advance(conversa, "aguardando_valor", context, "Qual e o valor do aluguer?")

        if state == "aguardando_valor":
            valor = self._parse_money(message.texto)
            if valor is None:
                return "Envie um valor valido, por exemplo 120 ou 120,50."
            context["valor"] = str(valor)
            return self._advance(conversa, "aguardando_pago", context, "Esta pago? Responda sim ou nao.")

        if state == "aguardando_pago":
            pago = self._parse_yes_no(message.texto)
            if pago is None:
                return "Responda apenas sim ou nao."
            context["pago"] = pago
            return self._advance(conversa, "aguardando_forma_pagamento", context, "Qual foi a forma de pagamento?")

        if state == "aguardando_forma_pagamento":
            if not message.texto:
                return "Envie a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()
            service_context = {key: value for key, value in context.items() if key != "contentor_codigo" and key != "media_id"}
            aluguer = self.aluguer_service.registrar_novo_aluguer(**service_context)
            conversa.estado_atual = self.CONFIRMED_STATE
            conversa.contexto_json = {
                **context,
                "aluguer_id": aluguer.id,
                "data_vencimento": aluguer.data_vencimento.isoformat(),
            }
            self.db.commit()
            return f"Aluguer #{aluguer.id} registrado. Vence em {aluguer.data_vencimento:%d/%m/%Y}."

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

    def _parse_yes_no(self, value: str | None) -> bool | None:
        normalized = (value or "").strip().lower()
        if normalized in {"sim", "s", "yes", "y"}:
            return True
        if normalized in {"nao", "não", "n", "no"}:
            return False
        return None
