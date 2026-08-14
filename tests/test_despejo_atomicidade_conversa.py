from datetime import datetime, timezone
import pytest

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusOperacionalCarrinha,
    StatusRecolhaPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)


def _cenario(db_session, tipo):
    agent = PedidoV24Agent(db_session)
    item = {
        "tipo_equipamento": tipo,
        "residuo_contratado": "Entulho Limpo",
    }
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        item["horario_agendado"] = "10:00"
    pedido = agent.service.criar(
        nome_cliente="R2",
        telefone_cliente="351900000002",
        data_planejada=datetime.now(timezone.utc),
        valor_global="100",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="teste",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[item],
    )
    ativo = pedido.contentores[0]
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        ativo.status_operacional_carrinha = StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    else:
        ativo.status_recolha = StatusRecolhaPedido.RECOLHIDO.value
        ativo.status_ciclo = StatusCicloPedido.EM_ANDAMENTO.value
    contexto = {
        "pedido_id": pedido.id,
        "contentor_id": ativo.id,
        "fotos_despejo": [f"foto-r2-{tipo.lower()}"],
        "residuo_contratado": "Entulho Limpo",
        "residuo_efetivo": "Entulho Limpo",
        "residuo_assumido": "Entulho Limpo",
        "carga_errada": False,
        "relato_carga": None,
        "residuos_disponiveis": ["Entulho Limpo"],
    }
    conversa = ConversaWhatsApp(
        telefone=f"r2-{tipo.lower()}",
        estado_atual="v24_despejo_confirmacao",
        contexto_json=contexto,
    )
    db_session.add(conversa)
    db_session.commit()
    return agent, conversa, ativo.id, pedido.id


def _confirmar(agent, conversa, tipo):
    contexto = dict(conversa.contexto_json)
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        return agent.confirmar_despejo_carrinha(conversa, contexto)
    return agent.confirm_despejo_contentor(conversa, contexto)


def _assert_sem_persistencia(db_session, conversa_id, ativo_id, pedido_id, tipo):
    db_session.expire_all()
    ativo = db_session.get(PedidoContentor, ativo_id)
    conversa = db_session.get(ConversaWhatsApp, conversa_id)
    assert ativo.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        assert ativo.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert ativo.despejo_data_hora is None
    assert db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=ativo_id, tipo_foto=TipoFoto.DESPEJO.value
    ).count() == 0
    assert agent_cotas(db_session, pedido_id)["Entulho Limpo"]["saldo"] == 1
    assert conversa.estado_atual == "v24_despejo_confirmacao"


def agent_cotas(db_session, pedido_id):
    return PedidoV24Agent(db_session).service.cotas_residuos(pedido_id)


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_falha_entre_despejo_e_conversa_reverte_tudo(db_session, monkeypatch, tipo):
    agent, conversa, ativo_id, pedido_id = _cenario(db_session, tipo)
    conversa_id = conversa.id
    monkeypatch.setattr(
        agent,
        "_idle_sem_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("falha na conversa")),
    )

    with pytest.raises(RuntimeError, match="falha na conversa"):
        _confirmar(agent, conversa, tipo)

    _assert_sem_persistencia(db_session, conversa_id, ativo_id, pedido_id, tipo)


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_falha_no_commit_final_reverte_tudo(db_session, monkeypatch, tipo):
    agent, conversa, ativo_id, pedido_id = _cenario(db_session, tipo)
    conversa_id = conversa.id
    commit_real = db_session.commit
    monkeypatch.setattr(db_session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit final")))

    with pytest.raises(RuntimeError, match="commit final"):
        _confirmar(agent, conversa, tipo)

    monkeypatch.setattr(db_session, "commit", commit_real)
    _assert_sem_persistencia(db_session, conversa_id, ativo_id, pedido_id, tipo)


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_retry_depois_do_rollback_conclui_uma_unica_vez(db_session, monkeypatch, tipo):
    agent, conversa, ativo_id, pedido_id = _cenario(db_session, tipo)
    original = agent._idle_sem_commit
    monkeypatch.setattr(
        agent,
        "_idle_sem_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primeira tentativa")),
    )
    with pytest.raises(RuntimeError):
        _confirmar(agent, conversa, tipo)
    monkeypatch.setattr(agent, "_idle_sem_commit", original)
    db_session.refresh(conversa)

    resposta = _confirmar(agent, conversa, tipo)

    db_session.expire_all()
    ativo = db_session.get(PedidoContentor, ativo_id)
    conversa = db_session.get(ConversaWhatsApp, conversa.id)
    assert "conclu" in resposta.lower()
    assert ativo.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        assert ativo.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value
    assert db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=ativo_id, tipo_foto=TipoFoto.DESPEJO.value
    ).count() == 1
    assert agent_cotas(db_session, pedido_id)["Entulho Limpo"]["saldo"] == 0
    assert conversa.estado_atual == "idle"


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_confirmacao_moderna_faz_um_unico_commit(db_session, monkeypatch, tipo):
    agent, conversa, _, _ = _cenario(db_session, tipo)
    commit_real = db_session.commit
    commits = 0

    def contar_commit():
        nonlocal commits
        commits += 1
        return commit_real()

    monkeypatch.setattr(db_session, "commit", contar_commit)
    _confirmar(agent, conversa, tipo)

    assert commits == 1
