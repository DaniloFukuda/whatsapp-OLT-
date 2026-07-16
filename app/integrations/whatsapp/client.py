import hashlib
import re
import unicodedata
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.whatsapp.errors import build_meta_error, build_transport_error

logger = get_logger(__name__)

MAX_BUTTON_OPTIONS = 3
MAX_LIST_OPTIONS = 5
MAX_CORRIGIR_LIST_OPTIONS = 12
MAX_ENTREGA_LIST_OPTIONS = 10
MAX_BUTTON_TITLE_CHARS = 20
MAX_LIST_TITLE_CHARS = 24
MAX_LIST_DESCRIPTION_CHARS = 72
LIST_BUTTON_TITLE = "Escolher opção"
LIST_SECTION_TITLE = "Opções"
OPTION_ID_PREFIX = "option_"


def send_whatsapp_message(to: str, body: str, force_mock: bool = False) -> dict[str, Any]:
    if _is_main_menu(body):
        return send_text_message(to, body, force_mock=force_mock)

    options = _options_for_body(body)
    if _is_entrega_pedido_body(body) and 0 < len(options) <= MAX_ENTREGA_LIST_OPTIONS:
        if any(len(_clean_option_title(option["title"])) > MAX_LIST_TITLE_CHARS for option in options):
            return send_text_message(to, _entrega_text_fallback(body), force_mock=force_mock)
        result = send_list_message(
            to,
            _entrega_body_without_options(body),
            _list_rows_from_options(options)[:MAX_ENTREGA_LIST_OPTIONS],
            force_mock=force_mock,
        )
        return _fallback_to_text_if_needed(to, body, result, force_mock, interactive_type="list")

    if 0 < len(options) <= MAX_BUTTON_OPTIONS:
        result = send_button_message(
            to,
            _body_without_numbered_options(body),
            _buttons_from_options(options),
            force_mock=force_mock,
        )
        return _fallback_to_text_if_needed(to, body, result, force_mock, interactive_type="button")

    if _is_corrigir_pedido_body(body) and MAX_BUTTON_OPTIONS < len(options) <= MAX_CORRIGIR_LIST_OPTIONS:
        result = send_list_message(
            to,
            _body_without_numbered_options(body),
            _list_rows_from_options(options)[:MAX_CORRIGIR_LIST_OPTIONS],
            force_mock=force_mock,
        )
        return _fallback_to_text_if_needed(to, body, result, force_mock, interactive_type="list")

    if MAX_BUTTON_OPTIONS < len(options) <= MAX_LIST_OPTIONS:
        result = send_list_message(
            to,
            _body_without_numbered_options(body),
            _list_rows_from_options(options),
            force_mock=force_mock,
        )
        return _fallback_to_text_if_needed(to, body, result, force_mock, interactive_type="list")

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
                    for button in buttons[:MAX_BUTTON_OPTIONS]
                ]
            },
        },
    }
    return _send_payload(
        to=to,
        body=body,
        payload=payload,
        force_mock=force_mock,
        buttons=buttons[:MAX_BUTTON_OPTIONS],
        include_type=True,
    )


def send_list_message(
    to: str,
    body: str,
    rows: list[dict[str, str]],
    force_mock: bool = False,
) -> dict[str, Any]:
    max_rows = MAX_CORRIGIR_LIST_OPTIONS if len(rows) > MAX_LIST_OPTIONS else MAX_LIST_OPTIONS
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": LIST_BUTTON_TITLE,
                "sections": [{"title": LIST_SECTION_TITLE, "rows": rows[:max_rows]}],
            },
        },
    }
    return _send_payload(
        to=to,
        body=body,
        payload=payload,
        force_mock=force_mock,
        list_rows=rows[:max_rows],
        include_type=True,
    )


