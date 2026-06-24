"""Migra o banco existente para o modulo de confirmacao de recolha.

Uso no servidor, dentro do venv do projeto:
    python scripts/migrate_recolha_contentores_paulo.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.models.aluguer import ContentorFotoRecolha


COLUMNS = {
    "status_ciclo": "VARCHAR(20) DEFAULT 'EM_ANDAMENTO' NOT NULL",
    "recolha_feita_por": "VARCHAR(50) DEFAULT NULL",
    "recolha_data_hora": "TIMESTAMP DEFAULT NULL",
    "carga_errada": "BOOLEAN DEFAULT FALSE NOT NULL",
    "relato_carga": "TEXT DEFAULT NULL",
    "status_resolucao_carga": "VARCHAR(20) DEFAULT 'N/A' NOT NULL",
    "contentor_avariado": "BOOLEAN DEFAULT FALSE NOT NULL",
    "relato_avaria": "TEXT DEFAULT NULL",
    "status_resolucao_avaria": "VARCHAR(20) DEFAULT 'N/A' NOT NULL",
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

    ContentorFotoRecolha.__table__.create(bind=engine, checkfirst=True)
    print("OK tabela contentor_fotos_recolha verificada/criada")


if __name__ == "__main__":
    main()
