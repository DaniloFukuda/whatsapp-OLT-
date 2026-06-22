from app.models.operador import Operador, PerfilOperador
from app.repositories.operador_repository import OperadorRepository
from app.services.operador_service import OperadorService


def test_repositorio_busca_operador_por_telefone_whatsapp(db_session):
    operador = Operador(
        telefone_whatsapp="351900000100",
        nome_operador="Operador Um",
        perfil=PerfilOperador.FUNCIONARIO,
    )
    db_session.add(operador)
    db_session.commit()

    repository = OperadorRepository(db_session)

    assert repository.get_by_telefone("351900000100") == operador
    assert repository.get_by_telefone("351900000999") is None
    assert repository.has_any() is True


def test_servico_normaliza_telefone_e_retorna_perfil_do_operador_ativo(db_session):
    db_session.add(
        Operador(
            telefone_whatsapp="351900000101",
            nome_operador="Gestor",
            perfil=PerfilOperador.GESTOR,
        )
    )
    db_session.commit()

    service = OperadorService(db_session)

    assert service.buscar_por_telefone("+351 900 000 101").perfil == PerfilOperador.GESTOR
    assert service.verificar_autorizacao("+351 900 000 101") is True
    assert service.obter_perfil("+351 900 000 101") == PerfilOperador.GESTOR


def test_servico_nao_retorna_perfil_para_operador_inativo(db_session):
    db_session.add(
        Operador(
            telefone_whatsapp="351900000102",
            nome_operador="Funcionario",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=False,
        )
    )
    db_session.commit()

    service = OperadorService(db_session)

    assert service.verificar_autorizacao("351900000102") is False
    assert service.obter_perfil("351900000102") is None
