from sqlalchemy.orm import Session

from app.services.aluguer_service import AluguerService


class RenovacaoAgent:
    def __init__(self, db: Session):
        self.aluguer_service = AluguerService(db)

    def renovar(self, aluguer_id: int) -> str:
        aluguer = self.aluguer_service.renovar_por_mais_5_dias(aluguer_id)
        return f"Aluguer #{aluguer.id} renovado ate {aluguer.data_vencimento:%d/%m/%Y}."
