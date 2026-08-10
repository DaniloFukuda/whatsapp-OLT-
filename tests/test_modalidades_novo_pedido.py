"""Flags de modalidade aplicadas somente ao início do Novo Pedido."""

import pytest

import app.core.config as config_module
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import Settings, get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import Pedido, PedidoContentor, TipoEquipamentoPedido


@pytest.fixture(autouse=True)
def isolated_modalidade_settings(monkeypatch):
    for nome in (
        "FEATURE_CONTENTORES_ENABLED",
        "FEATURE_CARRINHAS_ENABLED",
        "FEATURE_AVARIAS_ENABLED",
    ):
        monkeypatch.delenv(nome, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def configurar(monkeypatch, *, contentores=True, carrinhas=True, avarias=True):
    settings_real = Settings
    monkeypatch.setattr(
        config_module,
        "Settings",
        lambda: settings_real(
            _env_file=None,
            feature_contentores_enabled=contentores,
            feature_carrinhas_enabled=carrinhas,
            feature_avarias_enabled=avarias,
        ),
    )
    get_settings.cache_clear()


def conversa(db_session, *, estado="idle", contexto=None):
    atual = ConversaWhatsApp(
        telefone="351900019900",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto):
    return NormalizedWhatsAppMessage(
        telefone="351900019900",
        tipo="text",
        texto=texto,
        message_id="modalidades-novo-pedido",
    )


def test_on_on_preserva_escolha_atual(db_session, monkeypatch):
    configurar(monkeypatch)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert atual.estado_atual == "v24_cadastro_tipo_solicitacao"
    assert "Contentor" in resposta
    assert "Carrinha" in resposta


def test_on_on_contentor_segue_fluxo_atual(db_session, monkeypatch):
    configurar(monkeypatch)
    atual = conversa(db_session)
    agente = PedidoV24Agent(db_session)
    agente.start_cadastro(atual)

    resposta = agente.handle(atual, mensagem("contentor"))

    assert atual.estado_atual == "v24_cadastro_nome"
    assert atual.contexto_json["tipo_solicitacao"] == TipoEquipamentoPedido.CONTENTOR.value
    assert "nome do cliente" in resposta.lower()


def test_on_on_carrinha_segue_fluxo_atual(db_session, monkeypatch):
    configurar(monkeypatch)
    atual = conversa(db_session)
    agente = PedidoV24Agent(db_session)
    agente.start_cadastro(atual)

    resposta = agente.handle(atual, mensagem("carrinha"))

    assert atual.estado_atual == "v24_cadastro_quantidade"
    assert atual.contexto_json["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value
    assert "carrinhas" in resposta.lower()


def test_on_off_seleciona_contentor_automaticamente(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert atual.estado_atual == "v24_cadastro_nome"
    assert atual.contexto_json["tipo_solicitacao"] == TipoEquipamentoPedido.CONTENTOR.value
    assert "nome do cliente" in resposta.lower()


def test_on_off_nao_oferece_carrinha(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert "Carrinha" not in resposta


def test_off_on_seleciona_carrinha_automaticamente(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert atual.estado_atual == "v24_cadastro_quantidade"
    assert atual.contexto_json["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value
    assert "carrinhas" in resposta.lower()


def test_off_on_nao_oferece_contentor(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert "Contentor" not in resposta


def test_off_off_nao_cria_pedido(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(db_session)

    PedidoV24Agent(db_session).start_cadastro(atual)

    assert db_session.query(Pedido).count() == 0


def test_off_off_nao_cria_item_operacional(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(db_session)

    PedidoV24Agent(db_session).start_cadastro(atual)

    assert db_session.query(PedidoContentor).count() == 0


def test_off_off_reseta_contexto_com_mensagem_controlada(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(db_session, estado="v24_cadastro_nome", contexto={"nome": "parcial"})

    resposta = PedidoV24Agent(db_session).start_cadastro(atual)

    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}
    assert "não há modalidade habilitada" in resposta.lower()


def test_bypass_rejeita_contentor_desabilitado(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    atual = conversa(db_session, estado="v24_cadastro_tipo_solicitacao")

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("contentor"))

    assert atual.estado_atual == "v24_cadastro_tipo_solicitacao"
    assert atual.contexto_json == {}
    assert "não está habilitada" in resposta.lower()
    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0


def test_bypass_rejeita_carrinha_desabilitada(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    atual = conversa(db_session, estado="v24_cadastro_tipo_solicitacao")

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("carrinha"))

    assert atual.estado_atual == "v24_cadastro_tipo_solicitacao"
    assert atual.contexto_json == {}
    assert "não está habilitada" in resposta.lower()
    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0


def test_flags_de_modalidade_nao_interferem_em_avarias(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False, avarias=True)

    PedidoV24Agent(db_session).start_cadastro(conversa(db_session))

    assert get_settings().feature_avarias_enabled is True


def test_defaults_on_on_mantem_comportamento_anterior(db_session, monkeypatch):
    configurar(monkeypatch)

    resposta = PedidoV24Agent(db_session).start_cadastro(conversa(db_session))

    assert "tipo de solicitação" in resposta.lower()
    assert "Contentor" in resposta
    assert "Carrinha" in resposta
