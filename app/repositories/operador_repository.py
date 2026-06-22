from sqlalchemy.orm import Session

from app.models.operador import Operador


class OperadorRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_telefone(self, telefone_whatsapp: str) -> Operador | None:
        return self.db.get(Operador, telefone_whatsapp)

    def has_any(self) -> bool:
        return self.db.query(Operador.telefone_whatsapp).first() is not None
