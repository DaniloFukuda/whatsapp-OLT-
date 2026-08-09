"""Caracterização operacional fundamental da Carrinha V2.4."""

from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from app.models.aluguer import ContentorFoto
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusOperacionalCarrinha,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


def criar_pedido_carrinha(service: PedidoService):
    return service.criar(
        nome_cliente="Cliente Carrinha C2",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor-c2",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "10:00",
            }
        ],
    )


def confirmar_chegada(service: PedidoService, pedido, frota="17"):
    carrinha = pedido.contentores[0]
    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "motorista-chegada-c2",
        38.7223,
        -9.1393,
        "Portao principal",
        [{"contentor_id": carrinha.id, "numero_adesivo": frota, "fotos": [f"foto-chegada-{frota}"]}],
    )
    service.db.commit()
    service.db.refresh(carrinha)
    return carrinha


def ids(itens):
    return {item.id for item in itens}


def test_pedido_novo_cria_carrinha_no_estado_operacional_inicial(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    db_session.refresh(carrinha)

    assert carrinha.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
    assert pedido.id not in {item.id for item in service.pedidos_pendentes_entrega()}


def test_carrinha_nova_aparece_na_fila_de_chegada(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    assert carrinha.id in ids(service.carrinhas_aguardando_chegada())
    assert pedido.id in {item.id for item in service.pedidos_carrinha_aguardando_chegada()}


def test_carrinha_aguardando_chegada_nao_aparece_em_partida_despejo_ou_filas_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    assert carrinha.id not in ids(service.carrinhas_aguardando_partida())
    assert carrinha.id not in ids(service.carrinhas_aguardando_despejo())
    assert carrinha.id not in ids(service.contentores_para_recolha())
    assert carrinha.id not in ids(service.contentores_para_despejo())


def test_confirmacao_valida_da_chegada_muda_estado_e_remove_da_fila(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido)

    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert persistida.id == carrinha.id
    assert persistida.id not in ids(service.carrinhas_aguardando_chegada())
    assert pedido.id not in {item.id for item in service.pedidos_carrinha_aguardando_chegada()}


def test_carrinha_em_atendimento_aparece_na_partida_mas_nao_no_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "18")

    assert carrinha.id in ids(service.carrinhas_aguardando_partida())
    assert pedido.id in {item.id for item in service.pedidos_carrinha_aguardando_partida()}
    assert carrinha.id not in ids(service.carrinhas_aguardando_despejo())


def test_confirmacao_valida_da_partida_muda_filas_para_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "19")

    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida-c2", False, None, ["foto-partida-19"]
    )
    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert persistida.id not in ids(service.carrinhas_aguardando_partida())
    assert persistida.id in ids(service.carrinhas_aguardando_despejo())
    assert persistida.id not in ids(service.contentores_para_recolha())
    assert persistida.id not in ids(service.contentores_para_despejo())


def test_confirmacao_valida_do_despejo_conclui_e_remove_das_filas(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "20")
    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida-c2", False, None, ["foto-partida-20"]
    )

    service.confirmar_despejo_carrinha(
        carrinha.id,
        "Entulho Limpo",
        False,
        None,
        "motorista-despejo-c2",
        ["foto-despejo-20"],
    )
    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value
    assert persistida.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert persistida.id not in ids(service.carrinhas_aguardando_chegada())
    assert persistida.id not in ids(service.carrinhas_aguardando_partida())
    assert persistida.id not in ids(service.carrinhas_aguardando_despejo())


def test_cancelamento_antes_do_commit_da_chegada_nao_persiste_dados_parciais(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha_id = pedido.contentores[0].id

    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "motorista-nao-persistir",
        38.7,
        -9.1,
        "Nao persistir",
        [{"contentor_id": carrinha_id, "numero_adesivo": "21", "fotos": ["foto-nao-persistir"]}],
    )
    db_session.rollback()

    SessionLocal = sessionmaker(bind=db_session.get_bind())
    with SessionLocal() as verificacao:
        persistida = verificacao.get(PedidoContentor, carrinha_id)
        assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
        assert persistida.frota_carrinha is None
        assert persistida.chegada_carrinha_data_hora is None
        assert persistida.chegada_carrinha_latitude is None
        assert persistida.chegada_carrinha_longitude is None
        assert verificacao.query(ContentorFoto).filter_by(
            pedido_contentor_id=carrinha_id, tipo_foto=TipoFoto.ENTREGA.value
        ).count() == 0
