from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage


class LocalizacaoAgent:
    def extract(self, message: NormalizedWhatsAppMessage) -> tuple[float | None, float | None]:
        if message.tipo != "location":
            return None, None
        return message.latitude, message.longitude
