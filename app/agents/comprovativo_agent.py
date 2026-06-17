from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage


class ComprovativoAgent:
    def extract_media_path(self, message: NormalizedWhatsAppMessage) -> str | None:
        if not message.media_id:
            return None
        return f"whatsapp://media/{message.media_id}"
