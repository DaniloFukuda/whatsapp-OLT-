import re


def normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00351"):
        return "351" + digits[5:]
    if len(digits) == 9 and digits.startswith("9"):
        return "351" + digits
    return digits


def normalize_portugal_phone(value: str | None) -> str:
    return normalize_phone(value)


def whatsapp_link(value: str | None) -> str:
    phone = normalize_portugal_phone(value)
    return f"https://wa.me/{phone}" if phone else ""
