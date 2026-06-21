from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedWhatsAppMessage:
    telefone: str
    tipo: str
    message_id: str | None = None
    texto: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    media_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    contact_phone: str | None = None
    raw: dict[str, Any] | None = None


def parse_whatsapp_payload(payload: dict[str, Any]) -> list[NormalizedWhatsAppMessage]:
    messages: list[NormalizedWhatsAppMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                normalized = _parse_message(message)
                if normalized:
                    messages.append(normalized)
    return messages


def _parse_message(message: dict[str, Any]) -> NormalizedWhatsAppMessage | None:
    telefone = message.get("from")
    tipo = message.get("type")
    if not telefone or not tipo:
        return None

    data: dict[str, Any] = {
        "telefone": telefone,
        "tipo": tipo,
        "message_id": message.get("id"),
        "raw": message,
    }

    if tipo == "text":
        data["texto"] = message.get("text", {}).get("body")
    elif tipo == "location":
        location = message.get("location", {})
        data["latitude"] = location.get("latitude")
        data["longitude"] = location.get("longitude")
    elif tipo in {"image", "document"}:
        media = message.get(tipo, {})
        data["media_id"] = media.get("id")
        data["mime_type"] = media.get("mime_type")
        data["filename"] = media.get("filename")
    elif tipo == "contacts":
        contacts = message.get("contacts", [])
        if contacts:
            phones = contacts[0].get("phones", [])
            if phones:
                data["contact_phone"] = phones[0].get("phone") or phones[0].get("wa_id")

    return NormalizedWhatsAppMessage(**data)
