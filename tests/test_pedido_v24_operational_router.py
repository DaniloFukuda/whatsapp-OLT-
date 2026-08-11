"""Contrato do seam entre WhatsappRouterAgent e PedidoV24Agent."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24.router import PedidoV24OperationalRouter
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
