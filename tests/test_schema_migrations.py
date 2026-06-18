from sqlalchemy import create_engine, text

from app.core.schema_migrations import ensure_alugueres_contentor_schema


def test_ensure_alugueres_contentor_schema_migra_sqlite_antigo_sem_apagar_dados(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE alugueres_contentor (
                    id INTEGER PRIMARY KEY,
                    contentor_id INTEGER NOT NULL,
                    cliente_id INTEGER NOT NULL,
                    telefone_cliente VARCHAR(50) NOT NULL,
                    nome_cliente VARCHAR(255) NOT NULL,
                    data_entrega DATETIME NOT NULL,
                    data_vencimento DATETIME NOT NULL,
                    valor NUMERIC(10, 2) NOT NULL,
                    forma_pagamento VARCHAR(80),
                    pago BOOLEAN NOT NULL,
                    status VARCHAR(80) NOT NULL,
                    foto_entrega_path VARCHAR(500),
                    latitude FLOAT,
                    longitude FLOAT,
                    observacoes TEXT,
                    criado_em DATETIME NOT NULL,
                    atualizado_em DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO alugueres_contentor (
                    id, contentor_id, cliente_id, telefone_cliente, nome_cliente,
                    data_entrega, data_vencimento, valor, forma_pagamento, pago,
                    status, criado_em, atualizado_em
                )
                VALUES (
                    1, 1, 1, '351912345678', 'Cliente Antigo',
                    '2026-01-01 10:00:00', '2026-01-06 10:00:00', 120.00,
                    'dinheiro', 0, 'ATIVO', '2026-01-01 10:00:00', '2026-01-01 10:00:00'
                )
                """
            )
        )

    ensure_alugueres_contentor_schema(engine)
    ensure_alugueres_contentor_schema(engine)

    with engine.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(alugueres_contentor)")).mappings()
        }
        row_count = connection.execute(text("SELECT COUNT(*) FROM alugueres_contentor")).scalar_one()
        quantidade = connection.execute(
            text("SELECT quantidade_contentores FROM alugueres_contentor WHERE id = 1")
        ).scalar_one()

    assert {"email_cliente", "quantidade_contentores", "tipo_residuo", "operador_telefone"} <= columns
    assert row_count == 1
    assert quantidade == 1
