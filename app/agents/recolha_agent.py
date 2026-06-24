import unicodedata

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService


class RecolhaAgent:
    START_STATE = "recolha_aguardando_selecao"
    ACTIVE_STATES = {
        "recolha_aguardando_selecao",
        "recolha_aguardando_foto",
        "recolha_aguardando_mais_foto",
        "recolha_aguardando_triagem_carga",
        "recolha_aguardando_relato_carga",
        "recolha_aguardando_triagem_avaria",
        "recolha_aguardando_relato_avaria",
    }

    def __init__(self, db: Session):
        self.db = db
        self.aluguer_service = AluguerService(db)

    def marcar_recolha(self, aluguer_id: int) -> str:
        """Compatibilidade com testes/fluxos antigos que marcavam recolha manualmente."""
        aluguer = self.aluguer_service.marcar_recolha(aluguer_id)
        codigo = aluguer.contentor.codigo if aluguer.contentor else aluguer.numero_contentor
        return f"Contentor {codigo} marcado como aguardando recolha."

    def start(self, conversa: ConversaWhatsApp) -> str:
        candidatos = self.aluguer_service.listar_para_recolha()
        if not candidatos:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao existem contentores entregues aguardando recolha."

        conversa.estado_atual = self.START_STATE
        conversa.contexto_json = {
            "candidatos": [aluguer.id for aluguer in candidatos],
            "updated_at": utcnow().isoformat(),
        }
        self.db.commit()
        return self._format_selection_prompt(candidatos)

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})
        context["updated_at"] = utcnow().isoformat()

        if state == "recolha_aguardando_selecao":
            candidatos = self._load_candidates(context)
            aluguer = self._resolve_selection(message.texto, candidatos)
            if not aluguer:
                return "Opcao invalida. Escolha o numero da lista ou o ID do aluguer."
            context["aluguer_id"] = aluguer.id
            context["fotos_recolha"] = []
            return self._advance(
                conversa,
                "recolha_aguardando_foto",
                context,
                "📷 Envie a foto do contentor cheio e do estado do equipamento antes do içamento.",
            )

        if state == "recolha_aguardando_foto":
            foto = self._extract_photo_reference(message)
            if not foto:
                return "⚠️ Envie uma imagem do contentor para continuar."
            fotos = list(context.get("fotos_recolha") or [])
            fotos.append(foto)
            context["fotos_recolha"] = fotos
            return self._advance(
                conversa,
                "recolha_aguardando_mais_foto",
                context,
                "Foto registrada. Deseja adicionar mais uma foto?\n\n1. Sim\n2. Nao",
            )

        if state == "recolha_aguardando_mais_foto":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                return self._advance(
                    conversa,
                    "recolha_aguardando_foto",
                    context,
                    "📷 Envie a proxima foto da recolha.",
                )
            if choice is False:
                return self._advance(conversa, "recolha_aguardando_triagem_carga", context, self._triagem_carga_prompt(context))
            return "⚠️ Opcao invalida. Responda 1 para Sim ou 2 para Nao."

        if state == "recolha_aguardando_triagem_carga":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                context["carga_errada"] = False
                context["relato_carga"] = None
                return self._advance(conversa, "recolha_aguardando_triagem_avaria", context, self._triagem_avaria_prompt())
            if choice is False:
                context["carga_errada"] = True
                return self._advance(
                    conversa,
                    "recolha_aguardando_relato_carga",
                    context,
                    "📝 Descreva qual material incorreto foi encontrado. Minimo de 10 caracteres.",
                )
            return "⚠️ Opcao invalida. Responda 1 para tudo certo ou 2 para carga misturada/errada."

        if state == "recolha_aguardando_relato_carga":
            relato = (message.texto or "").strip()
            if len(relato) < 10:
                return "⚠️ O relato da carga precisa ter pelo menos 10 caracteres."
            context["relato_carga"] = relato
            return self._advance(conversa, "recolha_aguardando_triagem_avaria", context, self._triagem_avaria_prompt())

        if state == "recolha_aguardando_triagem_avaria":
            choice = self._parse_yes_no(message.texto)
            if choice is True:
                context["contentor_avariado"] = False
                context["relato_avaria"] = None
                return self._finish(conversa, context)
            if choice is False:
                context["contentor_avariado"] = True
                return self._advance(
                    conversa,
                    "recolha_aguardando_relato_avaria",
                    context,
                    "📝 Descreva o estrago ou avaria. Minimo de 10 caracteres.",
                )
            return "⚠️ Opcao invalida. Responda 1 para perfeito ou 2 para avariado."

        if state == "recolha_aguardando_relato_avaria":
            relato = (message.texto or "").strip()
            if len(relato) < 10:
                return "⚠️ O relato da avaria precisa ter pelo menos 10 caracteres."
            context["relato_avaria"] = relato
            return self._finish(conversa, context)

        return self.start(conversa)

    def _finish(self, conversa: ConversaWhatsApp, context: dict) -> str:
        try:
            aluguer = self.aluguer_service.confirmar_recolha(
                aluguer_id=int(context["aluguer_id"]),
                operador_telefone=conversa.telefone,
                fotos_recolha=context.get("fotos_recolha") or [],
                carga_errada=bool(context.get("carga_errada")),
                relato_carga=context.get("relato_carga"),
                contentor_avariado=bool(context.get("contentor_avariado")),
                relato_avaria=context.get("relato_avaria"),
            )
        except ValueError as exc:
            return f"⚠️ {exc}"
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        self.db.commit()
        pendencias = []
        if aluguer.carga_errada:
            pendencias.append("carga errada")
        if aluguer.contentor_avariado:
            pendencias.append("avaria")
        complemento = ""
        if pendencias:
            complemento = "\n\nPendencia criada para o gestor: " + ", ".join(pendencias) + "."
        return "✅ Recolha do contentor registrada com sucesso! Contentor liberado para novo pedido." + complemento

    def _format_selection_prompt(self, candidatos: list[AluguerContentor]) -> str:
        linhas = ["Confirmar recolha de contentor. Escolha o pedido:"]
        for index, aluguer in enumerate(candidatos, start=1):
            contentor = aluguer.contentor.codigo if aluguer.contentor else aluguer.numero_contentor
            rota = self._format_rota(aluguer)
            referencia = aluguer.entrega_ponto_referencia or aluguer.pedido_ponto_referencia or "sem referencia"
            linhas.append(
                f"{index}. {aluguer.nome_cliente} • {aluguer.tipo_residuo or 'Residuo nao informado'} "
                f"• {contentor}\n   📍 {rota} • Ref: {referencia}"
            )
        return "\n".join(linhas)

    def _triagem_carga_prompt(self, context: dict) -> str:
        aluguer = self.aluguer_service.alugueres.get(int(context["aluguer_id"]))
        tipo_residuo = aluguer.tipo_residuo if aluguer else "o contratado"
        return (
            f"O entulho dentro da caçamba está correto com o que foi contratado ({tipo_residuo})?\n\n"
            "1. ✅ Sim, tudo certo\n"
            "2. 🚨 Nao, esta misturado/errado"
        )

    def _triagem_avaria_prompt(self) -> str:
        return (
            "O contentor sofreu algum estrago ou avaria na obra?\n\n"
            "1. ✅ Nao, esta perfeito\n"
            "2. 💥 Sim, esta estragado/com avaria"
        )

    def _load_candidates(self, context: dict) -> list[AluguerContentor]:
        ids = set(context.get("candidatos") or [])
        candidatos = self.aluguer_service.listar_para_recolha()
        if not ids:
            return candidatos
        return [aluguer for aluguer in candidatos if aluguer.id in ids]

    def _resolve_selection(self, raw: str | None, candidatos: list[AluguerContentor]) -> AluguerContentor | None:
        option = self._normalize_option(raw)
        if not option.isdigit():
            return None
        number = int(option)
        if 1 <= number <= len(candidatos):
            return candidatos[number - 1]
        return next((aluguer for aluguer in candidatos if aluguer.id == number), None)

    def _extract_photo_reference(self, message: NormalizedWhatsAppMessage) -> str | None:
        if message.tipo != "image":
            return None
        return message.media_id or message.filename or message.message_id

    def _format_rota(self, aluguer: AluguerContentor) -> str:
        latitude = aluguer.entrega_latitude if aluguer.entrega_latitude is not None else aluguer.pedido_latitude
        longitude = aluguer.entrega_longitude if aluguer.entrega_longitude is not None else aluguer.pedido_longitude
        if latitude is not None and longitude is not None:
            return f"https://www.google.com/maps?q={latitude},{longitude}"
        return aluguer.pedido_endereco_texto or "localizacao nao informada"

    def _advance(self, conversa: ConversaWhatsApp, next_state: str, context: dict, response: str) -> str:
        conversa.estado_atual = next_state
        conversa.contexto_json = context
        self.db.commit()
        return response

    def _parse_yes_no(self, raw: str | None) -> bool | None:
        option = self._normalize_option(raw)
        yes_options = {"1", "sim", "s", "yes", "y", "ok", "tudo certo", "perfeito"}
        no_options = {"2", "nao", "não", "n", "no", "errado", "misturado", "avariado", "estragado"}
        if option in yes_options:
            return True
        if option in no_options:
            return False
        return None

    def _normalize_option(self, raw: str | None) -> str:
        text = (raw or "").strip().lower()
        normalized = unicodedata.normalize("NFKD", text)
        return "".join(char for char in normalized if not unicodedata.combining(char))
