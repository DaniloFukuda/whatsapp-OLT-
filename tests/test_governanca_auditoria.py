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
    ativo = service.criar_contentor("21")
    excluido = service.criar_contentor("22")
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


def test_exclusao_segura_de_contentor_marca_auditoria_sem_delete_fisico(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("23")

    excluido = service.excluir_com_auditoria(
        contentor_id=contentor.id,
        operador_telefone="351900000030",
        justificativa="Contentor cadastrado em duplicidade",
    )

    assert excluido.id == contentor.id
    assert excluido.is_deleted is True
    assert excluido.excluido_por_operador == "351900000030"
    assert excluido.justificativa_exclusao == "Contentor cadastrado em duplicidade"
    assert db_session.get(Contentor, contentor.id) is not None
    assert service.listar_contentores() == []


def test_exclusao_segura_de_contentor_aceita_codigo(db_session):
    service = ContentorService(db_session)
    service.criar_contentor("24")

    excluido = service.excluir_com_auditoria(
        codigo="24",
        operador_telefone="351900000031",
        justificativa="Contentor fora de operacao",
    )

    assert excluido.codigo == "24"
    assert excluido.is_deleted is True


def test_exclusao_segura_rejeita_justificativa_curta(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("25")

    with pytest.raises(ValueError, match="Justificativa"):
        service.excluir_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351900000032",
            justificativa="curta",
        )

    db_session.refresh(contentor)
    assert contentor.is_deleted is False


def test_exclusao_segura_nao_exclui_contentor_ja_excluido(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("26")
    service.excluir_com_auditoria(
        contentor_id=contentor.id,
        operador_telefone="351900000033",
        justificativa="Primeira exclusao segura",
    )

    with pytest.raises(ValueError, match="ja excluido"):
        service.excluir_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351900000034",
            justificativa="Segunda exclusao segura",
        )


def test_alteracao_auditada_de_contentor_altera_status_e_grava_operador(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("27")

    alterado = service.alterar_com_auditoria(
        contentor_id=contentor.id,
        operador_telefone="351900000040",
        campo="status",
        novo_valor="manutencao",
    )

    assert alterado.status == StatusContentor.MANUTENCAO
    assert alterado.alterado_por_operador == "351900000040"


def test_alteracao_auditada_de_contentor_aceita_codigo(db_session):
    service = ContentorService(db_session)
    service.criar_contentor("28")

    alterado = service.alterar_com_auditoria(
        codigo="28",
        operador_telefone="351900000041",
        campo="status",
        novo_valor=StatusContentor.AGUARDANDO_RECOLHA,
    )

    assert alterado.codigo == "28"
    assert alterado.status == StatusContentor.AGUARDANDO_RECOLHA
    assert alterado.alterado_por_operador == "351900000041"


def test_alteracao_auditada_rejeita_campo_nao_permitido(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("29")

    with pytest.raises(ValueError, match="Campo nao permitido"):
        service.alterar_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351900000042",
            campo="valor",
            novo_valor="100",
        )


def test_alteracao_auditada_bloqueia_contentor_excluido(db_session):
    service = ContentorService(db_session)
    contentor = service.criar_contentor("30")
    service.excluir_com_auditoria(
        contentor_id=contentor.id,
        operador_telefone="351900000043",
        justificativa="Contentor removido da operacao",
    )

    with pytest.raises(ValueError, match="excluido"):
        service.alterar_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351900000044",
            campo="status",
            novo_valor="manutencao",
        )


def test_alteracao_auditada_rejeita_contentor_inexistente(db_session):
    service = ContentorService(db_session)

    with pytest.raises(ValueError, match="Contentor nao encontrado"):
        service.alterar_com_auditoria(
            contentor_id=999,
            operador_telefone="351900000045",
            campo="status",
            novo_valor="manutencao",
        )
