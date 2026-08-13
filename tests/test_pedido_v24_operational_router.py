"""Contrato do seam entre WhatsappRouterAgent e PedidoV24Agent."""

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.agents.pedido_v24.contentor import (
    CancelarRecolhaContentor,
    ConfirmEntregaContentor,
    ConfirmarDespejoContentor,
    ConfirmarRecolhaContentor,
    ContentorOperationalAgent,
    PrepararConfirmacaoDespejoContentor,
    PrepararConformidadeDespejoContentor,
    PrepararFotoDespejoContentor,
    PrepararConfirmacaoRecolhaContentor,
    RegistrarPagamentoEntregaContentor,
)
from app.agents.pedido_v24.contentor_cadastro import (
    CadastroModality,
    ConfirmarCadastroContentor,
    ContentorCadastroAgent,
    classify_cadastro_modality,
)
from app.agents.pedido_v24.carrinha_cadastro import (
    CarrinhaCadastroAgent,
    CarrinhaCadastroModality,
    ConfirmarCadastroCarrinha,
    classify_carrinha_cadastro,
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
        ({"tipo_solicitacao": "CONTENTOR"}, CadastroModality.CONTENTOR_INTENT),
        ({"tipo_solicitacao": "CARRINHA"}, CadastroModality.CARRINHA),
        ({}, CadastroModality.LEGACY_INDETERMINATE),
        (
            {
                "tipo_solicitacao": "CONTENTOR",
                "item_atual": {"tipo_equipamento": "CARRINHA"},
            },
            CadastroModality.DIVERGENT,
        ),
        (
            {
                "tipo_solicitacao": "CONTENTOR",
                "itens": [{"tipo_equipamento": "CARRINHA"}],
            },
            CadastroModality.DIVERGENT,
        ),
        (
            {
                "tipo_solicitacao": "CONTENTOR",
                "itens": [
                    {"tipo_equipamento": "CONTENTOR"},
                    {"tipo_equipamento": "CARRINHA"},
                ],
            },
            CadastroModality.DIVERGENT,
        ),
        (
            {
                "tipo_solicitacao": "CONTENTOR",
                "quantidade": 2,
                "itens": [
                    {"tipo_equipamento": "CONTENTOR"},
                    {"tipo_equipamento": "CONTENTOR"},
                ],
            },
            CadastroModality.CONTENTOR_PROVEN,
        ),
    ],
)
def test_classifica_modalidade_do_cadastro_sem_inferencia(context, expected):
    snapshot = repr(context)

    assert classify_cadastro_modality(context) is expected
    assert repr(context) == snapshot


def test_agente_de_cadastro_contentor_e_separado_e_sem_dependencias_persistentes():
    agent = ContentorCadastroAgent()

    assert not isinstance(agent, ContentorOperationalAgent)
    assert not hasattr(agent, "db")
    assert not hasattr(agent, "service")


@pytest.mark.parametrize("choice", ["1", "contentor", "contentores"])
def test_router_usa_cadastro_modular_somente_na_escolha_contentor(
    db_session,
    monkeypatch,
    choice,
):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="cadastro-contentor")
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_tipo_solicitacao",
        contexto_json={},
    )
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )

    resposta = router.handle(
        conversa,
        NormalizedWhatsAppMessage(
            telefone="351900000000",
            texto=choice,
            tipo="text",
        ),
    )

    assert resposta == "cadastro-contentor"
    transition = backend.apply_operational_transition.call_args.args[1]
    assert transition == AdvanceTransition(
        "v24_cadastro_nome",
        {"tipo_solicitacao": "CONTENTOR"},
        "Qual é o nome do cliente?",
    )
    backend.handle.assert_not_called()


@pytest.mark.parametrize("choice", ["invalido"])
def test_router_mantem_carrinha_e_entrada_invalida_no_legado(
    db_session,
    monkeypatch,
    choice,
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_tipo_solicitacao",
        contexto_json={},
    )
    message = NormalizedWhatsAppMessage(
        telefone="351900000000",
        texto=choice,
        tipo="text",
    )
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )

    assert router.handle(conversa, message) == "legado"
    backend.handle.assert_called_once_with(conversa, message)


def test_router_mantem_contentor_desabilitado_no_legado(db_session, monkeypatch):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="recusa-legada")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_tipo_solicitacao",
        contexto_json={},
    )
    message = NormalizedWhatsAppMessage(
        telefone="351900000000",
        texto="1",
        tipo="text",
    )
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )

    assert router.handle(conversa, message) == "recusa-legada"
    backend.handle.assert_called_once_with(conversa, message)


def _cadastro_message(texto="", **kwargs):
    return NormalizedWhatsAppMessage(
        telefone="351900000000",
        tipo=kwargs.pop("tipo", "text"),
        texto=texto,
        **kwargs,
    )


def test_cadastro_contentor_nome_texto_preserva_transicao_e_contexto():
    decision = ContentorCadastroAgent().decide_nome(
        {"tipo_solicitacao": "CONTENTOR", "existente": True},
        _cadastro_message("  Cliente Teste  "),
    )

    assert decision == AdvanceTransition(
        "v24_cadastro_telefone",
        {
            "tipo_solicitacao": "CONTENTOR",
            "existente": True,
            "nome": "Cliente Teste",
        },
        "Qual é o telefone do cliente?",
    )


def test_cadastro_contentor_contato_com_telefone_avanca_para_quantidade():
    decision = ContentorCadastroAgent().decide_nome(
        {"tipo_solicitacao": "CONTENTOR"},
        _cadastro_message(
            contact_name=" Cliente Contato ",
            contact_phone="+351 912 345 678",
        ),
    )

    assert decision.next_state == "v24_cadastro_quantidade"
    assert decision.context == {
        "tipo_solicitacao": "CONTENTOR",
        "nome": "Cliente Contato",
        "telefone": "351912345678",
    }


@pytest.mark.parametrize("name", ["", "A"])
def test_cadastro_contentor_nome_invalido_preserva_mensagem(name):
    assert ContentorCadastroAgent().decide_nome(
        {"tipo_solicitacao": "CONTENTOR"},
        _cadastro_message(name),
    ) == "Informe o nome completo do cliente."


@pytest.mark.parametrize(
    "message,expected",
    [
        (_cadastro_message("912 345 678"), "912345678"),
        (_cadastro_message(contact_phone="+351 912 345 678"), "351912345678"),
    ],
)
def test_cadastro_contentor_telefone_valido_avanca_para_quantidade(
    message,
    expected,
):
    decision = ContentorCadastroAgent().decide_telefone(
        {"tipo_solicitacao": "CONTENTOR", "nome": "Cliente"},
        message,
    )

    assert decision.next_state == "v24_cadastro_quantidade"
    assert decision.context["telefone"] == expected


@pytest.mark.parametrize("phone", ["", "12345678", "telefone"])
def test_cadastro_contentor_telefone_invalido_preserva_mensagem(phone):
    assert ContentorCadastroAgent().decide_telefone(
        {"tipo_solicitacao": "CONTENTOR"},
        _cadastro_message(phone),
    ) == "O telefone informado não é válido."


@pytest.mark.parametrize("quantidade", ["1", "50"])
def test_cadastro_contentor_quantidade_limites_e_resets(quantidade):
    decision = ContentorCadastroAgent().decide_quantidade(
        {
            "tipo_solicitacao": "CONTENTOR",
            "itens": [{"antigo": True}],
            "residuos": ["antigo"],
            "outro": "preservado",
        },
        _cadastro_message(quantidade),
    )

    assert decision.next_state == "v24_cadastro_tipo_equipamento"
    assert decision.context == {
        "tipo_solicitacao": "CONTENTOR",
        "quantidade": int(quantidade),
        "itens": [],
        "residuos": [],
        "outro": "preservado",
    }


@pytest.mark.parametrize("quantidade", ["0", "51", "1.5", "abc"])
def test_cadastro_contentor_quantidade_invalida_preserva_mensagem(quantidade):
    assert ContentorCadastroAgent().decide_quantidade(
        {"tipo_solicitacao": "CONTENTOR"},
        _cadastro_message(quantidade),
    ) == "Informe uma quantidade entre 1 e 50."


@pytest.mark.parametrize(
    "state",
    [
        "v24_cadastro_nome",
        "v24_cadastro_telefone",
        "v24_cadastro_quantidade",
    ],
)
def test_router_usa_modulo_nos_estados_iniciais_de_contentor(
    db_session,
    state,
):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="modular")
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual=state,
        contexto_json={"tipo_solicitacao": "CONTENTOR"},
    )
    messages = {
        "v24_cadastro_nome": _cadastro_message("Cliente"),
        "v24_cadastro_telefone": _cadastro_message("912345678"),
        "v24_cadastro_quantidade": _cadastro_message("2"),
    }

    assert router.handle(conversa, messages[state]) == "modular"
    backend.apply_operational_transition.assert_called_once()
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "context",
    [
        {"tipo_solicitacao": "CARRINHA"},
        {},
        {
            "tipo_solicitacao": "CONTENTOR",
            "item_atual": {"tipo_equipamento": "CARRINHA"},
        },
    ],
)
def test_router_mantem_contextos_nao_contentor_intent_no_legado(
    db_session,
    context,
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_nome",
        contexto_json=context,
    )
    message = _cadastro_message("Cliente")

    assert router.handle(conversa, message) == "legado"
    backend.handle.assert_called_once_with(conversa, message)


@pytest.mark.parametrize("choice", ["1", "contentor"])
def test_cadastro_contentor_seleciona_tipo_do_item(choice):
    agent = ContentorCadastroAgent()
    assert agent.is_contentor_selection(choice)

    decision = agent.decide_tipo_equipamento(
        {
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 2,
            "itens": [],
            "residuos": [],
        }
    )

    assert decision.next_state == "v24_cadastro_mao_obra"
    assert decision.context["item_atual"] == {
        "tipo_equipamento": "CONTENTOR",
        "horario_agendado": None,
    }


