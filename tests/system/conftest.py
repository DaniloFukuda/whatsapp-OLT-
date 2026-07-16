import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.db import Base, get_db
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.models.operador import Operador, PerfilOperador
from app.routes import webhook

from tests.system.helpers.database_assertions import DatabaseAssertions
from tests.system.helpers.fake_meta_client import FakeMetaClient


GESTOR_PHONE = "351999000001"
FUNCIONARIO_PHONE = "351999000002"
UNAUTHORIZED_PHONE = "351999000099"


@pytest.fixture(autouse=True)
def isolate_system_environment(monkeypatch):
    get_settings.cache_clear()
    for name in (
        "WHATSAPP_OWNER_PHONE",
        "AUTHORIZED_OPERATOR_PHONE",
        "AUTHORIZED_OPERATOR_PHONES",
        "OWNER_WHATSAPP",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ENV", "test")
    yield
    get_settings.cache_clear()


@pytest.fixture()
def system_app(tmp_path):
    db_path = tmp_path / "system_smoke.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI(title="olt-system-smoke-test")
    app.include_router(webhook.router)

    session_ids: list[int] = []

    def override_get_db():
        session = SessionLocal()
        session_ids.append(id(session))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, SessionLocal, session_ids
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def system_db(system_app):
    _app, SessionLocal, _session_ids = system_app
    return SessionLocal


@pytest.fixture()
def fake_meta(monkeypatch):
    fake = FakeMetaClient()
    fake.install(monkeypatch)
    return fake


@pytest.fixture()
def gestor(system_db):
    with system_db() as session:
        operador = Operador(
            telefone_whatsapp=GESTOR_PHONE,
            nome_operador="Gestor Sistema Teste",
            perfil=PerfilOperador.GESTOR,
            ativo=True,
        )
        session.add(operador)
        session.commit()
    return GESTOR_PHONE


@pytest.fixture()
def funcionario(system_db):
    with system_db() as session:
        operador = Operador(
            telefone_whatsapp=FUNCIONARIO_PHONE,
            nome_operador="Funcionario Sistema Teste",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=True,
        )
        session.add(operador)
        session.commit()
    return FUNCIONARIO_PHONE


@pytest.fixture()
def unauthorized_phone():
    return UNAUTHORIZED_PHONE


@pytest.fixture()
def db_assertions(system_db):
    return DatabaseAssertions(system_db)
