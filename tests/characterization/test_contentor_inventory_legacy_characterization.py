"""Testes de Caracterização - Contentor.

Documenta o comportamento atual da gestão de contentores antes de refatorações.
Cobre: cadastro, listagem, disponibilidade, estados e transições de contentores.
"""
from datetime import datetime, timedelta

import pytest
from decimal import Decimal

from app.models.contentor import Contentor, StatusContentor
from app.models.aluguer import AluguerContentor, StatusAluguer, StatusEntrega, StatusCiclo
from app.repositories.contentor_repository import ContentorRepository
from app.services.contentor_service import ContentorService, NUMERO_CONTENTOR_INVALIDO
from app.services.seed_service import SeedService
from app.services.aluguer_service import AluguerService


# =============================================================================
# REPOSITORY TESTS
# =============================================================================

class TestContentorRepository:
    """Testes do repositório de contentores."""

    def test_create_cria_contentor_com_codigo_e_status_padrao(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="10", status=StatusContentor.DISPONIVEL)

        assert contentor.id is not None
        assert contentor.codigo == "10"
        assert contentor.status == StatusContentor.DISPONIVEL
        assert contentor.is_deleted is False

    def test_create_com_status_personalizado(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="15", status=StatusContentor.MANUTENCAO)

        assert contentor.status == StatusContentor.MANUTENCAO

    def test_list_retorna_apenas_nao_deletados_ordenados_por_codigo_numerico(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="2")
        repo.create(codigo="10")
        repo.create(codigo="1")
        repo.create(codigo="3")

        result = repo.list()

        assert [c.codigo for c in result] == ["1", "2", "3", "10"]

    def test_list_ignora_contentores_deletados(self, db_session):
        repo = ContentorRepository(db_session)
        c1 = repo.create(codigo="1")
        c2 = repo.create(codigo="2")
        c2.is_deleted = True
        db_session.commit()

        result = repo.list()

        assert len(result) == 1
        assert result[0].codigo == "1"

    def test_get_por_id_retorna_contentor(self, db_session):
        repo = ContentorRepository(db_session)
        criado = repo.create(codigo="5")

        resultado = repo.get(criado.id)

        assert resultado is not None
        assert resultado.codigo == "5"

    def test_get_por_id_inexistente_retorna_none(self, db_session):
        repo = ContentorRepository(db_session)

        resultado = repo.get(999)

        assert resultado is None

    def test_get_by_codigo_retorna_contentor(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="7")

        resultado = repo.get_by_codigo("7")

        assert resultado is not None
        assert resultado.codigo == "7"

    def test_get_by_codigo_inexistente_retorna_none(self, db_session):
        repo = ContentorRepository(db_session)

        resultado = repo.get_by_codigo("999")

        assert resultado is None

    def test_first_available_retorna_primeiro_disponivel_por_id(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="1", status=StatusContentor.ALUGADO)
        c2 = repo.create(codigo="2", status=StatusContentor.DISPONIVEL)
        repo.create(codigo="3", status=StatusContentor.DISPONIVEL)

        resultado = repo.first_available()

        assert resultado is not None
        assert resultado.id == c2.id
        assert resultado.status == StatusContentor.DISPONIVEL

    def test_first_available_retorna_none_quando_nenhum_disponivel(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="1", status=StatusContentor.ALUGADO)
        repo.create(codigo="2", status=StatusContentor.MANUTENCAO)

        resultado = repo.first_available()

        assert resultado is None

    def test_count_retorna_total_nao_deletados(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="1")
        repo.create(codigo="2")
        c3 = repo.create(codigo="3")
        c3.is_deleted = True
        db_session.commit()

        assert repo.count() == 2

    def test_update_status_atualiza_status_e_persiste(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="1", status=StatusContentor.DISPONIVEL)

        atualizado = repo.update_status(contentor, StatusContentor.ALUGADO)

        assert atualizado.status == StatusContentor.ALUGADO
        assert atualizado.id == contentor.id
        # Verifica persistência
        db_session.expire_all()
        assert db_session.get(Contentor, contentor.id).status == StatusContentor.ALUGADO

    def test_save_persiste_alteracoes_no_contentor(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="1", status=StatusContentor.DISPONIVEL)
        contentor.status = StatusContentor.AGUARDANDO_RECOLHA

        salvo = repo.save(contentor)

        assert salvo.status == StatusContentor.AGUARDANDO_RECOLHA
        db_session.expire_all()
        assert db_session.get(Contentor, contentor.id).status == StatusContentor.AGUARDANDO_RECOLHA

    def test_get_including_deleted_retorna_tambem_deletados(self, db_session):
        repo = ContentorRepository(db_session)
        c1 = repo.create(codigo="1")
        c2 = repo.create(codigo="2")
        c2.is_deleted = True
        db_session.commit()

        ativos = repo.list()
        todos = db_session.query(Contentor).all()

        assert len(ativos) == 1
        assert len(todos) == 2
        assert repo.get_including_deleted(c2.id) is not None

    def test_get_by_codigo_including_deleted_retorna_tambem_deletados(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="1")
        c2 = repo.create(codigo="2")
        c2.is_deleted = True
        db_session.commit()

        assert repo.get_by_codigo("2") is None
        assert repo.get_by_codigo_including_deleted("2") is not None


