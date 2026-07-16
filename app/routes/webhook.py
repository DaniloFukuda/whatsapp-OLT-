from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.db import get_db
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import parse_whatsapp_payload
from app.services.whatsapp_dedup_service import WhatsAppMessageDedupService

router = APIRouter(prefix="/webhook", tags=["webhook"])
logger = get_logger(__name__)


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify_whatsapp_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> str:
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Invalid verify token")


@router.post("/whatsapp")
def receive_whatsapp_webhook(
    payload: dict,
    db: Session = Depends(get_db),
    x_olt_mock_whatsapp: str | None = Header(default=None),
) -> dict:
    router_agent = WhatsappRouterAgent(db)
    dedup = WhatsAppMessageDedupService(db)
    sent_messages = []
    force_mock = (x_olt_mock_whatsapp or "").strip().lower() in {"1", "true", "yes", "sim"}
    for message in parse_whatsapp_payload(payload):
        message_id = (message.message_id or "").strip()
        claim = dedup.claim(message_id)
        if not claim.should_process:
            logger.info(
                "WhatsApp webhook message skipped by dedup message_id=%s reason=%s type=%s",
                message_id,
                claim.reason,
                message.tipo,
            )
            continue
        try:
            response = router_agent.handle(message)
            sent_messages.append(send_whatsapp_message(message.telefone, response, force_mock=force_mock))
            for pending in router_agent.pop_pending_messages():
                sent_messages.append(send_whatsapp_message(message.telefone, pending, force_mock=force_mock))
            dedup.mark_completed(message_id)
        except Exception:
            dedup.mark_failed(message_id)
            logger.exception(
                "WhatsApp webhook message processing failed message_id=%s type=%s",
                message_id,
                message.tipo,
            )
            raise
    return {"status": "ok", "messages": sent_messages}
