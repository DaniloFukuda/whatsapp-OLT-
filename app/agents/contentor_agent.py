from sqlalchemy.orm import Session

from app.services.contentor_service import ContentorService


class ContentorAgent:
    def __init__(self, db: Session):
        self.contentor_service = ContentorService(db)

    def listar_status(self) -> str:
        contentores = self.contentor_service.listar_contentores()
        if not contentores:
            return "Ainda nao existem contentores registrados."
        return "\n".join(f"{contentor.codigo}: {contentor.status.value}" for contentor in contentores)