@pytest.mark.parametrize(
    "choice,expected",
    [("1", True), ("sim", True), ("2", False), ("não", False)],
)
def test_cadastro_contentor_mao_obra_preserva_mutacao_antiga(choice, expected):
    decision = ContentorCadastroAgent().decide_mao_obra(
        {
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 1,
            "itens": [],
            "residuos": [],
            "item_atual": {
                "tipo_equipamento": "CONTENTOR",
                "horario_agendado": None,
            },
        },
        _cadastro_message(choice),
    )

    assert decision.next_state == "v24_cadastro_residuo"
    assert decision.context["precisa_mao_de_obra"] is expected
    assert decision.context["item_atual"]["precisa_mao_de_obra"] is expected


@pytest.mark.parametrize(
    "choice,residue",
    [
        ("1", "Entulho Limpo"),
        ("limpo", "Entulho Limpo"),
        ("2", "Entulho Misto"),
        ("misto", "Entulho Misto"),
    ],
)
def test_cadastro_contentor_residuo_registra_item_e_conclui(choice, residue):
    decision = ContentorCadastroAgent().decide_residuo(
        {
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 1,
            "itens": [],
            "residuos": [],
            "item_atual": {
                "tipo_equipamento": "CONTENTOR",
                "horario_agendado": None,
                "precisa_mao_de_obra": True,
            },
        },
        _cadastro_message(choice),
    )

    assert decision.next_state == "v24_cadastro_data"
    assert decision.context["residuos"] == [residue]
    assert decision.context["itens"] == [
        {
            "tipo_equipamento": "CONTENTOR",
            "horario_agendado": None,
            "precisa_mao_de_obra": False,
            "residuo_contratado": residue,
        }
    ]
    assert "item_atual" not in decision.context
    assert (
        classify_cadastro_modality(decision.context)
        is CadastroModality.CONTENTOR_PROVEN
    )


def test_cadastro_contentor_residuo_repete_quando_faltam_itens():
    decision = ContentorCadastroAgent().decide_residuo(
        {
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 2,
            "itens": [],
            "residuos": [],
            "item_atual": {
                "tipo_equipamento": "CONTENTOR",
                "horario_agendado": None,
                "precisa_mao_de_obra": False,
            },
        },
        _cadastro_message("1"),
    )

    assert decision.next_state == "v24_cadastro_tipo_equipamento"
    assert len(decision.context["itens"]) == 1
    assert "item_atual" not in decision.context
    assert (
        classify_cadastro_modality(decision.context)
        is CadastroModality.CONTENTOR_INTENT
    )


@pytest.mark.parametrize(
    "choice",
    ["2", "carrinha", "contentores", "misto", "invalido"],
)
def test_router_nao_consume_escolhas_nao_contentor_em_tipo_equipamento(
    db_session,
    choice,
):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_tipo_equipamento",
        contexto_json={
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 1,
            "itens": [],
            "residuos": [],
        },
    )
    message = _cadastro_message(choice)

    assert router.handle(conversa, message) == "legado"
    backend.handle.assert_called_once_with(conversa, message)


def _contentor_proven_context():
    return {
        "tipo_solicitacao": "CONTENTOR",
        "quantidade": 1,
        "itens": [
            {
                "tipo_equipamento": "CONTENTOR",
                "horario_agendado": None,
                "precisa_mao_de_obra": False,
                "residuo_contratado": "Entulho Limpo",
            }
        ],
        "residuos": ["Entulho Limpo"],
    }


@pytest.mark.parametrize("choice,days", [("1", 0), ("hoje", 0), ("2", 1), ("amanhã", 1)])
def test_cadastro_contentor_data_preserva_atalhos_e_timezone(choice, days):
    now = datetime(2026, 8, 12, 14, 30, tzinfo=timezone.utc)
    decision = ContentorCadastroAgent().decide_data(
        _contentor_proven_context(),
        _cadastro_message(choice),
        now,
    )

    assert decision.next_state == "v24_cadastro_valor"
    assert decision.context["data"] == (now + timedelta(days=days)).isoformat()


def test_cadastro_contentor_data_manual_preserva_formato_e_timezone():
    decision = ContentorCadastroAgent().decide_data_manual(
        _contentor_proven_context(),
        _cadastro_message("31/12/2026"),
        timezone.utc,
    )

    assert decision.next_state == "v24_cadastro_valor"
    assert decision.context["data"] == "2026-12-31T00:00:00+00:00"


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("3", "v24_cadastro_data_manual"),
        ("outra data", "v24_cadastro_data_manual"),
    ],
)
def test_cadastro_contentor_data_preserva_caminho_manual(choice, expected):
    decision = ContentorCadastroAgent().decide_data(
        _contentor_proven_context(),
        _cadastro_message(choice),
        datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    assert decision.next_state == expected


@pytest.mark.parametrize("value", ["", "31-12-2026", "31/02/2026"])
def test_cadastro_contentor_data_manual_invalida_preserva_mensagem(value):
    assert ContentorCadastroAgent().decide_data_manual(
        _contentor_proven_context(),
        _cadastro_message(value),
        timezone.utc,
    ) == "Data inválida. Use o formato DD/MM/AAAA."


@pytest.mark.parametrize("value,expected", [("12,50", "12.5"), ("0", "0.0"), ("-1", "-1.0")])
def test_cadastro_contentor_valor_preserva_float_do_contexto(value, expected):
    decision = ContentorCadastroAgent().decide_valor(
        _contentor_proven_context(),
        _cadastro_message(value),
    )

    assert decision.next_state == "v24_cadastro_pago"
    assert decision.context["valor"] == expected


def test_cadastro_contentor_valor_invalido_preserva_mensagem():
    assert ContentorCadastroAgent().decide_valor(
        _contentor_proven_context(),
        _cadastro_message("invalido"),
    ) == "Valor inválido."


@pytest.mark.parametrize(
    "state,message",
    [
        ("v24_cadastro_data", "1"),
        ("v24_cadastro_data_manual", "31/12/2026"),
        ("v24_cadastro_valor", "10,50"),
    ],
)
def test_router_usa_planejamento_modular_somente_para_contentor_proven(
    db_session,
    state,
    message,
):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock(return_value="modular")
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual=state,
        contexto_json=_contentor_proven_context(),
    )

    assert router.handle(conversa, _cadastro_message(message)) == "modular"
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "context",
    [
        {"tipo_solicitacao": "CONTENTOR"},
        {"tipo_solicitacao": "CARRINHA"},
        {},
        {
            "tipo_solicitacao": "CONTENTOR",
            "quantidade": 1,
            "itens": [{"tipo_equipamento": "CARRINHA"}],
        },
    ],
)
def test_router_mantem_planejamento_nao_comprovado_no_legado(db_session, context):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_data",
        contexto_json=context,
    )
    message = _cadastro_message("1")

    assert router.handle(conversa, message) == "legado"
    backend.handle.assert_called_once_with(conversa, message)


@pytest.mark.parametrize(
    "choice,next_state,pago,forma",
    [
        ("1", "v24_cadastro_forma", True, "ausente"),
        ("sim", "v24_cadastro_forma", True, "ausente"),
        ("2", "v24_cadastro_endereco", False, None),
        ("não", "v24_cadastro_endereco", False, None),
    ],
)
def test_cadastro_contentor_pago_preserva_decisao(choice, next_state, pago, forma):
    context = {**_contentor_proven_context(), "forma": "anterior"}
    decision = ContentorCadastroAgent().decide_pago(
        context,
        _cadastro_message(choice),
    )

    assert decision.next_state == next_state
    assert decision.context["pago"] is pago
    if forma == "ausente":
        assert decision.context["forma"] == "anterior"
    else:
        assert decision.context["forma"] is None


@pytest.mark.parametrize(
    "choice,forma,next_state",
    [
        ("1", "MBWay", "v24_cadastro_endereco"),
        ("mbway", "MBWay", "v24_cadastro_endereco"),
        ("2", "Transferência", "v24_cadastro_endereco"),
        ("transferência", "Transferência", "v24_cadastro_endereco"),
        ("3", "Dinheiro", "v24_cadastro_endereco"),
        ("dinheiro", "Dinheiro", "v24_cadastro_endereco"),
        ("4", None, "v24_cadastro_forma_outro"),
        ("outro", None, "v24_cadastro_forma_outro"),
    ],
)
def test_cadastro_contentor_formas_preservadas(choice, forma, next_state):
    decision = ContentorCadastroAgent().decide_forma(
        _contentor_proven_context(),
        _cadastro_message(choice),
    )
    assert decision.next_state == next_state
    if forma:
        assert decision.context["forma"] == forma


def test_cadastro_contentor_forma_outro_strip_e_trunca_80():
    decision = ContentorCadastroAgent().decide_forma_outro(
        _contentor_proven_context(),
        _cadastro_message("  " + "x" * 90 + "  "),
    )
    assert decision.next_state == "v24_cadastro_endereco"
    assert decision.context["forma"] == "x" * 80


def test_cadastro_contentor_endereco_textual_e_coordenadas():
    decision = ContentorCadastroAgent().decide_endereco(
        _contentor_proven_context(),
        _cadastro_message(" Rua Teste "),
        (38.7, -9.1),
    )
    assert decision.next_state == "v24_cadastro_referencia_opcao"
    assert decision.context["endereco"] == "Rua Teste"
    assert decision.context["endereco_latitude"] == 38.7
    assert decision.context["endereco_longitude"] == -9.1


@pytest.mark.parametrize("choice,next_state", [("1", "v24_cadastro_referencia"), ("sim", "v24_cadastro_referencia")])
def test_cadastro_contentor_referencia_opcao_sim(choice, next_state):
    decision = ContentorCadastroAgent().decide_referencia_opcao(
        _contentor_proven_context(),
        _cadastro_message(choice),
    )
    assert decision.next_state == next_state


def test_cadastro_contentor_referencia_valida_prepara_confirmacao():
    decision = ContentorCadastroAgent().decide_referencia(
        _contentor_proven_context(),
        _cadastro_message("  Portão azul  "),
    )
    assert decision.next_state == "v24_cadastro_confirmacao"
    assert decision.context["referencia"] == "Portão azul"


