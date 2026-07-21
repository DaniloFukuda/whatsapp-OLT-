from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.operador_service as operador_service_module
from app.models.operador import Operador, PerfilOperador
from app.repositories.operador_repository import OperadorRepository
from app.services.operador_service import AccessDecision, OperadorService
from app.services.seed_service import SeedService


@pytest.fixture(autouse=True)
def settings_in_memory(monkeypatch):
    settings = SimpleNamespace(authorized_operator_phone="", authorized_operator_phones="")
    getter = MagicMock(return_value=settings)
    monkeypatch.setattr(operador_service_module, "get_settings", getter)
    return settings, getter


@pytest.fixture
def operator_factory(db_session):
    def create(phone, profile=PerfilOperador.GESTOR, active=True):
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


def test_repositorio_busca_operador_por_telefone_whatsapp(db_session):
    operador = Operador(
        telefone_whatsapp="351900000100",
        nome_operador="Operador Um",
        perfil=PerfilOperador.FUNCIONARIO,
    )
    db_session.add(operador)
    db_session.commit()

    repository = OperadorRepository(db_session)

    assert repository.get_by_telefone("351900000100") == operador
    assert repository.get_by_telefone("351900000999") is None
    assert repository.has_any() is True


def test_servico_normaliza_telefone_e_retorna_perfil_do_operador_ativo(db_session):
    db_session.add(
        Operador(
            telefone_whatsapp="351900000101",
            nome_operador="Gestor",
            perfil=PerfilOperador.GESTOR,
        )
    )
    db_session.commit()

    service = OperadorService(db_session)

    assert service.buscar_por_telefone("+351 900 000 101").perfil == PerfilOperador.GESTOR
    assert service.verificar_autorizacao("+351 900 000 101") is True
    assert service.obter_perfil("+351 900 000 101") == PerfilOperador.GESTOR


def test_servico_nao_retorna_perfil_para_operador_inativo(db_session):
    db_session.add(
        Operador(
            telefone_whatsapp="351900000102",
            nome_operador="Funcionario",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=False,
        )
    )
    db_session.commit()

    service = OperadorService(db_session)

    assert service.verificar_autorizacao("351900000102") is False
    assert service.obter_perfil("351900000102") is None


def assert_denied(decision: AccessDecision, origin: str | None = None):
    assert decision.autorizado is False
    assert decision.perfil is None
    if origin:
        assert decision.origem == origin


def test_olt_operator_001_empty_table_and_empty_fallback_denies(db_session):
    """OLT-OPERATOR-001"""
    assert_denied(OperadorService(db_session).decidir_acesso("351911000001"), "NENHUMA")


def test_olt_operator_002_single_fallback_match_authorizes_manager(db_session, settings_in_memory):
    """OLT-OPERATOR-002"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "351911000002"
    decision = OperadorService(db_session).decidir_acesso("351911000002")
    assert (decision.autorizado, decision.perfil, decision.origem) == (True, PerfilOperador.GESTOR, "FALLBACK")


def test_olt_operator_003_single_fallback_mismatch_denies(db_session, settings_in_memory):
    """OLT-OPERATOR-003"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "351911000003"
    assert_denied(OperadorService(db_session).decidir_acesso("351911000099"), "FALLBACK")


@pytest.mark.parametrize(
    ("operator_id", "multiple", "phone", "authorized"),
    [
        ("OLT-OPERATOR-004", "351911000004,351911000005", "351911000005", True),
        ("OLT-OPERATOR-005", "351911000004,351911000005", "351911000006", False),
        ("OLT-OPERATOR-006", "  +351 911 000 006 , 351 911 000 007 ", "351911000006", True),
        ("OLT-OPERATOR-007", "351911000007, 351911000007", "351911000007", True),
    ],
    ids=lambda value: value if str(value).startswith("OLT-OPERATOR") else None,
)
def test_multiple_fallback_contracts(operator_id, multiple, phone, authorized, db_session, settings_in_memory):
    settings, _ = settings_in_memory
    settings.authorized_operator_phones = multiple
    decision = OperadorService(db_session).decidir_acesso(phone)
    assert operator_id
    assert decision.autorizado is authorized
    assert decision.perfil == (PerfilOperador.GESTOR if authorized else None)


