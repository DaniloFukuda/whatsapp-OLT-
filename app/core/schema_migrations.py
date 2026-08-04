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
    "status_chegada_carrinha": "TEXT DEFAULT 'AGUARDANDO_CHEGADA' NOT NULL",
    "chegada_carrinha_feita_por": "TEXT",
    "chegada_carrinha_data_hora": "DATETIME",
    "status_partida_carrinha": "TEXT DEFAULT 'AGUARDANDO_PARTIDA' NOT NULL",
    "partida_carrinha_feita_por": "TEXT",
    "partida_carrinha_data_hora": "DATETIME",
    "status_operacional_carrinha": "TEXT DEFAULT 'AGUARDANDO_CHEGADA' NOT NULL",
    "frota_carrinha": "TEXT",
    "chegada_carrinha_latitude": "FLOAT",
    "chegada_carrinha_longitude": "FLOAT",
    "chegada_carrinha_ponto_referencia": "TEXT",
    "partida_prevista_carrinha_data_hora": "DATETIME",
    "avaria_estado_anterior": "TEXT",
    "avaria_resolvida_em": "DATETIME",
    "avaria_resolvida_por": "TEXT",
}


PEDIDOS_COLUMNS = {
    "precisa_mao_de_obra": "BOOLEAN DEFAULT 0 NOT NULL",
    "pagamento_recebido_em": "DATETIME",
    "pagamento_recebido_por": "TEXT",
}


def ensure_alugueres_contentor_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mensagens_webhook (
                    message_id VARCHAR(512) PRIMARY KEY,
                    payload_hash VARCHAR(64) NOT NULL,
                    telefone VARCHAR(50),
                    status VARCHAR(32) NOT NULL
                        CHECK (status IN ('PROCESSANDO', 'CONCLUIDA', 'FALHOU_REPROCESSAVEL', 'FALHOU_DEFINITIVA')),
                    tentativas INTEGER DEFAULT 1 NOT NULL,
                    recebido_em DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    concluido_em DATETIME,
                    codigo_erro VARCHAR(80),
                    resposta_enviada BOOLEAN DEFAULT 0 NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_mensagens_webhook_atualizado_em "
                "ON mensagens_webhook (atualizado_em)"
            )
        )
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
        pedido_contentores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(pedido_contentores)")).mappings()
        }
        if {
            "entrega_feita_por",
            "entrega_data_hora",
            "entrega_latitude",
            "entrega_longitude",
            "entrega_ponto_referencia",
            "recolha_feita_por",
            "recolha_data_hora",
        } <= pedido_contentores_columns:
            connection.execute(text("""
                UPDATE pedido_contentores
                   SET status_chegada_carrinha = 'CHEGOU',
                       status_operacional_carrinha = 'EM_ATENDIMENTO',
                       chegada_carrinha_feita_por = entrega_feita_por,
                       chegada_carrinha_data_hora = entrega_data_hora,
                       chegada_carrinha_latitude = entrega_latitude,
                       chegada_carrinha_longitude = entrega_longitude,
                       chegada_carrinha_ponto_referencia = entrega_ponto_referencia,
                       partida_prevista_carrinha_data_hora = datetime(entrega_data_hora, '+2 hours')
                 WHERE tipo_equipamento = 'CARRINHA'
                   AND entrega_data_hora IS NOT NULL
                   AND status_operacional_carrinha = 'AGUARDANDO_CHEGADA'
                   AND chegada_carrinha_data_hora IS NULL
            """))
            connection.execute(text("""
                UPDATE pedido_contentores
                   SET status_partida_carrinha = 'PARTIU',
                       status_operacional_carrinha = 'AGUARDANDO_DESPEJO',
                       partida_carrinha_feita_por = recolha_feita_por,
                       partida_carrinha_data_hora = recolha_data_hora
                 WHERE tipo_equipamento = 'CARRINHA'
                   AND recolha_data_hora IS NOT NULL
                   AND status_operacional_carrinha IN ('AGUARDANDO_CHEGADA', 'EM_ATENDIMENTO')
                   AND partida_carrinha_data_hora IS NULL
            """))
            connection.execute(text("""
                UPDATE pedido_contentores
                   SET status_operacional_carrinha = 'CONCLUIDA'
                 WHERE tipo_equipamento = 'CARRINHA'
                   AND despejo_data_hora IS NOT NULL
                   AND status_ciclo = 'CONCLUIDO'
                   AND status_operacional_carrinha != 'CONCLUIDA'
            """))

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
