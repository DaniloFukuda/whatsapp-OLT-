from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WhatsAppErrorCategory(StrEnum):
    PAIR_RATE_LIMIT = "PAIR_RATE_LIMIT"
    GLOBAL_THROTTLE = "GLOBAL_THROTTLE"
    QUALITY_RESTRICTION = "QUALITY_RESTRICTION"
    AUTHENTICATION_OR_PERMISSION = "AUTHENTICATION_OR_PERMISSION"
    INVALID_INTERACTIVE_PAYLOAD = "INVALID_INTERACTIVE_PAYLOAD"
    TEMPORARY_PLATFORM_ERROR = "TEMPORARY_PLATFORM_ERROR"
    PERMANENT_UNKNOWN_ERROR = "PERMANENT_UNKNOWN_ERROR"


PAIR_RATE_LIMIT_CODES = {131056}
GLOBAL_THROTTLE_CODES = {4, 80007, 130429}
QUALITY_RESTRICTION_CODES = {131048}
AUTHENTICATION_OR_PERMISSION_CODES = {10, 190}
TEMPORARY_PLATFORM_CODES = {2}
AUTHENTICATION_OR_PERMISSION_STATUSES = {401, 403}
GLOBAL_THROTTLE_STATUSES = {429}
TEMPORARY_PLATFORM_STATUSES = {500, 502, 503, 504}
INTERACTIVE_ERROR_HINTS = (
    "interactive",
    "button",
    "buttons",
    "list",
    "row",
    "section",
    "parameter",
    "payload",
)


@dataclass(frozen=True)
class WhatsAppAPIError(Exception):
    http_status: int | None
    meta_code: int | None
    meta_type: str | None
    message: str | None
    details: str | None
    fbtrace_id: str | None
    category: WhatsAppErrorCategory
    retryable: bool
    fallback_allowed: bool
    recipient_scoped: bool

    def __post_init__(self) -> None:
        safe_message = (
            "WhatsApp API error "
            f"category={self.category.value} "
            f"http_status={self.http_status} "
            f"meta_code={self.meta_code} "
            f"retryable={self.retryable} "
            f"fallback_allowed={self.fallback_allowed} "
            f"recipient_scoped={self.recipient_scoped} "
            f"fbtrace_id={self.fbtrace_id}"
        )
        Exception.__init__(self, safe_message)

    def safe_response(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": _truncate(self.message),
            "type": self.meta_type,
            "code": self.meta_code,
            "category": self.category.value,
            "retryable": self.retryable,
            "fallback_allowed": self.fallback_allowed,
            "recipient_scoped": self.recipient_scoped,
        }
        if self.details is not None:
            error["error_data"] = {"details": _truncate(self.details)}
        if self.fbtrace_id is not None:
            error["fbtrace_id"] = self.fbtrace_id
        return {"error": error}

    def to_result(self, *, to: str, body: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "to": to,
            "body": body,
            "status": "error",
            "response": self.safe_response(),
            "error_class": self.category.value,
            "retryable": self.retryable,
            "fallback_allowed": self.fallback_allowed,
            "recipient_scoped": self.recipient_scoped,
            "meta_code": self.meta_code,
            "meta_type": self.meta_type,
            "fbtrace_id": self.fbtrace_id,
        }
        if self.http_status is not None:
            result["status_code"] = self.http_status
        return result


def build_meta_error(
    *,
    http_status: int | None,
    response_body: dict[str, Any] | None,
    is_interactive: bool,
) -> WhatsAppAPIError:
    error_body = (response_body or {}).get("error")
    error = error_body if isinstance(error_body, dict) else {}
    meta_code = _coerce_int(error.get("code"))
    meta_type = _coerce_str(error.get("type"))
    message = _coerce_str(error.get("message"))
    error_data = error.get("error_data")
    details = _coerce_str(error_data.get("details")) if isinstance(error_data, dict) else None
    fbtrace_id = _coerce_str(error.get("fbtrace_id"))
    return classify_meta_error(
        http_status=http_status,
        meta_code=meta_code,
        meta_type=meta_type,
        message=message,
        details=details,
        fbtrace_id=fbtrace_id,
        is_interactive=is_interactive,
    )


