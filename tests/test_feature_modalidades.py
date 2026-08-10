"""Testes da configuração independente das modalidades operacionais."""

import pytest
from pydantic import ValidationError

import app.core.config as config_module
from app.core.config import Settings, get_settings


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


def settings_sem_arquivo(**valores):
    return Settings(_env_file=None, **valores)


def test_defaults_habilitam_as_duas_modalidades():
    settings = settings_sem_arquivo()

    assert settings.feature_contentores_enabled is True
    assert settings.feature_carrinhas_enabled is True


@pytest.mark.parametrize(
    ("contentores", "carrinhas"),
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ],
)
def test_quatro_combinacoes_sao_representaveis(contentores, carrinhas):
    settings = settings_sem_arquivo(
        feature_contentores_enabled=contentores,
        feature_carrinhas_enabled=carrinhas,
    )

    assert settings.feature_contentores_enabled is contentores
    assert settings.feature_carrinhas_enabled is carrinhas


def test_alterar_contentores_nao_altera_carrinhas():
    habilitado = settings_sem_arquivo(feature_contentores_enabled=True)
    desabilitado = settings_sem_arquivo(feature_contentores_enabled=False)

    assert habilitado.feature_carrinhas_enabled is True
    assert desabilitado.feature_carrinhas_enabled is True


def test_alterar_carrinhas_nao_altera_contentores():
    habilitado = settings_sem_arquivo(feature_carrinhas_enabled=True)
    desabilitado = settings_sem_arquivo(feature_carrinhas_enabled=False)

    assert habilitado.feature_contentores_enabled is True
    assert desabilitado.feature_contentores_enabled is True


def test_feature_avarias_permanece_independente():
    settings = settings_sem_arquivo(
        feature_contentores_enabled=False,
        feature_carrinhas_enabled=False,
        feature_avarias_enabled=True,
    )

    assert settings.feature_contentores_enabled is False
    assert settings.feature_carrinhas_enabled is False
    assert settings.feature_avarias_enabled is True


def test_cache_respeita_monkeypatch_depois_de_cache_clear(monkeypatch):
    settings_real = Settings
    monkeypatch.setattr(
        config_module,
        "Settings",
        lambda: settings_real(_env_file=None),
    )
    monkeypatch.setenv("FEATURE_CONTENTORES_ENABLED", "false")
    monkeypatch.setenv("FEATURE_CARRINHAS_ENABLED", "true")
    get_settings.cache_clear()
    primeira = get_settings()

    monkeypatch.setenv("FEATURE_CONTENTORES_ENABLED", "true")
    assert get_settings() is primeira
    assert get_settings().feature_contentores_enabled is False

    get_settings.cache_clear()
    recarregada = get_settings()
    assert recarregada.feature_contentores_enabled is True
    assert recarregada.feature_carrinhas_enabled is True


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("true", True), ("false", False), ("1", True), ("0", False)],
)
def test_booleanos_seguem_parser_nativo_do_settings(valor, esperado):
    settings = settings_sem_arquivo(
        feature_contentores_enabled=valor,
        feature_carrinhas_enabled=valor,
    )

    assert settings.feature_contentores_enabled is esperado
    assert settings.feature_carrinhas_enabled is esperado


def test_valor_booleano_invalido_e_rejeitado_pelo_parser_existente():
    with pytest.raises(ValidationError):
        settings_sem_arquivo(feature_contentores_enabled="talvez")