@pytest.mark.parametrize(
    "choice,expected_type,expected_state",
    [
        ("2", AdvanceTransition, "v24_cadastro_corrigir"),
        ("corrigir", AdvanceTransition, "v24_cadastro_corrigir"),
        ("3", IdleTransition, None),
        ("cancelar", IdleTransition, None),
    ],
)
def test_cadastro_contentor_confirmacao_decide_sem_persistir(
    choice,
    expected_type,
    expected_state,
):
    decision = ContentorCadastroAgent().decide_confirmacao(
        _contentor_proven_context(),
        _cadastro_message(choice),
    )
    assert isinstance(decision, expected_type)
    if expected_state:
        assert decision.next_state == expected_state


@pytest.mark.parametrize("choice", ["1", "sim", "confirmar", "confirmar e salvar"])
def test_router_confirmar_contentor_usa_boundary_especifico(db_session, choice):
    backend = PedidoV24Agent(db_session)
    backend.confirmar_cadastro_contentor = Mock(return_value="confirmacao-contentor")
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_confirmacao",
        contexto_json=_contentor_proven_context(),
    )
    message = _cadastro_message(choice)

    assert router.handle(conversa, message) == "confirmacao-contentor"
    backend.confirmar_cadastro_contentor.assert_called_once_with(
        conversa,
        conversa.contexto_json,
    )
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "field,value,context_key,expected",
    [
        ("quantidade", "2", "quantidade", 2),
        ("nome_cliente", " Novo Nome ", "nome", "Novo Nome"),
        ("telefone", "912 345 678", "telefone", "912345678"),
        ("valor_total", "12,50", "valor", "12.5"),
        ("mao_de_obra", "1", "precisa_mao_de_obra", True),
        ("tipo_residuo", "2", "residuos", ["Entulho Misto"]),
    ],
)
def test_cadastro_contentor_edicao_atomica_preserva_regras(
    field,
    value,
    context_key,
    expected,
):
    context = {**_contentor_proven_context(), "editing_field": field}
    decision = ContentorCadastroAgent().decide_edicao(
        context,
        _cadastro_message(value),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    assert decision.next_state == "v24_cadastro_confirmacao"
    assert decision.context[context_key] == expected
    assert "editing_field" not in decision.context


def test_cadastro_contentor_edicao_quantidade_copia_item_sem_reconstruir_modalidade():
    context = {
        **_contentor_proven_context(),
        "editing_field": "quantidade",
    }
    decision = ContentorCadastroAgent().decide_edicao(
        context,
        _cadastro_message("2"),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    assert len(decision.context["itens"]) == 2
    assert classify_cadastro_modality(decision.context) is CadastroModality.CONTENTOR_PROVEN


def test_cadastro_contentor_edicao_status_pago_encadeia_forma():
    decision = ContentorCadastroAgent().decide_edicao(
        {**_contentor_proven_context(), "editing_field": "status_pagamento"},
        _cadastro_message("sim"),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    assert decision.next_state == "v24_cadastro_edicao_opcao"
    assert decision.context["pago"] is True
    assert decision.context["editing_field"] == "forma_pagamento"


@pytest.mark.parametrize("choice", ["1", "sim", "confirmar", "confirmar e salvar"])
def test_cadastro_contentor_confirmar_produz_comando_especifico(choice):
    context = _contentor_proven_context()
    decision = ContentorCadastroAgent().decide_confirmacao(
        context,
        _cadastro_message(choice),
    )

    assert decision == ConfirmarCadastroContentor(context)


def test_router_encaminha_confirmacao_contentor_ao_boundary_especifico(db_session):
    backend = PedidoV24Agent(db_session)
    backend.confirmar_cadastro_contentor = Mock(return_value="confirmado")
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    context = _contentor_proven_context()
    conversa = SimpleNamespace(
        estado_atual="v24_cadastro_confirmacao",
        contexto_json=context,
    )

    assert router.handle(conversa, _cadastro_message("confirmar")) == "confirmado"
    backend.confirmar_cadastro_contentor.assert_called_once_with(conversa, context)
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "state,context",
    [
        ("idle", _contentor_proven_context()),
        ("v24_cadastro_confirmacao", {"tipo_solicitacao": "CARRINHA"}),
        ("v24_cadastro_confirmacao", {"tipo_solicitacao": "CONTENTOR"}),
        (
            "v24_cadastro_confirmacao",
            {
                "tipo_solicitacao": "CONTENTOR",
                "quantidade": 1,
                "itens": [{"tipo_equipamento": "CARRINHA"}],
            },
        ),
    ],
)
def test_boundary_cadastro_contentor_recusa_estado_ou_modalidade_nao_comprovada(
    db_session,
    state,
    context,
):
    backend = PedidoV24Agent(db_session)
    backend._finish_cadastro = Mock(return_value="nao-deveria")
    conversa = SimpleNamespace(estado_atual=state)

    assert backend.confirmar_cadastro_contentor(conversa, context) is None
    backend._finish_cadastro.assert_not_called()


def test_boundary_cadastro_contentor_delega_uma_vez_sem_duplicar_finish(db_session):
    backend = PedidoV24Agent(db_session)
    backend._finish_cadastro = Mock(return_value="confirmado")
    context = _contentor_proven_context()
    conversa = SimpleNamespace(estado_atual="v24_cadastro_confirmacao")

    assert backend.confirmar_cadastro_contentor(conversa, context) == "confirmado"
    expected = {**context, "_confirmado": True}
    backend._finish_cadastro.assert_called_once_with(conversa, expected)
    assert "_confirmado" not in context


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
    router._contentor.select_entrega_pedido.assert_called_once_with(
        conversa,
        {
            "pedido_id": 17,
            "pedido_exists": False,
            "contentor_ids": (),
            "prompt": "",
        },
    )


def test_agente_decide_selecao_entrega_contentor_por_snapshot_sem_backend():
    backend = Mock()
    agent = ContentorOperationalAgent(Mock(), backend)
    conversa = SimpleNamespace(
        contexto_json={"ids": [17], "preservado": True},
    )

    decision = agent.select_entrega_pedido(
        conversa,
        {
            "pedido_id": 17,
            "pedido_exists": True,
            "contentor_ids": (31, 32),
            "prompt": "Digite o número do contentor que está a descarregar agora:",
        },
    )

    assert decision == AdvanceTransition(
        "v24_entrega_adesivo",
        {
            "ids": [17],
            "preservado": True,
            "pedido_id": 17,
            "contentores": [31, 32],
            "indice": 0,
            "entregas": [],
        },
        "Digite o número do contentor que está a descarregar agora:",
    )
    backend.handle.assert_not_called()


def test_agente_selecao_entrega_invalida_preserva_mensagem_sem_backend():
    backend = Mock()
    agent = ContentorOperationalAgent(Mock(), backend)

    assert agent.select_entrega_pedido(SimpleNamespace(contexto_json={}), None) == (
        "Selecione um pedido da lista."
    )
    backend.handle.assert_not_called()


def test_agente_selecao_entrega_sem_pendentes_retorna_idle():
    agent = ContentorOperationalAgent(Mock(), Mock())
    decision = agent.select_entrega_pedido(
        SimpleNamespace(contexto_json={}),
        {
            "pedido_id": 17,
            "pedido_exists": True,
            "contentor_ids": (),
            "prompt": "",
        },
    )

    assert decision == IdleTransition(
        "Esse pedido já não possui ativos pendentes de entrega."
    )


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
    backend.resolve_entrega_contentor_adesivo = Mock(return_value={"snapshot": True})
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
    router._contentor.decide_entrega_adesivo.assert_called_once_with(
        conversa,
        entrada,
        {"snapshot": True},
    )
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)


def test_adesivo_contentor_invalido_preserva_resposta_sem_aplicar_transicao(db_session):
    backend = PedidoV24Agent(db_session)
    backend.apply_operational_transition = Mock()
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    router._contentor.decide_entrega_adesivo.return_value = "Informe somente o número visível no contentor."
    backend.resolve_entrega_contentor_adesivo = Mock(return_value={"snapshot": True})
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
    agent = ContentorOperationalAgent(lambda: [], backend)
    original = {
        "pedido_id": 17,
        "contentores": [5],
        "indice": 0,
        "entregas": [],
    }
    conversa = SimpleNamespace(contexto_json=original)

    decision = agent.decide_entrega_adesivo(
        conversa,
        mensagem("101"),
        {
            "ativo_exists": True,
            "is_contentor": True,
            "status_entrega": "PENDENTE",
            "adesivo_em_ciclo_ativo": False,
        },
    )

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
    assert "db" not in backend.__dict__


def test_decisao_adesivo_ativo_invalido_produz_idle_sem_commit():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(
        contexto_json={"contentores": [5], "indice": 0, "entregas": []},
    )

    assert agent.decide_entrega_adesivo(
        conversa,
        mensagem("101"),
        {
            "ativo_exists": False,
            "is_contentor": False,
            "status_entrega": None,
            "adesivo_em_ciclo_ativo": False,
        },
    ) == IdleTransition(
        "Esse ativo já não está pendente. Reinicie a entrega."
    )
    assert "db" not in backend.__dict__


def test_adapter_adesivo_contentor_retorna_snapshot_simples_read_only(db_session):
    backend = PedidoV24Agent(db_session)
    contentor = SimpleNamespace(
        tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
        status_entrega="PENDENTE",
    )
    backend.db = Mock()
    backend.db.get.return_value = contentor
    backend.db.query.return_value.filter.return_value.first.return_value = object()

    snapshot = backend.resolve_entrega_contentor_adesivo(
        mensagem("101"),
        {"contentores": [5], "indice": 0},
    )

    assert snapshot == {
        "ativo_exists": True,
        "is_contentor": True,
        "status_entrega": "PENDENTE",
        "adesivo_em_ciclo_ativo": True,
    }
    backend.db.get.assert_called_once()
    backend.db.commit.assert_not_called()
    backend.db.rollback.assert_not_called()


def test_decisao_adesivo_duplicado_preserva_mensagem_sem_acesso_a_db():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    conversa = SimpleNamespace(
        contexto_json={"contentores": [5], "indice": 0, "entregas": []},
    )

    resposta = agent.decide_entrega_adesivo(
        conversa,
        mensagem("101"),
        {
            "ativo_exists": True,
            "is_contentor": True,
            "status_entrega": "PENDENTE",
            "adesivo_em_ciclo_ativo": True,
        },
    )

    assert resposta == "Esse adesivo já está em um ciclo ativo."
    assert "db" not in backend.__dict__


def test_adesivo_interativo_preserva_mensagem_sem_leitura_de_infraestrutura(db_session):
    backend = PedidoV24Agent(db_session)
    backend.db = Mock()

    assert backend.resolve_entrega_contentor_adesivo(
        NormalizedWhatsAppMessage(
            telefone="351900077700",
            tipo="interactive",
            texto="101",
            message_id="adesivo-interativo",
        ),
        {},
    ) is None
    backend.db.get.assert_not_called()
    backend.db.query.assert_not_called()


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


def test_contentor_decide_selecao_recolha_sem_banco_ou_service():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {"pedido_id": 9, "contentores": [17], "recolhas": []}
    conversa = SimpleNamespace(contexto_json=contexto)
    selection = {
        "contentor_id": 17,
        "modality": TipoEquipamentoPedido.CONTENTOR,
        "foto_prompt": "Envie a foto de recolha do 📦 Contentor 42 cheio antes do icamento.",
    }

    decision = agent.decide_recolha_ativo(conversa, selection)

    assert decision == AdvanceTransition(
        "v24_recolha_foto",
        {
            "pedido_id": 9,
            "contentores": [17],
            "recolhas": [],
            "contentor_id": 17,
            "fotos_recolha": [],
            "avariado": None,
            "relato_avaria": None,
        },
        selection["foto_prompt"],
    )
    assert contexto == {"pedido_id": 9, "contentores": [17], "recolhas": []}
    backend.assert_not_called()
    assert not hasattr(agent, "service")


def test_adapter_recolha_comprova_contentor_vinculado_e_pendente_sem_escrita():
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    item = SimpleNamespace(
        id=17,
        pedido_id=9,
        tipo_equipamento="CONTENTOR",
        status_entrega="ENTREGUE",
        status_recolha="PENDENTE",
        numero_adesivo_contentor="42",
        horario_agendado=None,
        frota_carrinha=None,
    )
    backend.db = Mock()
    backend.db.get.return_value = item
    contexto = {
        "pedido_id": 9,
        "contentores": [17],
        "terminar_indice": 2,
    }

    selection = backend.resolve_recolha_ativo_selection(mensagem("1"), contexto)

    assert selection == {
        "contentor_id": 17,
        "modality": TipoEquipamentoPedido.CONTENTOR,
        "foto_prompt": "Envie a foto de recolha do 📦 Contentor 42 cheio antes do icamento.",
    }
    assert contexto == {
        "pedido_id": 9,
        "contentores": [17],
        "terminar_indice": 2,
    }
    backend.db.get.assert_called_once()
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "item",
    [
        None,
        SimpleNamespace(
            id=17,
            pedido_id=10,
            tipo_equipamento="CONTENTOR",
            status_entrega="ENTREGUE",
            status_recolha="PENDENTE",
        ),
        SimpleNamespace(
            id=17,
            pedido_id=9,
            tipo_equipamento="DESCONHECIDO",
            status_entrega="ENTREGUE",
            status_recolha="PENDENTE",
        ),
    ],
)
def test_adapter_recolha_recusa_desaparecido_desvinculado_ou_indeterminado(item):
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.db = Mock()
    backend.db.get.return_value = item

    assert backend.resolve_recolha_ativo_selection(
        mensagem("1"),
        {"pedido_id": 9, "contentores": [17], "terminar_indice": 2},
    ) is None
    backend.db.commit.assert_not_called()


