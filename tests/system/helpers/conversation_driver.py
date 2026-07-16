from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from app.models.conversa import ConversaWhatsApp


class ConversationDriver:
    def __init__(self, app, SessionLocal, user):
        self.client = TestClient(app)
        self.SessionLocal = SessionLocal
        self.user = user
        self.history: list[dict[str, Any]] = []

    def send_text(self, text: str):
        return self._post(self.user.send_text(text))

    def send_button_reply(self, reply_id: str, title: str | None = None):
        return self._post(self.user.send_button_reply(reply_id, title))

    def send_list_reply(self, reply_id: str, title: str | None = None):
        return self._post(self.user.send_list_reply(reply_id, title))

    def send_location(self, latitude: float, longitude: float):
        return self._post(self.user.send_location(latitude, longitude))

    def send_contact(self, name: str, phone: str):
        return self._post(self.user.send_contact(name, phone))

    def send_image(self, media_id: str = "media-sys-fake"):
        return self._post(self.user.send_image(media_id))

    def repeat_last_message_id(self, text: str):
        return self._post(self.user.repeat_last_text(text))

    def last_response(self) -> dict[str, Any]:
        assert self.history, "No response has been recorded."
        return self.history[-1]["response"]

    def last_sent_body(self) -> str:
        messages = self.last_response().get("messages") or []
        assert messages, "Last webhook response did not include sent messages."
        return str(messages[-1]["body"])

    def current_state(self) -> str | None:
        conversa = self.conversation()
        return conversa.estado_atual if conversa else None

    def context(self) -> dict:
        conversa = self.conversation()
        return dict(conversa.contexto_json or {}) if conversa else {}

    def conversation(self) -> ConversaWhatsApp | None:
        with self.database_session() as session:
            return session.query(ConversaWhatsApp).filter_by(telefone=self.user.phone).first()

    @contextmanager
    def database_session(self):
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def _post(self, sent_payload):
        response = self.client.post("/webhook/whatsapp", json=sent_payload.payload)
        parsed = response.json()
        entry = {
            "message_id": sent_payload.message_id,
            "description": sent_payload.description,
            "status_code": response.status_code,
            "response": parsed,
            "state": self.current_state(),
            "context": self.context(),
        }
        self.history.append(entry)
        return response