def build_transport_error(*, message: str | None, is_interactive: bool) -> WhatsAppAPIError:
    return classify_meta_error(
        http_status=None,
        meta_code=None,
        meta_type=None,
        message=message,
        details=message,
        fbtrace_id=None,
        is_interactive=is_interactive,
        force_temporary=True,
    )


def classify_meta_error(
    *,
    http_status: int | None,
    meta_code: int | None,
    meta_type: str | None,
    message: str | None,
    details: str | None,
    fbtrace_id: str | None,
    is_interactive: bool,
    force_temporary: bool = False,
) -> WhatsAppAPIError:
    if meta_code in PAIR_RATE_LIMIT_CODES:
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.PAIR_RATE_LIMIT,
            retryable=True,
            fallback_allowed=False,
            recipient_scoped=True,
        )
    if meta_code in GLOBAL_THROTTLE_CODES or http_status in GLOBAL_THROTTLE_STATUSES:
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.GLOBAL_THROTTLE,
            retryable=True,
            fallback_allowed=False,
            recipient_scoped=False,
        )
    if meta_code in QUALITY_RESTRICTION_CODES:
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.QUALITY_RESTRICTION,
            retryable=False,
            fallback_allowed=False,
            recipient_scoped=False,
        )
    if (
        meta_code in AUTHENTICATION_OR_PERMISSION_CODES
        or (meta_code is not None and 200 <= meta_code <= 299)
        or http_status in AUTHENTICATION_OR_PERMISSION_STATUSES
    ):
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.AUTHENTICATION_OR_PERMISSION,
            retryable=False,
            fallback_allowed=False,
            recipient_scoped=False,
        )
    if force_temporary or meta_code in TEMPORARY_PLATFORM_CODES or http_status in TEMPORARY_PLATFORM_STATUSES:
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.TEMPORARY_PLATFORM_ERROR,
            retryable=True,
            fallback_allowed=False,
            recipient_scoped=True,
        )
    if _is_invalid_interactive_payload(meta_code, message, details, is_interactive):
        return _make_error(
            http_status,
            meta_code,
            meta_type,
            message,
            details,
            fbtrace_id,
            WhatsAppErrorCategory.INVALID_INTERACTIVE_PAYLOAD,
            retryable=False,
            fallback_allowed=True,
            recipient_scoped=True,
        )
    return _make_error(
        http_status,
        meta_code,
        meta_type,
        message,
        details,
        fbtrace_id,
        WhatsAppErrorCategory.PERMANENT_UNKNOWN_ERROR,
        retryable=False,
        fallback_allowed=False,
        recipient_scoped=False,
    )


def _make_error(
    http_status: int | None,
    meta_code: int | None,
    meta_type: str | None,
    message: str | None,
    details: str | None,
    fbtrace_id: str | None,
    category: WhatsAppErrorCategory,
    *,
    retryable: bool,
    fallback_allowed: bool,
    recipient_scoped: bool,
) -> WhatsAppAPIError:
    return WhatsAppAPIError(
        http_status=http_status,
        meta_code=meta_code,
        meta_type=meta_type,
        message=message,
        details=details,
        fbtrace_id=fbtrace_id,
        category=category,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        recipient_scoped=recipient_scoped,
    )


def _is_invalid_interactive_payload(
    meta_code: int | None,
    message: str | None,
    details: str | None,
    is_interactive: bool,
) -> bool:
    if not is_interactive:
        return False
    text = f"{message or ''} {details or ''}".lower()
    has_interactive_hint = any(hint in text for hint in INTERACTIVE_ERROR_HINTS)
    return meta_code == 100 or has_interactive_hint


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _truncate(value: str | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 3].rstrip()}..."
