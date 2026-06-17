from sqlalchemy.orm import Session

from app.models.contentor import Contentor, StatusContentor
from app.repositories.contentor_repository import ContentorRepository


class ContentorService:
    def __init__(self, db: Session):
        self.repository = ContentorRepository(db)

    def criar_contentor(self, codigo: str) -> Contentor:
        existing = self.repository.get_by_codigo(codigo)
        if existing:
            return existing
        return self.repository.create(codigo=codigo)

    def listar_contentores(self) -> list[Contentor]:
        return self.repository.list()

    def mudar_status(self, contentor_id: int, status: StatusContentor) -> Contentor:
        contentor = self.repository.get(contentor_id)
        if not contentor:
            raise ValueError("Contentor nao encontrado")
        return self.repository.update_status(contentor, status)

    def obter_ou_criar_disponivel(self) -> Contentor:
        contentor = self.repository.first_available()
        if contentor:
            return contentor
        next_number = len(self.repository.list()) + 1
        return self.repository.create(codigo=f"C-{next_number:03d}")
