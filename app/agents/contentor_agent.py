from sqlalchemy.orm import Session

from app.core.phone import normalize_portugal_phone
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.contentor import Contentor
from app.services.contentor_service import ContentorService


class ContentorAgent:
    DELETE_START_STATE = "contentor_exclusao_aguardando_item"
    ACTIVE_STATES = {
        "contentor_exclusao_aguardando_item",
        "contentor_exclusao_aguardando_confirmacao",
        "contentor_exclusao_aguardando_justificativa",
    }

    def __init__(self, db: Session):
        self.db = db
        self.contentor_service = ContentorService(db)

    def listar_status(self) -> str:
        contentores = self.contentor_service.listar_contentores()
        if not contentores:
            return "Ainda nao existem contentores registrados."
        return "\n".join(f"{contentor.codigo}: {contentor.status.value}" for contentor in contentores)

    def start_exclusao(self, conversa: ConversaWhatsApp) -> str:
        contentores = self.contentor_service.listar_contentores()
        if not contentores:
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Nao encontrei contentores ativos para excluir."
        conversa.estado_atual = self.DELETE_START_STATE
        conversa.contexto_json = {"contentor_ids": [contentor.id for contentor in contentores]}
        self.db.commit()
        return "Escolha o contentor para excluir:\n" + self._format_list(contentores) + "\n0 - Cancelar"

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        context = dict(conversa.contexto_json or {})

        if state == "contentor_exclusao_aguardando_item":
            contentor = self._select_contentor(context, message.texto)
            if not contentor:
                return "Informe um numero da lista ou codigo de contentor valido."
            context["contentor_id"] = contentor.id
            context["contentor_codigo"] = contentor.codigo
            conversa.estado_atual = "contentor_exclusao_aguardando_confirmacao"
            conversa.contexto_json = context
            self.db.commit()
            return (
                f"Contentor selecionado: {contentor.codigo}\n\n"
                "Tem certeza que deseja excluir este contentor com seguranca?\n\n"
                "1 - Sim, continuar\n2 - Nao, cancelar\n0 - Cancelar"
            )

        if state == "contentor_exclusao_aguardando_confirmacao":
            confirmation = self._parse_confirmacao(message.texto)
            if confirmation is True:
                conversa.estado_atual = "contentor_exclusao_aguardando_justificativa"
                conversa.contexto_json = context
                self.db.commit()
                return "Informe a justificativa da exclusao com pelo menos 10 caracteres."
            if confirmation is None:
                return "Opcao invalida. Responda 1 para Sim ou 2 para Nao."
            conversa.estado_atual = "idle"
            conversa.contexto_json = {}
            self.db.commit()
            return "Exclusao cancelada. Nenhum contentor foi excluido."

        if state == "contentor_exclusao_aguardando_justificativa":
            justificativa = (message.texto or "").strip()
            if len(justificativa) < 10:
                return "A justificativa deve ter pelo menos 10 caracteres."
            contentor = self.contentor_service.excluir_com_auditoria(
                contentor_id=context["contentor_id"],
                operador_telefone=normalize_portugal_phone(message.telefone),
                justificativa=justificativa,
            )
            conversa.estado_atual = "idle"
            conversa.contexto_json = {"contentor_id": contentor.id, "ultima_operacao": "exclusao_contentor"}
            self.db.commit()
            return f"Contentor {contentor.codigo} excluido com seguranca."

        return "Comando nao reconhecido. Envie 'excluir contentor' para iniciar."

    def _select_contentor(self, context: dict, value: str | None) -> Contentor | None:
        text = (value or "").strip()
        ids = context.get("contentor_ids") or []
        try:
            index = int(text)
        except ValueError:
            index = None
        if index is not None and 1 <= index <= len(ids):
            return self.contentor_service.repository.get(ids[index - 1])
        if text:
            return self.contentor_service.repository.get_by_codigo(text)
        return None

    def _format_list(self, contentores: list[Contentor]) -> str:
        return "\n".join(
            f"{index}. {contentor.codigo} - {contentor.status.value}"
            for index, contentor in enumerate(contentores, start=1)
        )

    def _parse_confirmacao(self, value: str | None) -> bool | None:
        normalized = (value or "").strip().lower()
        if normalized in {"1", "sim", "s", "confirmar", "excluir", "apagar"}:
            return True
        if normalized in {"2", "nao", "não", "n", "cancelar"}:
            return False
        return None
