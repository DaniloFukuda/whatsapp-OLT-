from typing import Any

import httpx

from app.routes import webhook


class FakeMetaClient:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.external_attempts: list[str] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(webhook, "send_whatsapp_message", self.send_whatsapp_message)
        monkeypatch.setattr(httpx, "post", self._blocked_http_post)

    def send_whatsapp_message(self, to: str, body: str, **_kwargs) -> dict[str, Any]:
        record = {
            "to": to,
            "body": body,
            "status": "mocked",
            "message_id": f"wamid.fake.meta.{len(self.sent) + 1:03d}",
        }
        self.sent.append(record)
        return record

    def _blocked_http_post(self, *args, **_kwargs):
        target = str(args[0]) if args else "unknown"
        self.external_attempts.append(target)
        raise AssertionError(f"External HTTP call blocked during system smoke: {target}")

    def last_body(self) -> str:
        assert self.sent, "Fake Meta did not record any sent message."
        return str(self.sent[-1]["body"])
