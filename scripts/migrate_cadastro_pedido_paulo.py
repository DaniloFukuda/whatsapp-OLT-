"""Migra o banco existente para o fluxo de cadastro de pedido em duas etapas.

Uso no servidor, dentro do venv do projeto:
    python scripts/migrate_cadastro_pedido_paulo.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.models.aluguer import ContentorFoto


COLUMNS = {
    "pedido_feito_por": "VARCHAR(50) DEFAULT NULL",
    "entrega_feita_por": "VARCHAR(50) DEFAULT NULL",
    "status_entrega": "VARCHAR(20) DEFAULT 'ENTREGUE' NOT NULL",
    "pedido_endereco_tipo": "VARCHAR(20) DEFAULT NULL",
    "pedido_endereco_texto": "TEXT DEFAULT NULL",
    "pedido_latitude": "FLOAT DEFAULT NULL",
    "pedido_longitude": "FLOAT DEFAULT NULL",
    "pedido_ponto_referencia": "VARCHAR(50) DEFAULT NULL",
    "entrega_latitude": "FLOAT DEFAULT NULL",
    "entrega_longitude": "FLOAT DEFAULT NULL",
    "entrega_ponto_referencia": "VARCHAR(50) DEFAULT NULL",
}


def main() -> None:
    settings = get_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args)

    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_columns = {column["name"] for column in inspector.get_columns("alugueres_contentor")}
        for name, ddl in COLUMNS.items():
            if name not in existing_columns:
                conn.execute(text(f"ALTER TABLE alugueres_contentor ADD COLUMN {name} {ddl}"))
                print(f"OK coluna adicionada: {name}")
            else:
                print(f"SKIP coluna existente: {name}")

    ContentorFoto.__table__.create(bind=engine, checkfirst=True)
    print("OK tabela contentor_fotos verificada/criada")


if __name__ == "__main__":
    main()
