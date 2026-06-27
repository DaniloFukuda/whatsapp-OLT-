from sqlalchemy.orm import Session

from app.models.aluguer import AluguerContentor
from app.models.contentor import StatusContentor
from app.repositories.contentor_repository import ContentorRepository


class SeedService:
    def __init__(self, db: Session):
        self.db = db
        self.contentores = ContentorRepository(db)

    def seed_contentores_iniciais(self) -> list[str]:
        self._migrar_codigos_legados()
        codigos_criados: list[str] = []
        for index in range(1, 21):
            codigo = str(index)
            if self.contentores.get_by_codigo(codigo):
                continue
            self.contentores.create(codigo=codigo, status=StatusContentor.DISPONIVEL)
            codigos_criados.append(codigo)
        return codigos_criados

    def _migrar_codigos_legados(self) -> None:
        alterado = False
        for index in range(1, 100):
            codigo_antigo = f"C{index:02d}"
            contentor = self.contentores.get_by_codigo_including_deleted(codigo_antigo)
            if not contentor or self.contentores.get_by_codigo_including_deleted(str(index)):
                continue
            contentor.codigo = str(index)
            self.db.query(AluguerContentor).filter(
                AluguerContentor.numero_contentor == codigo_antigo
            ).update({"numero_contentor": str(index)}, synchronize_session=False)
            alterado = True
        if alterado:
            self.db.commit()
