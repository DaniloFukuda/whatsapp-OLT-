from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
import app.services.operador_service as operador_service_module

from app.agents.whatsapp_router_agent import (
    FORBIDDEN_MESSAGE,
    UNAUTHORIZED_MESSAGE,
    WhatsappRouterAgent,
)
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.contentor import Contentor, StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.services.seed_service import SeedService


UNAUTHORIZED_PHONE = "351911000001"
EMPLOYEE_PHONE = "351911000002"
MANAGER_PHONE = "351911000003"
INACTIVE_PHONE = "351911000004"
CLIENT_SENTINEL = "CLIENTE_SIGILOSO"
CONTENTOR_SENTINEL = "CONTENTOR_SIGILOSO"


def message(text: str, phone: str) -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=phone, tipo="text", texto=text, message_id=f"auth-{text}")


@pytest.fixture(autouse=True)
def controlled_fallback(monkeypatch):
    settings = SimpleNamespace(
        authorized_operator_phone="351911999999",
        authorized_operator_phones="",
    )
    monkeypatch.setattr(operador_service_module, "get_settings", lambda: settings)
    yield settings


@pytest.fixture
def operator_factory(db_session):
    def create(phone: str, profile: PerfilOperador, active: bool = True) -> Operador:
        operator = Operador(
            telefone_whatsapp=phone,
            nome_operador=f"Operador {phone}",
            perfil=profile,
            ativo=active,
        )
        db_session.add(operator)
        db_session.commit()
        return operator

    return create


@pytest.fixture
def active_manager(operator_factory):
    return operator_factory(MANAGER_PHONE, PerfilOperador.GESTOR)


@pytest.fixture
def active_employee(operator_factory):
    return operator_factory(EMPLOYEE_PHONE, PerfilOperador.FUNCIONARIO)


@pytest.fixture
def sensitive_inventory(db_session):
    contentor = Contentor(codigo=CONTENTOR_SENTINEL, status=StatusContentor.DISPONIVEL)
    db_session.add(contentor)
    db_session.commit()
    return contentor


def install_business_spies(router: WhatsappRouterAgent) -> list[MagicMock]:
    spies = []
    targets = (
        (router.contentor_service, "listar_contentores"),
        (router.aluguer_service, "listar_cadastrados_nos_ultimos_dias"),
        (router.aluguer_service, "listar_vencendo_amanha"),
        (router.aluguer_service, "listar_atrasados"),
        (router.aluguer_service, "resolver_pendencia_carga"),
        (router.aluguer_service, "resolver_pendencia_avaria"),
        (router.pedido_service, "resolver"),
        (router.contentor_agent, "listar_status"),
        (router.contentor_agent, "start_alteracao_status"),
        (router.contentor_agent, "start_exclusao"),
        (router.gestao_aluguer_agent, "start_alteracao"),
        (router.gestao_aluguer_agent, "start_exclusao"),
        (router.renovacao_agent, "start"),
    )
    for owner, name in targets:
        spy = MagicMock(name=f"{type(owner).__name__}.{name}")
        setattr(owner, name, spy)
        spies.append(spy)
    return spies


