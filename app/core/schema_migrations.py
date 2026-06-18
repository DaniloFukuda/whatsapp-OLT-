from sqlalchemy import Engine, text


ALUGUERES_CONTENTOR_COLUMNS = {
    "email_cliente": "TEXT",
    "quantidade_contentores": "INTEGER DEFAULT 1",
    "tipo_residuo": "TEXT",
    "operador_telefone": "TEXT",
    "criado_por_operador": "TEXT",
    "alterado_por_operador": "TEXT",
    "excluido_por_operador": "TEXT",
    "is_deleted": "BOOLEAN DEFAULT 0 NOT NULL",
    "justificativa_exclusao": "TEXT",
    "id_fatura_fiscal": "TEXT",
    "google_event_entrega_id": "TEXT",
    "google_event_retirada_id": "TEXT",
    "status_ciclo_cliente": "TEXT DEFAULT 'EM_ANDAMENTO' NOT NULL",
}


def ensure_alugueres_contentor_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS operadores (
                    telefone_whatsapp VARCHAR(50) PRIMARY KEY,
                    nome_operador VARCHAR(255) NOT NULL,
                    perfil VARCHAR(11) NOT NULL,
                    ativo BOOLEAN DEFAULT 1 NOT NULL
                )
                """
            )
        )
        columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(alugueres_contentor)")).mappings()
        }
        if not columns:
            return

        for column_name, column_definition in ALUGUERES_CONTENTOR_COLUMNS.items():
            if column_name not in columns:
                connection.execute(
                    text(f"ALTER TABLE alugueres_contentor ADD COLUMN {column_name} {column_definition}")
                )
