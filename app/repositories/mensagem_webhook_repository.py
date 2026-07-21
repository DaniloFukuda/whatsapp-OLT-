from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.mensagem_webhook import MensagemWebhook, StatusMensagemWebhook


class MensagemWebhookRepository:
    def __init__(self, db: Session):
        self.db = db

    def tentar_criar_claim(self, message_id: str, payload_hash: str, telefone: str | None) -> MensagemWebhook | None:
        try:
            self.db.execute(
                insert(MensagemWebhook).values(
                    message_id=message_id,
                    payload_hash=payload_hash,
                    telefone=telefone,
                    status=StatusMensagemWebhook.PROCESSANDO.value,
                    tentativas=1,
                    recebido_em=utcnow(),
                    atualizado_em=utcnow(),
                    resposta_enviada=False,
                )
            )
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return None
        return self.buscar_por_message_id(message_id)

    def buscar_por_message_id(self, message_id: str) -> MensagemWebhook | None:
        return self.db.get(MensagemWebhook, message_id)

    def tentar_reassumir_reprocessavel(self, message_id: str, payload_hash: str) -> MensagemWebhook | None:
        atualizados = (
            self.db.query(MensagemWebhook)
            .filter(MensagemWebhook.message_id == message_id)
            .filter(MensagemWebhook.payload_hash == payload_hash)
            .filter(MensagemWebhook.status == StatusMensagemWebhook.FALHOU_REPROCESSAVEL.value)
            .update(
                {
                    MensagemWebhook.status: StatusMensagemWebhook.PROCESSANDO.value,
                    MensagemWebhook.tentativas: MensagemWebhook.tentativas + 1,
                    MensagemWebhook.atualizado_em: utcnow(),
                    MensagemWebhook.codigo_erro: None,
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        if atualizados != 1:
            return None
        return self.buscar_por_message_id(message_id)

    def marcar_concluida(self, registro: MensagemWebhook, resposta_enviada: bool) -> None:
        agora = utcnow()
        registro.status = StatusMensagemWebhook.CONCLUIDA.value
        registro.resposta_enviada = resposta_enviada
        registro.codigo_erro = None
        registro.concluido_em = agora
        registro.atualizado_em = agora
        self.db.commit()

    def marcar_falhou_reprocessavel(self, registro: MensagemWebhook, codigo_erro: str) -> None:
        registro.status = StatusMensagemWebhook.FALHOU_REPROCESSAVEL.value
        registro.codigo_erro = codigo_erro[:80]
        registro.atualizado_em = utcnow()
        self.db.commit()

    def marcar_falhou_definitiva(self, registro: MensagemWebhook, codigo_erro: str) -> None:
        registro.status = StatusMensagemWebhook.FALHOU_DEFINITIVA.value
        registro.codigo_erro = codigo_erro[:80]
        registro.atualizado_em = utcnow()
        self.db.commit()
