from sqlalchemy.orm import Session

from app.models.aluguer import AluguerContentor, EventoAluguer
from app.models.contentor import Contentor, StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.services.seed_service import SeedService


class DemoResetService:
    def __init__(self, db: Session):
        self.db = db

    def reset(self) -> dict[str, int]:
        eventos = self.db.query(EventoAluguer).delete()
        alugueres = self.db.query(AluguerContentor).delete()
        conversas = self.db.query(ConversaWhatsApp).delete()

        SeedService(self.db).seed_contentores_iniciais()
        contentores = (
            self.db.query(Contentor)
            .filter(Contentor.codigo.in_([f"C{index:02d}" for index in range(1, 21)]))
            .all()
        )
        for contentor in contentores:
            contentor.status = StatusContentor.DISPONIVEL

        self.db.commit()
        return {
            "eventos_removidos": eventos,
            "alugueres_removidos": alugueres,
            "conversas_removidas": conversas,
            "contentores_disponiveis": len(contentores),
        }