def test_olt_operator_008_single_and_multiple_fallback_are_unioned(db_session, settings_in_memory):
    """OLT-OPERATOR-008"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "351911000008"
    settings.authorized_operator_phones = "351911000009"
    service = OperadorService(db_session)
    assert service.decidir_acesso("351911000008").autorizado is True
    assert service.decidir_acesso("351911000009").autorizado is True


def test_olt_operator_009_empty_phone_is_denied(db_session):
    """OLT-OPERATOR-009"""
    decision = OperadorService(db_session).decidir_acesso(None)
    assert_denied(decision, "NENHUMA")
    assert decision.motivo_interno == "TELEFONE_INVALIDO"


def test_olt_operator_010_implausible_phones_are_denied(db_session):
    """OLT-OPERATOR-010"""
    service = OperadorService(db_session)
    for phone in ("abc", "123", "1234567890123456"):
        decision = service.decidir_acesso(phone)
        assert_denied(decision, "NENHUMA")
        assert decision.motivo_interno == "TELEFONE_INVALIDO"


def test_olt_operator_011_equivalent_formatted_fallback_matches(db_session, settings_in_memory):
    """OLT-OPERATOR-011"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "+351 911 000 011"
    decision = OperadorService(db_session).decidir_acesso("00351 911-000-011")
    assert decision.autorizado is True
    assert decision.perfil == PerfilOperador.GESTOR


@pytest.mark.parametrize(
    ("operator_id", "profile"),
    [("OLT-OPERATOR-012", PerfilOperador.GESTOR), ("OLT-OPERATOR-013", PerfilOperador.FUNCIONARIO)],
    ids=lambda value: value if str(value).startswith("OLT-OPERATOR") else None,
)
def test_active_persisted_operator_is_authorized(operator_id, profile, db_session, operator_factory):
    phone = "351911000012" if profile == PerfilOperador.GESTOR else "351911000013"
    operator_factory(phone, profile)
    decision = OperadorService(db_session).decidir_acesso(phone)
    assert operator_id
    assert (decision.autorizado, decision.perfil, decision.origem) == (True, profile, "TABELA")


def test_olt_operator_014_inactive_operator_is_denied(db_session, operator_factory):
    """OLT-OPERATOR-014"""
    operator_factory("351911000014", active=False)
    assert_denied(OperadorService(db_session).decidir_acesso("351911000014"), "TABELA")


def test_olt_operator_015_unknown_phone_is_denied_with_populated_table(db_session, operator_factory):
    """OLT-OPERATOR-015"""
    operator_factory("351911000150")
    assert_denied(OperadorService(db_session).decidir_acesso("351911000015"), "TABELA")


@pytest.mark.parametrize(
    ("operator_id", "inactive"),
    [("OLT-OPERATOR-016", False), ("OLT-OPERATOR-017", True)],
    ids=lambda value: value if str(value).startswith("OLT-OPERATOR") else None,
)
def test_populated_table_disables_fallback(operator_id, inactive, db_session, operator_factory, settings_in_memory):
    settings, _ = settings_in_memory
    phone = "351911000017" if inactive else "351911000016"
    settings.authorized_operator_phone = phone
    operator_factory("351911000160")
    if inactive:
        operator_factory(phone, active=False)
    assert operator_id
    assert_denied(OperadorService(db_session).decidir_acesso(phone), "TABELA")


