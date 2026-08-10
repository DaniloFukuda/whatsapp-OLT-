"""Isolamento operacional por modalidade no Painel/Opção 5."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.core.config as config_module
from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import Settings, get_settings
from app.models.operador import PerfilOperador


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
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


def preparar_blocos(router, monkeypatch):
    def entregas(_pedidos, _data, _tipo):
        return [(SimpleNamespace(), [SimpleNamespace()])]

    monkeypatch.setattr(router, "_pedidos_por_entrega", entregas)
    monkeypatch.setattr(
        router,
        "_format_entrega_hoje",
        lambda _pedido, _itens, incluir_horario: "chegada-carrinha" if incluir_horario else "entrega-contentor",
    )
    monkeypatch.setattr(router, "_carrinhas_em_atendimento", lambda _pedidos: [SimpleNamespace()])
    monkeypatch.setattr(router, "_format_carrinha_em_atendimento", lambda _item: "carrinha-atendimento")
    monkeypatch.setattr(router, "_contentores_vencendo_amanha", lambda _pedidos, _hoje: [(SimpleNamespace(), [])])
    monkeypatch.setattr(router, "_format_renovacao", lambda _pedido, _itens: "renovacao-moderna")
    monkeypatch.setattr(router, "_format_renovacao_aluguer", lambda _aluguer: "renovacao-legada")
    monkeypatch.setattr(
        router,
        "_contentores_para_recolha_em",
        lambda _pedidos, _data: [(SimpleNamespace(nome_cliente="recolha-moderna"), [])],
    )
    monkeypatch.setattr(
        router,
        "_alugueres_por_vencimento",
        lambda _alugueres, _data: [SimpleNamespace(nome_cliente="recolha-legada")],
    )
    monkeypatch.setattr(router, "_format_carrinha_amanha", lambda _pedido, _itens: "carrinha-amanha")
    monkeypatch.setattr(router, "_contentores_vencidos", lambda _pedidos, _hoje: [(SimpleNamespace(), object(), [])])
    monkeypatch.setattr(router, "_alugueres_vencidos", lambda _alugueres, _hoje: [SimpleNamespace()])
    monkeypatch.setattr(router, "_format_contentor_vencido", lambda *_args: "vencido-moderno")
    monkeypatch.setattr(router, "_format_aluguer_vencido", lambda *_args: "vencido-legado")
    monkeypatch.setattr(router, "_pedidos_pagamento_pendente", lambda _pedidos: [SimpleNamespace()])
    monkeypatch.setattr(router, "_alugueres_pagamento_pendente", lambda _alugueres: [])
    monkeypatch.setattr(router, "_format_pagamento_pendente", lambda _pedido: "pagamento-preservado")
    monkeypatch.setattr(router, "_avarias_ativas", lambda _pedidos: [SimpleNamespace()])
    monkeypatch.setattr(router, "_alugueres_avarias_ativas", lambda _alugueres: [])
    monkeypatch.setattr(router, "_format_avaria", lambda _item: "avaria-preservada")


def render_operacional(router):
    hoje = datetime.now(timezone.utc).date()
    return "\n".join(
        (
            router._painel_v4_acoes_hoje([], [], hoje, PerfilOperador.GESTOR),
            router._painel_v4_proximos_dias([], [], hoje),
            router._painel_v4_pendencias([], [], hoje, PerfilOperador.GESTOR),
        )
    )


@pytest.mark.parametrize(
    "contentores,carrinhas,presentes,ausentes",
    [
        (True, True, ("entrega-contentor", "renovacao-moderna", "renovacao-legada", "recolha-moderna", "recolha-legada", "chegada-carrinha", "carrinha-atendimento", "carrinha-amanha", "vencido-moderno", "vencido-legado"), ()),
        (True, False, ("entrega-contentor", "renovacao-moderna", "renovacao-legada", "recolha-moderna", "recolha-legada", "vencido-moderno", "vencido-legado"), ("chegada-carrinha", "carrinha-atendimento", "carrinha-amanha")),
        (False, True, ("chegada-carrinha", "carrinha-atendimento", "carrinha-amanha"), ("entrega-contentor", "renovacao-moderna", "renovacao-legada", "recolha-moderna", "recolha-legada", "vencido-moderno", "vencido-legado")),
        (False, False, (), ("entrega-contentor", "renovacao-moderna", "renovacao-legada", "recolha-moderna", "recolha-legada", "chegada-carrinha", "carrinha-atendimento", "carrinha-amanha", "vencido-moderno", "vencido-legado")),
    ],
)
def test_quatro_combinacoes_isolam_somente_blocos_operacionais(
    db_session, monkeypatch, contentores, carrinhas, presentes, ausentes
):
    configurar(monkeypatch, contentores=contentores, carrinhas=carrinhas)
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    resposta = render_operacional(router)
    for trecho in presentes:
        assert trecho in resposta
    for trecho in ausentes:
        assert trecho not in resposta
    assert "pagamento-preservado" in resposta
    assert "avaria-preservada" in resposta


def test_off_off_preserva_secoes_e_mensagens_vazias(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    monkeypatch.setattr(router, "_pedidos_pagamento_pendente", lambda _pedidos: [])
    monkeypatch.setattr(router, "_avarias_ativas", lambda _pedidos: [])
    resposta = render_operacional(router)
    assert "1. AÇÕES PARA HOJE" in resposta
    assert "2. AÇÕES AGENDADAS PARA OS PRÓXIMOS DIAS" in resposta
    assert "3. PENDÊNCIAS ATIVAS" in resposta
    assert "Nenhuma ação para hoje" in resposta
    assert "Nenhuma ação agendada" in resposta
    assert "Nenhuma pendência ativa" in resposta


@pytest.mark.parametrize("contentores,carrinhas", [(True, False), (False, True), (False, False)])
def test_flags_nao_filtram_financeiro_contentor_carrinha_ou_totais(
    db_session, monkeypatch, contentores, carrinhas
):
    configurar(monkeypatch, contentores=contentores, carrinhas=carrinhas)
    router = WhatsappRouterAgent(db_session)
    monkeypatch.setattr(
        router,
        "_financeiro_mes",
        lambda _pedidos, _hoje: {
            "contentores_pago": 100,
            "contentores_pendente": 20,
            "carrinhas_pago": 300,
            "carrinhas_pendente": 40,
        },
    )
    resposta = router._painel_v4_financeiro([], [], datetime.now(timezone.utc).date())
    assert "FATURAMENTO CONTENTORES" in resposta
    assert "FATURAMENTO CARRINHAS" in resposta
    assert "Total faturado" in resposta and "400,00 €" in resposta
    assert "Total a receber" in resposta and "60,00 €" in resposta
    assert "Total projetado" in resposta and "460,00 €" in resposta


def test_aluguer_legado_some_da_operacao_mas_permanece_no_financeiro(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    resposta_operacional = render_operacional(router)
    assert "renovacao-legada" not in resposta_operacional
    assert "vencido-legado" not in resposta_operacional
    monkeypatch.setattr(router, "_financeiro_mes", lambda _pedidos, _hoje: {
        "contentores_pago": 0, "contentores_pendente": 0,
        "carrinhas_pago": 0, "carrinhas_pendente": 0,
    })
    legado = SimpleNamespace(criado_em=datetime.now(timezone.utc), pago=True, valor=75)
    resposta_financeira = router._painel_v4_financeiro([], [legado], datetime.now(timezone.utc).date())
    assert "Pago: 75,00 € | Pendente: 0,00 €" in resposta_financeira


@pytest.mark.parametrize("contentores,carrinhas", [(True, False), (False, True), (False, False)])
def test_pagamentos_registro_e_avarias_permanecem_com_modalidade_off(
    db_session, monkeypatch, contentores, carrinhas
):
    configurar(monkeypatch, contentores=contentores, carrinhas=carrinhas, avarias=True)
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    monkeypatch.setattr(router, "_pedidos_v32", lambda: [])
    monkeypatch.setattr(router, "_alugueres_v4", lambda: [])
    monkeypatch.setattr(router, "_painel_v4_financeiro", lambda *_args: "financeiro-preservado")
    monkeypatch.setattr(router.pedido_service, "pedidos_pagamento_pendente", lambda: [SimpleNamespace()])
    resposta = router._painel_v4(PerfilOperador.GESTOR)
    assert "pagamento-preservado" in resposta
    assert "Registrar pagamento pendente" in resposta
    assert "avaria-preservada" in resposta
    assert "financeiro-preservado" in resposta


def test_avarias_off_continua_ocultando_avarias(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False, avarias=False)
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    resposta = render_operacional(router)
    assert "avaria-preservada" not in resposta
    assert "pagamento-preservado" in resposta


def test_funcionario_continua_sem_financeiro(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    router = WhatsappRouterAgent(db_session)
    monkeypatch.setattr(router, "_pedidos_v32", lambda: [])
    monkeypatch.setattr(router, "_alugueres_v4", lambda: [])
    monkeypatch.setattr(router, "_painel_v4_acoes_hoje", lambda *_args: "acoes")
    monkeypatch.setattr(router, "_painel_v4_proximos_dias", lambda *_args: "agenda")
    monkeypatch.setattr(router, "_painel_v4_pendencias", lambda *_args: "pendencias")
    monkeypatch.setattr(router, "_painel_v4_financeiro", lambda *_args: "financeiro-proibido")
    assert "financeiro-proibido" not in router._painel_v4(PerfilOperador.FUNCIONARIO)


def test_flags_nao_modificam_registros(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    router = WhatsappRouterAgent(db_session)
    antes = (db_session.new.copy(), db_session.dirty.copy(), db_session.deleted.copy())
    router._painel_v4(PerfilOperador.GESTOR)
    depois = (db_session.new.copy(), db_session.dirty.copy(), db_session.deleted.copy())
    assert depois == antes


def test_defaults_on_on_preservam_modalidades(db_session, monkeypatch):
    router = WhatsappRouterAgent(db_session)
    preparar_blocos(router, monkeypatch)
    assert get_settings().feature_contentores_enabled is True
    assert get_settings().feature_carrinhas_enabled is True
    resposta = render_operacional(router)
    assert "entrega-contentor" in resposta
    assert "chegada-carrinha" in resposta
