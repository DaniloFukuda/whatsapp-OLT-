from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from app.models.aluguer import ContentorFoto
from app.models.pedido import Pedido, PedidoContentor, StatusEntregaPedido, TipoFoto
from app.services.pedido_service import PedidoService


def criar_pedido(db_session, quantidade=2):
    return PedidoService(db_session).criar(
        nome_cliente="Cliente Entrega Atomica",
        telefone_cliente="351900000000",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="351911000000",
        endereco_aproximado="Rua de Teste",
        ponto_referencia=None,
        residuos=["Entulho Limpo"] * quantidade,
    )


def entregas_para(pedido):
    return [
        {
            "contentor_id": item.id,
            "numero_adesivo": str(100 + indice),
            "fotos": [f"media-entrega-{indice}"],
        }
        for indice, item in enumerate(pedido.contentores, 1)
    ]


def confirmar_transacional(service, pedido, entregas=None, latitude=38.72, longitude=-9.13):
    return service.confirmar_entrega_lote_transacional(
        pedido.id,
        "351911000000",
        latitude,
        longitude,
        "Portao azul",
        entregas if entregas is not None else entregas_para(pedido),
    )


def assert_lote_pendente(db_session, pedido_id, quantidade=2):
    db_session.expire_all()
    itens = db_session.query(PedidoContentor).filter_by(pedido_id=pedido_id).order_by(PedidoContentor.id).all()
    assert len(itens) == quantidade
    assert all(item.status_entrega == StatusEntregaPedido.PENDENTE.value for item in itens)
    assert all(item.numero_adesivo_contentor is None for item in itens)
    assert all(item.entrega_feita_por is None for item in itens)
    assert all(item.entrega_latitude is None and item.entrega_longitude is None for item in itens)
    assert all(item.entrega_data_hora is None for item in itens)
    assert db_session.query(ContentorFoto).count() == 0


def test_wrapper_preserva_entrega_em_lote(db_session):
    """OLT-DELIVERY-002"""
    pedido = criar_pedido(db_session)

    resultado = PedidoService(db_session).confirmar_entrega_lote(
        pedido.id, "351911000000", 38.72, -9.13, "Portao azul", entregas_para(pedido)
    )

    db_session.expire_all()
    itens = db_session.query(PedidoContentor).filter_by(pedido_id=pedido.id).all()
    assert resultado.id == pedido.id
    assert all(item.status_entrega == StatusEntregaPedido.ENTREGUE.value for item in itens)
    assert {item.numero_adesivo_contentor for item in itens} == {"101", "102"}
    assert db_session.query(ContentorFoto).count() == 2


def test_falha_antes_das_mutacoes_nao_altera_lote(db_session):
    """OLT-DELIVERY-003"""
    pedido = criar_pedido(db_session)
    entregas = entregas_para(pedido)
    entregas.pop()

    with pytest.raises(ValueError, match="ativos pendentes mudou"):
        confirmar_transacional(PedidoService(db_session), pedido, entregas)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)


def test_falha_depois_do_primeiro_item_permite_rollback_total(db_session, monkeypatch):
    """OLT-DELIVERY-004"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original = service._aplicar_entrega_item
    chamadas = 0

    def falhar(*args, **kwargs):
        nonlocal chamadas
        chamadas += 1
        original(*args, **kwargs)
        if chamadas == 1:
            raise RuntimeError("falha-depois-primeiro")

    monkeypatch.setattr(service, "_aplicar_entrega_item", falhar)
    with pytest.raises(RuntimeError, match="falha-depois-primeiro"):
        confirmar_transacional(service, pedido)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_falha_durante_fotos_permite_rollback_total(db_session, monkeypatch):
    """OLT-DELIVERY-005"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original_add = db_session.add
    fotos = 0

    def add(instance):
        nonlocal fotos
        original_add(instance)
        if isinstance(instance, ContentorFoto):
            fotos += 1
            if fotos == 2:
                raise RuntimeError("falha-foto")

    monkeypatch.setattr(db_session, "add", add)
    with pytest.raises(RuntimeError, match="falha-foto"):
        confirmar_transacional(service, pedido)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)


