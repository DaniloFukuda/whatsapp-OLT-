from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from sqlalchemy.orm import Session

from app.agents.localizacao_agent import LocalizacaoAgent
from app.core.phone import normalize_portugal_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import StatusEntrega
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService


class AluguerAgent:
    START_STATE = "aguardando_nome_cliente"
    CONFIRMED_STATE = "idle"
    ACTIVE_STATES = {
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_confirmacao_data_entrega",
        "aguardando_data_entrega_manual",
        "aguardando_tipo_residuo",
        "aguardando_valor",
        "aguardando_pago",
        "aguardando_forma_pagamento",
        "aguardando_forma_pagamento_outro",
        "aguardando_tipo_endereco_pedido",
        "aguardando_endereco_pedido_localizacao",
        "aguardando_endereco_pedido_texto",
        "aguardando_ponto_referencia_opcao",
        "aguardando_ponto_referencia_texto",
        "aguardando_confirmacao_final",
        "aguardando_campo_correcao",
        "aguardando_valor_correcao",
    }

    FIELD_LABELS = {
        "1": ("nome_cliente", "Nome", "👤 Qual o nome do cliente?"),
        "2": ("telefone_cliente", "Telefone", "📞 Envie o telefone do cliente ou anexe o contacto."),
        "3": ("data_entrega", "Data de entrega", "📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA."),
        "4": ("tipo_residuo", "Residuo", "🧱 Qual o tipo de residuo?\n\n1. Entulho Limpo\n2. Entulho Misto"),
        "5": ("valor", "Valor", "💰 Qual o valor do servico? Ex: 75 ou 120.50"),
        "6": ("pago", "Status do pagamento", "✅ O pedido ja esta pago?\n\n1. Sim, ja esta pago\n2. Nao, pendente"),
        "7": (
            "forma_pagamento",
            "Forma de pagamento",
            "💳 Qual a forma de pagamento?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro",
        ),
        "8": (
            "pedido_endereco_tipo",
            "Endereco do pedido",
            "Como deseja inserir o endereco aproximado do pedido?\n\n1. 📍 Enviar localizacao\n2. ✍️ Digitar endereco",
        ),
        "9": (
            "pedido_ponto_referencia",
            "Ponto de referencia",
            "Deseja informar algum ponto de referencia? Responda o texto ou Nao.",
        ),
    }

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)
        self.contentor_service = ContentorService(db)
        self.localizacao_agent = LocalizacaoAgent()

    def start(self, conversa: ConversaWhatsApp) -> str:
        contentor = self.contentor_service.repository.first_available()
        if not contentor:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {"erro": "sem_contentor_disponivel"}
            self.db.commit()
            return "Nao ha contentores disponiveis para iniciar um novo pedido."

        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "contentor_id": contentor.id,
            "contentor_codigo": contentor.codigo,
            "numero_contentor": contentor.codigo,
            "operador_telefone": normalize_portugal_phone(conversa.telefone),
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        return "Cadastro de pedido iniciado.\n\n👤 Qual o nome do cliente?"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if state == "aguardando_nome_cliente":
            nome = self._validate_name(message.texto)
            if not nome:
                return "⚠️ O nome do cliente deve ter entre 3 e 50 caracteres. Por favor, digite novamente."
            context["nome_cliente"] = nome
            return self._advance(conversa, "aguardando_telefone_cliente", context, "📞 Envie o telefone do cliente ou anexe o contacto.")

        if state == "aguardando_telefone_cliente":
            telefone = self._parse_phone(message)
            if not telefone:
                return "⚠️ Envie um telefone valido do cliente."
            context["telefone_cliente"] = telefone
            context["whatsapp_cliente_link"] = whatsapp_link(telefone)
            return self._advance(conversa, "aguardando_confirmacao_data_entrega", context, self._data_entrega_prompt())

        if state == "aguardando_confirmacao_data_entrega":
            choice = self._parse_date_choice(message.texto)
            if choice == "HOJE":
                self._set_delivery_date(context, utcnow())
                return self._advance(conversa, "aguardando_tipo_residuo", context, self._tipo_residuo_prompt())
            if choice == "AMANHA":
                self._set_delivery_date(context, utcnow() + timedelta(days=1))
                return self._advance(conversa, "aguardando_tipo_residuo", context, self._tipo_residuo_prompt())
            if choice == "OUTRA":
                return self._advance(
                    conversa,
                    "aguardando_data_entrega_manual",
                    context,
                    "📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA.",
                )
            return "⚠️ Opcao invalida. Responda 1 para hoje, 2 para amanha ou 3 para outra data."

        if state == "aguardando_data_entrega_manual":
            entrega = self._parse_date(message.texto)
            if not entrega:
                return "⚠️ Data invalida. Envie no formato DD/MM ou DD/MM/AAAA."
            self._set_delivery_date(context, entrega)
            return self._advance(conversa, "aguardando_tipo_residuo", context, self._tipo_residuo_prompt())

        if state == "aguardando_tipo_residuo":
            tipo_residuo = self._parse_tipo_residuo(message.texto)
            if tipo_residuo is None:
                return "⚠️ Opcao invalida. Escolha 1 para Entulho Limpo ou 2 para Entulho Misto."
            context["tipo_residuo"] = tipo_residuo
            return self._advance(conversa, "aguardando_valor", context, "💰 Qual o valor do servico? Ex: 75 ou 120.50")

        if state == "aguardando_valor":
            valor = self._parse_money(message.texto)
            if valor is None:
                return "⚠️ O valor inserido excede o limite permitido por unidade. Por favor, insira um valor valido (Ex: 75 ou 120.50)."
            context["valor"] = str(valor)
            return self._advance(conversa, "aguardando_pago", context, self._status_pagamento_prompt())

        if state == "aguardando_pago":
            pago = self._parse_pagamento_opcao(message.texto)
            if pago is None:
                return "⚠️ Opcao invalida. Responda 1 para pago ou 2 para pendente."
            context["pago"] = pago
            if not pago:
                context["forma_pagamento"] = None
                return self._advance(conversa, "aguardando_tipo_endereco_pedido", context, self._tipo_endereco_prompt())
            return self._advance(conversa, "aguardando_forma_pagamento", context, self._forma_pagamento_prompt())

        if state == "aguardando_forma_pagamento":
            forma = self._parse_forma_pagamento(message.texto)
            if forma is None:
                return "⚠️ Opcao invalida. Escolha 1, 2, 3 ou 4."
            if forma == "Outro":
                return self._advance(
                    conversa,
                    "aguardando_forma_pagamento_outro",
                    context,
                    "💳 Por favor, digite textualmente a forma de pagamento.",
                )
            context["forma_pagamento"] = forma
            return self._advance(conversa, "aguardando_tipo_endereco_pedido", context, self._tipo_endereco_prompt())

        if state == "aguardando_forma_pagamento_outro":
            if not (message.texto or "").strip():
                return "⚠️ Por favor, digite textualmente a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()[:80]
            return self._advance(conversa, "aguardando_tipo_endereco_pedido", context, self._tipo_endereco_prompt())

        if state == "aguardando_tipo_endereco_pedido":
            endereco_tipo = self._parse_endereco_tipo(message.texto)
            if endereco_tipo is None:
                return "⚠️ Opcao invalida. Escolha 1 para enviar localizacao ou 2 para digitar endereco."
            context["pedido_endereco_tipo"] = endereco_tipo
            if endereco_tipo == "LOCALIZACAO":
                return self._advance(
                    conversa,
                    "aguardando_endereco_pedido_localizacao",
                    context,
                    "📍 Envie a localizacao aproximada do pedido pelo WhatsApp ou cole um link do Google Maps.",
                )
            return self._advance(conversa, "aguardando_endereco_pedido_texto", context, "✍️ Digite o endereco aproximado do pedido, com no maximo 300 caracteres.")

        if state == "aguardando_endereco_pedido_localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "⚠️ Nao consegui identificar a localizacao. Envie o PIN do WhatsApp ou um link do Google Maps com coordenadas."
            context["pedido_latitude"] = latitude
            context["pedido_longitude"] = longitude
            context["pedido_endereco_texto"] = None
            return self._advance(conversa, "aguardando_ponto_referencia_opcao", context, self._ponto_referencia_prompt())

        if state == "aguardando_endereco_pedido_texto":
            endereco = self._validate_endereco(message.texto)
            if not endereco:
                return "⚠️ O endereco deve ter entre 1 e 300 caracteres. Por favor, digite novamente."
            context["pedido_endereco_texto"] = endereco
            context["pedido_latitude"] = None
            context["pedido_longitude"] = None
            return self._advance(conversa, "aguardando_ponto_referencia_opcao", context, self._ponto_referencia_prompt())

        if state == "aguardando_ponto_referencia_opcao":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                return self._advance(conversa, "aguardando_ponto_referencia_texto", context, "📌 Digite o ponto de referencia com no maximo 50 caracteres.")
            if choice is False:
                context["pedido_ponto_referencia"] = None
                return self._advance(conversa, "aguardando_confirmacao_final", context, self._format_confirmation(context))
            return "⚠️ Opcao invalida. Responda 1/Sim para informar referencia ou 2/Nao para continuar."

        if state == "aguardando_ponto_referencia_texto":
            referencia = self._validate_referencia(message.texto)
            if not referencia:
                return "⚠️ O ponto de referencia deve ter entre 1 e 50 caracteres. Por favor, digite novamente."
            context["pedido_ponto_referencia"] = referencia
            return self._advance(conversa, "aguardando_confirmacao_final", context, self._format_confirmation(context))

        if state == "aguardando_confirmacao_final":
            choice = self._normalize_option(message.texto)
            if choice in {"1", "confirmar", "confirmar e salvar"}:
                return self._save(conversa, context)
            if choice in {"2", "corrigir", "corrigir dados"}:
                return self._advance(conversa, "aguardando_campo_correcao", context, self._correction_menu())
            if choice in {"3", "cancelar", "cancelar tudo"}:
                conversa.estado_atual = "idle"
                conversa.contexto_json = {}
                self.db.commit()
                return "🚫 Cadastro cancelado. Nenhum dado foi salvo.\n\n" + self.initial_menu()
            return "⚠️ Opcao invalida. Escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar."

        if state == "aguardando_campo_correcao":
            field = self.FIELD_LABELS.get((message.texto or "").strip())
            if not field:
                return self._correction_menu()
            context["campo_correcao"] = field[0]
            return self._advance(conversa, "aguardando_valor_correcao", context, field[2])

        if state == "aguardando_valor_correcao":
            return self._handle_correction(conversa, context, message)

        return self.start(conversa)

    @classmethod
    def initial_menu(cls) -> str:
        return (
            "Ola, sou o Robo de Gestao de Contentores da OLT. O que vamos fazer agora?\n\n"
            "1. Cadastrar pedido de contentor\n"
            "2. Alterar informacoes\n"
            "3. Excluir pedidos\n"
            "4. Ver resumo"
        )

    def timeout_prompt(self, conversa: ConversaWhatsApp) -> str:
        nome = (conversa.contexto_json or {}).get("nome_cliente") or "ainda sem nome"
        conversa.estado_atual = "cadastro_expirado"
        self.db.commit()
        return (
            f"Vi que voce nao terminou o cadastro do cliente {nome}. Deseja continuar de onde parou?\n\n"
            "1. Sim, continuar\n"
            "2. Nao, recomecar"
        )

    def _handle_correction(self, conversa: ConversaWhatsApp, context: dict, message: NormalizedWhatsAppMessage) -> str:
        field = context.get("campo_correcao")
        if field == "nome_cliente":
            value = self._validate_name(message.texto)
            if not value:
                return "⚠️ O nome do cliente deve ter entre 3 e 50 caracteres. Por favor, digite novamente."
            context[field] = value
        elif field == "telefone_cliente":
            value = self._parse_phone(message)
            if not value:
                return "⚠️ Envie um telefone valido do cliente."
            context[field] = value
            context["whatsapp_cliente_link"] = whatsapp_link(value)
        elif field == "data_entrega":
            value = self._parse_date(message.texto)
            if not value:
                return "⚠️ Data invalida. Envie no formato DD/MM ou DD/MM/AAAA."
            self._set_delivery_date(context, value)
        elif field == "tipo_residuo":
            value = self._parse_tipo_residuo(message.texto)
            if value is None:
                return "⚠️ Opcao invalida. Escolha 1 para Entulho Limpo ou 2 para Entulho Misto."
            context[field] = value
        elif field == "valor":
            value = self._parse_money(message.texto)
            if value is None:
                return "⚠️ O valor inserido excede o limite permitido por unidade. Por favor, insira um valor valido (Ex: 75 ou 120.50)."
            context[field] = str(value)
        elif field == "pago":
            value = self._parse_pagamento_opcao(message.texto)
            if value is None:
                return "⚠️ Opcao invalida. Responda 1 para pago ou 2 para pendente."
            context[field] = value
            if not value:
                context["forma_pagamento"] = None
        elif field == "forma_pagamento":
            value = self._parse_forma_pagamento(message.texto)
            if value is None:
                return "⚠️ Opcao invalida. Escolha 1, 2, 3 ou 4."
            if value == "Outro":
                context["campo_correcao"] = "forma_pagamento_outro"
                return self._advance(
                    conversa,
                    "aguardando_valor_correcao",
                    context,
                    "💳 Por favor, digite textualmente a forma de pagamento.",
                )
            context[field] = value
            context["pago"] = True
        elif field == "forma_pagamento_outro":
            if not (message.texto or "").strip():
                return "⚠️ Por favor, digite textualmente a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()[:80]
            context["pago"] = True
        elif field == "pedido_endereco_tipo":
            value = self._parse_endereco_tipo(message.texto)
            if value is None:
                return "⚠️ Opcao invalida. Escolha 1 para enviar localizacao ou 2 para digitar endereco."
            context["pedido_endereco_tipo"] = value
            context["campo_correcao"] = "pedido_endereco_localizacao" if value == "LOCALIZACAO" else "pedido_endereco_texto"
            prompt = (
                "📍 Envie a localizacao aproximada do pedido pelo WhatsApp ou cole um link do Google Maps."
                if value == "LOCALIZACAO"
                else "✍️ Digite o endereco aproximado do pedido, com no maximo 300 caracteres."
            )
            return self._advance(conversa, "aguardando_valor_correcao", context, prompt)
        elif field == "pedido_endereco_localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if latitude is None or longitude is None:
                return "⚠️ Nao consegui identificar a localizacao. Envie o PIN do WhatsApp ou um link do Google Maps com coordenadas."
            context["pedido_endereco_tipo"] = "LOCALIZACAO"
            context["pedido_latitude"] = latitude
            context["pedido_longitude"] = longitude
            context["pedido_endereco_texto"] = None
        elif field == "pedido_endereco_texto":
            value = self._validate_endereco(message.texto)
            if not value:
                return "⚠️ O endereco deve ter entre 1 e 300 caracteres. Por favor, digite novamente."
            context["pedido_endereco_tipo"] = "TEXTO"
            context["pedido_endereco_texto"] = value
            context["pedido_latitude"] = None
            context["pedido_longitude"] = None
        elif field == "pedido_ponto_referencia":
            if self._parse_yes_no(message.texto) is False:
                context[field] = None
            else:
                value = self._validate_referencia(message.texto)
                if not value:
                    return "⚠️ O ponto de referencia deve ter entre 1 e 50 caracteres. Por favor, digite novamente."
                context[field] = value

        context.pop("campo_correcao", None)
        return self._advance(conversa, "aguardando_confirmacao_final", context, self._format_confirmation(context))

    def _save(self, conversa: ConversaWhatsApp, context: dict) -> str:
        service_context = {
            "contentor_id": context["contentor_id"],
            "numero_contentor": context["numero_contentor"],
            "nome_cliente": context["nome_cliente"],
            "telefone_cliente": context["telefone_cliente"],
            "tipo_residuo": context["tipo_residuo"],
            "valor": Decimal(context["valor"]),
            "forma_pagamento": context.get("forma_pagamento"),
            "pago": bool(context["pago"]),
            "operador_telefone": context.get("operador_telefone"),
            "pedido_feito_por": context.get("operador_telefone"),
            "status_entrega": StatusEntrega.PENDENTE.value,
            "pedido_endereco_tipo": context.get("pedido_endereco_tipo"),
            "pedido_endereco_texto": context.get("pedido_endereco_texto"),
            "pedido_latitude": context.get("pedido_latitude"),
            "pedido_longitude": context.get("pedido_longitude"),
            "pedido_ponto_referencia": context.get("pedido_ponto_referencia"),
            "data_entrega": datetime.fromisoformat(context["data_entrega"]),
        }
        aluguer = self.aluguer_service.registrar_novo_aluguer(**service_context)
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        self.db.commit()
        return self._format_summary(aluguer)

    def _advance(self, conversa: ConversaWhatsApp, next_state: str, context: dict, response: str) -> str:
        conversa.estado_atual = next_state
        conversa.contexto_json = context
        self.db.commit()
        return response

    def _set_delivery_date(self, context: dict, entrega: datetime) -> None:
        if entrega.tzinfo is None:
            entrega = entrega.replace(tzinfo=utcnow().tzinfo)
        context["data_entrega"] = entrega.isoformat()
        context["data_retirada_prevista"] = (entrega + timedelta(days=5)).isoformat()

    def _validate_name(self, value: str | None) -> str | None:
        nome = (value or "").strip()
        return nome if 3 <= len(nome) <= 50 else None

    def _parse_phone(self, message: NormalizedWhatsAppMessage) -> str | None:
        raw = message.contact_phone or message.texto
        telefone = normalize_portugal_phone(raw)
        return telefone if len(telefone) >= 9 and telefone.isdigit() else None

    def _parse_date(self, value: str | None) -> datetime | None:
        text = (value or "").strip()
        for fmt in ("%d/%m/%Y", "%d/%m"):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt == "%d/%m":
                    parsed = parsed.replace(year=utcnow().year)
                return parsed.replace(hour=12)
            except ValueError:
                continue
        return None

    def _parse_date_choice(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "hoje", "sim"}:
            return "HOJE"
        if normalized in {"2", "amanha"}:
            return "AMANHA"
        if normalized in {"3", "outra", "outra data"}:
            return "OUTRA"
        return None

    def _parse_money(self, value: str | None) -> Decimal | None:
        if not value:
            return None
        text = self._normalize_option(value).replace("euros", "").replace("euro", "").replace("eur", "")
        text = text.replace("€", "").strip()
        if not re.search(r"\d", text) or re.fullmatch(r"[a-zA-Z\s]+", text):
            return None
        text = re.sub(r"[^0-9,.]", "", text)
        if not text:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
        integer_part = text.split(".", 1)[0]
        if len(integer_part.lstrip("0") or "0") > 3:
            return None
        try:
            valor = Decimal(text).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
        if valor < 0 or valor > Decimal("999.99"):
            return None
        return valor

    def _parse_tipo_residuo(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "entulho limpo", "limpo"}:
            return "Entulho Limpo"
        if normalized in {"2", "entulho misto", "misto"}:
            return "Entulho Misto"
        return None

    def _parse_forma_pagamento(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
        options = {
            "1": "MBWay",
            "mbway": "MBWay",
            "2": "Transferencia",
            "transferencia": "Transferencia",
            "transferência": "Transferencia",
            "3": "Dinheiro",
            "dinheiro": "Dinheiro",
            "4": "Outro",
            "outro": "Outro",
        }
        return options.get(normalized)

    def _parse_pagamento_opcao(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "pago", "paga", "sim", "sim ja esta pago", "sim, ja esta pago"}:
            return True
        if normalized in {"2", "pendente", "nao", "nao pago", "nao pendente", "nao, pendente"}:
            return False
        return None

    def _parse_endereco_tipo(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "localizacao", "enviar localizacao", "enviar localização", "pin"}:
            return "LOCALIZACAO"
        if normalized in {"2", "digitar", "digitar endereco", "digitar endereço", "texto", "endereco", "endereço"}:
            return "TEXTO"
        return None

    def _parse_yes_no(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "sim", "s"}:
            return True
        if normalized in {"2", "nao", "não", "n"}:
            return False
        return None

    def _validate_endereco(self, value: str | None) -> str | None:
        endereco = (value or "").strip()
        return endereco if 1 <= len(endereco) <= 300 else None

    def _validate_referencia(self, value: str | None) -> str | None:
        referencia = (value or "").strip()
        return referencia if 1 <= len(referencia) <= 50 else None

    def _normalize_option(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()

    def _data_entrega_prompt(self) -> str:
        return "📅 Quando sera a entrega?\n\n1. Hoje\n2. Amanha\n3. Outra data"

    def _tipo_residuo_prompt(self) -> str:
        return "🧱 Qual o tipo de residuo?\n\n1. Entulho Limpo\n2. Entulho Misto"

    def _forma_pagamento_prompt(self) -> str:
        return "💳 Qual a forma de pagamento?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro"

    def _status_pagamento_prompt(self) -> str:
        return "✅ O pedido ja esta pago?\n\n1. Sim, ja esta pago\n2. Nao, pendente"

    def _tipo_endereco_prompt(self) -> str:
        return "Como deseja inserir o endereco aproximado do pedido?\n\n1. 📍 Enviar localizacao\n2. ✍️ Digitar endereco"

    def _ponto_referencia_prompt(self) -> str:
        return "Deseja informar algum ponto de referencia?\n\n1. Sim\n2. Nao"

    def _correction_menu(self) -> str:
        return (
            "Qual campo deseja corrigir?\n\n"
            "1. Nome\n"
            "2. Telefone\n"
            "3. Data de entrega\n"
            "4. Residuo\n"
            "5. Valor\n"
            "6. Status do pagamento\n"
            "7. Forma de pagamento\n"
            "8. Endereco do pedido\n"
            "9. Ponto de referencia"
        )

    def _format_endereco(self, context: dict) -> str:
        if context.get("pedido_endereco_tipo") == "LOCALIZACAO":
            return f"Localizacao aproximada: {context.get('pedido_latitude')},{context.get('pedido_longitude')}"
        return context.get("pedido_endereco_texto") or "Nao informado"

    def _format_confirmation(self, context: dict) -> str:
        entrega = datetime.fromisoformat(context["data_entrega"])
        status = "Pago" if context.get("pago") else "Pendente"
        forma = context.get("forma_pagamento") or "A definir quando receber"
        referencia = context.get("pedido_ponto_referencia") or "Nao informado"
        return "\n".join(
            [
                "✅ Confirmacao do Pedido do Contentor:",
                "",
                f"👤 Cliente: {context['nome_cliente']}",
                f"📞 Contacto: {context['telefone_cliente']}",
                f"📅 Entrega combinada: {entrega:%d/%m/%Y}",
                f"🧱 Residuo: {context['tipo_residuo']}",
                f"💰 Valor: {Decimal(context['valor']):.2f} EUR",
                f"💳 Pagamento: {forma} ({status})",
                f"📍 Endereco do pedido: {self._format_endereco(context)}",
                f"📌 Referencia: {referencia}",
                "",
                "Status da entrega apos salvar: PENDENTE",
                "O ciclo de 5 dias sera iniciado na confirmacao da entrega pelo motorista.",
                "",
                "Os dados estao corretos?",
                "",
                "1. Confirmar e Salvar",
                "2. Corrigir Dados",
                "3. Cancelar Tudo",
            ]
        )

    def _format_summary(self, aluguer) -> str:
        pagamento = "Pago" if aluguer.pago else "Pendente"
        forma = aluguer.forma_pagamento or "A definir quando receber"
        endereco = aluguer.pedido_endereco_texto or ""
        if aluguer.pedido_latitude is not None and aluguer.pedido_longitude is not None:
            endereco = f"https://www.google.com/maps?q={aluguer.pedido_latitude},{aluguer.pedido_longitude}"
        return "\n".join(
            [
                f"✅ Pedido salvo com sucesso. ID/referencia: #{aluguer.id}",
                f"🚛 Contentor reservado: {aluguer.numero_contentor}",
                f"👤 Cliente: {aluguer.nome_cliente}",
                f"📞 Telefone: {aluguer.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(aluguer.telefone_cliente)}",
                f"📅 Data combinada de entrega: {aluguer.data_entrega:%d/%m/%Y}",
                f"🧱 Tipo residuo: {aluguer.tipo_residuo}",
                f"💰 Valor: {Decimal(aluguer.valor):.2f} EUR",
                f"💳 Forma pagamento: {forma}",
                f"✅ Status pagamento: {pagamento}",
                f"📍 Endereco aproximado: {endereco or 'Nao informado'}",
                f"📌 Referencia: {aluguer.pedido_ponto_referencia or 'Nao informado'}",
                f"📦 Status entrega: {aluguer.status_entrega}",
                f"Pedido feito por: {aluguer.pedido_feito_por}",
            ]
        )
