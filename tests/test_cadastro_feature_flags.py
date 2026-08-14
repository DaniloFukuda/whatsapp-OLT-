from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.pedido_v24.router import PedidoV24OperationalRouter
from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import Pedido, PedidoContentor, StatusPagamento, TipoEquipamentoPedido


def _contexto(tipo):
    carrinha = tipo == TipoEquipamentoPedido.CARRINHA.value
    item = {
        "tipo_equipamento": tipo,
        "residuo_contratado": "Entulho Limpo",
        "horario_agendado": "14:00" if carrinha else None,
        "precisa_mao_de_obra": False,
    }
    return {
        "tipo_solicitacao": tipo,
        "quantidade": 1,
        "itens": [item],
        "residuos": ["Entulho Limpo"],
        "nome": f"Cliente {tipo}",
        "telefone": "351912345678",
        "data": datetime(2026, 8, 14, tzinfo=timezone.utc).isoformat(),
        "horario_agendado": "14:00" if carrinha else None,
        "precisa_mao_de_obra": False,
        "valor": "100.00",
        "pago": False,
        "forma": None,
        "endereco": "Rua R4",
        "referencia": None,
    }


def _conversa(db_session, tipo):
    contexto = _contexto(tipo)
    conversa = ConversaWhatsApp(
        telefone=f"r4-{tipo.lower()}",
        estado_atual="v24_cadastro_confirmacao",
        contexto_json=contexto,
    )
    db_session.add(conversa)
    db_session.commit()
    return conversa, contexto


def _confirmar(agent, conversa, contexto, tipo):
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        return agent.confirmar_cadastro_carrinha(conversa, contexto)
    return agent.confirmar_cadastro_contentor(conversa, contexto)


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_flag_on_na_confirmacao_cria_normalmente(db_session, monkeypatch, tipo):
    monkeypatch.setattr(
        "app.agents.pedido_v24_agent.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True, feature_carrinhas_enabled=True),
    )
    agent = PedidoV24Agent(db_session)
    conversa, contexto = _conversa(db_session, tipo)

    resposta = _confirmar(agent, conversa, contexto, tipo)

    assert resposta.startswith("✅ Pedido #")
    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1
    assert db_session.query(Pedido).one().status_pagamento == StatusPagamento.PENDENTE.value
    assert conversa.estado_atual == "idle"


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_flag_off_bloqueia_antes_do_finish_e_preserva_conversa(db_session, monkeypatch, tipo):
    settings = SimpleNamespace(
        feature_contentores_enabled=tipo != TipoEquipamentoPedido.CONTENTOR.value,
        feature_carrinhas_enabled=tipo != TipoEquipamentoPedido.CARRINHA.value,
    )
    monkeypatch.setattr("app.agents.pedido_v24_agent.get_settings", lambda: settings)
    agent = PedidoV24Agent(db_session)
    agent._finish_cadastro = Mock(return_value="não deveria")
    conversa, contexto = _conversa(db_session, tipo)
    contexto_original = dict(contexto)

    resposta = _confirmar(agent, conversa, contexto, tipo)

    assert "temporariamente indisponível" in resposta
    agent._finish_cadastro.assert_not_called()
    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    assert conversa.estado_atual == "v24_cadastro_confirmacao"
    assert conversa.contexto_json == contexto_original
    assert contexto == contexto_original


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_flag_off_on_permite_retry_e_cria_uma_unica_vez(db_session, monkeypatch, tipo):
    settings = SimpleNamespace(feature_contentores_enabled=True, feature_carrinhas_enabled=True)
    setattr(settings, "feature_contentores_enabled" if tipo == "CONTENTOR" else "feature_carrinhas_enabled", False)
    monkeypatch.setattr("app.agents.pedido_v24_agent.get_settings", lambda: settings)
    agent = PedidoV24Agent(db_session)
    conversa, contexto = _conversa(db_session, tipo)

    assert "temporariamente indisponível" in _confirmar(agent, conversa, contexto, tipo)
    setattr(settings, "feature_contentores_enabled" if tipo == "CONTENTOR" else "feature_carrinhas_enabled", True)
    assert _confirmar(agent, conversa, contexto, tipo).startswith("✅ Pedido #")
    assert _confirmar(agent, conversa, contexto, tipo) is None
    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1


@pytest.mark.parametrize(
    "contentor_on,carrinha_on",
    [(False, True), (True, False), (True, True), (False, False)],
)
def test_flags_sao_isoladas_por_boundary(monkeypatch, contentor_on, carrinha_on):
    monkeypatch.setattr(
        "app.agents.pedido_v24_agent.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=contentor_on,
            feature_carrinhas_enabled=carrinha_on,
        ),
    )
    agent = PedidoV24Agent.__new__(PedidoV24Agent)
    agent._finish_cadastro = Mock(return_value="criado")
    conversa = SimpleNamespace(estado_atual="v24_cadastro_confirmacao")

    contentor = agent.confirmar_cadastro_contentor(conversa, _contexto("CONTENTOR"))
    carrinha = agent.confirmar_cadastro_carrinha(conversa, _contexto("CARRINHA"))

    assert (contentor == "criado") is contentor_on
    assert (carrinha == "criado") is carrinha_on
    assert agent._finish_cadastro.call_count == int(contentor_on) + int(carrinha_on)


def _mensagem(texto):
    return NormalizedWhatsAppMessage(telefone="351900000004", tipo="text", texto=texto)


@pytest.mark.parametrize(
    "tipo,choice,transition_type",
    [
        ("CONTENTOR", "corrigir", AdvanceTransition),
        ("CARRINHA", "corrigir", AdvanceTransition),
        ("CONTENTOR", "cancelar", IdleTransition),
        ("CARRINHA", "cancelar", IdleTransition),
    ],
)
def test_corrigir_e_cancelar_continuam_no_router_com_flag_off(
    monkeypatch, tipo, choice, transition_type
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=False,
            feature_carrinhas_enabled=False,
        ),
    )
    backend = Mock()
    backend._corrigir_prompt.return_value = "corrigir"
    backend.apply_operational_transition.side_effect = lambda conversa, decision: decision
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_confirmacao",
        contexto_json=_contexto(tipo),
    )

    decisao = router.handle(conversa, _mensagem(choice))

    assert isinstance(decisao, transition_type)
    if choice == "corrigir":
        assert decisao.next_state == "v24_cadastro_corrigir"
    backend.confirmar_cadastro_contentor.assert_not_called()
    backend.confirmar_cadastro_carrinha.assert_not_called()
