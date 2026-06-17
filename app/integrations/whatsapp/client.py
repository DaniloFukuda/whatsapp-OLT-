from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def send_text_message(to: str, body: str, force_mock: bool = False) -> dict[str, Any]:
    settings = get_settings()
    if force_mock or _should_mock(settings):
        logger.info("Mock WhatsApp send to=%s body=%s", to, body)
        return {"to": to, "body": body, "status": "mocked"}

    url = (
        f"https://graph.facebook.com/{settings.whatsapp_api_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=15)
    except httpx.HTTPError as exc:
        safe_error = _redact_token(str(exc), settings.whatsapp_access_token)
        logger.exception("WhatsApp API request failed to=%s error=%s", to, safe_error)
        return {"to": to, "body": body, "status": "error", "error": safe_error}

    response_body = _safe_json(response)
    if response.status_code >= 400:
        safe_response_text = _redact_token(response.text, settings.whatsapp_access_token)
        safe_response_body = _redact_token(response_body, settings.whatsapp_access_token)
        logger.error(
            "WhatsApp API error to=%s status_code=%s response=%s",
            to,
            response.status_code,
            safe_response_text,
        )
        return {
            "to": to,
            "body": body,
            "status": "error",
            "status_code": response.status_code,
            "response": safe_response_body,
        }

    message_id = _extract_message_id(response_body)
    logger.info("WhatsApp API message sent to=%s message_id=%s", to, message_id)
    return {"to": to, "body": body, "status": "sent", "message_id": message_id}


def _should_mock(settings) -> bool:
    return (
        settings.env.lower() == "test"
        or not settings.whatsapp_access_token.strip()
        or not settings.whatsapp_phone_number_id.strip()
    )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"raw": response.text}
    return data if isinstance(data, dict) else {"raw": data}


def _extract_message_id(response_body: dict[str, Any]) -> str | None:
    messages = response_body.get("messages") or []
    if not messages:
        return None
    first_message = messages[0]
    if not isinstance(first_message, dict):
        return None
    return first_message.get("id")


def _redact_token(value: Any, token: str) -> Any:
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, dict):
        return {key: _redact_token(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_token(item, token) for item in value]
    return value
