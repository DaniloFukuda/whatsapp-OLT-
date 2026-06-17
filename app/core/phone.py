import re


def normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)
