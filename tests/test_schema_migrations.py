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
                    quantidade_contentores INTEGER DEFAULT 1,
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
        connection.execute(
            text(
                """
                CREATE TABLE contentores (
                    id INTEGER PRIMARY KEY,
                    codigo VARCHAR(80) NOT NULL,
                    status VARCHAR(19) NOT NULL,
                    criado_em DATETIME NOT NULL,
                    atualizado_em DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO contentores (
                    id, codigo, status, criado_em, atualizado_em
                )
                VALUES (
                    1, 'C01', 'DISPONIVEL', '2026-01-01 10:00:00', '2026-01-01 10:00:00'
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

    operadores_columns = set()
    with engine.connect() as connection:
        operadores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(operadores)")).mappings()
        }
        contentores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(contentores)")).mappings()
        }
        contentor_row = connection.execute(
            text("SELECT is_deleted, justificativa_exclusao FROM contentores WHERE id = 1")
        ).mappings().one()

    assert {
        "email_cliente",
        "numero_contentor",
        "tipo_residuo",
        "operador_telefone",
        "criado_por_operador",
        "alterado_por_operador",
        "excluido_por_operador",
        "is_deleted",
        "justificativa_exclusao",
        "id_fatura_fiscal",
        "google_event_entrega_id",
        "google_event_retirada_id",
        "status_ciclo_cliente",
    } <= columns
    assert {"telefone_whatsapp", "nome_operador", "perfil", "ativo"} <= operadores_columns
    assert {
        "criado_por_operador",
        "alterado_por_operador",
        "excluido_por_operador",
        "is_deleted",
        "justificativa_exclusao",
    } <= contentores_columns
    assert contentor_row["is_deleted"] == 0
    assert contentor_row["justificativa_exclusao"] is None
    assert "quantidade_contentores" not in columns
    assert row_count == 1
