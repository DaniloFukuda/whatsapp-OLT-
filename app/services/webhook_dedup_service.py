from dataclasses import dataclass
import hashlib
import json

from sqlalchemy.orm import Session

from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.mensagem_webhook import MensagemWebhook, StatusMensagemWebhook
from app.repositories.mensagem_webhook_repository import MensagemWebhookRepository


MAX_MESSAGE_ID_LENGTH = 512


@dataclass(frozen=True)
class DedupDecision:
    deve_processar: bool
    motivo_interno: str
    registro: MensagemWebhook | None
    duplicada: bool = False
    conflito_payload: bool = False
    reprocessamento: bool = False


class WebhookDedupService:
    def __init__(self, db: Session):
        self.repository = MensagemWebhookRepository(db)

    @staticmethod
    def payload_hash(raw: dict | None) -> str:
        canonical = json.dumps(raw or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def log_key(message_id: str | None) -> str:
        return hashlib.sha256((message_id or "").encode("utf-8")).hexdigest()[:12]

    def adquirir(self, message: NormalizedWhatsAppMessage) -> DedupDecision:
        message_id = message.message_id
        if not isinstance(message_id, str) or not message_id.strip():
            return DedupDecision(False, "MESSAGE_ID_AUSENTE", None)
        message_id = message_id.strip()
        if len(message_id) > MAX_MESSAGE_ID_LENGTH:
            return DedupDecision(False, "MESSAGE_ID_LONGO", None)

        payload_hash = self.payload_hash(message.raw)
        registro = self.repository.tentar_criar_claim(message_id, payload_hash, None)
        if registro is not None:
            return DedupDecision(True, "CLAIM_ADQUIRIDO", registro)

        existente = self.repository.buscar_por_message_id(message_id)
        if existente is None:
            raise RuntimeError("CLAIM_CONFLITO_SEM_REGISTRO")
        if existente.payload_hash != payload_hash:
            return DedupDecision(False, "PAYLOAD_DIVERGENTE", existente, True, True)
        if existente.status == StatusMensagemWebhook.FALHOU_REPROCESSAVEL.value:
            reassumido = self.repository.tentar_reassumir_reprocessavel(message_id, payload_hash)
            if reassumido is not None:
                return DedupDecision(True, "REPROCESSAMENTO_ADQUIRIDO", reassumido, reprocessamento=True)
        return DedupDecision(False, f"DUPLICADA_{existente.status}", existente, True)

    def marcar_concluida(self, registro: MensagemWebhook, resposta_enviada: bool) -> None:
        self.repository.marcar_concluida(registro, resposta_enviada)

    def marcar_falhou_reprocessavel(self, registro: MensagemWebhook, error: BaseException) -> None:
        self.repository.marcar_falhou_reprocessavel(registro, type(error).__name__)

    def marcar_falhou_definitiva(self, registro: MensagemWebhook, error: BaseException) -> None:
        self.repository.marcar_falhou_definitiva(registro, type(error).__name__)
