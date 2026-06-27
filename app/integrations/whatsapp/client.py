import re
import unicodedata
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def send_whatsapp_message(to: str, body: str, force_mock: bool = False) -> dict[str, Any]:
    buttons = _yes_no_buttons_for_body(body)
    if buttons:
        return send_button_message(to, body, buttons, force_mock=force_mock)
    return send_text_message(to, body, force_mock=force_mock)


def send_text_message(to: str, body: str, force_mock: bool = False) -> dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return _send_payload(to=to, body=body, payload=payload, force_mock=force_mock)


def send_button_message(
    to: str,
    body: str,
    buttons: list[dict[str, str]],
    force_mock: bool = False,
) -> dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button["id"], "title": button["title"]}}
                    for button in buttons[:3]
                ]
            },
        },
    }
    return _send_payload(
        to=to,
        body=body,
        payload=payload,
        force_mock=force_mock,
        buttons=buttons[:3],
        include_type=True,
    )


def _send_payload(
    to: str,
    body: str,
    payload: dict[str, Any],
    force_mock: bool = False,
    buttons: list[dict[str, str]] | None = None,
    include_type: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    if force_mock or _should_mock(settings):
        logger.info("Mock WhatsApp send to=%s body=%s", to, body)
        result = {"to": to, "body": body, "status": "mocked"}
        if include_type:
            result["type"] = payload["type"]
        if buttons:
            result["buttons"] = buttons
        return result

    url = (
        f"https://graph.facebook.com/{settings.whatsapp_api_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
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
    result = {"to": to, "body": body, "status": "sent", "message_id": message_id}
    if include_type:
        result["type"] = payload["type"]
    if buttons:
        result["buttons"] = buttons
    return result


def _yes_no_buttons_for_body(body: str) -> list[dict[str, str]]:
    normalized = _normalize_button_text(body)
    numbered_options = re.findall(r"(?im)^\s*(\d+)\s*[\.\-\)]\s*(.+?)\s*$", normalized)
    if numbered_options and {number for number, _ in numbered_options} != {"1", "2"}:
        return []
    has_sim = any(number == "1" and re.search(r"\bsim\b", title) for number, title in numbered_options)
    has_nao = any(number == "2" and re.search(r"\bnao\b", title) for number, title in numbered_options)
    has_sim = bool(has_sim) or "[sim]" in normalized
    has_nao = bool(has_nao) or "[nao]" in normalized
    if not (has_sim and has_nao):
        return []
    return [{"id": "1", "title": "Sim"}, {"id": "2", "title": "Nao"}]


def _normalize_button_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


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