def test_olt_operator_018_table_profile_wins_over_fallback(db_session, operator_factory, settings_in_memory):
    """OLT-OPERATOR-018"""
    settings, _ = settings_in_memory
    phone = "351911000018"
    settings.authorized_operator_phone = phone
    operator_factory(phone, PerfilOperador.FUNCIONARIO)
    decision = OperadorService(db_session).decidir_acesso(phone)
    assert decision.perfil == PerfilOperador.FUNCIONARIO
    assert decision.origem == "TABELA"


@pytest.mark.parametrize(
    ("operator_id", "profile"),
    [("OLT-OPERATOR-019", PerfilOperador.GESTOR), ("OLT-OPERATOR-020", PerfilOperador.FUNCIONARIO)],
    ids=lambda value: value if str(value).startswith("OLT-OPERATOR") else None,
)
def test_decision_preserves_known_profile(operator_id, profile, db_session, operator_factory):
    phone = "351911000019" if profile == PerfilOperador.GESTOR else "351911000020"
    operator_factory(phone, profile)
    assert operator_id
    assert OperadorService(db_session).decidir_acesso(phone).perfil == profile


def test_olt_operator_021_null_profile_is_rejected_and_runtime_fails_closed(db_session):
    """OLT-OPERATOR-021"""
    db_session.add(Operador(telefone_whatsapp="351911000021", nome_operador="Nulo", perfil=None))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    service = OperadorService(db_session)
    service.repository.has_any = MagicMock(return_value=True)
    service.repository.get_by_telefone = MagicMock(
        return_value=SimpleNamespace(ativo=True, perfil=None)
    )
    assert_denied(service.decidir_acesso("351911000021"), "TABELA")


def test_olt_operator_022_invalid_profile_is_rejected_and_runtime_fails_closed(db_session):
    """OLT-OPERATOR-022"""
    db_session.add(Operador(telefone_whatsapp="351911000022", nome_operador="Invalido", perfil="ADMIN"))
    with pytest.raises((IntegrityError, LookupError)):
        db_session.commit()
    db_session.rollback()
    service = OperadorService(db_session)
    service.repository.has_any = MagicMock(return_value=True)
    service.repository.get_by_telefone = MagicMock(
        return_value=SimpleNamespace(ativo=True, perfil="ADMIN")
    )
    assert_denied(service.decidir_acesso("351911000022"), "TABELA")


def test_olt_operator_023_noncanonical_stored_phone_fails_closed(db_session, operator_factory):
    """OLT-OPERATOR-023"""
    operator_factory("+351 911 000 023")
    assert_denied(OperadorService(db_session).decidir_acesso("351911000023"), "TABELA")


def test_olt_operator_024_equivalent_inputs_find_canonical_operator(db_session, operator_factory):
    """OLT-OPERATOR-024"""
    operator_factory("351911000024", PerfilOperador.FUNCIONARIO)
    service = OperadorService(db_session)
    decisions = [service.decidir_acesso(value) for value in ("+351 911 000 024", "00351-911-000-024")]
    assert all(decision.perfil == PerfilOperador.FUNCIONARIO for decision in decisions)


def test_olt_operator_025_repeated_decision_is_deterministic(db_session, operator_factory):
    """OLT-OPERATOR-025"""
    operator_factory("351911000025")
    service = OperadorService(db_session)
    assert service.decidir_acesso("351911000025") == service.decidir_acesso("351911000025")


def test_olt_operator_026_inactivation_is_visible_on_next_decision(db_session, operator_factory):
    """OLT-OPERATOR-026"""
    operator = operator_factory("351911000026")
    service = OperadorService(db_session)
    assert service.decidir_acesso(operator.telefone_whatsapp).autorizado is True
    operator.ativo = False
    db_session.commit()
    assert_denied(service.decidir_acesso(operator.telefone_whatsapp), "TABELA")


