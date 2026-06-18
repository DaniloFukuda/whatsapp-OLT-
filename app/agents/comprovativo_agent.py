from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage


class ComprovativoAgent:
    def extract_media_path(self, message: NormalizedWhatsAppMessage) -> str | None:
        if message.tipo != "image":
            return None
        if message.mime_type and not message.mime_type.startswith("image/"):
            return None
        if not message.media_id:
            return None
        return f"whatsapp://media/{message.media_id}"