def test_recolha_ativo_contentor_comprovado_usa_modulo(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    selection = {
        "contentor_id": 17,
        "modality": TipoEquipamentoPedido.CONTENTOR,
        "foto_prompt": "prompt",
    }
    backend.resolve_recolha_ativo_selection.return_value = selection
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decision = AdvanceTransition("v24_recolha_foto", {"contentor_id": 17}, "prompt")
    router._contentor.decide_recolha_ativo.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_ativo",
        contexto_json={"pedido_id": 9, "contentores": [17]},
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "aplicada"
    router._contentor.decide_recolha_ativo.assert_called_once_with(
        conversa, selection
    )
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "selection",
    [
        None,
        {"contentor_id": 17, "modality": TipoEquipamentoPedido.CARRINHA, "foto_prompt": "prompt"},
    ],
)
def test_recolha_ativo_invalido_ou_carrinha_permanece_legado(
    monkeypatch, selection
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.resolve_recolha_ativo_selection.return_value = selection
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_ativo", contexto_json={"pedido_id": 9}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_recolha_ativo.assert_not_called()


def test_recolha_ativo_terminar_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.resolve_recolha_ativo_selection.return_value = None
    backend.handle.return_value = "encerrada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_ativo", contexto_json={"pedido_id": 9}
    )
    entrada = mensagem("Terminar")

    assert router.handle(conversa, entrada) == "encerrada-legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_recolha_ativo.assert_not_called()


def test_recolha_ativo_contentor_off_nao_consulta_adapter(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_ativo", contexto_json={"pedido_id": 9}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "bloqueada-legado"
    backend.resolve_recolha_ativo_selection.assert_not_called()
    router._contentor.decide_recolha_ativo.assert_not_called()


def _despejo_backend(item, cotas):
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.db = Mock()
    backend.db.get.return_value = item
    backend.service = Mock()
    backend.service.cotas_residuos.return_value = cotas
    return backend


def _item_despejo(**overrides):
    values = {
        "id": 17,
        "pedido_id": 9,
        "tipo_equipamento": "CONTENTOR",
        "status_recolha": "RECOLHIDO",
        "status_ciclo": "EM_ANDAMENTO",
        "status_operacional_carrinha": None,
        "residuo_contratado": "Entulho Limpo",
        "numero_adesivo_contentor": "42",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_contentor_decide_selecao_despejo_sem_banco_ou_service():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {"pedido_id": 9, "contentores": [17], "despejos": []}
    conversa = SimpleNamespace(contexto_json=contexto)
    selection = {
        "contentor_id": 17,
        "modality": TipoEquipamentoPedido.CONTENTOR,
        "context_updates": {
            "contentor_id": 17,
            "fotos_despejo": [],
            "residuo_contratado": "Entulho Limpo",
            "residuo_efetivo": None,
            "residuo_assumido": None,
            "carga_errada": None,
            "relato_carga": None,
            "saldo_cotas_visualizado": {"limpo": 1, "misto": 1},
            "residuos_disponiveis": ["Entulho Limpo", "Entulho Misto"],
        },
        "next_state": "v24_despejo_residuo",
        "response": "prompt legado",
    }

    decision = agent.decide_despejo_ativo(conversa, selection)

    assert decision.next_state == "v24_despejo_residuo"
    assert decision.context["contentor_id"] == 17
    assert decision.context["fotos_despejo"] == []
    assert decision.response == "prompt legado"
    assert contexto == {"pedido_id": 9, "contentores": [17], "despejos": []}
    backend.assert_not_called()
    assert not hasattr(agent, "service")


@pytest.mark.parametrize(
    "cotas,next_state",
    [
        (
            {
                "Entulho Limpo": {"saldo": 1},
                "Entulho Misto": {"saldo": 1},
            },
            "v24_despejo_residuo",
        ),
        (
            {
                "Entulho Limpo": {"saldo": 1},
                "Entulho Misto": {"saldo": 0},
            },
            "v24_despejo_conformidade",
        ),
    ],
)
def test_adapter_despejo_comprova_contentor_e_prepara_cotas(
    monkeypatch, cotas, next_state
):
    monkeypatch.setattr(
        "app.agents.pedido_v24_agent.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_carrinhas_enabled=True,
        ),
    )
    contexto = {
        "pedido_id": 9,
        "contentores": [17],
        "terminar_indice": 2,
        "despejos": [],
    }
    backend = _despejo_backend(_item_despejo(), cotas)

    selection = backend.resolve_despejo_ativo_selection(
        mensagem("1"), contexto
    )

    assert selection["contentor_id"] == 17
    assert selection["modality"] is TipoEquipamentoPedido.CONTENTOR
    assert selection["next_state"] == next_state
    assert selection["context_updates"]["contentor_id"] == 17
    assert selection["context_updates"]["fotos_despejo"] == []
    assert selection["context_updates"]["saldo_cotas_visualizado"] == {
        "limpo": 1,
        "misto": cotas["Entulho Misto"]["saldo"],
    }
    assert contexto == {
        "pedido_id": 9,
        "contentores": [17],
        "terminar_indice": 2,
        "despejos": [],
    }
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize(
    "item",
    [
        None,
        _item_despejo(pedido_id=10),
        _item_despejo(status_recolha="PENDENTE"),
        _item_despejo(status_ciclo="CONCLUIDO"),
        _item_despejo(tipo_equipamento="DESCONHECIDO"),
    ],
)
def test_adapter_despejo_recusa_desaparecido_desvinculado_inelegivel_ou_indeterminado(
    monkeypatch, item
):
    monkeypatch.setattr(
        "app.agents.pedido_v24_agent.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_carrinhas_enabled=True,
        ),
    )
    backend = _despejo_backend(item, {})

    assert backend.resolve_despejo_ativo_selection(
        mensagem("1"),
        {"pedido_id": 9, "contentores": [17], "terminar_indice": 2},
    ) is None
    backend.service.cotas_residuos.assert_not_called()
    backend.db.commit.assert_not_called()


