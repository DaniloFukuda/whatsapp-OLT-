from enum import StrEnum

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PerfilOperador(StrEnum):
    FUNCIONARIO = "FUNCIONARIO"
    GESTOR = "GESTOR"


class Operador(Base):
    __tablename__ = "operadores"

    telefone_whatsapp: Mapped[str] = mapped_column(String(50), primary_key=True)
    nome_operador: Mapped[str] = mapped_column(String(255), nullable=False)
    perfil: Mapped[PerfilOperador] = mapped_column(Enum(PerfilOperador), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
