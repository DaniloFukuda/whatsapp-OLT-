from sqlalchemy.orm import Session

from app.services.reminder_service import ReminderService


class LembreteAgent:
    def __init__(self, db: Session):
        self.reminder_service = ReminderService(db)

    def montar_lembretes(self) -> list[str]:
        return self.reminder_service.mensagens_vencendo_amanha()
