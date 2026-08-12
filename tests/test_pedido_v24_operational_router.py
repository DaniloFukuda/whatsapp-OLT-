"""Contrato do seam entre WhatsappRouterAgent e PedidoV24Agent."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.agents.pedido_v24.contentor import (
    ConfirmEntregaContentor,
    ContentorOperationalAgent,
    RegistrarPagamentoEntregaContentor,
)
from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24.router import PedidoV24OperationalRouter
from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.operador import PerfilOperador
from app.models.pedido import TipoEquipamentoPedido


@pytest.mark.parametrize(
    "context,expected",
    [
        ({"tipo_solicitacao": "CONTENTOR"}, TipoEquipamentoPedido.CONTENTOR),
        ({"tipo_equipamento": "CARRINHA"}, TipoEquipamentoPedido.CARRINHA),
        ({"item_atual": {"tipo_equipamento": "CONTENTOR"}}, TipoEquipamentoPedido.CONTENTOR),
        ({"itens": [{"tipo_equipamento": "CARRINHA"}]}, TipoEquipamentoPedido.CARRINHA),
        ({}, None),
        ({"contentor_id": 42}, None),
        ({"estado_atual": "v24_recolha_contentor"}, None),
        ({"tipo_solicitacao": "CONTENTOR", "itens": [{"tipo_equipamento": "CARRINHA"}]}, None),
        ({"tipo_solicitacao": "DESCONHECIDO", "itens": [{"tipo_equipamento": "CONTENTOR"}]}, None),
    ],
)
def test_resolve_modalidade_somente_com_evidencia_explicita(context, expected):
    assert resolve_operational_modality(context) is expected


def test_resolver_nao_modifica_contexto():
    context = {
        "tipo_solicitacao": "CONTENTOR",
        "item_atual": {"tipo_equipamento": "CONTENTOR"},
        "itens": [{"tipo_equipamento": "CONTENTOR"}],
    }
    snapshot = {
        "tipo_solicitacao": "CONTENTOR",
        "item_atual": {"tipo_equipamento": "CONTENTOR"},
        "itens": [{"tipo_equipamento": "CONTENTOR"}],
    }

    assert resolve_operational_modality(context) is TipoEquipamentoPedido.CONTENTOR
    assert context == snapshot


@pytest.mark.parametrize(
    "tipo,expected",
    [
        ("CONTENTOR", TipoEquipamentoPedido.CONTENTOR),
        ("CARRINHA", TipoEquipamentoPedido.CARRINHA),
    ],
)
def test_resolver_modalidade_da_opcao_tipificada(tipo, expected):
    context = {
        "ids": [17],
        "operational_options": [
            {"pedido_id": 17, "tipos_equipamento": [tipo]},
        ],
    }

    assert resolve_operational_modality(context, 17) is expected


def test_resolver_contexto_legado_sem_metadado_permanece_indeterminado():
    assert resolve_operational_modality({"ids": [17]}, 17) is None


@pytest.mark.parametrize(
    "options",
    [
        [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}],
        [
            {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            {"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]},
        ],
    ],
)
def test_resolver_opcao_ambigua_permanece_indeterminado(options):
    context = {"ids": [17], "operational_options": options}

    assert resolve_operational_modality(context, 17) is None


def test_operational_router_apenas_expoe_resolucao_sem_mudar_dispatch():
    backend = BackendSpy()
    router = PedidoV24OperationalRouter(backend=backend)
    context = {"tipo_solicitacao": "CARRINHA"}

    assert router.resolve_modality(context) is TipoEquipamentoPedido.CARRINHA
    assert backend.calls == []


class BackendSpy:
    def __init__(self):
        self.calls = []

    def _call(self, method, *args):
        self.calls.append((method, args))
        return f"retorno:{method}"

    def start_cadastro(self, conversa):
        return self._call("start_cadastro", conversa)

    def start_entrega(self, conversa):
        return self._call("start_entrega", conversa)

    def start_recolha(self, conversa):
        return self._call("start_recolha", conversa)

    def start_despejo(self, conversa):
        return self._call("start_despejo", conversa)

    def handle(self, conversa, message):
        return self._call("handle", conversa, message)

    def start(self, operation, conversa):
        return self._call("start", operation, conversa)


@pytest.mark.parametrize(
    "operation,method",
    [
        ("cadastro", "start_cadastro"),
        ("entrega", "start_entrega"),
        ("recolha", "start_recolha"),
        ("despejo", "start_despejo"),
    ],
)
def test_start_mapeia_operacao_e_delega_uma_vez(operation, method):
    backend = BackendSpy()
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = object()

    resposta = router.start(operation, conversa)

    assert resposta == f"retorno:{method}"
    assert backend.calls == [(method, (conversa,))]


def test_start_preserva_excecao_do_backend():
    backend = Mock()
    backend.start_entrega.side_effect = RuntimeError("erro original")
    router = PedidoV24OperationalRouter(backend=backend)

    with pytest.raises(RuntimeError, match="erro original"):
        router.start("entrega", object())


def test_contentor_on_usa_novo_modulo_na_entrada(db_session, monkeypatch):
    backend = PedidoV24Agent(db_session)
    router = PedidoV24OperationalRouter(backend=backend)
    novo_modulo = Mock()
    novo_modulo.start_entrega.return_value = "entrada-contentor"
    router._contentor = novo_modulo
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    conversa = object()

    assert router.start_entrega(conversa) == "entrada-contentor"
    novo_modulo.start_entrega.assert_called_once_with(conversa)


def test_contentor_off_mantem_entrada_no_backend_legado(db_session, monkeypatch):
    backend = BackendSpy()
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    conversa = object()

    assert router.start_entrega(conversa) == "retorno:start_entrega"
    assert backend.calls == [("start_entrega", (conversa,))]
    router._contentor.start_entrega.assert_not_called()


def test_start_rejeita_operacao_invalida_sem_chamar_backend():
    backend = Mock()
    router = PedidoV24OperationalRouter(backend=backend)

    with pytest.raises(ValueError, match="Operação operacional inválida"):
        router.start("inexistente", object())

    backend.assert_not_called()
    assert backend.mock_calls == []


@pytest.mark.parametrize(
    "method",
    ["start_cadastro", "start_entrega", "start_recolha", "start_despejo"],
)
def test_start_delega_uma_vez_com_mesma_conversa_e_retorno(method):
    backend = BackendSpy()
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = object()

    resposta = getattr(router, method)(conversa)

    assert resposta == f"retorno:{method}"
    assert backend.calls == [(method, (conversa,))]


def test_handle_preserva_instancias_argumentos_e_retorno():
    backend = BackendSpy()
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = object()
    message = object()

    resposta = router.handle(conversa, message)

    assert resposta == "retorno:handle"
    assert backend.calls == [("handle", (conversa, message))]


def test_selecao_contentor_inequivoca_usa_modulo_novo(db_session):
    backend = PedidoV24Agent(db_session)
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    router._contentor.select_entrega_pedido.return_value = "selecao-contentor"
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_pedido",
        contexto_json={
            "ids": [17],
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "selecao-contentor"
    router._contentor.select_entrega_pedido.assert_called_once_with(conversa, entrada)


@pytest.mark.parametrize(
    "contexto",
    [
        {"ids": [17], "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"ids": [17]},
        {"ids": [17], "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
    ],
)
def test_selecao_nao_contentor_permanece_no_legado(db_session, contexto):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_pedido", contexto_json=contexto)
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.select_entrega_pedido.assert_not_called()


def test_opcao_invalida_e_estado_posterior_permanecem_no_legado(db_session):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    contexto = {
        "ids": [17],
        "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]}],
    }

    invalida = SimpleNamespace(estado_atual="v24_entrega_pedido", contexto_json=contexto)
    posterior = SimpleNamespace(estado_atual="v24_entrega_adesivo", contexto_json=contexto)
    assert router.handle(invalida, mensagem("9")) == "legado"
    assert router.handle(posterior, mensagem("101")) == "legado"
    assert backend.handle.call_count == 2
    router._contentor.select_entrega_pedido.assert_not_called()


def test_selecao_tipificada_ignora_campos_legados_conflitantes():
    context = {
        "tipo_solicitacao": "CARRINHA",
        "ids": [17],
        "operational_options": [
            {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
        ],
    }

    assert resolve_operational_modality(context, 17) is TipoEquipamentoPedido.CONTENTOR


def test_adapter_advance_preserva_estado_contexto_resposta_e_commit(db_session):
    agent = PedidoV24Agent(db_session)
    agent.db.commit = Mock()
    conversa = SimpleNamespace(estado_atual="anterior", contexto_json={"antigo": True})
    context = {"pedido_id": 17}

    response = agent.apply_operational_transition(
        conversa,
        AdvanceTransition("v24_entrega_foto", context, "Envie a foto."),
    )

    assert response == "Envie a foto."
    assert conversa.estado_atual == "v24_entrega_foto"
    assert conversa.contexto_json is context
    agent.db.commit.assert_called_once_with()


def test_adapter_idle_preserva_limpeza_resposta_e_commit(db_session):
    agent = PedidoV24Agent(db_session)
    agent.db.commit = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_adesivo", contexto_json={"pedido_id": 17})

    response = agent.apply_operational_transition(
        conversa,
        IdleTransition("Operação encerrada."),
    )

    assert response == "Operação encerrada."
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    agent.db.commit.assert_called_once_with()


@pytest.mark.parametrize(
    "transition",
    [
        AdvanceTransition("v24_entrega_foto", {}, "resposta"),
        IdleTransition("resposta"),
    ],
)
def test_adapter_preserva_excecao_do_commit(db_session, transition):
    agent = PedidoV24Agent(db_session)
    agent.db.commit = Mock(side_effect=RuntimeError("commit falhou"))
    conversa = SimpleNamespace(estado_atual="anterior", contexto_json={})

    with pytest.raises(RuntimeError, match="commit falhou"):
        agent.apply_operational_transition(conversa, transition)


def test_decisoes_de_transicao_sao_puras_e_imutaveis():
    advance = AdvanceTransition("proximo", {"chave": "valor"}, "resposta")
    idle = IdleTransition("fim")

    assert advance.next_state == "proximo"
    assert advance.context == {"chave": "valor"}
    assert idle.response == "fim"
    with pytest.raises(AttributeError):
        advance.next_state = "outro"


def test_adesivo_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="aplicada")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition("v24_entrega_foto", {"entregas": []}, "foto")
    router._contentor = Mock()
    router._contentor.decide_entrega_adesivo.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_adesivo",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("101")

    assert router.handle(conversa, entrada) == "aplicada"
    router._contentor.decide_entrega_adesivo.assert_called_once_with(conversa, entrada)
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_adesivo_contentor_invalido_preserva_resposta_sem_aplicar_transicao(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock()
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    router._contentor.decide_entrega_adesivo.return_value = "Informe somente o número visível no contentor."
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_adesivo",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )

    assert "número visível" in router.handle(conversa, mensagem("0"))
    backend.apply_operational_transition.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "contentores": [5]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
    ],
)
def test_adesivo_carrinha_legado_ou_ambiguo_permanece_no_backend(db_session, contexto):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_adesivo", contexto_json=contexto)
    entrada = mensagem("77")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_adesivo.assert_not_called()


def test_decisao_adesivo_valido_preserva_contexto_resposta_e_proximo_estado():
    backend = Mock()
    backend.db.get.return_value = SimpleNamespace(
        tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
        status_entrega="PENDENTE",
    )
    backend.db.query.return_value.filter.return_value.first.return_value = None
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {
        "pedido_id": 17,
        "contentores": [5],
        "indice": 0,
        "entregas": [],
    }
    conversa = SimpleNamespace(contexto_json=original)

    decision = agent.decide_entrega_adesivo(conversa, mensagem("101"))

    assert decision == AdvanceTransition(
        "v24_entrega_foto",
        {
            **original,
            "entregas": [
                {"contentor_id": 5, "numero_adesivo": "101", "fotos": []},
            ],
        },
        "Envie a foto do Contentor 101 posicionado no local.",
    )
    assert original["entregas"] == []
    backend.db.commit.assert_not_called()


def test_decisao_adesivo_ativo_invalido_produz_idle_sem_commit():
    backend = Mock()
    backend.db.get.return_value = None
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(
        contexto_json={"contentores": [5], "indice": 0, "entregas": []},
    )

    assert agent.decide_entrega_adesivo(conversa, mensagem("101")) == IdleTransition(
        "Esse ativo já não está pendente. Reinicie a entrega."
    )
    backend.db.commit.assert_not_called()


def test_estado_de_foto_continua_integralmente_legado(db_session):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-foto")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_foto", contexto_json={})
    entrada = mensagem("foto")

    assert router.handle(conversa, entrada) == "legado-foto"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.assert_not_called()


def test_foto_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="foto-aplicada")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition("v24_entrega_foto_acao", {"entregas": []}, "ação")
    router._contentor = Mock()
    router._contentor.decide_entrega_foto.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_foto",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = SimpleNamespace(tipo="image", media_id="foto-101", filename=None, message_id="m1")

    assert router.handle(conversa, entrada) == "foto-aplicada"
    router._contentor.decide_entrega_foto.assert_called_once_with(conversa, entrada)
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_decisao_foto_valida_preserva_contexto_resposta_e_proximo_estado():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {
        "pedido_id": 17,
        "entregas": [
            {"contentor_id": 5, "numero_adesivo": "101", "fotos": []},
        ],
    }
    conversa = SimpleNamespace(contexto_json=original)
    entrada = SimpleNamespace(tipo="image", media_id="foto-101", filename=None, message_id="m1")

    decision = agent.decide_entrega_foto(conversa, entrada)

    assert decision == AdvanceTransition(
        "v24_entrega_foto_acao",
        {
            **original,
            "entregas": [
                {
                    "contentor_id": 5,
                    "numero_adesivo": "101",
                    "fotos": ["foto-101"],
                },
            ],
        },
        "Foto guardada. O que deseja fazer?\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
    )
    assert original["entregas"][0]["fotos"] == []
    backend.db.commit.assert_not_called()


def test_foto_invalida_preserva_resposta_sem_transicao_ou_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"entregas": []})
    entrada = SimpleNamespace(tipo="text", media_id=None, filename=None, message_id="m1")

    assert agent.decide_entrega_foto(conversa, entrada) == "Envie uma imagem para continuar."
    backend.apply_operational_transition.assert_not_called()
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "entregas": [{"contentor_id": 5}]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
    ],
)
def test_foto_carrinha_legado_ou_ambiguo_permanece_no_backend(db_session, contexto):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_foto", contexto_json=contexto)
    entrada = SimpleNamespace(tipo="image", media_id="foto", filename=None, message_id="m1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_foto.assert_not_called()


def test_estado_foto_acao_continua_integralmente_legado(db_session):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-acao")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_foto_acao", contexto_json={})
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado-acao"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.assert_not_called()


def test_foto_acao_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="acao-aplicada")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition("v24_entrega_foto", {"indice": 0}, "outra")
    router._contentor = Mock()
    router._contentor.decide_entrega_foto_acao.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_foto_acao",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "acao-aplicada"
    router._contentor.decide_entrega_foto_acao.assert_called_once_with(conversa, entrada)
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_foto_acao_outra_foto_preserva_contexto_resposta_e_estado():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    context = {"pedido_id": 17, "contentores": [5], "indice": 0, "entregas": []}
    conversa = SimpleNamespace(contexto_json=context)

    decision = agent.decide_entrega_foto_acao(conversa, mensagem("Outra Foto"))

    assert decision == AdvanceTransition(
        "v24_entrega_foto",
        context,
        "Envie a próxima foto deste contentor.",
    )
    assert conversa.contexto_json is context
    backend.db.commit.assert_not_called()


def test_foto_acao_continuar_com_proximo_contentor_preserva_transicao():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    context = {"pedido_id": 17, "contentores": [5, 6], "indice": 0, "entregas": []}
    conversa = SimpleNamespace(contexto_json=context)

    decision = agent.decide_entrega_foto_acao(conversa, mensagem("Próximo Passo"))

    assert decision == AdvanceTransition(
        "v24_entrega_adesivo",
        {**context, "indice": 1},
        "Contentor 1 de 2 registrado.\n\nVamos registrar o próximo.\n\n"
        "Digite o número do contentor que está a descarregar agora:",
    )
    assert context["indice"] == 0
    backend.db.commit.assert_not_called()


def test_foto_acao_continuar_ultimo_contentor_preserva_transicao_gps():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    context = {"pedido_id": 17, "contentores": [5], "indice": 0, "entregas": []}
    conversa = SimpleNamespace(contexto_json=context)

    decision = agent.decide_entrega_foto_acao(conversa, mensagem("2"))

    assert decision == AdvanceTransition(
        "v24_entrega_gps",
        {**context, "indice": 1},
        "Contentor 1 de 1 registrado.\n\nCompartilhe a localização GPS da obra.",
    )
    assert context["indice"] == 0
    backend.db.commit.assert_not_called()


def test_foto_acao_invalida_preserva_resposta_sem_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"indice": 0, "contentores": [5]})

    assert agent.decide_entrega_foto_acao(conversa, mensagem("9")) == (
        "Selecione Outra Foto ou Próximo Passo."
    )
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "indice": 0, "contentores": [5]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
    ],
)
def test_foto_acao_carrinha_legado_ou_ambiguo_permanece_no_backend(db_session, contexto):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_foto_acao", contexto_json=contexto)
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_foto_acao.assert_not_called()


def test_estado_gps_continua_integralmente_legado(db_session):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-gps")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_gps", contexto_json={})
    entrada = mensagem("localização")

    assert router.handle(conversa, entrada) == "legado-gps"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.assert_not_called()


def test_gps_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="gps-aplicado")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition(
        "v24_entrega_referencia_opcao",
        {"latitude": 38.7, "longitude": -9.1},
        "referência",
    )
    router._contentor = Mock()
    router._contentor.decide_entrega_gps.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_gps",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = SimpleNamespace(
        tipo="location", texto=None, latitude=38.7, longitude=-9.1
    )

    assert router.handle(conversa, entrada) == "gps-aplicado"
    router._contentor.decide_entrega_gps.assert_called_once_with(conversa, entrada)
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_decisao_gps_nativo_preserva_coordenadas_contexto_resposta_e_estado():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {"pedido_id": 17, "entregas": []}
    conversa = SimpleNamespace(contexto_json=original)
    entrada = SimpleNamespace(
        tipo="location", texto=None, latitude="38.7001", longitude="-9.1002"
    )

    decision = agent.decide_entrega_gps(conversa, entrada)

    assert decision == AdvanceTransition(
        "v24_entrega_referencia_opcao",
        {**original, "latitude": 38.7001, "longitude": -9.1002},
        "Deseja informar algum ponto de referência para a entrega?\n\n1. Sim\n2. Não",
    )
    assert original == {"pedido_id": 17, "entregas": []}
    backend.db.commit.assert_not_called()


def test_decisao_gps_location_com_texto_preserva_parser_legado():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})
    entrada = SimpleNamespace(
        tipo="location",
        texto="https://maps.google.com/?q=38.7001,-9.1002",
        latitude=None,
        longitude=None,
    )

    decision = agent.decide_entrega_gps(conversa, entrada)

    assert decision.context["latitude"] == 38.7001
    assert decision.context["longitude"] == -9.1002
    assert decision.next_state == "v24_entrega_referencia_opcao"


def test_gps_invalido_preserva_resposta_sem_transicao_ou_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})
    entrada = SimpleNamespace(
        tipo="text", texto="38.7001,-9.1002", latitude=None, longitude=None
    )

    assert agent.decide_entrega_gps(conversa, entrada) == (
        "Compartilhe a localização nativa do WhatsApp para confirmar a entrega."
    )
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "entregas": [{"contentor_id": 5}]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
    ],
)
def test_gps_carrinha_legado_ou_ambiguo_permanece_no_backend(db_session, contexto):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_entrega_gps", contexto_json=contexto)
    entrada = SimpleNamespace(
        tipo="location", texto=None, latitude=38.7, longitude=-9.1
    )

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_gps.assert_not_called()


def test_estado_referencia_opcao_continua_integralmente_legado(db_session):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-referencia")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_referencia_opcao", contexto_json={}
    )
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado-referencia"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.assert_not_called()


def test_adapter_prompt_confirmacao_delega_mesmo_contexto_e_retorno(db_session):
    agent = PedidoV24Agent(db_session)
    context = {"pedido_id": 17, "entregas": []}
    agent._entrega_confirmacao_prompt = Mock(return_value="prompt legado")

    assert agent.entrega_confirmacao_prompt(context) == "prompt legado"
    agent._entrega_confirmacao_prompt.assert_called_once_with(context)


def test_adapter_prompt_confirmacao_preserva_consultas_contexto_e_commit(db_session):
    agent = PedidoV24Agent(db_session)
    agent.service.get = Mock(
        return_value=SimpleNamespace(nome_cliente="Cliente", status_pagamento="PAGO")
    )
    contentor = SimpleNamespace(
        id=5,
        tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
        numero_adesivo_contentor=None,
    )
    agent.db.get = Mock(return_value=contentor)
    agent.db.commit = Mock()
    context = {
        "pedido_id": 17,
        "entregas": [
            {"contentor_id": 5, "numero_adesivo": "101", "fotos": ["foto"]},
        ],
        "referencia_entrega": None,
    }
    snapshot = {
        "pedido_id": 17,
        "entregas": [
            {"contentor_id": 5, "numero_adesivo": "101", "fotos": ["foto"]},
        ],
        "referencia_entrega": None,
    }

    response = agent.entrega_confirmacao_prompt(context)

    assert response == (
        "Confirme a entrega preparada:\n\nCliente: Cliente\nPedido: #17\n"
        "Quantidade de ativos: 1\n1. 📦 Contentor 5 | identificação: 101 | fotos: 1\n"
        "Ponto de referência: sem referência\nPagamento: pago\n\n"
        "1. ✅ Confirmar entrega\n2. ❌ Cancelar"
    )
    agent.service.get.assert_called_once_with(17)
    assert agent.db.get.call_count == 2
    assert context == snapshot
    agent.db.commit.assert_not_called()


def test_adapter_prompt_confirmacao_preserva_excecao(db_session):
    agent = PedidoV24Agent(db_session)
    context = {"pedido_id": 17}
    agent._entrega_confirmacao_prompt = Mock(side_effect=RuntimeError("consulta falhou"))

    with pytest.raises(RuntimeError, match="consulta falhou"):
        agent.entrega_confirmacao_prompt(context)


def test_router_delega_prompt_confirmacao_sem_alterar_argumento():
    backend = Mock()
    backend.entrega_confirmacao_prompt.return_value = "prompt"
    router = PedidoV24OperationalRouter(backend=backend)
    context = {"pedido_id": 17}

    assert router.entrega_confirmacao_prompt(context) == "prompt"
    backend.entrega_confirmacao_prompt.assert_called_once_with(context)


def test_referencia_opcao_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="referencia-aplicada")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition(
        "v24_entrega_referencia",
        {"pedido_id": 17},
        "Digite o ponto de referência.",
    )
    router._contentor = Mock()
    router._contentor.decide_entrega_referencia_opcao.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_referencia_opcao",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "referencia-aplicada"
    router._contentor.decide_entrega_referencia_opcao.assert_called_once_with(
        conversa, entrada
    )
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_referencia_opcao_adicionar_preserva_estado_resposta_contexto_sem_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    context = {"pedido_id": 17, "entregas": [{"contentor_id": 5}]}
    conversa = SimpleNamespace(contexto_json=context)

    decision = agent.decide_entrega_referencia_opcao(conversa, mensagem("Sim"))

    assert decision == AdvanceTransition(
        "v24_entrega_referencia",
        context,
        "Digite o ponto de referência.",
    )
    assert conversa.contexto_json == context
    backend.entrega_confirmacao_prompt.assert_not_called()
    backend.db.commit.assert_not_called()


def test_referencia_opcao_sem_referencia_usa_seam_e_preserva_contexto_sem_commit():
    backend = Mock()
    backend.entrega_confirmacao_prompt.return_value = "prompt legado"
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {"pedido_id": 17, "entregas": [{"contentor_id": 5}]}
    conversa = SimpleNamespace(contexto_json=original)

    decision = agent.decide_entrega_referencia_opcao(conversa, mensagem("Não"))

    expected_context = {**original, "referencia_entrega": None}
    assert decision == AdvanceTransition(
        "v24_entrega_confirmacao",
        expected_context,
        "prompt legado",
    )
    backend.entrega_confirmacao_prompt.assert_called_once_with(expected_context)
    assert conversa.contexto_json == original
    backend.db.commit.assert_not_called()


def test_referencia_opcao_invalida_preserva_resposta_sem_prompt_ou_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})

    assert agent.decide_entrega_referencia_opcao(conversa, mensagem("9")) == (
        "Selecione Sim ou Não."
    )
    backend.entrega_confirmacao_prompt.assert_not_called()
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "entregas": [{"contentor_id": 5}]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
        {"pedido_id": 17, "tipo_solicitacao": "CONTENTOR", "tipo_equipamento": "CARRINHA"},
    ],
)
def test_referencia_opcao_carrinha_legado_ou_ambiguo_permanece_no_backend(
    db_session, contexto
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-referencia")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_referencia_opcao", contexto_json=contexto
    )
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado-referencia"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_referencia_opcao.assert_not_called()


def test_referencia_contentor_inequivoco_usa_modulo_e_adapter(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="referencia-aplicada")
    router = PedidoV24OperationalRouter(backend=backend)
    decision = AdvanceTransition(
        "v24_entrega_confirmacao",
        {"pedido_id": 17, "referencia_entrega": "Portão azul"},
        "prompt legado",
    )
    router._contentor = Mock()
    router._contentor.decide_entrega_referencia.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_referencia",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("Portão azul")

    assert router.handle(conversa, entrada) == "referencia-aplicada"
    router._contentor.decide_entrega_referencia.assert_called_once_with(
        conversa, entrada
    )
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_referencia_valida_preserva_contexto_resposta_estado_seam_e_sem_commit():
    backend = Mock()
    backend.entrega_confirmacao_prompt.return_value = "prompt legado"
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {"pedido_id": 17, "entregas": [{"contentor_id": 5}]}
    conversa = SimpleNamespace(contexto_json=original)

    decision = agent.decide_entrega_referencia(
        conversa, mensagem("  Portão azul  ")
    )

    expected_context = {**original, "referencia_entrega": "Portão azul"}
    assert decision == AdvanceTransition(
        "v24_entrega_confirmacao",
        expected_context,
        "prompt legado",
    )
    backend.entrega_confirmacao_prompt.assert_called_once_with(expected_context)
    assert conversa.contexto_json == original
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize("texto", ["", " " * 3, "x" * 51])
def test_referencia_invalida_preserva_resposta_sem_prompt_ou_commit(texto):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})

    assert agent.decide_entrega_referencia(conversa, mensagem(texto)) == (
        "O ponto de referência deve ter no máximo 50 caracteres."
    )
    backend.entrega_confirmacao_prompt.assert_not_called()
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "referencia_entrega": None},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
        {"pedido_id": 17, "tipo_solicitacao": "CONTENTOR", "tipo_equipamento": "CARRINHA"},
    ],
)
def test_referencia_carrinha_legado_ou_ambiguo_permanece_no_backend(
    db_session, contexto
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-referencia")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_referencia", contexto_json=contexto
    )
    entrada = mensagem("Portão azul")

    assert router.handle(conversa, entrada) == "legado-referencia"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_referencia.assert_not_called()


def test_confirmacao_contentor_inequivoco_usa_modulo_e_boundary(db_session):
    backend = PedidoV24Agent(db_session)
    backend.confirm_entrega_contentor = Mock(return_value="confirmada")
    router = PedidoV24OperationalRouter(backend=backend)
    context = {
        "pedido_id": 17,
        "operational_options": [
            {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
        ],
    }
    command = ConfirmEntregaContentor(context)
    router._contentor = Mock()
    router._contentor.decide_entrega_confirmacao.return_value = command
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_confirmacao", contexto_json=context
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "confirmada"
    router._contentor.decide_entrega_confirmacao.assert_called_once_with(
        conversa, entrada
    )
    backend.confirm_entrega_contentor.assert_called_once_with(conversa, context)


@pytest.mark.parametrize(
    "texto",
    ["1", "confirmar entrega", "✅ confirmar entrega"],
)
def test_decisao_confirmar_contentor_preserva_comando_e_contexto(texto):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    context = {"pedido_id": 17, "entregas": [{"contentor_id": 5}]}
    conversa = SimpleNamespace(contexto_json=context)

    decision = agent.decide_entrega_confirmacao(conversa, mensagem(texto))

    assert decision == ConfirmEntregaContentor(context)
    assert decision.context is not context
    assert conversa.contexto_json == context
    backend.assert_not_called()
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize("texto", ["2", "cancelar", "❌ cancelar"])
def test_decisao_cancelar_contentor_preserva_idle_sem_commit(texto):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})

    assert agent.decide_entrega_confirmacao(conversa, mensagem(texto)) == (
        IdleTransition("Entrega cancelada. Nenhum ativo foi marcado como entregue.")
    )
    backend.assert_not_called()
    backend.db.commit.assert_not_called()


def test_decisao_confirmacao_invalida_preserva_resposta_sem_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})

    assert agent.decide_entrega_confirmacao(conversa, mensagem("3")) == (
        "Escolha Confirmar entrega ou Cancelar."
    )
    backend.assert_not_called()
    backend.db.commit.assert_not_called()


def test_cancelamento_contentor_usa_adapter_idle_e_um_commit(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="cancelada")
    backend.confirm_entrega_contentor = Mock()
    router = PedidoV24OperationalRouter(backend=backend)
    decision = IdleTransition(
        "Entrega cancelada. Nenhum ativo foi marcado como entregue."
    )
    router._contentor = Mock()
    router._contentor.decide_entrega_confirmacao.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_confirmacao",
        contexto_json={
            "pedido_id": 17,
            "operational_options": [
                {"pedido_id": 17, "tipos_equipamento": ["CONTENTOR"]},
            ],
        },
    )
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "cancelada"
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)
    backend.confirm_entrega_contentor.assert_not_called()


def test_boundary_contentor_reutiliza_confirmacao_transacional_legada(db_session):
    backend = PedidoV24Agent(db_session)
    backend._confirmar_entrega_preparada = Mock(return_value="confirmada")
    conversa = SimpleNamespace(estado_atual="v24_entrega_confirmacao")
    context = {"pedido_id": 17}

    assert backend.confirm_entrega_contentor(conversa, context) == "confirmada"
    backend._confirmar_entrega_preparada.assert_called_once_with(
        conversa,
        context,
        expected_tipo=TipoEquipamentoPedido.CONTENTOR.value,
    )


@pytest.mark.parametrize("erro", [ValueError("inválida"), RuntimeError("falha")])
def test_boundary_contentor_preserva_excecao_da_confirmacao_legada(
    db_session, erro
):
    backend = PedidoV24Agent(db_session)
    backend._confirmar_entrega_preparada = Mock(side_effect=erro)

    with pytest.raises(type(erro), match=str(erro)):
        backend.confirm_entrega_contentor(
            SimpleNamespace(estado_atual="v24_entrega_confirmacao"),
            {"pedido_id": 17},
        )


@pytest.mark.parametrize(
    "contexto",
    [
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CARRINHA"]}]},
        {"pedido_id": 17, "entregas": [{"contentor_id": 5}]},
        {"pedido_id": 17, "operational_options": [{"pedido_id": 17, "tipos_equipamento": ["CONTENTOR", "CARRINHA"]}]},
        {"pedido_id": 17, "tipo_solicitacao": "CONTENTOR", "tipo_equipamento": "CARRINHA"},
    ],
)
def test_confirmacao_carrinha_legado_ou_ambiguo_permanece_no_backend(
    db_session, contexto
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado-confirmacao")
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_confirmacao", contexto_json=contexto
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado-confirmacao"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_confirmacao.assert_not_called()


@pytest.mark.parametrize(
    "entrada,transition_type,next_state,response",
    [
        ("1", AdvanceTransition, "v24_entrega_forma", "Selecione a forma recebida:"),
        ("sim", AdvanceTransition, "v24_entrega_forma", "Selecione a forma recebida:"),
        ("✅ Sim, foi pago", AdvanceTransition, "v24_entrega_forma", "Selecione a forma recebida:"),
        ("2", IdleTransition, None, "Pagamento permanece pendente."),
        ("não", IdleTransition, None, "Pagamento permanece pendente."),
        ("🕒 Não, continua pendente", IdleTransition, None, "Pagamento permanece pendente."),
    ],
)
def test_contentor_decide_entrega_pagou_sem_mutacao_financeira(
    entrada, transition_type, next_state, response
):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {"pedido_id": 17}
    conversa = SimpleNamespace(contexto_json=contexto)

    decision = agent.decide_entrega_pagou(conversa, mensagem(entrada))

    assert isinstance(decision, transition_type)
    assert getattr(decision, "next_state", None) == next_state
    assert response in decision.response
    assert contexto == {"pedido_id": 17}
    backend.assert_not_called()
    assert not hasattr(agent, "service")


def test_contentor_entrega_pagou_invalido_preserva_estado_e_mensagem():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_pagou",
        contexto_json={"pedido_id": 17},
    )

    assert agent.decide_entrega_pagou(conversa, mensagem("talvez")) == "Selecione Sim ou Não."
    assert conversa.estado_atual == "v24_entrega_pagou"
    assert conversa.contexto_json == {"pedido_id": 17}


@pytest.mark.parametrize(
    "tipos,expected",
    [
        (["CONTENTOR"], TipoEquipamentoPedido.CONTENTOR),
        (["CARRINHA"], TipoEquipamentoPedido.CARRINHA),
        (["CONTENTOR", "CARRINHA"], None),
        (["DESCONHECIDO"], None),
        ([], None),
    ],
)
def test_adapter_pagamento_resolve_itens_persistidos_sem_escrita(tipos, expected):
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.service = Mock()
    backend.service.get.return_value = SimpleNamespace(
        contentores=[SimpleNamespace(tipo_equipamento=tipo) for tipo in tipos]
    )
    contexto = {"pedido_id": 17}

    assert backend.resolve_entrega_pagamento_modality(contexto) is expected
    assert contexto == {"pedido_id": 17}
    backend.service.get.assert_called_once_with(17)
    backend.service.registrar_pagamento.assert_not_called()
    backend.service.db.commit.assert_not_called()


def test_pagou_contentor_comprovado_usa_modulo_e_transition_seam():
    backend = Mock()
    backend.resolve_entrega_pagamento_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decision = AdvanceTransition("v24_entrega_forma", {"pedido_id": 17}, "forma")
    router._contentor.decide_entrega_pagou.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_pagou", contexto_json={"pedido_id": 17}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "aplicada"
    router._contentor.decide_entrega_pagou.assert_called_once_with(conversa, entrada)
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)
    backend.handle.assert_not_called()
    backend.registrar_pagamento.assert_not_called()


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
def test_pagou_carrinha_ou_indeterminado_permanece_legado(modality):
    backend = Mock()
    backend.resolve_entrega_pagamento_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_pagou", contexto_json={"pedido_id": 17}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_pagou.assert_not_called()


@pytest.mark.parametrize(
    "entrada,forma",
    [
        ("1", "MBWay"),
        ("mbway", "MBWay"),
        ("2", "Transferência"),
        ("transferência", "Transferência"),
        ("3", "Dinheiro"),
        ("dinheiro", "Dinheiro"),
    ],
)
def test_contentor_decide_formas_diretas_sem_acessar_backend(entrada, forma):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})

    decision = agent.decide_entrega_forma(conversa, mensagem(entrada))

    assert decision == RegistrarPagamentoEntregaContentor(
        {"pedido_id": 17}, forma
    )
    backend.assert_not_called()
    assert not hasattr(agent, "service")


@pytest.mark.parametrize("entrada", ["4", "outro"])
def test_contentor_forma_outro_avanca_para_texto_livre(entrada):
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_entrega_forma(
        SimpleNamespace(contexto_json={"pedido_id": 17}),
        mensagem(entrada),
    )

    assert isinstance(decision, AdvanceTransition)
    assert decision.next_state == "v24_entrega_forma_outro"
    assert decision.response == "Qual foi a forma recebida?"


def test_contentor_forma_invalida_preserva_resposta_e_estado():
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_forma", contexto_json={"pedido_id": 17}
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_entrega_forma(conversa, mensagem("bitcoin")) == (
        "Selecione uma forma de pagamento."
    )
    assert conversa.estado_atual == "v24_entrega_forma"


def test_contentor_forma_outro_preserva_strip_e_limite_de_80():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    conversa = SimpleNamespace(contexto_json={"pedido_id": 17})
    texto = "  " + "x" * 81 + "  "

    decision = agent.decide_entrega_forma_outro(conversa, mensagem(texto))

    assert decision == RegistrarPagamentoEntregaContentor(
        {"pedido_id": 17}, "x" * 80
    )


def test_contentor_forma_outro_vazia_preserva_resposta_e_estado():
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_forma_outro",
        contexto_json={"pedido_id": 17},
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_entrega_forma_outro(conversa, mensagem("   ")) == (
        "Informe a forma recebida."
    )
    assert conversa.estado_atual == "v24_entrega_forma_outro"


@pytest.mark.parametrize(
    "estado,method_name",
    [
        ("v24_entrega_forma", "decide_entrega_forma"),
        ("v24_entrega_forma_outro", "decide_entrega_forma_outro"),
    ],
)
def test_forma_contentor_comprovado_usa_modulo_e_boundary(estado, method_name):
    backend = Mock()
    backend.resolve_entrega_pagamento_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.registrar_pagamento_entrega_contentor.return_value = "registrado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decision = RegistrarPagamentoEntregaContentor({"pedido_id": 17}, "Dinheiro")
    getattr(router._contentor, method_name).return_value = decision
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={"pedido_id": 17})
    entrada = mensagem("3")

    assert router.handle(conversa, entrada) == "registrado"
    getattr(router._contentor, method_name).assert_called_once_with(conversa, entrada)
    backend.registrar_pagamento_entrega_contentor.assert_called_once_with(
        conversa, {"pedido_id": 17}, "Dinheiro"
    )
    backend.handle.assert_not_called()


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
@pytest.mark.parametrize("estado", ["v24_entrega_forma", "v24_entrega_forma_outro"])
def test_formas_carrinha_misto_ou_indeterminado_permanecem_legado(
    modality, estado
):
    backend = Mock()
    backend.resolve_entrega_pagamento_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={"pedido_id": 17})
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_entrega_forma.assert_not_called()
    router._contentor.decide_entrega_forma_outro.assert_not_called()


def test_boundary_pagamento_contentor_preserva_registro_e_commit_do_idle():
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.service = Mock()
    backend.db = Mock()
    calls = Mock()
    backend.db.commit = calls.commit
    pedido = SimpleNamespace(status_pagamento="PENDENTE", forma_pagamento=None)

    def registrar(pedido_id, forma):
        pedido.status_pagamento = "PAGO"
        pedido.forma_pagamento = forma
        calls.commit()

    backend.service.registrar_pagamento = calls.registrar_pagamento
    calls.registrar_pagamento.side_effect = registrar
    backend.resolve_entrega_pagamento_modality = Mock(
        return_value=TipoEquipamentoPedido.CONTENTOR
    )
    conversa = SimpleNamespace(
        estado_atual="v24_entrega_forma", contexto_json={"pedido_id": 17}
    )

    response = backend.registrar_pagamento_entrega_contentor(
        conversa, {"pedido_id": 17}, "MBWay"
    )

    backend.service.registrar_pagamento.assert_called_once_with(17, "MBWay")
    assert calls.mock_calls == [
        call.registrar_pagamento(17, "MBWay"),
        call.commit(),
        call.commit(),
    ]
    assert pedido.status_pagamento == "PAGO"
    assert pedido.forma_pagamento == "MBWay"
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert response == (
        "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado."
    )


def test_boundary_pagamento_contentor_recusa_modalidade_nao_comprovada():
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.service = Mock()
    backend.db = Mock()
    backend.resolve_entrega_pagamento_modality = Mock(return_value=None)

    with pytest.raises(ValueError, match="aceita apenas contentores"):
        backend.registrar_pagamento_entrega_contentor(
            SimpleNamespace(), {"pedido_id": 17}, "MBWay"
        )

    backend.service.registrar_pagamento.assert_not_called()
    backend.db.commit.assert_not_called()


def test_excecao_do_backend_nao_e_convertida():
    backend = Mock()
    backend.start_cadastro.side_effect = RuntimeError("erro original")
    router = PedidoV24OperationalRouter(backend=backend)

    with pytest.raises(RuntimeError, match="erro original"):
        router.start_cadastro(object())


def test_router_nao_altera_estado_ou_contexto():
    conversa = SimpleNamespace(estado_atual="v24_estado", contexto_json={"chave": "valor"})
    backend = Mock()
    backend.handle.return_value = "inalterado"
    router = PedidoV24OperationalRouter(backend=backend)

    assert router.handle(conversa, object()) == "inalterado"
    assert conversa.estado_atual == "v24_estado"
    assert conversa.contexto_json == {"chave": "valor"}


def mensagem(texto, telefone="351900077700"):
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="text",
        texto=texto,
        message_id=f"seam-{texto}",
    )


@pytest.fixture
def whatsapp_router(db_session, monkeypatch):
    router = WhatsappRouterAgent(db_session)
    monkeypatch.setattr(
        router.operador_service,
        "decidir_acesso",
        lambda _telefone: SimpleNamespace(
            autorizado=True,
            perfil=PerfilOperador.GESTOR,
            operador=None,
        ),
    )
    spy = BackendSpy()
    router.pedido_v24_router = spy
    return router, spy


@pytest.mark.parametrize(
    "opcao,method",
    [
        ("1", "start_cadastro"),
        ("2", "start_entrega"),
        ("3", "start_recolha"),
        ("4", "start_despejo"),
    ],
)
def test_opcoes_um_a_quatro_passam_uma_vez_pelo_seam(
    whatsapp_router, monkeypatch, opcao, method
):
    router, spy = whatsapp_router
    if opcao == "2":
        monkeypatch.setattr(router.pedido_service, "pedidos_pendentes_entrega", lambda: [object()])
        monkeypatch.setattr(router.pedido_service, "carrinhas_aguardando_chegada", lambda: [])
    if opcao == "3":
        monkeypatch.setattr(router.pedido_service, "contentores_para_recolha", lambda: [object()])
        monkeypatch.setattr(router.pedido_service, "carrinhas_aguardando_partida", lambda: [])

    resposta = router.handle(mensagem(opcao))

    assert resposta == "retorno:start"
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "start"
    assert spy.calls[0][1][0] == method.removeprefix("start_")


def test_estado_v24_ativo_e_retomado_via_handle(whatsapp_router):
    router, spy = whatsapp_router
    conversa = router._get_or_create_conversa("351900077700")
    conversa.estado_atual = "v24_cadastro_nome"
    conversa.contexto_json = {"tipo_solicitacao": "normal"}
    router.db.commit()
    entrada = mensagem("Cliente")

    resposta = router.handle(entrada)

    assert resposta == "retorno:handle"
    assert spy.calls == [("handle", (conversa, entrada))]


def test_cancelamento_global_acontece_antes_do_seam(whatsapp_router):
    router, spy = whatsapp_router
    conversa = router._get_or_create_conversa("351900077700")
    conversa.estado_atual = "v24_cadastro_nome"
    conversa.contexto_json = {"nome": "parcial"}
    router.db.commit()

    resposta = router.handle(mensagem("cancelar"))

    assert "cancelada" in resposta
    assert spy.calls == []
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}


def test_opcao_cinco_permanece_fora_do_seam(whatsapp_router, monkeypatch):
    router, spy = whatsapp_router
    monkeypatch.setattr(router, "_handle_operational_command", lambda *_args: "painel-atual")

    assert router.handle(mensagem("5")) == "painel-atual"
    assert spy.calls == []


def test_autorizacao_acontece_antes_do_seam(whatsapp_router, monkeypatch):
    router, spy = whatsapp_router
    monkeypatch.setattr(
        router.operador_service,
        "decidir_acesso",
        lambda _telefone: SimpleNamespace(autorizado=False, perfil=None, operador=None),
    )

    assert router.handle(mensagem("1")) == "Telefone não autorizado."
    assert spy.calls == []