def test_despejo_ativo_contentor_comprovado_usa_modulo(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    selection = {
        "contentor_id": 17,
        "modality": TipoEquipamentoPedido.CONTENTOR,
        "context_updates": {"contentor_id": 17},
        "next_state": "v24_despejo_residuo",
        "response": "prompt",
    }
    backend.resolve_despejo_ativo_selection.return_value = selection
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decision = AdvanceTransition("v24_despejo_residuo", {}, "prompt")
    router._contentor.decide_despejo_ativo.return_value = decision
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_ativo", contexto_json={"pedido_id": 9}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "aplicada"
    router._contentor.decide_despejo_ativo.assert_called_once_with(
        conversa, selection
    )
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)
    backend.handle.assert_not_called()


@pytest.mark.parametrize(
    "selection",
    [
        None,
        {"contentor_id": 17, "modality": TipoEquipamentoPedido.CARRINHA},
    ],
)
def test_despejo_ativo_invalido_ou_carrinha_permanece_legado(
    monkeypatch, selection
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.resolve_despejo_ativo_selection.return_value = selection
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_despejo_ativo", contexto_json={})
    entrada = mensagem("Terminar" if selection is None else "1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_despejo_ativo.assert_not_called()


def test_despejo_ativo_contentor_off_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_despejo_ativo", contexto_json={})

    assert router.handle(conversa, mensagem("1")) == "bloqueada-legado"
    backend.resolve_despejo_ativo_selection.assert_not_called()
    router._contentor.decide_despejo_ativo.assert_not_called()


def _mensagem_imagem(media="foto-1"):
    return SimpleNamespace(
        tipo="image",
        texto=None,
        media_id=media,
        filename=None,
        message_id="mensagem-foto",
    )


def test_contentor_despejo_foto_valida_deduplica_e_nao_acessa_backend():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {
        "pedido_id": 9,
        "contentor_id": 17,
        "fotos_despejo": ["foto-1"],
    }
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_foto", contexto_json=contexto
    )

    decision = agent.decide_despejo_foto(conversa, _mensagem_imagem())

    assert decision == AdvanceTransition(
        "v24_despejo_foto_acao",
        contexto,
        "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
    )
    assert decision.context["fotos_despejo"] == ["foto-1"]
    assert conversa.contexto_json == contexto
    backend.assert_not_called()


def test_contentor_despejo_foto_exige_imagem_sem_mudar_estado_contexto():
    contexto = {"pedido_id": 9, "contentor_id": 17, "fotos_despejo": []}
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_foto", contexto_json=contexto
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_despejo_foto(conversa, mensagem("texto")) == (
        "Envie uma imagem para continuar."
    )
    assert conversa.estado_atual == "v24_despejo_foto"
    assert conversa.contexto_json == contexto


@pytest.mark.parametrize("entrada", ["1", "Outra Foto", "➕ Outra Foto"])
def test_contentor_despejo_foto_acao_outra_foto_preserva_aliases(entrada):
    contexto = {"pedido_id": 9, "contentor_id": 17, "fotos_despejo": ["foto"]}
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_foto_acao(
        SimpleNamespace(contexto_json=contexto), mensagem(entrada)
    )

    assert decision == AdvanceTransition(
        "v24_despejo_foto",
        contexto,
        "Envie a próxima foto do despejo.",
    )


@pytest.mark.parametrize("entrada", ["2", "Próximo Passo", "➡️ Próximo Passo"])
def test_contentor_despejo_foto_acao_proximo_prepara_prompt_legado(entrada):
    contexto = {"pedido_id": 9, "contentor_id": 17, "fotos_despejo": ["foto"]}
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_foto_acao(
        SimpleNamespace(contexto_json=contexto), mensagem(entrada)
    )

    assert decision == PrepararConfirmacaoDespejoContentor(contexto)


def test_contentor_despejo_foto_acao_exige_foto_e_preserva_invalido():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    sem_foto = SimpleNamespace(
        estado_atual="v24_despejo_foto_acao",
        contexto_json={"pedido_id": 9, "contentor_id": 17, "fotos_despejo": []},
    )
    invalido = SimpleNamespace(
        estado_atual="v24_despejo_foto_acao",
        contexto_json={"pedido_id": 9, "contentor_id": 17, "fotos_despejo": ["foto"]},
    )

    assert agent.decide_despejo_foto_acao(sem_foto, mensagem("2")) == (
        "Envie pelo menos uma imagem para continuar."
    )
    assert agent.decide_despejo_foto_acao(invalido, mensagem("talvez")) == (
        "Selecione Outra Foto ou Próximo Passo."
    )
    assert sem_foto.estado_atual == invalido.estado_atual == "v24_despejo_foto_acao"


@pytest.mark.parametrize(
    "item,expected",
    [
        (_item_despejo(), TipoEquipamentoPedido.CONTENTOR),
        (
            _item_despejo(
                tipo_equipamento="CARRINHA",
                status_recolha="PENDENTE",
                status_operacional_carrinha="AGUARDANDO_DESPEJO",
            ),
            TipoEquipamentoPedido.CARRINHA,
        ),
        (_item_despejo(pedido_id=10), None),
        (_item_despejo(status_ciclo="CONCLUIDO"), None),
        (None, None),
    ],
)
def test_adapter_contexto_despejo_comprova_modalidade_e_elegibilidade(
    monkeypatch, item, expected
):
    monkeypatch.setattr(
        "app.agents.pedido_v24_agent.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_carrinhas_enabled=True,
        ),
    )
    backend = _despejo_backend(item, {})

    assert backend.resolve_despejo_context_modality(
        {"pedido_id": 9, "contentor_id": 17}
    ) is expected
    backend.db.commit.assert_not_called()


@pytest.mark.parametrize("estado", ["v24_despejo_foto", "v24_despejo_foto_acao"])
def test_despejo_foto_contentor_comprovado_usa_modulo(monkeypatch, estado):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.resolve_despejo_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.apply_operational_transition.return_value = "aplicada"
    backend.despejo_confirmacao_prompt.return_value = "prompt legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    context = {"pedido_id": 9, "contentor_id": 17, "fotos_despejo": ["foto"]}
    conversa = SimpleNamespace(estado_atual=estado, contexto_json=context)
    entrada = _mensagem_imagem() if estado.endswith("_foto") else mensagem("2")
    if estado.endswith("_foto"):
        decision = AdvanceTransition("v24_despejo_foto_acao", context, "foto")
        router._contentor.decide_despejo_foto.return_value = decision
    else:
        command = PrepararConfirmacaoDespejoContentor(context)
        router._contentor.decide_despejo_foto_acao.return_value = command

    assert router.handle(conversa, entrada) == "aplicada"
    backend.handle.assert_not_called()
    transition = backend.apply_operational_transition.call_args.args[1]
    if estado.endswith("_foto_acao"):
        assert transition == AdvanceTransition(
            "v24_despejo_confirmacao", context, "prompt legado"
        )
        backend.despejo_confirmacao_prompt.assert_called_once_with(context)


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
@pytest.mark.parametrize("estado", ["v24_despejo_foto", "v24_despejo_foto_acao"])
def test_despejo_foto_carrinha_ou_indeterminado_permanece_legado(
    monkeypatch, modality, estado
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.resolve_despejo_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={})
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado"
    router._contentor.decide_despejo_foto.assert_not_called()
    router._contentor.decide_despejo_foto_acao.assert_not_called()


def test_despejo_foto_contentor_off_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_despejo_foto", contexto_json={})

    assert router.handle(conversa, mensagem("x")) == "bloqueada-legado"
    backend.resolve_despejo_context_modality.assert_not_called()


def _contexto_despejo_moderno(**overrides):
    contexto = {
        "pedido_id": 9,
        "contentor_id": 17,
        "fotos_despejo": [],
        "residuo_contratado": "Entulho Limpo",
        "residuo_efetivo": None,
        "residuo_assumido": None,
        "carga_errada": None,
        "relato_carga": None,
        "residuos_disponiveis": ["Entulho Limpo", "Entulho Misto"],
    }
    contexto.update(overrides)
    return contexto


@pytest.mark.parametrize("entrada", ["1", "Entulho Limpo", "despejo_residuo:limpo"])
def test_contentor_despejo_residuo_valido_preserva_aliases_e_contexto(entrada):
    backend = Mock()
    contexto = _contexto_despejo_moderno(
        carga_errada=True, relato_carga="relato anterior"
    )
    decision = ContentorOperationalAgent(
        lambda: [], backend
    ).decide_despejo_residuo(
        SimpleNamespace(contexto_json=contexto), mensagem(entrada)
    )

    assert isinstance(decision, PrepararFotoDespejoContentor)
    assert decision.context["residuos_disponiveis"] == [
        "Entulho Limpo", "Entulho Misto"
    ]
    assert decision.context["residuo_efetivo"] == "Entulho Limpo"
    assert decision.context["carga_errada"] is False
    assert decision.context["relato_carga"] is None
    assert contexto["carga_errada"] is True
    backend.assert_not_called()


def test_contentor_despejo_residuo_invalido_preserva_estado_contexto_e_mensagem():
    contexto = _contexto_despejo_moderno()
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_residuo", contexto_json=contexto
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_despejo_residuo(conversa, mensagem("Madeira")) == (
        "Selecione um tipo de resíduo com cota em aberto."
    )
    assert conversa.estado_atual == "v24_despejo_residuo"
    assert conversa.contexto_json == contexto


@pytest.mark.parametrize(
    "entrada",
    ["1", "sim", "Sim, corresponde", "✅ Sim, corresponde", "Sim, tudo certo"],
)
def test_contentor_despejo_conformidade_conforme_preserva_aliases(entrada):
    contexto = _contexto_despejo_moderno(residuo_assumido="Entulho Misto")
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_conformidade(
        SimpleNamespace(contexto_json=contexto), mensagem(entrada)
    )

    assert isinstance(decision, PrepararFotoDespejoContentor)
    assert decision.context["residuo_efetivo"] == "Entulho Misto"
    assert decision.context["carga_errada"] is False
    assert decision.context["relato_carga"] is None