def test_falha_depois_das_mutacoes_antes_do_commit_permite_rollback(db_session, monkeypatch):
    """OLT-DELIVERY-006"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original_flush = db_session.flush

    def flush(*args, **kwargs):
        original_flush(*args, **kwargs)
        raise RuntimeError("falha-apos-flush")

    monkeypatch.setattr(db_session, "flush", flush)
    with pytest.raises(RuntimeError, match="falha-apos-flush"):
        confirmar_transacional(service, pedido)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_metodo_transacional_faz_flush_sem_commit(db_session, monkeypatch):
    """OLT-DELIVERY-011"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original_flush = db_session.flush
    flush = Mock(wraps=original_flush)
    commit = Mock()
    monkeypatch.setattr(db_session, "flush", flush)
    monkeypatch.setattr(db_session, "commit", commit)

    resultado = confirmar_transacional(service, pedido)

    assert resultado.id == pedido.id
    flush.assert_called_once_with()
    commit.assert_not_called()


def test_wrapper_executa_exatamente_um_commit(db_session, monkeypatch):
    """OLT-DELIVERY-012"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original_commit = db_session.commit
    commit = Mock(wraps=original_commit)
    monkeypatch.setattr(db_session, "commit", commit)

    service.confirmar_entrega_lote(
        pedido.id, "351911000000", 38.72, -9.13, None, entregas_para(pedido)
    )

    commit.assert_called_once_with()


def test_lote_de_dois_confirma_estado_fotos_gps_e_auditoria(db_session):
    """OLT-DELIVERY-013"""
    pedido = criar_pedido(db_session)
    PedidoService(db_session).confirmar_entrega_lote(
        pedido.id, "351911000000", 38.72, -9.13, "Portao azul", entregas_para(pedido)
    )

    db_session.expire_all()
    itens = db_session.query(PedidoContentor).filter_by(pedido_id=pedido.id).order_by(PedidoContentor.id).all()
    assert len(itens) == 2
    assert all(item.status_entrega == StatusEntregaPedido.ENTREGUE.value for item in itens)
    assert all(item.entrega_feita_por == "351911000000" for item in itens)
    assert all(item.entrega_latitude == 38.72 and item.entrega_longitude == -9.13 for item in itens)
    assert all(item.entrega_ponto_referencia == "Portao azul" for item in itens)
    assert all(item.entrega_data_hora is not None for item in itens)
    fotos = db_session.query(ContentorFoto).order_by(ContentorFoto.id).all()
    assert [foto.pedido_contentor_id for foto in fotos] == [item.id for item in itens]
    assert all(foto.tipo_foto == TipoFoto.ENTREGA.value for foto in fotos)


def test_falha_no_segundo_item_reverte_primeiro_e_segundo(db_session, monkeypatch):
    """OLT-DELIVERY-014"""
    pedido = criar_pedido(db_session)
    service = PedidoService(db_session)
    original = service._aplicar_entrega_item
    chamadas = 0

    def falhar(*args, **kwargs):
        nonlocal chamadas
        chamadas += 1
        original(*args, **kwargs)
        if chamadas == 2:
            raise RuntimeError("falha-segundo")

    monkeypatch.setattr(service, "_aplicar_entrega_item", falhar)
    with pytest.raises(RuntimeError, match="falha-segundo"):
        confirmar_transacional(service, pedido)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(None, -9.13), (38.72, None), (91, -9.13), (38.72, -181)],
)
def test_gps_ausente_ou_invalido_nao_muta(db_session, latitude, longitude):
    """OLT-DELIVERY-019"""
    pedido = criar_pedido(db_session)

    with pytest.raises(ValueError, match="GPS"):
        confirmar_transacional(PedidoService(db_session), pedido, latitude=latitude, longitude=longitude)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)


def test_fotos_ausentes_nao_mutam_lote(db_session):
    """OLT-DELIVERY-020"""
    pedido = criar_pedido(db_session)
    entregas = entregas_para(pedido)
    entregas[0]["fotos"] = []

    with pytest.raises(ValueError, match="pelo menos uma foto"):
        confirmar_transacional(PedidoService(db_session), pedido, entregas)
    db_session.rollback()

    assert_lote_pendente(db_session, pedido.id)
