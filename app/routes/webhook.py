from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.core.db import get_db
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import parse_whatsapp_payload

router = APIRouter(prefix="/webhook", tags=["webhook"])


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
    sent_messages = []
    force_mock = (x_olt_mock_whatsapp or "").strip().lower() in {"1", "true", "yes", "sim"}
    for message in parse_whatsapp_payload(payload):
        response = router_agent.handle(message)
        sent_messages.append(send_whatsapp_message(message.telefone, response, force_mock=force_mock))
        for pending in router_agent.pop_pending_messages():
            sent_messages.append(send_whatsapp_message(message.telefone, pending, force_mock=force_mock))
    return {"status": "ok", "messages": sent_messages}
