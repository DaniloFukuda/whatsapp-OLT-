from datetime import datetime, timedelta

from app.models.aluguer import StatusAluguer
from app.models.contentor import StatusContentor
from app.repositories.cliente_repository import ClienteRepository
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService
from app.services.seed_service import SeedService


def test_criacao_de_cliente_contentor_e_aluguer(db_session):
    cliente = ClienteRepository(db_session).create(nome="Cliente Um", telefone="351900000001")
    contentor = ContentorService(db_session).criar_contentor("C-001")

    aluguer = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente=cliente.nome,
        telefone_cliente=cliente.telefone,
        valor="120.50",
        forma_pagamento="mbway",
        pago=True,
        contentor_id=contentor.id,
    )

    assert aluguer.id is not None
    assert aluguer.cliente_id == cliente.id
    assert aluguer.contentor_id == contentor.id
    assert aluguer.status == StatusAluguer.ATIVO
    assert aluguer.contentor.status == StatusContentor.ALUGADO


def test_vencimento_calculado_em_5_dias(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    entrega = datetime(2026, 1, 1, 10, 0, 0)

    aluguer = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Dois",
        telefone_cliente="351900000002",
        valor="100",
        forma_pagamento="dinheiro",
        pago=False,
        data_entrega=entrega,
    )

    assert aluguer.data_vencimento == entrega + timedelta(days=5)


def test_renovacao_por_mais_5_dias(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    service = AluguerService(db_session)
    aluguer = service.registrar_novo_aluguer(
        nome_cliente="Cliente Tres",
        telefone_cliente="351900000003",
        valor="90",
        forma_pagamento="transferencia",
        pago=True,
        data_entrega=datetime(2026, 1, 1, 10, 0, 0),
    )

    renovado = service.renovar_por_mais_5_dias(aluguer.id)

    assert renovado.data_vencimento == datetime(2026, 1, 11, 10, 0, 0)
    assert renovado.status == StatusAluguer.RENOVADO


def test_listagem_de_alugueres_que_vencem_amanha(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    service = AluguerService(db_session)
    vencendo = service.registrar_novo_aluguer(
        nome_cliente="Cliente Quatro",
        telefone_cliente="351900000004",
        valor="80",
        forma_pagamento="cartao",
        pago=True,
        data_entrega=datetime(2026, 1, 1, 10, 0, 0),
    )
    service.registrar_novo_aluguer(
        nome_cliente="Cliente Cinco",
        telefone_cliente="351900000005",
        valor="80",
        forma_pagamento="cartao",
        pago=True,
        data_entrega=datetime(2026, 1, 2, 10, 0, 0),
    )

    result = service.listar_vencendo_amanha(now=datetime(2026, 1, 5, 9, 0, 0))

    assert [aluguer.id for aluguer in result] == [vencendo.id]
