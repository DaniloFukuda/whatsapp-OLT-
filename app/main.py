from fastapi import FastAPI

from app.core.db import Base, SessionLocal, engine
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.models import pedido as _pedido_models  # noqa: F401
from app.routes import dashboard, health, webhook
from app.services.seed_service import SeedService


def create_app() -> FastAPI:
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    with SessionLocal() as db:
        SeedService(db).seed_contentores_iniciais()

    app = FastAPI(title="olt-entulhos", version="0.1.0")
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(dashboard.router)
    return app


app = create_app()