@pytest.mark.parametrize(
    ("operator_id", "before", "after"),
    [
        ("OLT-OPERATOR-027", PerfilOperador.FUNCIONARIO, PerfilOperador.GESTOR),
        ("OLT-OPERATOR-028", PerfilOperador.GESTOR, PerfilOperador.FUNCIONARIO),
    ],
    ids=lambda value: value if str(value).startswith("OLT-OPERATOR") else None,
)
def test_profile_change_is_visible_next_decision(operator_id, before, after, db_session, operator_factory):
    phone = "351911000027" if after == PerfilOperador.GESTOR else "351911000028"
    operator = operator_factory(phone, before)
    service = OperadorService(db_session)
    assert service.decidir_acesso(phone).perfil == before
    operator.perfil = after
    db_session.commit()
    assert operator_id
    assert service.decidir_acesso(phone).perfil == after


def test_olt_operator_030_first_operator_disables_bootstrap_fallback(db_session, operator_factory, settings_in_memory):
    """OLT-OPERATOR-030"""
    settings, _ = settings_in_memory
    phone = "351911000030"
    settings.authorized_operator_phone = phone
    service = OperadorService(db_session)
    assert service.decidir_acesso(phone).autorizado is True
    operator_factory("351911000300")
    assert_denied(service.decidir_acesso(phone), "TABELA")


def test_olt_operator_031_fallback_is_ignored_after_first_operator(db_session, operator_factory, settings_in_memory):
    """OLT-OPERATOR-031"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "351911000031"
    operator_factory("351911000310")
    assert_denied(OperadorService(db_session).decidir_acesso("351911000031"), "TABELA")


def test_olt_operator_032_fallback_does_not_create_operator(db_session, settings_in_memory):
    """OLT-OPERATOR-032"""
    settings, _ = settings_in_memory
    settings.authorized_operator_phone = "351911000032"
    assert OperadorService(db_session).decidir_acesso("351911000032").autorizado is True
    assert db_session.query(Operador).count() == 0


def test_olt_operator_033_seed_does_not_create_operator(db_session):
    """OLT-OPERATOR-033"""
    SeedService(db_session).seed_contentores_iniciais()
    assert db_session.query(Operador).count() == 0


def test_olt_operator_035_settings_are_in_memory(db_session, settings_in_memory):
    """OLT-OPERATOR-035"""
    settings, getter = settings_in_memory
    settings.authorized_operator_phone = "351911000035"
    assert OperadorService(db_session).decidir_acesso("351911000035").autorizado is True
    getter.assert_called_once_with()


def test_olt_operator_038_wrappers_match_access_decision(db_session, operator_factory):
    """OLT-OPERATOR-038"""
    operator_factory("351911000038", PerfilOperador.FUNCIONARIO)
    service = OperadorService(db_session)
    decision = service.decidir_acesso("351911000038")
    assert service.verificar_autorizacao("351911000038") == decision.autorizado
    assert service.obter_perfil("351911000038") == decision.perfil


def test_olt_operator_039_decision_is_immutable_and_internally_consistent(db_session, operator_factory):
    """OLT-OPERATOR-039"""
    operator_factory("351911000039", PerfilOperador.GESTOR)
    decision = OperadorService(db_session).decidir_acesso("351911000039")
    assert decision.autorizado is True
    assert decision.perfil == PerfilOperador.GESTOR
    with pytest.raises((AttributeError, TypeError)):
        decision.perfil = PerfilOperador.FUNCIONARIO


def test_olt_operator_040_single_decision_queries_each_source_at_most_once(db_session, operator_factory, settings_in_memory):
    """OLT-OPERATOR-040"""
    _, settings_getter = settings_in_memory
    operator_factory("351911000040")
    service = OperadorService(db_session)
    service.repository.has_any = MagicMock(wraps=service.repository.has_any)
    service.repository.get_by_telefone = MagicMock(wraps=service.repository.get_by_telefone)
    decision = service.decidir_acesso("351911000040")
    assert decision.autorizado is True
    service.repository.has_any.assert_called_once_with()
    service.repository.get_by_telefone.assert_called_once_with("351911000040")
    settings_getter.assert_not_called()
