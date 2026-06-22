import pytest
from sqlalchemy.exc import IntegrityError

from app.models.contentor import Contentor, StatusContentor
from app.models.operador import Operador, PerfilOperador
from app.services.contentor_service import ContentorService


def test_operadores_aceitam_perfis_validos(db_session):
    funcionario = Operador(
        telefone_whatsapp="351900000010",
        nome_operador="Funcionario",
        perfil=PerfilOperador.FUNCIONARIO,
    )
    gestor = Operador(
        telefone_whatsapp="351900000011",
        nome_operador="Gestor",
        perfil=PerfilOperador.GESTOR,
    )

    db_session.add_all([funcionario, gestor])
    db_session.commit()

    assert db_session.get(Operador, "351900000010").perfil == PerfilOperador.FUNCIONARIO
    assert db_session.get(Operador, "351900000011").perfil == PerfilOperador.GESTOR
    assert db_session.get(Operador, "351900000011").ativo is True


def test_operador_rejeita_perfil_invalido(db_session):
    db_session.add(
        Operador(
            telefone_whatsapp="351900000012",
            nome_operador="Perfil Invalido",
            perfil="ADMIN",
        )
    )

    with pytest.raises((IntegrityError, LookupError)):
        db_session.commit()


def test_contentores_excluidos_nao_aparecem_em_listagens_operacionais(db_session):
    service = ContentorService(db_session)
    ativo = service.criar_contentor("C-ATIVO")
    excluido = service.criar_contentor("C-EXCLUIDO")
    excluido.is_deleted = True
    db_session.commit()

    contentores = service.listar_contentores()

    assert [contentor.id for contentor in contentores] == [ativo.id]
    assert service.repository.first_available().id == ativo.id
    assert service.repository.count() == 1


def test_campos_de_auditoria_de_contentores_podem_ser_preenchidos(db_session):
    contentor = Contentor(
        codigo="C-AUDIT",
        status=StatusContentor.DISPONIVEL,
        criado_por_operador="351900000020",
        alterado_por_operador="351900000021",
        excluido_por_operador="351900000022",
        is_deleted=True,
        justificativa_exclusao="Cadastro duplicado identificado",
    )

    db_session.add(contentor)
    db_session.commit()
    db_session.refresh(contentor)

    assert contentor.criado_por_operador == "351900000020"
    assert contentor.alterado_por_operador == "351900000021"
    assert contentor.excluido_por_operador == "351900000022"
    assert contentor.is_deleted is True
    assert contentor.justificativa_exclusao == "Cadastro duplicado identificado"
