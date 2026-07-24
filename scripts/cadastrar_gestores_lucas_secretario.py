from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sys
from typing import Iterable

from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.db import SessionLocal
from app.core.phone import normalize_phone
from app.models.operador import Operador, PerfilOperador
from app.repositories.operador_repository import OperadorRepository


@dataclass(frozen=True)
class GestorCadastro:
    telefone: str
    nome: str


class EstadoCadastro(StrEnum):
    CRIADO = "CRIADO"
    ATUALIZADO = "ATUALIZADO"
    JA_ESTAVA_CORRETO = "JÁ ESTAVA CORRETO"


@dataclass(frozen=True)
class ResultadoCadastro:
    telefone: str
    nome: str
    estado: EstadoCadastro


GESTORES = (
    GestorCadastro(telefone="351937522741", nome="Lucas Santos"),
    GestorCadastro(telefone="351933372221", nome="Secretário OLT"),
)


def cadastrar_gestores(
    db: Session,
    gestores: Iterable[GestorCadastro] = GESTORES,
) -> list[ResultadoCadastro]:
    repository = OperadorRepository(db)
    resultados: list[ResultadoCadastro] = []

    with db.begin():
        for cadastro in gestores:
            telefone = normalize_phone(cadastro.telefone)
            if not telefone:
                raise ValueError("Telefone de gestor inválido")

            operador = repository.get_by_telefone(telefone)
            if operador is None:
                operador = Operador(
                    telefone_whatsapp=telefone,
                    nome_operador=cadastro.nome,
                    perfil=PerfilOperador.GESTOR,
                    ativo=True,
                )
                db.add(operador)
                estado = EstadoCadastro.CRIADO
            else:
                alterado = (
                    operador.nome_operador != cadastro.nome
                    or operador.perfil != PerfilOperador.GESTOR
                    or operador.ativo is not True
                )
                operador.nome_operador = cadastro.nome
                operador.perfil = PerfilOperador.GESTOR
                operador.ativo = True
                estado = EstadoCadastro.ATUALIZADO if alterado else EstadoCadastro.JA_ESTAVA_CORRETO

            db.flush()
            resultados.append(
                ResultadoCadastro(
                    telefone=telefone,
                    nome=cadastro.nome,
                    estado=estado,
                )
            )

    return resultados


def main() -> int:
    try:
        with SessionLocal() as db:
            resultados = cadastrar_gestores(db)
    except Exception as exc:
        print(f"ERRO: cadastro de gestores revertido: {type(exc).__name__}")
        return 1

    for resultado in resultados:
        print(f"{resultado.telefone} | {resultado.nome} | {resultado.estado}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
