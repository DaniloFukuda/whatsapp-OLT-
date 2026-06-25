import re
from urllib.parse import parse_qs, unquote, urlparse

from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage


class LocalizacaoAgent:
    COORD_PATTERN = re.compile(r"(-?\d{1,2}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)")
    GOOGLE_DATA_PATTERN = re.compile(r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)")

    def extract(self, message: NormalizedWhatsAppMessage) -> tuple[float | None, float | None]:
        if message.latitude is not None and message.longitude is not None:
            return float(message.latitude), float(message.longitude)

        text = message.texto or ""
        return self.extract_from_text(text)

    def extract_from_text(self, text: str) -> tuple[float | None, float | None]:
        if not text:
            return None, None

        candidates = [text, unquote(text)]
        parsed = urlparse(text)
        if parsed.query:
            query = parse_qs(parsed.query)
            for key in ("q", "query", "ll"):
                for value in query.get(key, []):
                    candidates.append(value)
                    candidates.append(unquote(value))

        for candidate in candidates:
            data_match = self.GOOGLE_DATA_PATTERN.search(candidate)
            if data_match:
                latitude = float(data_match.group(1))
                longitude = float(data_match.group(2))
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    return latitude, longitude

            match = self.COORD_PATTERN.search(candidate)
            if not match:
                continue
            latitude = float(match.group(1))
            longitude = float(match.group(2))
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return latitude, longitude

        return None, None
