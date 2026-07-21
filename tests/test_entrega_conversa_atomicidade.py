from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import PedidoContentor, StatusEntregaPedido
from app.services.pedido_service import PedidoService


def preparar_confirmacao(db_session, pago=True):
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Conversa Atomica",
        telefone_cliente="351900000000",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=pago,
        forma_pagamento="MBWay" if pago else None,
        pedido_feito_por="351911000000",
        endereco_aproximado="Rua de Teste",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    contexto = {
        "pedido_id": pedido.id,
        "contentores": [item.id for item in pedido.contentores],
        "indice": 2,
        "entregas": [
            {
                "contentor_id": item.id,
                "numero_adesivo": str(200 + index),
                "fotos": [f"foto-conversa-{index}"],
            }
            for index, item in enumerate(pedido.contentores, 1)
        ],
        "latitude": 38.72,
        "longitude": -9.13,
        "referencia_entrega": "Portao azul",
    }
    conversa = ConversaWhatsApp(
        telefone="351911000000",
        estado_atual="v24_entrega_confirmacao",
        contexto_json=contexto,
    )
    db_session.add(conversa)
    db_session.commit()
    db_session.refresh(conversa)
    return pedido, conversa, contexto


def estado_persistido(db_session, pedido_id, conversa_id):
    db_session.expire_all()
    itens = (
        db_session.query(PedidoContentor)
        .filter_by(pedido_id=pedido_id)
        .order_by(PedidoContentor.id)
        .all()
    )
    conversa = db_session.get(ConversaWhatsApp, conversa_id)
    return itens, conversa, db_session.query(ContentorFoto).order_by(ContentorFoto.id).all()


def assert_confirmacao_preservada(db_session, pedido_id, conversa_id, contexto):
    itens, conversa, fotos = estado_persistido(db_session, pedido_id, conversa_id)
    assert all(item.status_entrega == StatusEntregaPedido.PENDENTE.value for item in itens)
    assert all(item.numero_adesivo_contentor is None for item in itens)
    assert fotos == []
    assert conversa.estado_atual == "v24_entrega_confirmacao"
    assert conversa.contexto_json == contexto


def test_sucesso_pago_confirma_entrega_e_idle_em_um_commit(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session, pago=True)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    commit = Mock(wraps=original_commit)
    monkeypatch.setattr(db_session, "commit", commit)

    response = agent._confirmar_entrega_preparada(conversa, contexto)

    commit.assert_called_once_with()
    itens, conversa_db, fotos = estado_persistido(db_session, pedido.id, conversa.id)
    assert "Entrega confirmada com sucesso" in response
    assert all(item.status_entrega == StatusEntregaPedido.ENTREGUE.value for item in itens)
    assert len(fotos) == 2
    assert conversa_db.estado_atual == "idle"
    assert conversa_db.contexto_json == {}


def test_sucesso_pendente_avanca_pagamento_no_mesmo_commit(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session, pago=False)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    commit = Mock(wraps=original_commit)
    monkeypatch.setattr(db_session, "commit", commit)

    response = agent._confirmar_entrega_preparada(conversa, contexto)

    commit.assert_called_once_with()
    itens, conversa_db, fotos = estado_persistido(db_session, pedido.id, conversa.id)
    assert "pagamento no local" in response
    assert all(item.status_entrega == StatusEntregaPedido.ENTREGUE.value for item in itens)
    assert len(fotos) == 2
    assert conversa_db.estado_atual == "v24_entrega_pagou"
    assert conversa_db.contexto_json == {"pedido_id": pedido.id}


def test_value_error_preserva_entrega_conversa_e_contexto(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    rollback = Mock(wraps=db_session.rollback)
    commit = Mock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "rollback", rollback)
    monkeypatch.setattr(db_session, "commit", commit)
    contexto_invalido = {**contexto, "entregas": contexto["entregas"][:-1]}

    response = agent._confirmar_entrega_preparada(conversa, contexto_invalido)

    assert "ativos pendentes mudou" in response.lower()
    rollback.assert_called_once_with()
    commit.assert_not_called()
    assert_confirmacao_preservada(db_session, pedido.id, conversa.id, contexto)


def test_falha_no_flush_reverte_tudo_e_propaga(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    original_flush = db_session.flush

    def flush(*args, **kwargs):
        original_flush(*args, **kwargs)
        raise RuntimeError("falha-flush")

    monkeypatch.setattr(db_session, "flush", flush)
    with pytest.raises(RuntimeError, match="falha-flush"):
        agent._confirmar_entrega_preparada(conversa, contexto)

    assert_confirmacao_preservada(db_session, pedido.id, conversa.id, contexto)
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_falha_ao_preparar_idle_reverte_entrega_e_fotos(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    original = agent._aplicar_idle

    def falhar(conversa_atual):
        original(conversa_atual)
        raise RuntimeError("falha-conversa")

    monkeypatch.setattr(agent, "_aplicar_idle", falhar)
    with pytest.raises(RuntimeError, match="falha-conversa"):
        agent._confirmar_entrega_preparada(conversa, contexto)

    assert_confirmacao_preservada(db_session, pedido.id, conversa.id, contexto)


def test_falha_no_commit_executa_rollback_sem_sucesso(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    rollback = Mock(wraps=db_session.rollback)
    commit = Mock(side_effect=RuntimeError("falha-commit"))
    monkeypatch.setattr(db_session, "rollback", rollback)
    monkeypatch.setattr(db_session, "commit", commit)

    with pytest.raises(RuntimeError, match="falha-commit"):
        agent._confirmar_entrega_preparada(conversa, contexto)

    commit.assert_called_once_with()
    rollback.assert_called_once_with()
    assert_confirmacao_preservada(db_session, pedido.id, conversa.id, contexto)


def test_resposta_so_e_retornada_depois_do_commit(db_session, monkeypatch):
    _, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    ordem = []

    def commit():
        ordem.append("commit")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    response = agent._confirmar_entrega_preparada(conversa, contexto)
    ordem.append("retorno")

    assert response.startswith("✅ Entrega confirmada")
    assert ordem == ["commit", "retorno"]


def test_sessao_reutilizavel_e_retry_sem_fotos_duplicadas(db_session, monkeypatch):
    pedido, conversa, contexto = preparar_confirmacao(db_session)
    agent = PedidoV24Agent(db_session)
    original = agent.service.confirmar_entrega_lote_transacional
    chamadas = 0

    def falhar_uma_vez(*args, **kwargs):
        nonlocal chamadas
        chamadas += 1
        resultado = original(*args, **kwargs)
        if chamadas == 1:
            raise RuntimeError("falha-transitoria")
        return resultado

    monkeypatch.setattr(agent.service, "confirmar_entrega_lote_transacional", falhar_uma_vez)
    with pytest.raises(RuntimeError, match="falha-transitoria"):
        agent._confirmar_entrega_preparada(conversa, contexto)

    assert db_session.execute(text("SELECT 1")).scalar_one() == 1
    response = agent._confirmar_entrega_preparada(conversa, contexto)

    assert "Entrega confirmada com sucesso" in response
    itens, conversa_db, fotos = estado_persistido(db_session, pedido.id, conversa.id)
    assert all(item.status_entrega == StatusEntregaPedido.ENTREGUE.value for item in itens)
    assert conversa_db.estado_atual == "idle"
    assert len(fotos) == 2
    assert {foto.url_midia for foto in fotos} == {"foto-conversa-1", "foto-conversa-2"}
