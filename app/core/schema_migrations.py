from sqlalchemy import Engine, text


ALUGUERES_CONTENTOR_COLUMNS = {
    "email_cliente": "TEXT",
    "numero_contentor": "TEXT DEFAULT '' NOT NULL",
    "tipo_residuo": "TEXT",
    "operador_telefone": "TEXT",
    "criado_por_operador": "TEXT",
    "pedido_feito_por": "TEXT",
    "entrega_feita_por": "TEXT",
    "alterado_por_operador": "TEXT",
    "excluido_por_operador": "TEXT",
    "is_deleted": "BOOLEAN DEFAULT 0 NOT NULL",
    "justificativa_exclusao": "TEXT",
    "id_fatura_fiscal": "TEXT",
    "google_event_entrega_id": "TEXT",
    "google_event_retirada_id": "TEXT",
    "status_ciclo_cliente": "TEXT DEFAULT 'EM_ANDAMENTO' NOT NULL",
    "status_entrega": "TEXT DEFAULT 'ENTREGUE' NOT NULL",
    "status_ciclo": "TEXT DEFAULT 'EM_ANDAMENTO' NOT NULL",
    "pedido_endereco_tipo": "TEXT",
    "pedido_endereco_texto": "TEXT",
    "pedido_latitude": "FLOAT",
    "pedido_longitude": "FLOAT",
    "pedido_ponto_referencia": "TEXT",
    "entrega_latitude": "FLOAT",
    "entrega_longitude": "FLOAT",
    "entrega_ponto_referencia": "TEXT",
    "recolha_feita_por": "TEXT",
    "recolha_data_hora": "DATETIME",
    "carga_errada": "BOOLEAN DEFAULT 0 NOT NULL",
    "relato_carga": "TEXT",
    "status_resolucao_carga": "TEXT DEFAULT 'N/A' NOT NULL",
    "contentor_avariado": "BOOLEAN DEFAULT 0 NOT NULL",
    "relato_avaria": "TEXT",
    "status_resolucao_avaria": "TEXT DEFAULT 'N/A' NOT NULL",
}


CONTENTORES_COLUMNS = {
    "criado_por_operador": "TEXT",
    "alterado_por_operador": "TEXT",
    "excluido_por_operador": "TEXT",
    "is_deleted": "BOOLEAN DEFAULT 0 NOT NULL",
    "justificativa_exclusao": "TEXT",
}


PEDIDO_CONTENTORES_COLUMNS = {
    "tipo_equipamento": "TEXT DEFAULT 'CONTENTOR' NOT NULL",
    "horario_agendado": "TEXT",
    "precisa_mao_de_obra": "BOOLEAN DEFAULT 0 NOT NULL",
    "despejo_feito_por": "TEXT",
    "despejo_data_hora": "DATETIME",
}


PEDIDOS_COLUMNS = {
    "precisa_mao_de_obra": "BOOLEAN DEFAULT 0 NOT NULL",
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
                    perfil VARCHAR(11) NOT NULL CHECK (perfil IN ('FUNCIONARIO', 'GESTOR')),
                    ativo BOOLEAN DEFAULT 1 NOT NULL
                )
                """
            )
        )
        contentores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(contentores)")).mappings()
        }
        for column_name, column_definition in CONTENTORES_COLUMNS.items():
            if contentores_columns and column_name not in contentores_columns:
                connection.execute(
                    text(f"ALTER TABLE contentores ADD COLUMN {column_name} {column_definition}")
                )

        pedido_contentores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(pedido_contentores)")).mappings()
        }
        for column_name, column_definition in PEDIDO_CONTENTORES_COLUMNS.items():
            if pedido_contentores_columns and column_name not in pedido_contentores_columns:
                connection.execute(
                    text(f"ALTER TABLE pedido_contentores ADD COLUMN {column_name} {column_definition}")
                )

        pedidos_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(pedidos)")).mappings()
        }
        for column_name, column_definition in PEDIDOS_COLUMNS.items():
            if pedidos_columns and column_name not in pedidos_columns:
                connection.execute(
                    text(f"ALTER TABLE pedidos ADD COLUMN {column_name} {column_definition}")
                )

        columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(alugueres_contentor)")).mappings()
        }
        if not columns:
            return

        if "quantidade_contentores" in columns:
            connection.execute(text("ALTER TABLE alugueres_contentor DROP COLUMN quantidade_contentores"))
            columns.remove("quantidade_contentores")

        for column_name, column_definition in ALUGUERES_CONTENTOR_COLUMNS.items():
            if column_name not in columns:
                connection.execute(
                    text(f"ALTER TABLE alugueres_contentor ADD COLUMN {column_name} {column_definition}")
                )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS contentor_fotos (
                    id INTEGER PRIMARY KEY,
                    aluguer_id INTEGER NOT NULL,
                    url_foto VARCHAR(500) NOT NULL,
                    tipo VARCHAR(30) DEFAULT 'entrega' NOT NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        foto_info = list(
            connection.execute(text("PRAGMA table_info(contentor_fotos)")).mappings()
        )
        foto_columns = {row["name"] for row in foto_info}
        needs_rebuild = any(
            row["name"] in {"aluguer_id", "url_foto", "tipo"} and row["notnull"]
            for row in foto_info
        )
        if needs_rebuild:
            connection.execute(text("ALTER TABLE contentor_fotos RENAME TO contentor_fotos_legacy"))
            connection.execute(
                text(
                    """
                    CREATE TABLE contentor_fotos (
                        id INTEGER PRIMARY KEY,
                        aluguer_id INTEGER REFERENCES alugueres_contentor(id),
                        pedido_contentor_id INTEGER REFERENCES pedido_contentores(id) ON DELETE CASCADE,
                        url_foto VARCHAR(500),
                        url_midia VARCHAR(500),
                        tipo VARCHAR(30),
                        tipo_foto VARCHAR(20),
                        criado_em DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO contentor_fotos
                        (id, aluguer_id, url_foto, url_midia, tipo, tipo_foto, criado_em)
                    SELECT id, aluguer_id, url_foto, url_foto, tipo, UPPER(tipo), criado_em
                    FROM contentor_fotos_legacy
                    """
                )
            )
            connection.execute(text("DROP TABLE contentor_fotos_legacy"))
        else:
            for column_name, definition in {
                "pedido_contentor_id": "INTEGER REFERENCES pedido_contentores(id)",
                "url_midia": "VARCHAR(500)",
                "tipo_foto": "VARCHAR(20)",
            }.items():
                if column_name not in foto_columns:
                    connection.execute(
                        text(f"ALTER TABLE contentor_fotos ADD COLUMN {column_name} {definition}")
                    )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS contentor_fotos_recolha (
                    id INTEGER PRIMARY KEY,
                    aluguer_id INTEGER NOT NULL,
                    url_foto_recolha VARCHAR(500) NOT NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
