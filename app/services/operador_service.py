from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.phone import normalize_phone
from app.models.operador import Operador, PerfilOperador


class OperadorService:
    def __init__(self, db: Session):
        self.db = db

    def buscar_por_telefone(self, telefone: str | None) -> Operador | None:
        telefone_normalizado = normalize_phone(telefone)
        if not telefone_normalizado:
            return None
        return self.db.get(Operador, telefone_normalizado)

    def verificar_autorizacao(self, telefone: str | None) -> bool:
        telefone_normalizado = normalize_phone(telefone)
        if not telefone_normalizado:
            return False

        if self._tem_operadores():
            operador = self.buscar_por_telefone(telefone_normalizado)
            return bool(operador and operador.ativo)

        telefones_fallback = self._telefones_fallback()
        return not telefones_fallback or telefone_normalizado in telefones_fallback

    def obter_perfil(self, telefone: str | None) -> PerfilOperador | None:
        telefone_normalizado = normalize_phone(telefone)
        if not telefone_normalizado:
            return None

        if self._tem_operadores():
            operador = self.buscar_por_telefone(telefone_normalizado)
            if operador and operador.ativo:
                return operador.perfil
            return None

        telefones_fallback = self._telefones_fallback()
        if not telefones_fallback or telefone_normalizado in telefones_fallback:
            return PerfilOperador.GESTOR
        return None

    def _tem_operadores(self) -> bool:
        return self.db.query(Operador).first() is not None

    def _telefones_fallback(self) -> set[str]:
        settings = get_settings()
        raw_values = [settings.authorized_operator_phone]
        raw_values.extend(settings.authorized_operator_phones.split(","))
        return {normalized for value in raw_values if (normalized := normalize_phone(value))}
