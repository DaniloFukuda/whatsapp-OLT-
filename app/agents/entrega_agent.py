import unicodedata

from sqlalchemy.orm import Session

from app.agents.localizacao_agent import LocalizacaoAgent
from app.core.phone import normalize_portugal_phone
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import StatusContentor
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


MAIN_MENU = (
    "Menu principal - OLT Gestão de Resíduos & Demolições\n\n"
    "1. Novo pedido\n"
    "2. Entrega de contentor\n"
    "3. Recolha de contentor\n"
    "4. Alterar registro\n"
    "5. Apagar registro\n"
    "6. Resumo dos contentores\n"
    "7. Manutencao / avarias\n"
    "0. Sair\n\n"
    "Digite o número da opção desejada."
)


class EntregaAgent:
    START_STATE = "entrega_aguardando_selecao"
    ACTIVE_STATES = {
        "entrega_aguardando_selecao",
        "entrega_aguardando_contentor",
        "entrega_aguardando_foto",
        "entrega_aguardando_mais_foto",
        "entrega_aguardando_localizacao",
        "entrega_aguardando_referencia_opcao",
        "entrega_aguardando_referencia_texto",
        "entrega_aguardando_pagamento_no_ato",
        "entrega_aguardando_forma_pagamento",
        "entrega_aguardando_forma_pagamento_outro",
    }

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.localizacao_agent = LocalizacaoAgent()

    def start(self, conversa: ConversaWhatsApp) -> str:
        pendentes = self.aluguer_service.listar_pendentes_entrega()
        if not pendentes:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao existem pedidos pendentes de entrega."
        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "pendentes": [aluguer.id for aluguer in pendentes],
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        return "Entrega de contentor. Escolha o pedido:\n" + self._format_pedidos(pendentes) + "\n0 - Cancelar"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if state == "entrega_aguardando_selecao":
            pendentes = self._load_pendentes(context)
            aluguer = self._resolve_selection(message.texto, pendentes)
            if not aluguer:
                return "Opcao invalida. Escolha o numero da lista ou o ID do pedido."
            context["aluguer_id"] = aluguer.id
            return self._advance(
                conversa,
                "entrega_aguardando_contentor",
                context,
                "Informe o contentor entregue: use somente o numero (1 a 99, sem letras e sem zero a esquerda).\n\n"
                + self._format_contentores_disponiveis(),
            )

        if state == "entrega_aguardando_contentor":
            try:
                contentor = self.contentor_service.buscar_para_entrega(message.texto)
            except ValueError as exc:
                return str(exc)
            context["contentor_codigo"] = contentor.codigo
            context["fotos_entrega"] = []
            return self._advance(conversa, "entrega_aguardando_foto", context, "Por favor, envie a foto do contentor no local.")

        if state == "entrega_aguardando_foto":
            foto = self._extract_photo_reference(message)
            if not foto:
                return "Envie uma imagem do contentor para continuar."
            fotos = list(context.get("fotos_entrega") or [])
            fotos.append(foto)
            context["fotos_entrega"] = fotos
            return self._advance(
                conversa,
                "entrega_aguardando_mais_foto",
                context,
                "Foto registrada. Deseja adicionar mais uma foto?\n\n1. Sim\n2. Nao",
            )

        if state == "entrega_aguardando_mais_foto":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                return self._advance(conversa, "entrega_aguardando_foto", context, "Envie a proxima foto da entrega.")
            if choice is False:
                return self._advance(
                    conversa,
                    "entrega_aguardando_localizacao",
                    context,
                    "Agora, envie a localizacao GPS exata onde o contentor foi posicionado.",
                )
            return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."

        if state == "entrega_aguardando_localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "Nao consegui identificar a localizacao. Envie o PIN do WhatsApp ou um link do Google Maps com coordenadas."
            context["entrega_latitude"] = latitude
            context["entrega_longitude"] = longitude
            return self._advance(conversa, "entrega_aguardando_referencia_opcao", context, self._referencia_prompt())

        if state == "entrega_aguardando_referencia_opcao":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                return self._advance(
                    conversa,
                    "entrega_aguardando_referencia_texto",
                    context,
                    "Digite o ponto de referencia da entrega com no maximo 50 caracteres.",
                )
            if choice is False:
                context["entrega_ponto_referencia"] = None
                return self._after_referencia(conversa, context)
            return "Opcao invalida. Responda 1/Sim para informar referencia ou 2/Nao para continuar."

        if state == "entrega_aguardando_referencia_texto":
            referencia = (message.texto or "").strip()
            if not 1 <= len(referencia) <= 50:
                return "O ponto de referencia deve ter entre 1 e 50 caracteres."
            context["entrega_ponto_referencia"] = referencia
            return self._after_referencia(conversa, context)

        if state == "entrega_aguardando_pagamento_no_ato":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                context["pago_no_ato"] = True
                return self._advance(conversa, "entrega_aguardando_forma_pagamento", context, self._forma_pagamento_prompt())
            if choice is False:
                context["pago_no_ato"] = False
                context["forma_pagamento"] = None
                return self._finish(conversa, context)
            return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."

        if state == "entrega_aguardando_forma_pagamento":
            forma = self._parse_forma_pagamento(message.texto)
            if forma is None:
                return "Opcao invalida. Escolha 1, 2, 3 ou 4."
            if forma == "Outro":
                return self._advance(
                    conversa,
                    "entrega_aguardando_forma_pagamento_outro",
                    context,
                    "Por favor, digite textualmente a forma de pagamento.",
                )
            context["forma_pagamento"] = forma
            return self._finish(conversa, context)

        if state == "entrega_aguardando_forma_pagamento_outro":
            forma = (message.texto or "").strip()
            if not forma:
                return "Por favor, digite textualmente a forma de pagamento."
            context["forma_pagamento"] = forma[:80]
            return self._finish(conversa, context)

        return self.start(conversa)

    def _after_referencia(self, conversa: ConversaWhatsApp, context: dict) -> str:
        aluguer = self.aluguer_service.alugueres.get(int(context["aluguer_id"]))
        if aluguer and not aluguer.pago:
            return self._advance(
                conversa,
                "entrega_aguardando_pagamento_no_ato",
                context,
                "O cliente realizou o pagamento no ato da entrega?\n\n1. Sim\n2. Nao",
            )
        context["pago_no_ato"] = None
        context["forma_pagamento"] = None
        return self._finish(conversa, context)

    def _finish(self, conversa: ConversaWhatsApp, context: dict) -> str:
        try:
            aluguer = self.aluguer_service.confirmar_entrega(
                aluguer_id=int(context["aluguer_id"]),
                contentor_codigo=context["contentor_codigo"],
                operador_telefone=normalize_portugal_phone(conversa.telefone),
                entrega_latitude=context.get("entrega_latitude"),
                entrega_longitude=context.get("entrega_longitude"),
                entrega_ponto_referencia=context.get("entrega_ponto_referencia"),
                fotos_entrega=context.get("fotos_entrega") or [],
                pago_no_ato=context.get("pago_no_ato"),
                forma_pagamento=context.get("forma_pagamento"),
            )
        except ValueError as exc:
            return str(exc)
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        self.db.commit()
        return (
            "Entrega do contentor registrada com sucesso! Ciclo de 5 dias iniciado.\n"
            f"Contentor {aluguer.numero_contentor} vinculado ao pedido #{aluguer.id}.\n\n"
            + MAIN_MENU
        )

    def _format_pedidos(self, alugueres: list[AluguerContentor]) -> str:
        return "\n".join(
            f"{index}. {aluguer.nome_cliente} - {aluguer.tipo_residuo or 'Residuo nao informado'}"
            f"\n   {aluguer.pedido_endereco_texto or 'Localizacao aproximada fornecida'}"
            for index, aluguer in enumerate(alugueres, start=1)
        )

    def _format_contentores_disponiveis(self) -> str:
        disponiveis = [
            contentor.codigo
            for contentor in self.contentor_service.listar_contentores()
            if contentor.status == StatusContentor.DISPONIVEL
        ]
        if not disponiveis:
            return "Nenhum contentor disponivel no momento."
        return "Disponiveis: " + ", ".join(disponiveis)

    def _load_pendentes(self, context: dict) -> list[AluguerContentor]:
        ids = set(context.get("pendentes") or [])
        pendentes = self.aluguer_service.listar_pendentes_entrega()
        if not ids:
            return pendentes
        return [aluguer for aluguer in pendentes if aluguer.id in ids]

    def _resolve_selection(self, raw: str | None, pendentes: list[AluguerContentor]) -> AluguerContentor | None:
        option = self._normalize_option(raw)
        if not option.isdigit():
            return None
        number = int(option)
        if 1 <= number <= len(pendentes):
            return pendentes[number - 1]
        return next((aluguer for aluguer in pendentes if aluguer.id == number), None)

    def _advance(self, conversa: ConversaWhatsApp, next_state: str, context: dict, response: str) -> str:
        conversa.estado_atual = next_state
        conversa.contexto_json = context
        self.db.commit()
        return response

    def _extract_photo_reference(self, message: NormalizedWhatsAppMessage) -> str | None:
        if message.tipo != "image":
            return None
        return message.media_id or message.filename or message.message_id

    def _parse_yes_no(self, raw: str | None) -> bool | None:
        option = self._normalize_option(raw)
        if option in {"1", "sim", "s", "yes", "y"}:
            return True
        if option in {"2", "nao", "n", "no"}:
            return False
        return None

    def _parse_forma_pagamento(self, raw: str | None) -> str | None:
        option = self._normalize_option(raw)
        options = {
            "1": "MBWay",
            "mbway": "MBWay",
            "2": "Transferencia",
            "transferencia": "Transferencia",
            "3": "Dinheiro",
            "dinheiro": "Dinheiro",
            "4": "Outro",
            "outro": "Outro",
        }
        return options.get(option)

    def _forma_pagamento_prompt(self) -> str:
        return "Qual a forma de pagamento recebida?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro"

    def _referencia_prompt(self) -> str:
        return "Deseja informar algum ponto de referencia da entrega?\n\n1. Sim\n2. Nao"

    def _normalize_option(self, raw: str | None) -> str:
        text = (raw or "").strip().lower()
        normalized = unicodedata.normalize("NFKD", text)
        return "".join(char for char in normalized if not unicodedata.combining(char))