def assert_spies_not_called(spies: list[MagicMock]) -> None:
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [
        ("OLT-AUTH-001", "lista"),
        ("OLT-AUTH-002", "disponiveis"),
        ("OLT-AUTH-003", "alugados"),
        ("OLT-AUTH-004", "vencendo"),
        ("OLT-AUTH-005", "atrasados"),
        ("OLT-AUTH-006", "contentores"),
        ("OLT-AUTH-007", "status"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_unauthorized_queries_are_blocked_before_business_access(
    auth_id, command, db_session, active_manager
):
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message(command, UNAUTHORIZED_PHONE))

    assert auth_id
    assert response == UNAUTHORIZED_MESSAGE
    assert_spies_not_called(spies)
    assert db_session.query(ConversaWhatsApp).filter_by(telefone=UNAUTHORIZED_PHONE).count() == 0


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [
        ("OLT-AUTH-008", "alterar"),
        ("OLT-AUTH-009", "modificar"),
        ("OLT-AUTH-010", "excluir"),
        ("OLT-AUTH-011", "deletar"),
        ("OLT-AUTH-012", "renovar"),
        ("OLT-AUTH-013", "prorrogar"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_employee_cannot_start_aluguer_administration(auth_id, command, db_session, active_employee):
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message(command, EMPLOYEE_PHONE))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=EMPLOYEE_PHONE).one()

    assert auth_id
    assert response == FORBIDDEN_MESSAGE
    assert_spies_not_called(spies)
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [
        ("OLT-AUTH-014", "alterar contentor"),
        ("OLT-AUTH-015", "status contentor"),
        ("OLT-AUTH-016", "apagar"),
        ("OLT-AUTH-017", "remover"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_employee_cannot_start_contentor_administration(auth_id, command, db_session, active_employee):
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message(command, EMPLOYEE_PHONE))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=EMPLOYEE_PHONE).one()

    assert auth_id
    assert response == FORBIDDEN_MESSAGE
    assert_spies_not_called(spies)
    assert conversa.estado_atual == "idle"


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [
        ("OLT-AUTH-018", "resolver carga 41"),
        ("OLT-AUTH-019", "resolver avaria 42"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_employee_cannot_resolve_pending_items(auth_id, command, db_session, active_employee):
    router = WhatsappRouterAgent(db_session)
    resolver = MagicMock()
    router._resolver_pendencia = resolver

    response = router.handle(message(command, EMPLOYEE_PHONE))

    assert auth_id
    assert response == FORBIDDEN_MESSAGE
    resolver.assert_not_called()


@pytest.mark.parametrize(
    ("auth_id", "command", "expected"),
    [
        ("OLT-AUTH-020", "lista", CONTENTOR_SENTINEL),
        ("OLT-AUTH-021", "disponiveis", CONTENTOR_SENTINEL),
        ("OLT-AUTH-022", "contentores", CONTENTOR_SENTINEL),
        ("OLT-AUTH-023", "status", CONTENTOR_SENTINEL),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_employee_keeps_operational_queries(
    auth_id, command, expected, db_session, active_employee, sensitive_inventory
):
    response = WhatsappRouterAgent(db_session).handle(message(command, EMPLOYEE_PHONE))

    assert auth_id
    assert expected in response
    assert CLIENT_SENTINEL not in response
    assert "telefone" not in response.lower()
    assert "valor" not in response.lower()
    assert "€" not in response


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [
        ("OLT-AUTH-024", "alugados"),
        ("OLT-AUTH-025", "vencendo"),
        ("OLT-AUTH-026", "atrasados"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_employee_cannot_access_commercial_queries(auth_id, command, db_session, active_employee):
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message(command, EMPLOYEE_PHONE))

    assert auth_id
    assert response == FORBIDDEN_MESSAGE
    assert CLIENT_SENTINEL not in response
    assert_spies_not_called(spies)


@pytest.mark.parametrize(
    ("auth_id", "command", "target", "expected_state"),
    [
        ("OLT-AUTH-027", "alterar", "alter_aluguer", "alteracao_aguardando_item"),
        ("OLT-AUTH-028", "excluir", "delete_aluguer", "exclusao_aguardando_item"),
        ("OLT-AUTH-029", "renovar", "renew", "renovacao_aguardando_item"),
        ("OLT-AUTH-030", "alterar contentor", "alter_contentor", "contentor_alteracao_aguardando_item"),
        ("OLT-AUTH-031", "remover", "delete_contentor", "contentor_exclusao_aguardando_item"),
        ("OLT-AUTH-032", "resolver carga 41", "resolve", "idle"),
        ("OLT-AUTH-033", "resolver avaria 42", "resolve", "idle"),
    ],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_manager_keeps_administrative_operations(
    auth_id, command, target, expected_state, db_session, active_manager
):
    router = WhatsappRouterAgent(db_session)
    called = MagicMock()

    def start(conversa):
        called(command)
        conversa.estado_atual = expected_state
        conversa.contexto_json = {}
        db_session.commit()
        return "OPERAÇÃO ADMINISTRATIVA INICIADA"

    if target == "alter_aluguer":
        router.gestao_aluguer_agent.start_alteracao = start
    elif target == "delete_aluguer":
        router.aluguer_service.listar_cadastrados_nos_ultimos_dias = MagicMock(return_value=[object()])
        router.gestao_aluguer_agent.start_exclusao = start
    elif target == "renew":
        router.renovacao_agent.start = start
    elif target == "alter_contentor":
        router.contentor_agent.start_alteracao_status = start
    elif target == "delete_contentor":
        router.contentor_agent.start_exclusao = start
    else:
        router._resolver_pendencia = MagicMock(return_value="PENDÊNCIA RESOLVIDA")

    response = router.handle(message(command, MANAGER_PHONE))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=MANAGER_PHONE).one()

    assert auth_id
    assert response in {"OPERAÇÃO ADMINISTRATIVA INICIADA", "PENDÊNCIA RESOLVIDA"}
    assert conversa.estado_atual == expected_state
    if target == "resolve":
        router._resolver_pendencia.assert_called_once()
    else:
        called.assert_called_once_with(command)


def test_olt_auth_034_unauthorized_mutation_does_not_create_conversation(db_session, active_manager):
    """OLT-AUTH-034"""
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message("alterar", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert_spies_not_called(spies)
    assert db_session.query(ConversaWhatsApp).filter_by(telefone=UNAUTHORIZED_PHONE).count() == 0


@pytest.mark.parametrize(
    ("auth_id", "command"),
    [("OLT-AUTH-035", "lista"), ("OLT-AUTH-036", "renovar")],
    ids=lambda value: value if str(value).startswith("OLT-AUTH") else None,
)
def test_inactive_operator_is_blocked(auth_id, command, db_session, operator_factory):
    operator_factory(INACTIVE_PHONE, PerfilOperador.GESTOR, active=False)
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)

    response = router.handle(message(command, INACTIVE_PHONE))

    assert auth_id
    assert response == UNAUTHORIZED_MESSAGE
    assert_spies_not_called(spies)
    assert db_session.query(ConversaWhatsApp).filter_by(telefone=INACTIVE_PHONE).count() == 0


def test_olt_auth_037_block_response_does_not_disclose_data(
    db_session, active_manager, sensitive_inventory
):
    """OLT-AUTH-037"""
    response = WhatsappRouterAgent(db_session).handle(message("lista", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    for forbidden in (CLIENT_SENTINEL, CONTENTOR_SENTINEL, "GESTOR", "FUNCIONARIO", "Menu principal"):
        assert forbidden not in response


def test_olt_auth_038_gate_runs_before_business_services(db_session, active_manager, monkeypatch):
    """OLT-AUTH-038"""
    router = WhatsappRouterAgent(db_session)
    spies = install_business_spies(router)
    session_get = MagicMock(wraps=db_session.get)
    monkeypatch.setattr(db_session, "get", session_get)

    response = router.handle(message("atrasados", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert_spies_not_called(spies)
    assert session_get.call_count >= 1
    assert all(call.args[0] is Operador for call in session_get.call_args_list)


def test_olt_auth_039_block_does_not_create_operational_conversation(db_session, active_manager):
    """OLT-AUTH-039"""
    existing = ConversaWhatsApp(
        telefone=UNAUTHORIZED_PHONE,
        estado_atual="estado_bloqueado_preexistente",
        contexto_json={"sentinela": "NAO_ALTERAR"},
    )
    db_session.add(existing)
    db_session.commit()
    original_updated_at = existing.atualizado_em
    before = db_session.query(ConversaWhatsApp).count()

    response = WhatsappRouterAgent(db_session).handle(message("alterar", UNAUTHORIZED_PHONE))
    db_session.refresh(existing)

    assert response == UNAUTHORIZED_MESSAGE
    assert db_session.query(ConversaWhatsApp).count() == before
    assert existing.estado_atual == "estado_bloqueado_preexistente"
    assert existing.contexto_json == {"sentinela": "NAO_ALTERAR"}
    assert existing.atualizado_em == original_updated_at


def test_olt_auth_040_unknown_command_does_not_reveal_internal_menu(db_session, active_manager):
    """OLT-AUTH-040"""
    response = WhatsappRouterAgent(db_session).handle(message("comando-interno-x", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert "menu" not in response.lower()
    assert "perfil" not in response.lower()
    assert CONTENTOR_SENTINEL not in response
    assert db_session.query(ConversaWhatsApp).filter_by(telefone=UNAUTHORIZED_PHONE).count() == 0


def test_olt_operator_029_existing_conversation_does_not_survive_operator_inactivation(
    db_session, active_manager
):
    """OLT-OPERATOR-029"""
    conversation = ConversaWhatsApp(
        telefone=MANAGER_PHONE,
        estado_atual="estado_administrativo_preexistente",
        contexto_json={"sentinela": "NAO_ALTERAR"},
    )
    db_session.add(conversation)
    db_session.commit()
    original_updated_at = conversation.atualizado_em
    active_manager.ativo = False
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(message("menu", MANAGER_PHONE))
    db_session.refresh(conversation)

    assert response == UNAUTHORIZED_MESSAGE
    assert conversation.estado_atual == "estado_administrativo_preexistente"
    assert conversation.contexto_json == {"sentinela": "NAO_ALTERAR"}
    assert conversation.atualizado_em == original_updated_at


def test_olt_operator_034_simulated_bootstrap_does_not_open_access(
    db_session, controlled_fallback
):
    """OLT-OPERATOR-034"""
    controlled_fallback.authorized_operator_phone = ""
    controlled_fallback.authorized_operator_phones = ""
    SeedService(db_session).seed_contentores_iniciais()

    response = WhatsappRouterAgent(db_session).handle(message("menu", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert db_session.query(Operador).count() == 0
    assert db_session.query(ConversaWhatsApp).count() == 0


def test_olt_operator_036_block_does_not_disclose_fallback_value(
    db_session, controlled_fallback
):
    """OLT-OPERATOR-036"""
    fallback_sentinel = "351911999936"
    controlled_fallback.authorized_operator_phone = fallback_sentinel

    response = WhatsappRouterAgent(db_session).handle(message("menu", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert fallback_sentinel not in response
    assert "fallback" not in response.lower()


def test_olt_operator_037_block_does_not_disclose_operator_table(db_session, active_manager):
    """OLT-OPERATOR-037"""
    response = WhatsappRouterAgent(db_session).handle(message("menu", UNAUTHORIZED_PHONE))

    assert response == UNAUTHORIZED_MESSAGE
    assert "operador" not in response.lower()
    assert "tabela" not in response.lower()
    assert "perfil" not in response.lower()


def test_router_uses_one_access_decision_per_message(db_session, active_manager):
    router = WhatsappRouterAgent(db_session)
    router.operador_service.decidir_acesso = MagicMock(
        wraps=router.operador_service.decidir_acesso
    )
    router.operador_service.verificar_autorizacao = MagicMock(
        side_effect=AssertionError("wrapper de autorização não deve ser chamado")
    )
    router.operador_service.obter_perfil = MagicMock(
        side_effect=AssertionError("wrapper de perfil não deve ser chamado")
    )

    response = router.handle(message("menu", MANAGER_PHONE))

    assert "Menu principal" in response
    router.operador_service.decidir_acesso.assert_called_once_with(MANAGER_PHONE)
    router.operador_service.verificar_autorizacao.assert_not_called()
    router.operador_service.obter_perfil.assert_not_called()
