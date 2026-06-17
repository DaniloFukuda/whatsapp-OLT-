from app.integrations.whatsapp.client import send_text_message


class NotificationService:
    def enviar_texto(self, telefone: str, mensagem: str, force_mock: bool = False) -> dict[str, str]:
        return send_text_message(telefone, mensagem, force_mock=force_mock)