# =============================================================================
# CONTENTOR SERVICE TESTS
# =============================================================================

class TestContentorService:
    """Testes do serviço de contentores."""

    def test_criar_contentor_cria_novo_com_codigo_validado(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("42")

        assert contentor.codigo == "42"
        assert contentor.status == StatusContentor.DISPONIVEL

    def test_criar_contentor_retorna_existente_se_codigo_ja_existe(self, db_session):
        service = ContentorService(db_session)
        primeiro = service.criar_contentor("10")
        segundo = service.criar_contentor("10")

        assert primeiro.id == segundo.id

    def test_criar_contentor_rejeita_codigo_invalido(self, db_session):
        service = ContentorService(db_session)

        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("C01")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("01")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("0")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("100")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("-1")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("abc")
        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.criar_contentor("")

    def test_listar_contentores_retorna_todos_ordenados(self, db_session):
        service = ContentorService(db_session)
        service.criar_contentor("3")
        service.criar_contentor("1")
        service.criar_contentor("2")

        result = service.listar_contentores()

        assert [c.codigo for c in result] == ["1", "2", "3"]

    def test_mudar_status_atualiza_status_do_contentor(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("5")

        atualizado = service.mudar_status(contentor.id, StatusContentor.MANUTENCAO)

        assert atualizado.status == StatusContentor.MANUTENCAO
        db_session.expire_all()
        assert db_session.get(Contentor, contentor.id).status == StatusContentor.MANUTENCAO

    def test_mudar_status_levanta_erro_se_contentor_nao_existe(self, db_session):
        service = ContentorService(db_session)

        with pytest.raises(ValueError, match="Contentor nao encontrado"):
            service.mudar_status(999, StatusContentor.MANUTENCAO)

    def test_excluir_com_auditoria_marca_como_deletado_e_registra_auditoria(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("7")

        excluido = service.excluir_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351912345678",
            justificativa="Contentor danificado irrecuperavel"
        )

        assert excluido.is_deleted is True
        assert excluido.excluido_por_operador == "351912345678"
        assert excluido.justificativa_exclusao == "Contentor danificado irrecuperavel"

    def test_excluir_com_auditoria_por_codigo_tambem_funciona(self, db_session):
        service = ContentorService(db_session)
        service.criar_contentor("8")

        excluido = service.excluir_com_auditoria(
            codigo="8",
            operador_telefone="351912345678",
            justificativa="Contentor danificado irrecuperavel"
        )

        assert excluido.codigo == "8"
        assert excluido.is_deleted is True

    def test_excluir_com_auditoria_rejeita_justificativa_curta(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("9")

        with pytest.raises(ValueError, match="pelo menos 10 caracteres"):
            service.excluir_com_auditoria(
                contentor_id=contentor.id,
                operador_telefone="351912345678",
                justificativa="Curto"
            )

    def test_excluir_com_auditoria_rejeita_contentor_ja_excluido(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("10")
        service.excluir_com_auditoria(contentor_id=contentor.id, operador_telefone="351912345678", justificativa="Primeira exclusao")

        with pytest.raises(ValueError, match="Contentor ja excluido"):
            service.excluir_com_auditoria(contentor_id=contentor.id, operador_telefone="351912345678", justificativa="Segunda exclusao")

    def test_alterar_com_auditoria_atualiza_status_e_registra_operador(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("11")

        alterado = service.alterar_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351912345678",
            campo="status",
            novo_valor="manutencao"
        )

        assert alterado.status == StatusContentor.MANUTENCAO
        assert alterado.alterado_por_operador == "351912345678"

    def test_alterar_com_auditoria_rejeita_campo_nao_permitido(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("12")

        with pytest.raises(ValueError, match="Campo nao permitido"):
            service.alterar_com_auditoria(
                contentor_id=contentor.id,
                operador_telefone="351912345678",
                campo="codigo",
                novo_valor="99"
            )

    def test_alterar_com_auditoria_rejeita_contentor_excluido(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("13")
        service.excluir_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351912345678",
            justificativa="Excluido para teste",
        )

        with pytest.raises(ValueError, match="excluido nao pode ser alterado"):
            service.alterar_com_auditoria(
                contentor_id=contentor.id,
                operador_telefone="351912345678",
                campo="status",
                novo_valor="disponivel"
            )

    def test_obter_ou_criar_disponivel_retorna_existente_disponivel(self, db_session):
        service = ContentorService(db_session)
        c1 = service.criar_contentor("1")
        service.mudar_status(c1.id, StatusContentor.ALUGADO)
        c2 = service.criar_contentor("2")

        resultado = service.obter_ou_criar_disponivel()

        assert resultado.id == c2.id

    def test_obter_ou_criar_disponivel_cria_novo_se_nenhum_disponivel(self, db_session):
        service = ContentorService(db_session)
        c1 = service.criar_contentor("1")
        c2 = service.criar_contentor("2")
        service.mudar_status(c1.id, StatusContentor.ALUGADO)
        service.mudar_status(c2.id, StatusContentor.MANUTENCAO)

        resultado = service.obter_ou_criar_disponivel()

        assert resultado.codigo == "3"
        assert resultado.status == StatusContentor.DISPONIVEL

    def test_validar_numero_aceita_inteiros_1_a_99(self):
        assert ContentorService.validar_numero("1") == "1"
        assert ContentorService.validar_numero("50") == "50"
        assert ContentorService.validar_numero("99") == "99"
        assert ContentorService.validar_numero(" 42 ") == "42"

    def test_validar_numero_rejeita_formatos_invalidos(self):
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("C01")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("01")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("0")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("100")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("-1")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("1.5")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("abc")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero("")
        with pytest.raises(ValueError, match=NUMERO_CONTENTOR_INVALIDO):
            ContentorService.validar_numero(None)

    def test_buscar_para_entrega_retorna_contentor_disponivel(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("20")

        resultado = service.buscar_para_entrega("20")

        assert resultado.codigo == "20"
        assert resultado.status == StatusContentor.DISPONIVEL

    def test_buscar_para_entrega_levanta_erro_se_contentor_nao_existe(self, db_session):
        service = ContentorService(db_session)

        with pytest.raises(ValueError, match="nao esta cadastrado"):
            service.buscar_para_entrega("99")

    def test_buscar_para_entrega_levanta_erro_se_contentor_ja_alugado(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("15")
        contentor.status = StatusContentor.ALUGADO
        db_session.commit()

        with pytest.raises(ValueError, match="ja esta alugado"):
            service.buscar_para_entrega("15")

    def test_buscar_para_entrega_levanta_erro_se_contentor_indisponivel(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("16")
        contentor.status = StatusContentor.MANUTENCAO
        db_session.commit()

        with pytest.raises(ValueError, match="indisponivel"):
            service.buscar_para_entrega("16")

    def test_buscar_para_entrega_rejeita_numero_invalido(self, db_session):
        service = ContentorService(db_session)

        with pytest.raises(ValueError, match="inteiro de 1 a 99"):
            service.buscar_para_entrega("C01")


# =============================================================================
# SEED SERVICE TESTS
# =============================================================================

class TestSeedService:
    """Testes do serviço de seed de contentores."""

    def test_seed_cria_20_contentores_codigos_1_a_20(self, db_session):
        service = SeedService(db_session)
        criados = service.seed_contentores_iniciais()

        assert criados == [str(i) for i in range(1, 21)]
        contentores = db_session.query(Contentor).all()
        assert len(contentores) == 20
        assert sorted((c.codigo for c in contentores), key=int) == [str(i) for i in range(1, 21)]
        assert all(c.status == StatusContentor.DISPONIVEL for c in contentores)

    def test_seed_e_idempotente_segunda_execucao_nao_cria_duplicados(self, db_session):
        service = SeedService(db_session)
        service.seed_contentores_iniciais()
        segunda = service.seed_contentores_iniciais()

        assert segunda == []
        assert db_session.query(Contentor).count() == 20

    def test_seed_migra_codigos_legados_C01_a_C99_para_numeros_simples(self, db_session):
        # Cria contentores com códigos legados
        repo = ContentorRepository(db_session)
        repo.create(codigo="C01", status=StatusContentor.DISPONIVEL)
        repo.create(codigo="C05", status=StatusContentor.DISPONIVEL)
        repo.create(codigo="C20", status=StatusContentor.DISPONIVEL)
        db_session.commit()

        service = SeedService(db_session)
        criados = service.seed_contentores_iniciais()

        # O retorno informa apenas os novos; 1, 5 e 20 foram migrados.
        assert criados == [str(i) for i in range(2, 20) if i != 5]
        contentores = db_session.query(Contentor).order_by(Contentor.codigo).all()
        codigos = [c.codigo for c in contentores]
        assert "1" in codigos
        assert "5" in codigos
        assert "20" in codigos
        assert "C01" not in codigos
        assert "C05" not in codigos
        assert "C20" not in codigos


# =============================================================================
# MODEL TESTS
# =============================================================================

class TestContentorModel:
    """Testes do modelo Contentor."""

    def test_status_contentor_enum_tem_valores_esperados(self):
        assert StatusContentor.DISPONIVEL == "disponivel"
        assert StatusContentor.ALUGADO == "alugado"
        assert StatusContentor.AGUARDANDO_RECOLHA == "aguardando_recolha"
        assert StatusContentor.MANUTENCAO == "manutencao"

    def test_contentor_tem_campos_de_auditoria(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="1")

        assert hasattr(contentor, "criado_por_operador")
        assert hasattr(contentor, "alterado_por_operador")
        assert hasattr(contentor, "excluido_por_operador")
        assert hasattr(contentor, "is_deleted")
        assert hasattr(contentor, "justificativa_exclusao")
        assert hasattr(contentor, "criado_em")
        assert hasattr(contentor, "atualizado_em")

    def test_contentor_relacionamento_alugueres(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="1")

        assert hasattr(contentor, "alugueres")
        assert contentor.alugueres == []


# =============================================================================
# ALUGUER SERVICE INTEGRATION TESTS (estados do contentor)
# =============================================================================

class TestContentorEstadosViaAluguer:
    """Testes das transições de estado do contentor através do AluguerService."""

    def test_registrar_novo_aluguer_muda_contentor_para_ALUGADO(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )

        assert aluguer.contentor.status == StatusContentor.ALUGADO
        db_session.expire_all()
        assert db_session.get(Contentor, aluguer.contentor_id).status == StatusContentor.ALUGADO

    def test_registrar_novo_aluguer_com_status_entrega_PENDENTE_nao_muda_status_contentor(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
            status_entrega=StatusEntrega.PENDENTE.value,
            numero_contentor="A definir",
        )

        assert aluguer.contentor.status == StatusContentor.DISPONIVEL

    def test_confirmar_entrega_muda_contentor_para_ALUGADO(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
            status_entrega=StatusEntrega.PENDENTE.value,
            numero_contentor="A definir",
        )
        contentor_codigo = aluguer.contentor.codigo

        confirmado = service.confirmar_entrega(
            aluguer_id=aluguer.id,
            contentor_codigo=contentor_codigo,
            operador_telefone="351912345678",
            entrega_latitude=38.7223,
            entrega_longitude=-9.1393,
            fotos_entrega=["foto1.jpg"],
        )

        assert confirmado.contentor.status == StatusContentor.ALUGADO
        assert confirmado.numero_contentor == contentor_codigo

    def test_confirmar_entrega_rejeita_contentor_ja_alugado(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer1 = service.registrar_novo_aluguer(
            nome_cliente="Cliente Um",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )
        contentor_codigo = aluguer1.contentor.codigo

        aluguer2 = service.registrar_novo_aluguer(
            nome_cliente="Cliente Dois",
            telefone_cliente="351987654321",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
            status_entrega=StatusEntrega.PENDENTE.value,
            numero_contentor="A definir",
        )

        with pytest.raises(ValueError, match="ja esta alugado"):
            service.confirmar_entrega(
                aluguer_id=aluguer2.id,
                contentor_codigo=contentor_codigo,
                operador_telefone="351987654321",
                entrega_latitude=38.7223,
                entrega_longitude=-9.1393,
                fotos_entrega=["foto1.jpg"],
            )

    def test_marcar_recolha_muda_contentor_para_AGUARDANDO_RECOLHA(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )

        recolhido = service.marcar_recolha(aluguer.id)

        assert recolhido.contentor.status == StatusContentor.AGUARDANDO_RECOLHA
        assert recolhido.status == StatusAluguer.AGUARDANDO_RECOLHA

    def test_confirmar_recolha_muda_contentor_para_DISPONIVEL(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )
        service.marcar_recolha(aluguer.id)

        confirmado = service.confirmar_recolha(
            aluguer_id=aluguer.id,
            operador_telefone="351912345678",
            fotos_recolha=["foto_recolha.jpg"],
        )

        assert confirmado.contentor.status == StatusContentor.DISPONIVEL
        assert confirmado.status == StatusAluguer.RECOLHIDO
        assert confirmado.status_ciclo == StatusCiclo.RECOLHIDO.value

    def test_excluir_aluguer_muda_contentor_para_DISPONIVEL(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )
        contentor_id = aluguer.contentor_id

        service.excluir(aluguer.id, "351912345678", "Cancelado por pedido do cliente")

        db_session.expire_all()
        contentor = db_session.get(Contentor, contentor_id)
        assert contentor.status == StatusContentor.DISPONIVEL

    def test_registrar_novo_aluguer_com_contentor_id_especifico_usa_esse_contentor(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        # Pega o contentor 5
        contentor_5 = db_session.query(Contentor).filter_by(codigo="5").one()

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
            contentor_id=contentor_5.id,
        )

        assert aluguer.contentor_id == contentor_5.id

    def test_registrar_novo_aluguer_levanta_erro_se_nenhum_contentor_disponivel(self, db_session):
        # Cria apenas contentores alugados
        repo = ContentorRepository(db_session)
        for i in range(1, 6):
            repo.create(codigo=str(i), status=StatusContentor.ALUGADO)

        service = AluguerService(db_session)

        with pytest.raises(ValueError, match="Nenhum contentor disponivel"):
            service.registrar_novo_aluguer(
                nome_cliente="Cliente Teste",
                telefone_cliente="351912345678",
                valor="100",
                forma_pagamento="mbway",
                pago=True,
            )


# =============================================================================
# EDGE CASES E REGRESSÕES
# =============================================================================

class TestContentorEdgeCases:
    """Testes de casos de borda e regressões conhecidas."""

    def test_contentor_codigo_unico_na_base(self, db_session):
        repo = ContentorRepository(db_session)
        repo.create(codigo="1")

        # Tentar criar com mesmo código deve falhar no banco (unique constraint)
        # Mas o serviço verifica antes
        service = ContentorService(db_session)
        assert service.criar_contentor("1").codigo == "1"  # Retorna o existente

    def test_contentor_criado_por_operador_preenchido_no_aluguer(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
            operador_telefone="351987654321",
        )

        assert aluguer.contentor.criado_por_operador == "351987654321"

    def test_alterar_status_para_string_funciona(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("10")

        alterado = service.alterar_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351912345678",
            campo="status",
            novo_valor="manutencao"  # string em vez de enum
        )

        assert alterado.status == StatusContentor.MANUTENCAO

    def test_alterar_status_com_enum_funciona(self, db_session):
        service = ContentorService(db_session)
        contentor = service.criar_contentor("11")

        alterado = service.alterar_com_auditoria(
            contentor_id=contentor.id,
            operador_telefone="351912345678",
            campo="status",
            novo_valor=StatusContentor.MANUTENCAO  # enum
        )

        assert alterado.status == StatusContentor.MANUTENCAO

    def test_obter_ou_criar_disponivel_usa_numeracao_sequencial_1_a_99(self, db_session):
        service = ContentorService(db_session)
        # Ocupa 1 a 98
        for i in range(1, 99):
            contentor = service.criar_contentor(str(i))
            service.mudar_status(contentor.id, StatusContentor.ALUGADO)

        # Deve criar o 99
        resultado = service.obter_ou_criar_disponivel()
        assert resultado.codigo == "99"

    def test_obter_ou_criar_disponivel_falha_se_todos_1_a_99_ocupados(self, db_session):
        service = ContentorService(db_session)
        for i in range(1, 100):
            contentor = service.criar_contentor(str(i))
            service.mudar_status(contentor.id, StatusContentor.ALUGADO)

        with pytest.raises(ValueError, match="Nao ha numeracao de contentor disponivel"):
            service.obter_ou_criar_disponivel()


# =============================================================================
# VERIFICAÇÃO DE CONSISTÊNCIA DOS DADOS
# =============================================================================

class TestContentorConsistencia:
    """Testes de consistência de dados."""

    def test_contentor_tem_updated_at_atualizado_em_save(self, db_session):
        repo = ContentorRepository(db_session)
        contentor = repo.create(codigo="1")
        criado_em = contentor.atualizado_em

        import time
        time.sleep(0.01)
        contentor.status = StatusContentor.ALUGADO
        repo.save(contentor)

        assert contentor.atualizado_em >= criado_em

    def test_aluguer_contentor_relationship_bidirecional(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )

        contentor = aluguer.contentor
        assert aluguer in contentor.alugueres

    def test_exclusao_logica_contentor_nao_remove_alugueres(self, db_session):
        SeedService(db_session).seed_contentores_iniciais()
        service = AluguerService(db_session)
        contentor_service = ContentorService(db_session)

        aluguer = service.registrar_novo_aluguer(
            nome_cliente="Cliente Teste",
            telefone_cliente="351912345678",
            valor="100",
            forma_pagamento="mbway",
            pago=True,
        )
        contentor_id = aluguer.contentor_id

        contentor_service.excluir_com_auditoria(
            contentor_id=contentor_id,
            operador_telefone="351912345678",
            justificativa="Exclusao logica do contentor"
        )

        db_session.expire_all()
        aluguer_reload = db_session.get(AluguerContentor, aluguer.id)
        contentor_reload = db_session.get(Contentor, contentor_id)

        assert aluguer_reload is not None
        assert aluguer_reload.is_deleted is False
        assert contentor_reload.is_deleted is True
        assert aluguer_reload.contentor_id == contentor_id
