from sqlalchemy.orm import Session

from app.services.aluguer_service import AluguerService


class RecolhaAgent:
    def __init__(self, db: Session):
        self.aluguer_service = AluguerService(db)

    def marcar_recolha(self, aluguer_id: int) -> str:
        aluguer = self.aluguer_service.marcar_recolha(aluguer_id)
        return f"Aluguer #{aluguer.id} marcado como aguardando recolha."