@pytest.mark.parametrize(
    "entrada",
    ["2", "nao", "Não, existe divergência", "❌ Não, existe divergência",
     "Não, está misturado/errado", "🚨 Não, está misturado/errado"],
)
def test_contentor_despejo_conformidade_divergente_preserva_aliases(entrada):
    contexto = _contexto_despejo_moderno()
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_conformidade(
        SimpleNamespace(contexto_json=contexto), mensagem(entrada)
    )

    assert decision.next_state == "v24_despejo_relato"
    assert decision.context["carga_errada"] is True
    assert decision.response == "Descreva a divergencia com pelo menos 10 caracteres."


def test_contentor_despejo_conformidade_invalida_preserva_mensagem():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    assert agent.decide_despejo_conformidade(
        SimpleNamespace(contexto_json=_contexto_despejo_moderno()),
        mensagem("talvez"),
    ) == "Selecione se o material corresponde ao residuo contratado."


def test_contentor_despejo_relato_preserva_strip_minimo_e_residuo():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    contexto = _contexto_despejo_moderno(residuo_assumido="Entulho Misto")
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_relato", contexto_json=contexto
    )

    assert agent.decide_despejo_relato(conversa, mensagem("  curto  ")) == (
        "O relato da carga precisa ter pelo menos 10 caracteres."
    )
    decision = agent.decide_despejo_relato(
        conversa, mensagem("  material bastante misturado  ")
    )
    assert isinstance(decision, PrepararFotoDespejoContentor)
    assert decision.context["relato_carga"] == "material bastante misturado"
    assert decision.context["carga_errada"] is True
    assert decision.context["residuo_efetivo"] == "Entulho Misto"


@pytest.mark.parametrize(
    "estado",
    ["v24_despejo_residuo", "v24_despejo_conformidade", "v24_despejo_relato"],
)
def test_contexto_despejo_moderno_exige_campos_preparados(estado):
    contexto = _contexto_despejo_moderno()
    assert PedidoV24Agent.despejo_context_is_modern(contexto, estado) is True
    contexto.pop("pedido_id")
    assert PedidoV24Agent.despejo_context_is_modern(contexto, estado) is False


@pytest.mark.parametrize(
    "estado",
    ["v24_despejo_residuo", "v24_despejo_conformidade", "v24_despejo_relato"],
)
def test_despejo_residuo_conformidade_relato_contentor_usam_modulo(
    monkeypatch, estado
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.despejo_context_is_modern.return_value = True
    backend.resolve_despejo_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.despejo_foto_prompt.return_value = "prompt legado da foto"
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decide = {
        "v24_despejo_residuo": router._contentor.decide_despejo_residuo,
        "v24_despejo_conformidade": router._contentor.decide_despejo_conformidade,
        "v24_despejo_relato": router._contentor.decide_despejo_relato,
    }[estado]
    contexto = _contexto_despejo_moderno()
    decide.return_value = PrepararFotoDespejoContentor(contexto)
    conversa = SimpleNamespace(estado_atual=estado, contexto_json=contexto)

    assert router.handle(conversa, mensagem("entrada")) == "aplicada"
    backend.handle.assert_not_called()
    backend.despejo_foto_prompt.assert_called_once_with(contexto)
    assert backend.apply_operational_transition.call_args.args[1] == AdvanceTransition(
        "v24_despejo_foto", contexto, "prompt legado da foto"
    )


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
def test_despejo_residuo_carrinha_indeterminado_ou_legado_faz_fallback(
    monkeypatch, modern, modality
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.despejo_context_is_modern.return_value = modern
    backend.resolve_despejo_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_residuo", contexto_json={"contentor_id": 17}
    )

    assert router.handle(conversa, mensagem("1")) == "legado"
    router._contentor.decide_despejo_residuo.assert_not_called()


def test_despejo_residuo_contentor_desabilitado_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual="v24_despejo_residuo", contexto_json={})

    assert router.handle(conversa, mensagem("1")) == "bloqueada-legado"
    backend.despejo_context_is_modern.assert_not_called()


@pytest.mark.parametrize(
    "entrada", ["1", "Confirmar despejo", "confirmar", "✅ Confirmar despejo"]
)
def test_contentor_decide_confirmar_despejo_preserva_aliases(entrada):
    backend = Mock()
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])
    decision = ContentorOperationalAgent(
        lambda: [], backend
    ).decide_despejo_confirmacao(
        SimpleNamespace(contexto_json=contexto),
        mensagem(entrada),
    )

    assert decision == ConfirmarDespejoContentor(contexto)
    backend.assert_not_called()


@pytest.mark.parametrize("entrada", ["2", "Voltar", "↩️ Voltar"])
def test_contentor_decide_voltar_despejo_preserva_aliases_contexto_prompt(entrada):
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_confirmacao(
        SimpleNamespace(contexto_json=contexto),
        mensagem(entrada),
    )

    assert decision == PrepararConformidadeDespejoContentor(contexto)


@pytest.mark.parametrize("entrada", ["3", "Cancelar", "❌ Cancelar"])
def test_contentor_decide_cancelar_despejo_preserva_aliases(entrada):
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])
    decision = ContentorOperationalAgent(
        lambda: [], Mock()
    ).decide_despejo_confirmacao(
        SimpleNamespace(contexto_json=contexto),
        mensagem(entrada),
    )

    assert decision == IdleTransition(
        "Despejo cancelado. Nenhuma foto foi salva e o ativo permanece em andamento."
    )


def test_contentor_confirmacao_despejo_invalida_preserva_estado_contexto():
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_confirmacao", contexto_json=contexto
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_despejo_confirmacao(
        conversa,
        mensagem("talvez"),
    ) == "Escolha Confirmar despejo, Voltar ou Cancelar."
    assert conversa.estado_atual == "v24_despejo_confirmacao"
    assert conversa.contexto_json == contexto


def test_despejo_confirmacao_contentor_usa_boundary_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.despejo_context_is_modern.return_value = True
    backend.resolve_despejo_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.despejo_conformidade_prompt.return_value = "prompt legado"
    backend.confirm_despejo_contentor.return_value = "confirmada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])
    command = ConfirmarDespejoContentor(contexto)
    router._contentor.decide_despejo_confirmacao.return_value = command
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_confirmacao", contexto_json=contexto
    )

    assert router.handle(conversa, mensagem("1")) == "confirmada"
    backend.confirm_despejo_contentor.assert_called_once_with(conversa, contexto)
    backend.handle.assert_not_called()
    backend.apply_operational_transition.assert_not_called()


def test_boundary_despejo_contentor_recomprova_modalidade_e_delega():
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.resolve_despejo_context_modality = Mock(
        return_value=TipoEquipamentoPedido.CONTENTOR
    )
    backend._confirmar_despejo_atual = Mock(return_value="confirmada")
    conversa = object()
    contexto = _contexto_despejo_moderno(fotos_despejo=["foto"])

    assert backend.confirm_despejo_contentor(conversa, contexto) == "confirmada"
    backend._confirmar_despejo_atual.assert_called_once_with(conversa, contexto)


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
def test_despejo_confirmacao_carrinha_ou_indeterminada_permanece_legado(
    monkeypatch, modality
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend = Mock()
    backend.despejo_context_is_modern.return_value = True
    backend.resolve_despejo_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_confirmacao", contexto_json={}
    )

    assert router.handle(conversa, mensagem("1")) == "legado"
    router._contentor.decide_despejo_confirmacao.assert_not_called()


def test_despejo_confirmacao_contexto_antigo_ou_contentor_off_faz_fallback(
    monkeypatch,
):
    backend = Mock()
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_despejo_confirmacao", contexto_json={"contentor_id": 17}
    )

    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=True),
    )
    backend.despejo_context_is_modern.return_value = False
    assert router.handle(conversa, mensagem("1")) == "legado"

    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(feature_contentores_enabled=False),
    )
    assert router.handle(conversa, mensagem("1")) == "legado"
    router._contentor.decide_despejo_confirmacao.assert_not_called()


def test_contentor_recolha_foto_exige_imagem_e_preserva_estado():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_foto",
        contexto_json={"pedido_id": 9, "contentor_id": 17, "fotos_recolha": []},
    )

    assert agent.decide_recolha_foto(conversa, mensagem("texto")) == (
        "Envie uma imagem para continuar."
    )
    assert conversa.estado_atual == "v24_recolha_foto"
    assert conversa.contexto_json["fotos_recolha"] == []


def test_adapter_contexto_recolha_comprova_contentor_sem_escrita():
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.db = Mock()
    backend.db.get.return_value = SimpleNamespace(
        id=17,
        pedido_id=9,
        tipo_equipamento="CONTENTOR",
        status_entrega="ENTREGUE",
        status_recolha="PENDENTE",
    )
    contexto = {"pedido_id": 9, "contentor_id": 17}

    assert backend.resolve_recolha_context_modality(contexto) is (
        TipoEquipamentoPedido.CONTENTOR
    )
    assert contexto == {"pedido_id": 9, "contentor_id": 17}
    backend.db.get.assert_called_once()
    backend.db.commit.assert_not_called()


def test_contentor_recolha_foto_deduplica_e_avanca_sem_backend():
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {
        "pedido_id": 9,
        "contentor_id": 17,
        "fotos_recolha": ["foto-1"],
    }
    conversa = SimpleNamespace(contexto_json=contexto)
    entrada = NormalizedWhatsAppMessage(
        telefone="351900077700",
        tipo="image",
        texto=None,
        media_id="foto-1",
        message_id="foto-msg",
    )

    decision = agent.decide_recolha_foto(conversa, entrada)

    assert decision.next_state == "v24_recolha_foto_acao"
    assert decision.context["fotos_recolha"] == ["foto-1"]
    assert decision.response == (
        "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo"
    )
    assert contexto["fotos_recolha"] == ["foto-1"]
    backend.assert_not_called()


def test_contentor_recolha_outra_foto_preserva_alias_e_estado_alvo():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    conversa = SimpleNamespace(contexto_json={"fotos_recolha": ["foto-1"]})

    decision = agent.decide_recolha_foto_acao(
        conversa,
        mensagem("➕ Outra Foto"),
        avarias_enabled=True,
    )

    assert decision == AdvanceTransition(
        "v24_recolha_foto",
        {"fotos_recolha": ["foto-1"]},
        "Envie a próxima foto.",
    )


