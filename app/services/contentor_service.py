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

    def excluir_com_auditoria(
        self,
        contentor_id: int | None = None,
        codigo: str | None = None,
        operador_telefone: str | None = None,
        justificativa: str | None = None,
    ) -> Contentor:
        justificativa_limpa = (justificativa or "").strip()
        if len(justificativa_limpa) < 10:
            raise ValueError("Justificativa de exclusao deve ter pelo menos 10 caracteres")

        contentor = self._buscar_para_exclusao(contentor_id=contentor_id, codigo=codigo)
        if not contentor:
            raise ValueError("Contentor nao encontrado")
        if contentor.is_deleted:
            raise ValueError("Contentor ja excluido")

        contentor.is_deleted = True
        contentor.excluido_por_operador = operador_telefone
        contentor.justificativa_exclusao = justificativa_limpa
        return self.repository.save(contentor)

    def obter_ou_criar_disponivel(self) -> Contentor:
        contentor = self.repository.first_available()
        if contentor:
            return contentor
        next_number = len(self.repository.list()) + 1
        return self.repository.create(codigo=f"C-{next_number:03d}")

    def _buscar_para_exclusao(self, contentor_id: int | None, codigo: str | None) -> Contentor | None:
        if contentor_id is not None:
            return self.repository.get_including_deleted(contentor_id)
        if codigo:
            return self.repository.get_by_codigo_including_deleted(codigo)
        raise ValueError("Informe o id ou codigo do contentor")
