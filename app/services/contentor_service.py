from sqlalchemy.orm import Session

from app.models.contentor import Contentor, StatusContentor
from app.repositories.contentor_repository import ContentorRepository


class ContentorService:
    CAMPOS_ALTERAVEIS = {"status"}

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

    def alterar_com_auditoria(
        self,
        contentor_id: int | None = None,
        codigo: str | None = None,
        operador_telefone: str | None = None,
        campo: str | None = None,
        novo_valor: str | StatusContentor | None = None,
    ) -> Contentor:
        campo_normalizado = (campo or "").strip().lower()
        if campo_normalizado not in self.CAMPOS_ALTERAVEIS:
            raise ValueError("Campo nao permitido para alteracao")

        contentor = self._buscar_contentor_incluindo_excluidos(contentor_id=contentor_id, codigo=codigo)
        if not contentor:
            raise ValueError("Contentor nao encontrado")
        if contentor.is_deleted:
            raise ValueError("Contentor excluido nao pode ser alterado")

        if campo_normalizado == "status":
            contentor.status = self._parse_status(novo_valor)
        contentor.alterado_por_operador = operador_telefone
        return self.repository.save(contentor)

    def obter_ou_criar_disponivel(self) -> Contentor:
        contentor = self.repository.first_available()
        if contentor:
            return contentor
        next_number = len(self.repository.list()) + 1
        return self.repository.create(codigo=f"C-{next_number:03d}")

    def _buscar_para_exclusao(self, contentor_id: int | None, codigo: str | None) -> Contentor | None:
        return self._buscar_contentor_incluindo_excluidos(contentor_id=contentor_id, codigo=codigo)

    def _buscar_contentor_incluindo_excluidos(self, contentor_id: int | None, codigo: str | None) -> Contentor | None:
        if contentor_id is not None:
            return self.repository.get_including_deleted(contentor_id)
        if codigo:
            return self.repository.get_by_codigo_including_deleted(codigo)
        raise ValueError("Informe o id ou codigo do contentor")

    def _parse_status(self, value: str | StatusContentor | None) -> StatusContentor:
        if isinstance(value, StatusContentor):
            return value
        normalized = (value or "").strip().lower()
        try:
            return StatusContentor(normalized)
        except ValueError as exc:
            raise ValueError("Status de contentor invalido") from exc
