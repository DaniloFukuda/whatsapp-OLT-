from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
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
    START_STATE = "aguardando_numero_contentor"
    CONFIRMED_STATE = "idle"
    ACTIVE_STATES = {
        "aguardando_numero_contentor",
        "aguardando_foto_entrega",
        "aguardando_localizacao",
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_email_cliente",
        "aguardando_confirmacao_data_entrega",
        "aguardando_data_entrega_manual",
        "aguardando_tipo_residuo",
        "aguardando_valor",
        "aguardando_forma_pagamento",
        "aguardando_forma_pagamento_outro",
        "aguardando_pago",
        "aguardando_confirmacao_final",
        "aguardando_campo_correcao",
        "aguardando_valor_correcao",
    }

    FIELD_LABELS = {
        "1": ("numero_contentor", "Numero do contentor", "🚛 Qual o numero do contentor?"),
        "2": ("nome_cliente", "Nome", "👤 Qual o nome do cliente?"),
        "3": ("telefone_cliente", "Telefone", "📞 Envie o telefone do cliente."),
        "4": ("email_cliente", "E-mail", "✉️ Qual o e-mail do cliente? Voce tambem pode responder Pular."),
        "5": ("data_entrega", "Data de entrega", "📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA."),
        "6": ("tipo_residuo", "Residuo", "🧱 Qual o tipo de residuo?\n\n1. Entulho Limpo\n2. Entulho Misto"),
        "7": ("valor", "Valor", "💰 Qual o valor do servico?"),
        "8": (
            "forma_pagamento",
            "Forma de pagamento",
            "💳 Qual a forma de pagamento?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro",
        ),
        "9": ("pago", "Status do pagamento", "✅ O servico ja esta pago?\n\n1. Pago\n2. Pendente"),
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
            conversa.estado_atual = "idle"
            conversa.contexto_json = {"erro": "sem_contentor_disponivel"}
            self.db.commit()
            return "Nao ha contentores disponiveis para iniciar um novo cadastro."

        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "contentor_id": contentor.id,
            "contentor_codigo": contentor.codigo,
            "operador_telefone": normalize_portugal_phone(conversa.telefone),
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        return "🚛 Qual o numero do contentor?"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if state == "aguardando_numero_contentor":
            numero_contentor = self._validate_numero_contentor(message.texto)
            if not numero_contentor:
                return "⚠️ Informe o numero do contentor. Exemplo: 12, C12 ou OLT-12."
            context["numero_contentor"] = numero_contentor
            return self._advance(
                conversa,
                "aguardando_foto_entrega",
                context,
                "📷 Por favor, envie a foto do contentor no local.",
            )

        if state == "aguardando_foto_entrega":
            media_path = self.comprovativo_agent.extract_media_path(message)
            if message.tipo != "image" or not media_path:
                return (
                    "⚠️ Ainda nao recebi a imagem. Por favor, clique no icone de camera/anexo e envie a foto "
                    "do contentor no local para prosseguirmos."
                )
            context["foto_entrega_path"] = media_path
            context["media_id"] = message.media_id
            return self._advance(conversa, "aguardando_localizacao", context, "📍 Agora, envie a localizacao GPS do local.")

        if state == "aguardando_localizacao":
            latitude, longitude = self.localizacao_agent.extract(message)
            if message.tipo != "location" or latitude is None or longitude is None:
                return (
                    "⚠️ Para garantir a precisao do mapa, preciso que envie a sua localizacao em tempo real "
                    "ou localizacao atual pelo botao de anexar (PIN) do WhatsApp."
                )
            context["latitude"] = latitude
            context["longitude"] = longitude
            return self._advance(conversa, "aguardando_nome_cliente", context, "👤 Qual o nome do cliente?")

        if state == "aguardando_nome_cliente":
            nome = self._validate_name(message.texto)
            if not nome:
                return "⚠️ O nome do cliente deve ter entre 3 e 50 caracteres. Por favor, digite novamente."
            context["nome_cliente"] = nome
            return self._advance(conversa, "aguardando_telefone_cliente", context, "📞 Envie o telefone do cliente.")

        if state == "aguardando_telefone_cliente":
            telefone = self._parse_phone(message)
            if not telefone:
                return "⚠️ Envie um telefone valido do cliente."
            context["telefone_cliente"] = telefone
            context["whatsapp_cliente_link"] = whatsapp_link(telefone)
            return self._advance(
                conversa,
                "aguardando_email_cliente",
                context,
                "✉️ Qual o e-mail do cliente? Voce tambem pode responder Pular.",
            )

        if state == "aguardando_email_cliente":
            email = self._parse_email(message.texto)
            if email == "INVALID":
                return "⚠️ E-mail invalido. Digite um e-mail valido ou responda Pular."
            context["email_cliente"] = email
            return self._advance(
                conversa,
                "aguardando_confirmacao_data_entrega",
                context,
                "📅 Confirma a entrega para hoje?\n\n1. Sim\n2. Outra data",
            )

        if state == "aguardando_confirmacao_data_entrega":
            choice = self._normalize_option(message.texto)
            if choice in {"1", "sim", "hoje"}:
                self._set_delivery_date(context, utcnow())
                return self._advance(conversa, "aguardando_tipo_residuo", context, self._tipo_residuo_prompt())
            if choice in {"2", "outra data", "outra"}:
                return self._advance(
                    conversa,
                    "aguardando_data_entrega_manual",
                    context,
                    "📅 Envie a data de entrega no formato DD/MM ou DD/MM/AAAA.",
                )
            return "⚠️ Opcao invalida. Responda 1 para hoje ou 2 para outra data."

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
            return self._advance(conversa, "aguardando_valor", context, "💰 Qual o valor do servico?")

        if state == "aguardando_valor":
            valor = self._parse_money(message.texto)
            if valor is None:
                return "⚠️ Informe um valor valido. Exemplo: 75 ou 75.50."
            context["valor"] = str(valor)
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
            return self._advance(conversa, "aguardando_pago", context, self._status_pagamento_prompt())

        if state == "aguardando_forma_pagamento_outro":
            if not (message.texto or "").strip():
                return "⚠️ Por favor, digite textualmente a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()
            return self._advance(conversa, "aguardando_pago", context, self._status_pagamento_prompt())

        if state == "aguardando_pago":
            pago = self._parse_pagamento_opcao(message.texto)
            if pago is None:
                return "⚠️ Opcao invalida. Responda 1 para Pago ou 2 para Pendente."
            context["pago"] = pago
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
            "1. Cadastrar entrega de contentor\n"
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
        if field == "numero_contentor":
            value = self._validate_numero_contentor(message.texto)
            if not value:
                return "⚠️ Informe o numero do contentor. Exemplo: 12, C12 ou OLT-12."
            context[field] = value
        elif field == "nome_cliente":
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
        elif field == "email_cliente":
            value = self._parse_email(message.texto)
            if value == "INVALID":
                return "⚠️ E-mail invalido. Digite um e-mail valido ou responda Pular."
            context[field] = value
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
                return "⚠️ Informe um valor valido. Exemplo: 75 ou 75.50."
            context[field] = str(value)
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
        elif field == "forma_pagamento_outro":
            if not (message.texto or "").strip():
                return "⚠️ Por favor, digite textualmente a forma de pagamento."
            context["forma_pagamento"] = message.texto.strip()
        elif field == "pago":
            value = self._parse_pagamento_opcao(message.texto)
            if value is None:
                return "⚠️ Opcao invalida. Responda 1 para Pago ou 2 para Pendente."
            context[field] = value

        context.pop("campo_correcao", None)
        return self._advance(conversa, "aguardando_confirmacao_final", context, self._format_confirmation(context))

    def _save(self, conversa: ConversaWhatsApp, context: dict) -> str:
        service_context = {
            "contentor_id": context["contentor_id"],
            "numero_contentor": context["numero_contentor"],
            "nome_cliente": context["nome_cliente"],
            "telefone_cliente": context["telefone_cliente"],
            "email_cliente": context.get("email_cliente"),
            "tipo_residuo": context["tipo_residuo"],
            "valor": Decimal(context["valor"]),
            "forma_pagamento": context["forma_pagamento"],
            "pago": bool(context["pago"]),
            "operador_telefone": context.get("operador_telefone"),
            "foto_entrega_path": context.get("foto_entrega_path"),
            "latitude": context.get("latitude"),
            "longitude": context.get("longitude"),
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
        context["data_retirada_prevista"] = (entrega + self._five_days()).isoformat()

    def _five_days(self):
        from datetime import timedelta

        return timedelta(days=5)

    def _validate_name(self, value: str | None) -> str | None:
        nome = (value or "").strip()
        return nome if 3 <= len(nome) <= 50 else None

    def _validate_numero_contentor(self, value: str | None) -> str | None:
        numero = (value or "").strip()
        return numero if 1 <= len(numero) <= 20 else None

    def _parse_phone(self, message: NormalizedWhatsAppMessage) -> str | None:
        raw = message.contact_phone or message.texto
        telefone = normalize_portugal_phone(raw)
        return telefone if len(telefone) >= 9 and telefone.isdigit() else None

    def _parse_email(self, value: str | None) -> str | None:
        email = (value or "").strip()
        if self._is_optional_skip(email):
            return None
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return email
        return "INVALID"

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

    def _parse_money(self, value: str | None) -> Decimal | None:
        if not value:
            return None
        text = self._normalize_option(value).replace("euros", "").replace("euro", "").replace("eur", "")
        text = text.replace("€", "").strip()
        if not re.search(r"\d", text) or re.fullmatch(r"[a-zA-Z\s]+", text):
            return None
        text = re.sub(r"[^0-9,.]", "", text)
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
        try:
            return Decimal(text).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    def _parse_tipo_residuo(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "entulho limpo"}:
            return "Entulho Limpo"
        if normalized in {"2", "entulho misto"}:
            return "Entulho Misto"
        return None

    def _parse_forma_pagamento(self, value: str | None) -> str | None:
        normalized = self._normalize_option(value)
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
        return options.get(normalized)

    def _parse_pagamento_opcao(self, value: str | None) -> bool | None:
        normalized = self._normalize_option(value)
        if normalized in {"1", "pago", "paga", "sim"}:
            return True
        if normalized in {"2", "pendente", "nao", "nao pago"}:
            return False
        return None

    def _normalize_option(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()

    def _is_optional_skip(self, value: str) -> bool:
        return self._normalize_option(value) in {"", "pular", "saltar", "sem email", "sem e-mail", "nao", "nao tem"}

    def _tipo_residuo_prompt(self) -> str:
        return "🧱 Qual o tipo de residuo?\n\n1. Entulho Limpo\n2. Entulho Misto"

    def _forma_pagamento_prompt(self) -> str:
        return "💳 Qual a forma de pagamento?\n\n1. MBWay\n2. Transferencia\n3. Dinheiro\n4. Outro"

    def _status_pagamento_prompt(self) -> str:
        return "✅ O servico ja esta pago?\n\n1. Pago\n2. Pendente"

    def _correction_menu(self) -> str:
        return (
            "Qual campo deseja corrigir?\n\n"
            "1. Numero do contentor\n"
            "2. Nome\n"
            "3. Telefone\n"
            "4. E-mail\n"
            "5. Data de entrega\n"
            "6. Residuo\n"
            "7. Valor\n"
            "8. Forma de pagamento\n"
            "9. Status do pagamento"
        )

    def _format_confirmation(self, context: dict) -> str:
        entrega = datetime.fromisoformat(context["data_entrega"])
        retirada = datetime.fromisoformat(context["data_retirada_prevista"])
        status = "Pago" if context.get("pago") else "Pendente"
        return "\n".join(
            [
                "✅ Confirmacao dos Dados do Contentor:",
                "",
                f"🚛 Contentor: {context['numero_contentor']}",
                f"👤 Cliente: {context['nome_cliente']}",
                f"📞 Contacto: {context['telefone_cliente']}",
                f"📅 Entrega: {entrega:%d/%m/%Y}",
                f"📅 Retirada prevista: {retirada:%d/%m/%Y}",
                f"🧱 Residuo: {context['tipo_residuo']}",
                f"💰 Valor: {Decimal(context['valor']):.2f} EUR",
                f"💳 Pagamento: {context['forma_pagamento']} ({status})",
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
        return "\n".join(
            [
                f"✅ Cadastro salvo com sucesso. ID/referencia: #{aluguer.id}",
                f"🚛 Contentor: {aluguer.numero_contentor}",
                f"👤 Cliente: {aluguer.nome_cliente}",
                f"📞 Telefone: {aluguer.telefone_cliente}",
                f"WhatsApp cliente: {whatsapp_link(aluguer.telefone_cliente)}",
                f"📅 Data entrega: {aluguer.data_entrega:%d/%m/%Y}",
                f"📅 Retirada prevista: {aluguer.data_vencimento:%d/%m/%Y}",
                f"🧱 Tipo residuo: {aluguer.tipo_residuo}",
                f"💰 Valor: {Decimal(aluguer.valor):.2f} EUR",
                f"💳 Forma pagamento: {aluguer.forma_pagamento}",
                f"✅ Status pagamento: {pagamento}",
                f"Operador: {aluguer.operador_telefone}",
            ]
        )