def test_contentor_recolha_proximo_passo_com_avarias_on():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_foto_acao(
        SimpleNamespace(contexto_json={"fotos_recolha": ["foto-1"]}),
        mensagem("Próximo Passo"),
        avarias_enabled=True,
    )

    assert decision.next_state == "v24_recolha_avaria"
    assert decision.response == (
        "O equipamento sofreu algum estrago ou avaria na obra?\n\n"
        "1. ✅ Não, está perfeito\n2. 💥 Sim, está estragado"
    )


def test_contentor_recolha_proximo_passo_com_avarias_off_solicita_prompt():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_foto_acao(
        SimpleNamespace(
            contexto_json={
                "fotos_recolha": ["foto-1"],
                "avariado": None,
                "relato_avaria": None,
            }
        ),
        mensagem("2"),
        avarias_enabled=False,
    )

    assert decision == PrepararConfirmacaoRecolhaContentor(
        {"fotos_recolha": ["foto-1"]}
    )


def test_contentor_recolha_foto_acao_invalida_preserva_mensagem():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    assert agent.decide_recolha_foto_acao(
        SimpleNamespace(contexto_json={}),
        mensagem("talvez"),
        avarias_enabled=True,
    ) == "Selecione Outra Foto ou Próximo Passo."


@pytest.mark.parametrize("estado", ["v24_recolha_foto", "v24_recolha_foto_acao"])
def test_recolha_foto_contentor_comprovado_usa_modulo(monkeypatch, estado):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    decision = AdvanceTransition("seguinte", {}, "resposta")
    method = (
        router._contentor.decide_recolha_foto
        if estado == "v24_recolha_foto"
        else router._contentor.decide_recolha_foto_acao
    )
    method.return_value = decision
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={"contentor_id": 17})
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "aplicada"
    backend.apply_operational_transition.assert_called_once_with(conversa, decision)
    backend.handle.assert_not_called()


def test_recolha_foto_acao_avarias_off_reutiliza_prompt_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=False,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.recolha_confirmacao_prompt.return_value = "prompt legado"
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    context = {"pedido_id": 9, "contentor_id": 17, "fotos_recolha": ["foto"]}
    command = PrepararConfirmacaoRecolhaContentor(context)
    router._contentor.decide_recolha_foto_acao.return_value = command
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_foto_acao", contexto_json=context
    )

    assert router.handle(conversa, mensagem("2")) == "aplicada"
    backend.recolha_confirmacao_prompt.assert_called_once_with(context)
    transition = backend.apply_operational_transition.call_args.args[1]
    assert transition == AdvanceTransition(
        "v24_recolha_confirmacao", context, "prompt legado"
    )


def test_recolha_foto_contentor_off_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=False,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_foto", contexto_json={"contentor_id": 17}
    )

    assert router.handle(conversa, mensagem("x")) == "bloqueada-legado"
    backend.resolve_recolha_context_modality.assert_not_called()
    router._contentor.decide_recolha_foto.assert_not_called()


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
@pytest.mark.parametrize("estado", ["v24_recolha_foto", "v24_recolha_foto_acao"])
def test_recolha_foto_carrinha_ou_indeterminada_permanece_legado(
    modality, estado
):
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={"contentor_id": 17})
    entrada = mensagem("2")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_recolha_foto.assert_not_called()
    router._contentor.decide_recolha_foto_acao.assert_not_called()


@pytest.mark.parametrize("entrada", ["1", "Não, está perfeito", "✅ Não, está perfeito"])
def test_contentor_recolha_avaria_nao_prepara_confirmacao(entrada):
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_avaria(
        SimpleNamespace(contexto_json={"pedido_id": 9, "contentor_id": 17}),
        mensagem(entrada),
        avarias_enabled=True,
    )

    assert isinstance(decision, PrepararConfirmacaoRecolhaContentor)
    assert decision.context["avariado"] is False
    assert decision.context["relato_avaria"] is None


@pytest.mark.parametrize("entrada", ["2", "Sim, está estragado", "💥 Sim, está estragado"])
def test_contentor_recolha_avaria_sim_avanca_para_relato(entrada):
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_avaria(
        SimpleNamespace(contexto_json={"pedido_id": 9, "contentor_id": 17}),
        mensagem(entrada),
        avarias_enabled=True,
    )

    assert decision.next_state == "v24_recolha_relato"
    assert decision.context["avariado"] is True
    assert decision.response == "Descreva a avaria com pelo menos 10 caracteres."


def test_contentor_recolha_avaria_invalida_preserva_estado_contexto_e_mensagem():
    contexto = {"pedido_id": 9, "contentor_id": 17, "avariado": None}
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_avaria", contexto_json=contexto
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_recolha_avaria(
        conversa, mensagem("talvez"), avarias_enabled=True
    ) == "Selecione uma das opções de avaria."
    assert conversa.estado_atual == "v24_recolha_avaria"
    assert conversa.contexto_json == contexto


def test_contentor_recolha_relato_curto_preserva_rejeicao():
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_relato",
        contexto_json={"pedido_id": 9, "contentor_id": 17, "avariado": True},
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_recolha_relato(
        conversa, mensagem("curto"), avarias_enabled=True
    ) == "O relato da avaria precisa ter pelo menos 10 caracteres."
    assert conversa.estado_atual == "v24_recolha_relato"


def test_contentor_recolha_relato_valido_preserva_strip_e_prepara_confirmacao():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_relato(
        SimpleNamespace(
            contexto_json={"pedido_id": 9, "contentor_id": 17, "avariado": True}
        ),
        mensagem("  porta lateral danificada  "),
        avarias_enabled=True,
    )

    assert decision == PrepararConfirmacaoRecolhaContentor(
        {
            "pedido_id": 9,
            "contentor_id": 17,
            "avariado": True,
            "relato_avaria": "porta lateral danificada",
        }
    )


@pytest.mark.parametrize("estado", ["v24_recolha_avaria", "v24_recolha_relato"])
def test_contentor_recolha_avarias_off_saneia_residual(estado):
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decide = (
        agent.decide_recolha_avaria
        if estado == "v24_recolha_avaria"
        else agent.decide_recolha_relato
    )

    decision = decide(
        SimpleNamespace(
            contexto_json={
                "pedido_id": 9,
                "contentor_id": 17,
                "avariado": True,
                "relato_avaria": "residual antigo",
            }
        ),
        mensagem("qualquer"),
        avarias_enabled=False,
    )

    assert "avariado" not in decision.context
    assert "relato_avaria" not in decision.context
    assert decision.response_prefix == (
        "A funcionalidade de avarias não está disponível nesta empresa. "
        "O subfluxo foi cancelado com segurança.\n\n"
    )


