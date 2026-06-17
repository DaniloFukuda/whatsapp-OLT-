from datetime import datetime

from sqlalchemy.orm import Session

from app.models.aluguer import AluguerContentor, EventoAluguer, StatusAluguer


class AluguerRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, **data) -> AluguerContentor:
        aluguer = AluguerContentor(**data)
        self.db.add(aluguer)
        self.db.commit()
        self.db.refresh(aluguer)
        return aluguer

    def get(self, aluguer_id: int) -> AluguerContentor | None:
        return self.db.get(AluguerContentor, aluguer_id)

    def list_by_due_range(self, start: datetime, end: datetime) -> list[AluguerContentor]:
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.data_vencimento >= start)
            .filter(AluguerContentor.data_vencimento < end)
            .filter(AluguerContentor.status.in_([StatusAluguer.ATIVO, StatusAluguer.VENCENDO, StatusAluguer.RENOVADO]))
            .order_by(AluguerContentor.data_vencimento)
            .all()
        )

    def list_overdue(self, now: datetime) -> list[AluguerContentor]:
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.data_vencimento < now)
            .filter(AluguerContentor.status.in_([StatusAluguer.ATIVO, StatusAluguer.VENCENDO, StatusAluguer.RENOVADO]))
            .order_by(AluguerContentor.data_vencimento)
            .all()
        )

    def add_event(self, aluguer_id: int, tipo: str, descricao: str) -> EventoAluguer:
        evento = EventoAluguer(aluguer_id=aluguer_id, tipo=tipo, descricao=descricao)
        self.db.add(evento)
        self.db.commit()
        self.db.refresh(evento)
        return evento

    def save(self, aluguer: AluguerContentor) -> AluguerContentor:
        self.db.commit()
        self.db.refresh(aluguer)
        return aluguer
