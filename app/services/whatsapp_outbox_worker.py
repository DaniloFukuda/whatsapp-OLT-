from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from app.core.logging import get_logger
from app.core.time import utcnow
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.errors import WhatsAppErrorCategory
from app.services.whatsapp_outbox_service import OutboxConfig, OutboxLease, WhatsAppOutboxService


logger = get_logger(__name__)

Sender = Callable[[str, str], dict[str, Any]]


class WhatsAppOutboxWorker:
    def __init__(
        self,
        service: WhatsAppOutboxService,
        *,
        sender: Sender | None = None,
        config: OutboxConfig | None = None,
        now_provider=utcnow,
    ):
        self._service = service
        self._sender = sender or (lambda recipient, body: send_whatsapp_message(recipient, body))
        self._config = config or OutboxConfig.from_settings()
        self._now_provider = now_provider

    def process_available(self, *, max_items: int = 10) -> int:
        processed = 0
        self._service.release_expired_leases()
        for _ in range(max_items):
            lease = self._service.claim_next()
            if lease is None:
                break
            self._process_lease(lease)
            processed += 1
        return processed

    def _process_lease(self, lease: OutboxLease) -> None:
        try:
            result = self._send(lease)
        except Exception as exc:
            logger.exception(
                "WhatsApp outbox sender raised item_id=%s recipient_key_prefix=%s",
                lease.item_id,
                lease.recipient_key[:12],
            )
            result = {
                "status": "error",
                "error_class": WhatsAppErrorCategory.TEMPORARY_PLATFORM_ERROR.value,
                "retryable": True,
                "fallback_allowed": False,
                "recipient_scoped": True,
                "error": str(exc),
            }

        if result.get("status") in {"sent", "mocked"}:
            self._service.mark_sent(lease.item_id, lease.lease_owner)
            return

        if result.get("retryable"):
            category = result.get("error_class")
            delay = self._retry_delay(lease)
            if category == WhatsAppErrorCategory.GLOBAL_THROTTLE.value:
                pause_until = self._now_provider() + delay
                self._service.apply_global_pause(pause_until)
            self._service.reschedule(
                lease.item_id,
                lease.lease_owner,
                delay=delay,
                error_result=result,
            )
            logger.warning(
                "WhatsApp outbox rescheduled item_id=%s recipient_key_prefix=%s category=%s attempts_next=%s",
                lease.item_id,
                lease.recipient_key[:12],
                category,
                lease.attempts + 1,
            )
            return

        self._service.mark_failed(lease.item_id, lease.lease_owner, error_result=result)
        logger.warning(
            "WhatsApp outbox failed item_id=%s recipient_key_prefix=%s category=%s",
            lease.item_id,
            lease.recipient_key[:12],
            result.get("error_class"),
        )

    def _send(self, lease: OutboxLease) -> dict[str, Any]:
        if lease.message_type != "body":
            return {
                "status": "error",
                "error_class": WhatsAppErrorCategory.PERMANENT_UNKNOWN_ERROR.value,
                "retryable": False,
                "fallback_allowed": False,
                "recipient_scoped": True,
                "error": "tipo de mensagem da outbox nao suportado",
            }
        body = str(lease.payload.get("body") or "")
        return self._sender(lease.recipient, body)

    def _retry_delay(self, lease: OutboxLease) -> timedelta:
        exponent = max(0, lease.attempts)
        seconds = self._config.backoff_base.total_seconds() * (2**exponent)
        capped = min(seconds, self._config.backoff_max.total_seconds())
        return max(timedelta(seconds=capped), self._config.min_recipient_interval)
