import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.operador import Operador, PerfilOperador
from app.services.operador_service import OperadorService
from scripts.cadastrar_gestores_lucas_secretario import (
    EstadoCadastro,
    GESTORES,
    GestorCadastro,
    cadastrar_gestores,
)


TELEFONES = {gestor.telefone for gestor in GESTORES}


def _operadores_alvo(db_session) -> list[Operador]:
    return list(
        db_session.scalars(
            select(Operador)
            .where(Operador.telefone_whatsapp.in_(TELEFONES))
            .order_by(Operador.telefone_whatsapp)
        )
    )


def test_cadastra_os_dois_contatos_como_gestores_ativos_e_normalizados(db_session):
    resultados = cadastrar_gestores(db_session)

    assert [resultado.estado for resultado in resultados] == [
        EstadoCadastro.CRIADO,
        EstadoCadastro.CRIADO,
    ]
    operadores = _operadores_alvo(db_session)
    assert {operador.telefone_whatsapp for operador in operadores} == TELEFONES
    assert all(operador.perfil == PerfilOperador.GESTOR for operador in operadores)
    assert all(operador.ativo is True for operador in operadores)


def test_execucao_repetida_e_idempotente(db_session):
    cadastrar_gestores(db_session)

    resultados = cadastrar_gestores(db_session)

    assert all(resultado.estado == EstadoCadastro.JA_ESTAVA_CORRETO for resultado in resultados)
    quantidade = db_session.scalar(
        select(func.count()).select_from(Operador).where(Operador.telefone_whatsapp.in_(TELEFONES))
    )
    assert quantidade == 2


def test_atualiza_funcionario_inativo_e_preserva_outro_operador(db_session):
    existente = Operador(
        telefone_whatsapp="351937522741",
        nome_operador="Nome anterior",
        perfil=PerfilOperador.FUNCIONARIO,
        ativo=False,
    )
    outro = Operador(
        telefone_whatsapp="351900000099",
        nome_operador="Outro operador",
        perfil=PerfilOperador.FUNCIONARIO,
        ativo=False,
    )
    db_session.add_all([existente, outro])
    db_session.commit()

    resultados = cadastrar_gestores(db_session)

    assert resultados[0].estado == EstadoCadastro.ATUALIZADO
    db_session.refresh(existente)
    db_session.refresh(outro)
    assert (existente.nome_operador, existente.perfil, existente.ativo) == (
        "Lucas Santos",
        PerfilOperador.GESTOR,
        True,
    )
    assert (outro.nome_operador, outro.perfil, outro.ativo) == (
        "Outro operador",
        PerfilOperador.FUNCIONARIO,
        False,
    )


def test_falha_no_segundo_cadastro_reverte_toda_a_transacao(db_session):
    gestores_invalidos = (
        GestorCadastro(telefone="+351 937 522 741", nome="Lucas Santos"),
        GestorCadastro(telefone="351933372221", nome=None),  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrityError):
        cadastrar_gestores(db_session, gestores_invalidos)

    assert db_session.scalar(select(func.count()).select_from(Operador)) == 0


def test_permissoes_sao_de_gestor_sem_fallback_do_env(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    cadastrar_gestores(db_session)

    decisoes = [OperadorService(db_session).decidir_acesso(telefone) for telefone in TELEFONES]

    assert all(decisao.autorizado is True for decisao in decisoes)
    assert all(decisao.perfil == PerfilOperador.GESTOR for decisao in decisoes)
    assert all(decisao.origem == "TABELA" for decisao in decisoes)
