from sqlalchemy import Engine, text


ALUGUERES_CONTENTOR_COLUMNS = {
    "email_cliente": "TEXT",
    "quantidade_contentores": "INTEGER DEFAULT 1",
    "tipo_residuo": "TEXT",
    "operador_telefone": "TEXT",
}


def ensure_alugueres_contentor_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
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