def _send_payload(
    to: str,
    body: str,
    payload: dict[str, Any],
    force_mock: bool = False,
    buttons: list[dict[str, str]] | None = None,
    list_rows: list[dict[str, str]] | None = None,
    include_type: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    if force_mock or _should_mock(settings):
        logger.info("Mock WhatsApp send to=%s body=%s", to, body)
        result = {"to": to, "body": body, "status": "mocked"}
        if include_type:
            result["type"] = payload["type"]
            if payload["type"] == "interactive":
                result["interactive_type"] = payload["interactive"]["type"]
        if buttons:
            result["buttons"] = buttons
        if list_rows:
            result["list_rows"] = list_rows
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
        api_error = build_transport_error(message=safe_error, is_interactive=_is_interactive_payload(payload))
        _log_api_error(api_error, to, "request_failed")
        result = api_error.to_result(to=to, body=body)
        result["error"] = safe_error
        return result

    response_body = _safe_json(response)
    if response.status_code >= 400:
        safe_response_body = _redact_token(response_body, settings.whatsapp_access_token)
        api_error = build_meta_error(
            http_status=response.status_code,
            response_body=safe_response_body,
            is_interactive=_is_interactive_payload(payload),
        )
        _log_api_error(api_error, to, "http_error")
        return api_error.to_result(to=to, body=body)

    message_id = _extract_message_id(response_body)
    logger.info("WhatsApp API message sent to=%s message_id=%s", to, message_id)
    result = {"to": to, "body": body, "status": "sent", "message_id": message_id}
    if include_type:
        result["type"] = payload["type"]
        if payload["type"] == "interactive":
            result["interactive_type"] = payload["interactive"]["type"]
    if buttons:
        result["buttons"] = buttons
    if list_rows:
        result["list_rows"] = list_rows
    return result


def _fallback_to_text_if_needed(
    to: str,
    original_body: str,
    result: dict[str, Any],
    force_mock: bool,
    interactive_type: str,
) -> dict[str, Any]:
    if result.get("status") != "error":
        return result

    if not result.get("fallback_allowed"):
        logger.warning(
            "WhatsApp interactive fallback suppressed type=%s recipient_hash=%s "
            "category=%s retryable=%s recipient_scoped=%s",
            interactive_type,
            _recipient_hash(to),
            result.get("error_class"),
            result.get("retryable"),
            result.get("recipient_scoped"),
        )
        return result

    logger.warning(
        "WhatsApp interactive %s failed recipient_hash=%s category=%s; falling back to numbered text",
        interactive_type,
        _recipient_hash(to),
        result.get("error_class"),
    )
    fallback_result = send_text_message(to, original_body, force_mock=force_mock)
    fallback_result["fallback_from"] = interactive_type
    fallback_result["interactive_error"] = result.get("response") or result.get("error")
    return fallback_result


def _buttons_for_body(body: str) -> list[dict[str, str]]:
    options = _options_for_body(body)
    if 0 < len(options) <= MAX_BUTTON_OPTIONS:
        return _buttons_from_options(options)
    return []


def _options_for_body(body: str) -> list[dict[str, str]]:
    normalized = _normalize_button_text(body)
    if (
        ("mao de obra" in normalized or "pessoal para carregamento" in normalized)
        and "1. sim" in normalized
        and "2. nao" in normalized
    ):
        return [
            {"id": "pedido_mao_obra_sim", "title": "✅ Sim"},
            {"id": "pedido_mao_obra_nao", "title": "❌ Não"},
        ]
    if (
        ("residuo do contentor" in normalized or "residuo da carrinha" in normalized)
        and "entulho limpo" in normalized
        and "entulho misto" in normalized
    ):
        return [
            {"id": "pedido_residuo_limpo", "title": "Entulho Limpo"},
            {"id": "pedido_residuo_misto", "title": "Entulho Misto"},
        ]
    if _is_corrigir_pedido_body(body):
        return _corrigir_pedido_options(body)
    if _is_entrega_pedido_body(body):
        return _entrega_pedido_options(body)
    if (
        "deseja informar algum ponto de referencia para a entrega" in normalized
        and "1. sim" in normalized
        and "2. nao" in normalized
    ):
        return [
            {"id": "entrega_referencia:sim", "title": "Sim"},
            {"id": "entrega_referencia:nao", "title": "Não"},
        ]
    if (
        "qual residuo caiu no chao" in normalized
        and "entulho limpo" in normalized
        and "entulho misto" in normalized
    ):
        return [
            {"id": "despejo_residuo:limpo", "title": "Entulho Limpo"},
            {"id": "despejo_residuo:misto", "title": "Entulho Misto"},
        ]
    if (
        "o entulho do contentor" in normalized
        and "sim, tudo certo" in normalized
        and "misturado/errado" in normalized
    ):
        return [
            {"id": "despejo_conformidade:sim", "title": "✅ Sim"},
            {"id": "despejo_conformidade:nao", "title": "🚨 Não"},
        ]

    numbered_options = _numbered_options_for_body(body)
    if 0 < len(numbered_options) <= MAX_LIST_OPTIONS:
        options = _options_from_numbered_options(numbered_options)
        if options:
            return options

    if "[sim]" in normalized and "[nao]" in normalized:
        return [{"id": "option_1", "title": "Sim"}, {"id": "option_2", "title": "Nao"}]

    return []


def _is_corrigir_pedido_body(body: str) -> bool:
    return "qual campo deseja corrigir" in _normalize_button_text(body)


def _is_entrega_pedido_body(body: str) -> bool:
    return "selecione o cliente para iniciar a entrega" in _normalize_button_text(body)


def _corrigir_pedido_options(body: str) -> list[dict[str, str]]:
    id_by_title = {
        "quantidade de carrinhas": "quantidade",
        "quantidade de contentores": "quantidade",
        "nome do cliente": "nome_cliente",
        "telefone": "telefone",
        "dia da entrega": "data_entrega",
        "hora da entrega": "hora_entrega",
        "tipo de residuo": "tipo_residuo",
        "pessoal para carregamento": "mao_de_obra",
        "valor total": "valor_total",
        "status do pagamento": "status_pagamento",
        "forma de pagamento": "forma_pagamento",
        "endereco": "endereco",
        "ponto de referencia": "ponto_referencia",
    }
    options = []
    for _number, title in _numbered_options_for_body(body):
        normalized_title = _normalize_button_text(title)
        field = id_by_title.get(normalized_title)
        if field:
            options.append({"id": f"corrigir_pedido:{field}", "title": _clean_option_title(title)})
    return options


def _entrega_pedido_options(body: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"(?ims)^\s*(\d+)\.\s*(?P<title>.+?)\s*\n"
        r"\s*Quantidade:\s*(?P<quantity>.+?)\s*\n"
        r"\s*Tipo:\s*(?P<kind>.+?)\s*\n"
        r"\s*ID:\s*(?P<id>entrega_pedido:\d+)\s*$"
    )
    options = []
    for match in pattern.finditer(body or ""):
        title = _clean_option_title(match.group("title"))
        quantity = _clean_option_title(match.group("quantity"))
        kind = _clean_option_title(match.group("kind"))
        row_id = match.group("id")
        if title and quantity and row_id:
            options.append(
                {
                    "id": row_id,
                    "title": title,
                    "description": f"Quantidade: {quantity}",
                    "preserve_title": "1",
                }
            )
    return options


def _numbered_options_for_body(body: str) -> list[tuple[str, str]]:
    return re.findall(r"(?im)^\s*(\d+)\s*[\.\-\)]\s*(.+?)\s*$", body or "")


def _options_from_numbered_options(numbered_options: list[tuple[str, str]]) -> list[dict[str, str]]:
    expected_numbers = [str(index) for index in range(1, len(numbered_options) + 1)]
    numbers = [number for number, _ in numbered_options]
    if numbers != expected_numbers:
        return []

    options = []
    for number, title in numbered_options:
        option_title = _clean_option_title(title)
        if not option_title:
            return []
        options.append({"id": f"{OPTION_ID_PREFIX}{number}", "title": option_title})
    return options


def _buttons_from_numbered_options(numbered_options: list[tuple[str, str]]) -> list[dict[str, str]]:
    options = _options_from_numbered_options(numbered_options)
    if not options:
        return []
    return _buttons_from_options(options)


def _buttons_from_options(options: list[dict[str, str]]) -> list[dict[str, str]]:
    buttons = []
    for option in options:
        button_title = _format_button_title(option["title"])
        if not button_title:
            return []
        buttons.append({"id": option["id"], "title": button_title})
    return buttons


def _list_rows_from_options(options: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for option in options:
        if option.get("preserve_title"):
            row_title = _clean_option_title(option["title"])
        else:
            row_title = _truncate_title(option["title"], MAX_LIST_TITLE_CHARS)
        if not row_title:
            return []
        row = {"id": option["id"], "title": row_title}
        description = option.get("description")
        if description:
            row["description"] = _truncate_title(description, MAX_LIST_DESCRIPTION_CHARS)
        rows.append(row)
    return rows


def _format_button_title(title: str) -> str | None:
    button_title = _clean_option_title(title)
    if not button_title:
        return None
    return _truncate_title(button_title, MAX_BUTTON_TITLE_CHARS)


def _clean_option_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


def _truncate_title(title: str, max_chars: int) -> str:
    clean_title = _clean_option_title(title)
    if len(clean_title) <= max_chars:
        return clean_title
    if max_chars <= 3:
        return clean_title[:max_chars]
    return f"{clean_title[: max_chars - 3].rstrip()}..."


def _body_without_numbered_options(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if re.match(r"^\s*\d+\s*[\.\-\)]\s*.+?\s*$", line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or body


def _entrega_body_without_options(body: str) -> str:
    return (body or "").split("\n\n", 1)[0].strip() or body


def _entrega_text_fallback(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        if re.match(r"^\s*ID:\s*entrega_pedido:\d+\s*$", line):
            continue
        if re.match(r"^\s*Tipo:\s*.+$", line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or _entrega_body_without_options(body)


def _normalize_button_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _is_main_menu(body: str) -> bool:
    return "Menu principal - OLT Gestão de Resíduos & Demolições" in (body or "")


def _should_mock(settings) -> bool:
    return (
        settings.env.lower() == "test"
        or not settings.whatsapp_access_token.strip()
        or not settings.whatsapp_phone_number_id.strip()
    )


def _is_interactive_payload(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "interactive"


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


def _log_api_error(api_error, to: str, event: str) -> None:
    logger.error(
        "WhatsApp API %s recipient_hash=%s status_code=%s meta_code=%s "
        "category=%s retryable=%s fallback_allowed=%s recipient_scoped=%s fbtrace_id=%s",
        event,
        _recipient_hash(to),
        api_error.http_status,
        api_error.meta_code,
        api_error.category.value,
        api_error.retryable,
        api_error.fallback_allowed,
        api_error.recipient_scoped,
        api_error.fbtrace_id,
    )


def _recipient_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:10]


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
