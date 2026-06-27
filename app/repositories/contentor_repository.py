from sqlalchemy import Integer, cast
from sqlalchemy.orm import Session

from app.models.contentor import Contentor, StatusContentor


class ContentorRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, codigo: str, status: StatusContentor = StatusContentor.DISPONIVEL) -> Contentor:
        contentor = Contentor(codigo=codigo, status=status)
        self.db.add(contentor)
        self.db.commit()
        self.db.refresh(contentor)
        return contentor

    def list(self) -> list[Contentor]:
        return self._active_query().order_by(cast(Contentor.codigo, Integer)).all()

    def get(self, contentor_id: int) -> Contentor | None:
        return self._active_query().filter(Contentor.id == contentor_id).first()

    def get_including_deleted(self, contentor_id: int) -> Contentor | None:
        return self.db.get(Contentor, contentor_id)

    def get_by_codigo(self, codigo: str) -> Contentor | None:
        return self._active_query().filter(Contentor.codigo == codigo).first()

    def get_by_codigo_including_deleted(self, codigo: str) -> Contentor | None:
        return self.db.query(Contentor).filter(Contentor.codigo == codigo).first()

    def first_available(self) -> Contentor | None:
        return (
            self._active_query()
            .filter(Contentor.status == StatusContentor.DISPONIVEL)
            .order_by(Contentor.id)
            .first()
        )

    def count(self) -> int:
        return self._active_query().count()

    def update_status(self, contentor: Contentor, status: StatusContentor) -> Contentor:
        contentor.status = status
        self.db.commit()
        self.db.refresh(contentor)
        return contentor

    def save(self, contentor: Contentor) -> Contentor:
        self.db.commit()
        self.db.refresh(contentor)
        return contentor

    def _active_query(self):
        return self.db.query(Contentor).filter(Contentor.is_deleted.is_(False))