@pytest.mark.parametrize("estado", ["v24_recolha_avaria", "v24_recolha_relato"])
def test_recolha_avaria_contentor_comprovado_usa_modulo_e_prompt_legado(
    monkeypatch, estado
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=False,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    backend.recolha_confirmacao_prompt.return_value = "prompt legado"
    backend.apply_operational_transition.return_value = "aplicada"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    command = PrepararConfirmacaoRecolhaContentor(
        {"pedido_id": 9, "contentor_id": 17}, "recuperada\n\n"
    )
    decide = (
        router._contentor.decide_recolha_avaria
        if estado == "v24_recolha_avaria"
        else router._contentor.decide_recolha_relato
    )
    decide.return_value = command
    conversa = SimpleNamespace(
        estado_atual=estado,
        contexto_json={"pedido_id": 9, "contentor_id": 17},
    )

    assert router.handle(conversa, mensagem("x")) == "aplicada"
    backend.recolha_confirmacao_prompt.assert_called_once_with(command.context)
    transition = backend.apply_operational_transition.call_args.args[1]
    assert transition == AdvanceTransition(
        "v24_recolha_confirmacao", command.context, "recuperada\n\nprompt legado"
    )


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
@pytest.mark.parametrize("estado", ["v24_recolha_avaria", "v24_recolha_relato"])
def test_recolha_avaria_carrinha_ou_indeterminada_permanece_legado(
    monkeypatch, modality, estado
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(estado_atual=estado, contexto_json={})
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_recolha_avaria.assert_not_called()
    router._contentor.decide_recolha_relato.assert_not_called()


@pytest.mark.parametrize("entrada", ["1", "Confirmar recolha", "confirmar"])
def test_contentor_decide_confirmar_recolha_preservando_aliases(entrada):
    backend = Mock()
    agent = ContentorOperationalAgent(lambda: [], backend)
    contexto = {"pedido_id": 9, "contentor_id": 17, "avariado": False}

    decision = agent.decide_recolha_confirmacao(
        SimpleNamespace(contexto_json=contexto),
        mensagem(entrada),
        avarias_enabled=True,
    )

    assert decision == ConfirmarRecolhaContentor(contexto)
    backend.assert_not_called()


@pytest.mark.parametrize("entrada", ["2", "Cancelar ativo", "cancelar"])
def test_contentor_decide_cancelar_recolha_preservando_aliases(entrada):
    agent = ContentorOperationalAgent(lambda: [], Mock())
    contexto = {"pedido_id": 9, "contentor_id": 17, "avariado": False}

    assert agent.decide_recolha_confirmacao(
        SimpleNamespace(contexto_json=contexto),
        mensagem(entrada),
        avarias_enabled=True,
    ) == CancelarRecolhaContentor(contexto)


def test_contentor_confirmacao_invalida_preserva_estado_contexto_e_mensagem():
    contexto = {"pedido_id": 9, "contentor_id": 17, "avariado": False}
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_confirmacao", contexto_json=contexto
    )
    agent = ContentorOperationalAgent(lambda: [], Mock())

    assert agent.decide_recolha_confirmacao(
        conversa, mensagem("talvez"), avarias_enabled=True
    ) == "Escolha Confirmar recolha ou Cancelar ativo."
    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert conversa.contexto_json == contexto


def test_contentor_confirmacao_avarias_off_saneia_antes_de_confirmar():
    agent = ContentorOperationalAgent(lambda: [], Mock())
    decision = agent.decide_recolha_confirmacao(
        SimpleNamespace(
            contexto_json={
                "pedido_id": 9,
                "contentor_id": 17,
                "avariado": True,
                "relato_avaria": "residual antigo",
            }
        ),
        mensagem("1"),
        avarias_enabled=False,
    )

    assert isinstance(decision, PrepararConfirmacaoRecolhaContentor)
    assert "avariado" not in decision.context
    assert "relato_avaria" not in decision.context
    assert "subfluxo foi cancelado" in decision.response_prefix


@pytest.mark.parametrize(
    "command,boundary",
    [
        (ConfirmarRecolhaContentor({"pedido_id": 9, "contentor_id": 17}), "confirm_recolha_contentor"),
        (CancelarRecolhaContentor({"pedido_id": 9, "contentor_id": 17}), "cancel_recolha_contentor"),
    ],
)
def test_recolha_confirmacao_contentor_usa_apenas_boundary_especifico(
    monkeypatch, command, boundary
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = TipoEquipamentoPedido.CONTENTOR
    getattr(backend, boundary).return_value = "boundary"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    router._contentor.decide_recolha_confirmacao.return_value = command
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_confirmacao",
        contexto_json={"pedido_id": 9, "contentor_id": 17},
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "boundary"
    getattr(backend, boundary).assert_called_once_with(conversa, command.context)
    backend.handle.assert_not_called()
    backend.apply_operational_transition.assert_not_called()


@pytest.mark.parametrize("method", ["confirm_recolha_contentor", "cancel_recolha_contentor"])
def test_boundary_recolha_contentor_recomprova_modalidade_e_delega(method):
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.resolve_recolha_context_modality = Mock(
        return_value=TipoEquipamentoPedido.CONTENTOR
    )
    backend._confirmar_recolha_atual = Mock(return_value="confirmada")
    backend._cancelar_recolha_atual = Mock(return_value="cancelada")
    conversa = object()
    contexto = {"pedido_id": 9, "contentor_id": 17}

    response = getattr(backend, method)(conversa, contexto)

    target = (
        backend._confirmar_recolha_atual
        if method == "confirm_recolha_contentor"
        else backend._cancelar_recolha_atual
    )
    target.assert_called_once_with(conversa, contexto)
    assert response in {"confirmada", "cancelada"}


@pytest.mark.parametrize("method", ["confirm_recolha_contentor", "cancel_recolha_contentor"])
def test_boundary_recolha_contentor_recusa_modalidade_nao_comprovada(method):
    backend = PedidoV24Agent.__new__(PedidoV24Agent)
    backend.resolve_recolha_context_modality = Mock(return_value=None)
    backend._confirmar_recolha_atual = Mock()
    backend._cancelar_recolha_atual = Mock()

    with pytest.raises(ValueError, match="aceita apenas contentores"):
        getattr(backend, method)(object(), {"pedido_id": 9, "contentor_id": 17})

    backend._confirmar_recolha_atual.assert_not_called()
    backend._cancelar_recolha_atual.assert_not_called()


@pytest.mark.parametrize("modality", [TipoEquipamentoPedido.CARRINHA, None])
def test_recolha_confirmacao_carrinha_ou_indeterminada_permanece_legado(
    monkeypatch, modality
):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=True,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.resolve_recolha_context_modality.return_value = modality
    backend.handle.return_value = "legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_confirmacao", contexto_json={}
    )
    entrada = mensagem("1")

    assert router.handle(conversa, entrada) == "legado"
    backend.handle.assert_called_once_with(conversa, entrada)
    router._contentor.decide_recolha_confirmacao.assert_not_called()


def test_recolha_confirmacao_contentor_desabilitado_permanece_legado(monkeypatch):
    monkeypatch.setattr(
        "app.agents.pedido_v24.router.get_settings",
        lambda: SimpleNamespace(
            feature_contentores_enabled=False,
            feature_avarias_enabled=True,
        ),
    )
    backend = Mock()
    backend.handle.return_value = "bloqueada-legado"
    router = PedidoV24OperationalRouter(backend=backend)
    router._contentor = Mock()
    conversa = SimpleNamespace(
        estado_atual="v24_recolha_confirmacao", contexto_json={}
    )

    assert router.handle(conversa, mensagem("1")) == "bloqueada-legado"
    backend.resolve_recolha_context_modality.assert_not_called()


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


def test_carrinha_cadastro_fluxo_preserva_ordem_itens_e_mao_obra_global():
    agent = CarrinhaCadastroAgent()
    ctx = agent.decide_tipo_solicitacao({}).context
    quantidade = agent.decide_quantidade(ctx, mensagem("2"))
    assert quantidade.next_state == "v24_cadastro_nome"
    nome = agent.decide_nome(quantidade.context, mensagem("Cliente Carrinha"))
    telefone = agent.decide_telefone(nome.context, mensagem("351912345678"))
    assert telefone.next_state == "v24_cadastro_data"
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    data = agent.decide_data(telefone.context, mensagem("1"), now)
    assert data.next_state == "v24_cadastro_horario_carrinha"
    horario = agent.decide_horario(data.context, mensagem("14:00"))
    assert horario.next_state == "v24_cadastro_residuo"
    primeiro = agent.decide_residuo(horario.context, mensagem("1"))
    assert primeiro.next_state == "v24_cadastro_residuo"
    segundo = agent.decide_residuo(primeiro.context, mensagem("2"))
    assert segundo.next_state == "v24_cadastro_mao_obra"
    assert [item["tipo_equipamento"] for item in segundo.context["itens"]] == [
        "CARRINHA", "CARRINHA"
    ]
    mao_obra = agent.decide_mao_obra(segundo.context, mensagem("1"))
    assert mao_obra.next_state == "v24_cadastro_valor"
    assert mao_obra.context["precisa_mao_de_obra"] is True
    assert all(item["precisa_mao_de_obra"] is False for item in mao_obra.context["itens"])


@pytest.mark.parametrize("choice", ["2", "carrinha", "carrinhas"])
def test_router_selecao_moderna_carrinha_entra_no_novo_agente(db_session, choice):
    backend = PedidoV24Agent(db_session)
    backend.handle = Mock(return_value="legado")
    router = PedidoV24OperationalRouter(backend=backend)
    conversa = SimpleNamespace(estado_atual="v24_cadastro_tipo_solicitacao", contexto_json={})

    resposta = router.handle(conversa, mensagem(choice))

    assert resposta == "🔢 Quantas carrinhas são necessárias para este pedido?"
    assert conversa.contexto_json == {"tipo_solicitacao": "CARRINHA"}
    backend.handle.assert_not_called()


def test_carrinha_cadastro_validacoes_especificas_e_confirmacao():
    agent = CarrinhaCadastroAgent()
    assert agent.decide_quantidade({"tipo_solicitacao": "CARRINHA"}, mensagem("0")) == "Informe uma quantidade entre 1 e 50."
    assert "Horário inválido" in agent.decide_horario({}, mensagem("25:00"))
    proven = {
        "tipo_solicitacao": "CARRINHA", "quantidade": 1,
        "itens": [{"tipo_equipamento": "CARRINHA"}], "residuos": ["Entulho Limpo"],
        "nome": "Cliente", "telefone": "351912345678",
        "data": "2026-08-13T00:00:00+00:00", "horario_agendado": "14:00",
    }
    assert classify_carrinha_cadastro(proven) is CarrinhaCadastroModality.CARRINHA_PROVEN
    assert isinstance(agent.decide_confirmacao(proven, mensagem("confirmar")), ConfirmarCadastroCarrinha)
    assert agent.decide_confirmacao(proven, mensagem("cancelar")) == IdleTransition(
        "Pedido cancelado. Nenhum pedido foi criado."
    )


def test_carrinha_cadastro_edicao_hora_atualiza_itens_e_retorna_resumo():
    agent = CarrinhaCadastroAgent()
    context = {
        "tipo_solicitacao": "CARRINHA", "quantidade": 1,
        "itens": [{"tipo_equipamento": "CARRINHA", "horario_agendado": "10:00"}],
        "residuos": ["Entulho Limpo"], "editing_field": "hora_entrega",
        "nome": "Cliente", "telefone": "351912345678",
        "data": "2026-08-13T00:00:00+00:00", "horario_agendado": "10:00",
    }
    decision = agent.decide_edicao(context, mensagem("11:30"), now=datetime.now(timezone.utc))
    assert decision.next_state == "v24_cadastro_confirmacao"
    assert decision.context["horario_agendado"] == "11:30"
    assert decision.context["itens"][0]["horario_agendado"] == "11:30"


def test_boundary_cadastro_carrinha_recomprova_modalidade_e_delega_finish(db_session):
    backend = PedidoV24Agent(db_session)
    backend._finish_cadastro = Mock(return_value="criado")
    conversa = SimpleNamespace(estado_atual="v24_cadastro_confirmacao")
    carrinha = {
        "tipo_solicitacao": "CARRINHA", "quantidade": 1,
        "itens": [{"tipo_equipamento": "CARRINHA"}],
        "nome": "Cliente", "telefone": "351912345678",
        "data": "2026-08-13T00:00:00+00:00", "horario_agendado": "14:00",
    }
    assert backend.confirmar_cadastro_carrinha(conversa, carrinha) == "criado"
    backend._finish_cadastro.assert_called_once()
    contentor = {**carrinha, "tipo_solicitacao": "CONTENTOR", "itens": [{"tipo_equipamento": "CONTENTOR"}]}
    assert backend.confirmar_cadastro_carrinha(conversa, contentor) is None


def test_carrinha_cadastro_agent_nao_declara_infraestrutura_persistente():
    source = Path(inspect.getsourcefile(CarrinhaCadastroAgent)).read_text(encoding="utf-8")
    assert "PedidoService" not in source
    assert ".db" not in source
    assert ".query(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


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
