from dataclasses import dataclass
from itertools import count
from typing import Any


@dataclass
class SentPayload:
    payload: dict[str, Any]
    message_id: str | None
    description: str


class FakeWhatsAppUser:
    def __init__(self, phone: str, scenario_id: str):
        self.phone = phone
        self.scenario_id = scenario_id.lower().replace("-", "")
        self._counter = count(1)
        self.last_message_id: str | None = None

    def send_text(self, text: str, *, message_id: str | None = None) -> SentPayload:
        message_id = message_id or self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap({"id": message_id, "from": self.phone, "type": "text", "text": {"body": text}}),
            message_id,
            f"text:{text}",
        )

    def repeat_last_text(self, text: str) -> SentPayload:
        if not self.last_message_id:
            raise AssertionError("No message_id available to repeat.")
        return self.send_text(text, message_id=self.last_message_id)

    def send_button_reply(self, reply_id: str, title: str | None = None) -> SentPayload:
        message_id = self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap(
                {
                    "id": message_id,
                    "from": self.phone,
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": reply_id, "title": title or reply_id},
                    },
                }
            ),
            message_id,
            f"button:{reply_id}",
        )

    def send_list_reply(self, reply_id: str, title: str | None = None) -> SentPayload:
        message_id = self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap(
                {
                    "id": message_id,
                    "from": self.phone,
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {"id": reply_id, "title": title or reply_id},
                    },
                }
            ),
            message_id,
            f"list:{reply_id}",
        )

    def send_location(self, latitude: float, longitude: float, *, name: str = "Local SYS") -> SentPayload:
        message_id = self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap(
                {
                    "id": message_id,
                    "from": self.phone,
                    "type": "location",
                    "location": {"latitude": latitude, "longitude": longitude, "name": name},
                }
            ),
            message_id,
            f"location:{latitude},{longitude}",
        )

    def send_contact(self, name: str, phone: str) -> SentPayload:
        message_id = self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap(
                {
                    "id": message_id,
                    "from": self.phone,
                    "type": "contacts",
                    "contacts": [{"name": {"formatted_name": name}, "phones": [{"wa_id": phone}]}],
                }
            ),
            message_id,
            f"contact:{name}",
        )

    def send_image(self, media_id: str = "media-sys-fake") -> SentPayload:
        message_id = self._next_message_id()
        self.last_message_id = message_id
        return SentPayload(
            self._wrap({"id": message_id, "from": self.phone, "type": "image", "image": {"id": media_id}}),
            message_id,
            f"image:{media_id}",
        )

    def _next_message_id(self) -> str:
        return f"wamid.test.{self.scenario_id}.{next(self._counter):03d}"

    @staticmethod
    def _wrap(message: dict[str, Any]) -> dict[str, Any]:
        return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
