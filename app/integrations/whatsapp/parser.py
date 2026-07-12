import re
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
    location_name: str | None = None
    location_address: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    contact_name: str | None = None
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
        data["location_name"] = _clean_text(location.get("name"))
        data["location_address"] = _clean_text(location.get("address"))
        data["texto"] = _location_text(
            data["latitude"],
            data["longitude"],
            data["location_name"],
            data["location_address"],
        )
    elif tipo in {"image", "document"}:
        media = message.get(tipo, {})
        data["media_id"] = media.get("id")
        data["mime_type"] = media.get("mime_type")
        data["filename"] = media.get("filename")
    elif tipo == "interactive":
        interactive = message.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        data["texto"] = _interactive_reply_text(reply)
    elif tipo in {"contact", "contacts", "vcard"}:
        contact = _first_contact(message)
        if contact:
            data["contact_name"] = _contact_name(contact)
            data["contact_phone"] = _contact_phone(contact)

    return NormalizedWhatsAppMessage(**data)


def _first_contact(message: dict[str, Any]) -> dict[str, Any] | None:
    contacts = message.get("contacts")
    if isinstance(contacts, list) and contacts:
        first = contacts[0]
        return first if isinstance(first, dict) else None
    contact = message.get("contact")
    if isinstance(contact, dict):
        return contact
    return message


def _contact_name(contact: dict[str, Any]) -> str | None:
    name = contact.get("name")
    if isinstance(name, dict):
        for key in ("formatted_name", "full_name", "display_name"):
            value = name.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("formatted_name", "full_name", "display_name", "name"):
        value = contact.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _contact_phone(contact: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    for key in ("wa_id", "phone"):
        value = contact.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    phones = contact.get("phones")
    if isinstance(phones, list):
        for phone in phones:
            if not isinstance(phone, dict):
                continue
            wa_id = phone.get("wa_id")
            number = phone.get("phone")
            if isinstance(wa_id, str) and wa_id.strip():
                candidates.append(wa_id)
            if isinstance(number, str) and number.strip():
                candidates.append(number)
    for candidate in candidates:
        if len(re.sub(r"\D", "", candidate)) >= 9:
            return candidate.strip()
    return candidates[0].strip() if candidates else None


def _interactive_reply_text(reply: dict[str, Any]) -> str | None:
    reply_id = reply.get("id")
    if isinstance(reply_id, str):
        reply_id = reply_id.strip()
        option_match = re.fullmatch(r"option_(\d+)", reply_id)
        if option_match:
            return option_match.group(1)
        if reply_id:
            return reply_id
    return reply.get("title")


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return re.sub(r"\s+", " ", value).strip()
    return None


def _location_text(
    latitude: Any,
    longitude: Any,
    name: str | None,
    address: str | None,
    max_chars: int = 300,
) -> str | None:
    coords = _location_coords(latitude, longitude)
    if not coords:
        return None
    lat, lon = coords
    link = f"https://www.google.com/maps?q={lat},{lon}"
    if name and address:
        description = f"{name} - {address}"
    else:
        description = address or name or "Localização enviada pelo WhatsApp"
    return _location_text_with_limit(description, link, max_chars)


def _location_coords(latitude: Any, longitude: Any) -> tuple[str, str] | None:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return f"{lat:g}", f"{lon:g}"


def _location_text_with_limit(description: str, link: str, max_chars: int) -> str:
    separator = " - "
    description = _clean_text(description) or "Localização enviada pelo WhatsApp"
    available = max_chars - len(separator) - len(link)
    if available <= 0:
        return link[-max_chars:]
    if len(description) > available:
        description = description[:available].rstrip()
    return f"{description}{separator}{link}"
