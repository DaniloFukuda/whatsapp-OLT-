from sqlalchemy.orm import Session

from app.models.contentor import StatusContentor
from app.repositories.contentor_repository import ContentorRepository


class SeedService:
    def __init__(self, db: Session):
        self.contentores = ContentorRepository(db)

    def seed_contentores_iniciais(self) -> list[str]:
        codigos_criados: list[str] = []
        for index in range(1, 21):
            codigo = f"C{index:02d}"
            if self.contentores.get_by_codigo(codigo):
                continue
            self.contentores.create(codigo=codigo, status=StatusContentor.DISPONIVEL)
            codigos_criados.append(codigo)
        return codigos_criados
