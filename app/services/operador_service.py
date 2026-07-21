from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.phone import normalize_phone
from app.models.operador import Operador, PerfilOperador
from app.repositories.operador_repository import OperadorRepository


@dataclass(frozen=True)
class AccessDecision:
    autorizado: bool
    perfil: PerfilOperador | None
    origem: str
    motivo_interno: str


class OperadorService:
    def __init__(self, db: Session):
        self.repository = OperadorRepository(db)

    def buscar_por_telefone(self, telefone: str | None) -> Operador | None:
        telefone_normalizado = normalize_phone(telefone)
        if not telefone_normalizado:
            return None
        return self.repository.get_by_telefone(telefone_normalizado)

    def verificar_autorizacao(self, telefone: str | None) -> bool:
        return self.decidir_acesso(telefone).autorizado

    def obter_perfil(self, telefone: str | None) -> PerfilOperador | None:
        return self.decidir_acesso(telefone).perfil

    def decidir_acesso(self, telefone: str | None) -> AccessDecision:
        telefone_normalizado = normalize_phone(telefone)
        if not self._telefone_valido(telefone_normalizado):
            return AccessDecision(False, None, "NENHUMA", "TELEFONE_INVALIDO")

        if self._tem_operadores():
            operador = self.repository.get_by_telefone(telefone_normalizado)
            if operador is None:
                return AccessDecision(False, None, "TABELA", "OPERADOR_INEXISTENTE")
            if not operador.ativo:
                return AccessDecision(False, None, "TABELA", "OPERADOR_INATIVO")
            if not isinstance(operador.perfil, PerfilOperador):
                return AccessDecision(False, None, "TABELA", "PERFIL_INVALIDO")
            return AccessDecision(True, operador.perfil, "TABELA", "OPERADOR_ATIVO")

        telefones_fallback = self._telefones_fallback()
        if not telefones_fallback:
            return AccessDecision(False, None, "NENHUMA", "FALLBACK_VAZIO")
        if telefone_normalizado in telefones_fallback:
            return AccessDecision(True, PerfilOperador.GESTOR, "FALLBACK", "FALLBACK_CORRESPONDENTE")
        return AccessDecision(False, None, "FALLBACK", "FALLBACK_NAO_CORRESPONDENTE")

    def _tem_operadores(self) -> bool:
        return self.repository.has_any()

    def _telefones_fallback(self) -> set[str]:
        settings = get_settings()
        raw_values = [settings.authorized_operator_phone]
        raw_values.extend(settings.authorized_operator_phones.split(","))
        return {
            normalized
            for value in raw_values
            if self._telefone_valido(normalized := normalize_phone(value))
        }

    def _telefone_valido(self, telefone_normalizado: str) -> bool:
        return 8 <= len(telefone_normalizado) <= 15
