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
        connection.execute(
            text(
                """
                CREATE TABLE pedidos (
                    id INTEGER PRIMARY KEY,
                    nome_cliente VARCHAR(255) NOT NULL,
                    telefone_cliente VARCHAR(50) NOT NULL,
                    data_planejada DATETIME NOT NULL,
                    valor_global NUMERIC(10, 2) NOT NULL,
                    status_pagamento VARCHAR(20) NOT NULL,
                    pedido_feito_por VARCHAR(50) NOT NULL,
                    endereco_aproximado TEXT NOT NULL,
                    criado_em DATETIME NOT NULL,
                    atualizado_em DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE pedido_contentores (
                    id INTEGER PRIMARY KEY,
                    pedido_id INTEGER NOT NULL,
                    numero_adesivo_contentor VARCHAR(20),
                    residuo_contratado VARCHAR(80) NOT NULL,
                    status_entrega VARCHAR(20) NOT NULL,
                    status_recolha VARCHAR(20) NOT NULL,
                    status_ciclo VARCHAR(20) NOT NULL,
                    contentor_avariado BOOLEAN DEFAULT 0 NOT NULL,
                    carga_errada BOOLEAN DEFAULT 0 NOT NULL,
                    criado_em DATETIME NOT NULL,
                    atualizado_em DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO pedidos (
                    id, nome_cliente, telefone_cliente, data_planejada, valor_global,
                    status_pagamento, pedido_feito_por, endereco_aproximado, criado_em, atualizado_em
                )
                VALUES (
                    1, 'Cliente Pedido Antigo', '351900000001', '2026-01-01 10:00:00',
                    200.00, 'PENDENTE', 'gestor', 'Rua Antiga',
                    '2026-01-01 10:00:00', '2026-01-01 10:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO pedido_contentores (
                    id, pedido_id, numero_adesivo_contentor, residuo_contratado,
                    status_entrega, status_recolha, status_ciclo,
                    contentor_avariado, carga_errada, criado_em, atualizado_em
                )
                VALUES (
                    1, 1, '77', 'Entulho Limpo', 'ENTREGUE', 'RECOLHIDO',
                    'EM_ANDAMENTO', 0, 0, '2026-01-01 10:00:00',
                    '2026-01-01 10:00:00'
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
        fotos_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(contentor_fotos)")).mappings()
        }
        fotos_recolha_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(contentor_fotos_recolha)")).mappings()
        }

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
        pedido_contentores_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(pedido_contentores)")).mappings()
        }
        pedido_contentor_row = connection.execute(
            text(
                """
                SELECT numero_adesivo_contentor, residuo_contratado, tipo_equipamento,
                       horario_agendado, precisa_mao_de_obra, despejo_feito_por,
                       despejo_data_hora, avaria_estado_anterior,
                       avaria_resolvida_em, avaria_resolvida_por
                FROM pedido_contentores WHERE id = 1
                """
            )
        ).mappings().one()
        pedidos_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(pedidos)")).mappings()
        }
        pedido_row = connection.execute(
            text(
                "SELECT precisa_mao_de_obra, pagamento_recebido_em, "
                "pagamento_recebido_por FROM pedidos WHERE id = 1"
            )
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
        "pedido_feito_por",
        "entrega_feita_por",
        "status_entrega",
        "status_ciclo",
        "pedido_endereco_tipo",
        "pedido_endereco_texto",
        "pedido_latitude",
        "pedido_longitude",
        "pedido_ponto_referencia",
        "entrega_latitude",
        "entrega_longitude",
        "entrega_ponto_referencia",
        "recolha_feita_por",
        "recolha_data_hora",
        "carga_errada",
        "relato_carga",
        "status_resolucao_carga",
        "contentor_avariado",
        "relato_avaria",
        "status_resolucao_avaria",
    } <= columns
    assert {"id", "aluguer_id", "url_foto", "tipo", "criado_em"} <= fotos_columns
    assert {"id", "aluguer_id", "url_foto_recolha", "criado_em"} <= fotos_recolha_columns
    assert {"telefone_whatsapp", "nome_operador", "perfil", "ativo"} <= operadores_columns
    assert {
        "criado_por_operador",
        "alterado_por_operador",
        "excluido_por_operador",
        "is_deleted",
        "justificativa_exclusao",
    } <= contentores_columns
    assert {
        "tipo_equipamento",
        "horario_agendado",
        "precisa_mao_de_obra",
        "despejo_feito_por",
        "despejo_data_hora",
        "avaria_estado_anterior",
        "avaria_resolvida_em",
        "avaria_resolvida_por",
    } <= pedido_contentores_columns
    assert {
        "precisa_mao_de_obra",
        "pagamento_recebido_em",
        "pagamento_recebido_por",
    } <= pedidos_columns
    assert contentor_row["is_deleted"] == 0
    assert contentor_row["justificativa_exclusao"] is None
    assert pedido_row["precisa_mao_de_obra"] == 0
    assert pedido_row["pagamento_recebido_em"] is None
    assert pedido_row["pagamento_recebido_por"] is None
    assert pedido_contentor_row["numero_adesivo_contentor"] == "77"
    assert pedido_contentor_row["residuo_contratado"] == "Entulho Limpo"
    assert pedido_contentor_row["tipo_equipamento"] == "CONTENTOR"
    assert pedido_contentor_row["horario_agendado"] is None
    assert pedido_contentor_row["precisa_mao_de_obra"] == 0
    assert pedido_contentor_row["despejo_feito_por"] is None
    assert pedido_contentor_row["despejo_data_hora"] is None
    assert pedido_contentor_row["avaria_estado_anterior"] is None
    assert pedido_contentor_row["avaria_resolvida_em"] is None
    assert pedido_contentor_row["avaria_resolvida_por"] is None
    assert "quantidade_contentores" not in columns
    assert row_count == 1


def test_migracao_carrinhas_legadas_por_evidencia_e_idempotente(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'carrinhas-legadas.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE pedidos (
                id INTEGER PRIMARY KEY,
                nome_cliente TEXT NOT NULL,
                telefone_cliente TEXT NOT NULL,
                data_planejada DATETIME NOT NULL,
                valor_global NUMERIC NOT NULL,
                status_pagamento TEXT NOT NULL,
                forma_pagamento TEXT,
                pedido_feito_por TEXT NOT NULL,
                endereco_aproximado TEXT NOT NULL,
                criado_em DATETIME NOT NULL,
                atualizado_em DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            INSERT INTO pedidos VALUES
            (1, 'Eloisa', '351900000001', '2026-07-29', 300, 'PENDENTE', NULL, 'gestor', 'Rua', '2026-07-29', '2026-07-29')
        """))
        connection.execute(text("""
            CREATE TABLE pedido_contentores (
                id INTEGER PRIMARY KEY,
                pedido_id INTEGER NOT NULL,
                numero_adesivo_contentor TEXT,
                tipo_equipamento TEXT NOT NULL,
                residuo_contratado TEXT NOT NULL,
                status_entrega TEXT NOT NULL,
                entrega_feita_por TEXT,
                entrega_latitude FLOAT,
                entrega_longitude FLOAT,
                entrega_ponto_referencia TEXT,
                entrega_data_hora DATETIME,
                status_recolha TEXT NOT NULL,
                recolha_feita_por TEXT,
                recolha_data_hora DATETIME,
                status_ciclo TEXT NOT NULL,
                despejo_data_hora DATETIME,
                contentor_avariado BOOLEAN DEFAULT 0 NOT NULL,
                carga_errada BOOLEAN DEFAULT 0 NOT NULL,
                criado_em DATETIME NOT NULL,
                atualizado_em DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            INSERT INTO pedido_contentores VALUES
            (1,1,NULL,'CARRINHA','Entulho Limpo','PENDENTE',NULL,NULL,NULL,NULL,NULL,'PENDENTE',NULL,NULL,'EM_ANDAMENTO',NULL,0,0,'2026-07-29','2026-07-29'),
            (2,1,NULL,'CARRINHA','Entulho Limpo','ENTREGUE','op',38.7,-9.1,'Portao','2026-07-29 10:00','PENDENTE',NULL,NULL,'EM_ANDAMENTO',NULL,0,0,'2026-07-29','2026-07-29'),
            (3,1,NULL,'CARRINHA','Entulho Limpo','ENTREGUE','op',38.7,-9.1,NULL,'2026-07-29 10:00','RECOLHIDO','op','2026-07-29 12:30','EM_ANDAMENTO',NULL,0,0,'2026-07-29','2026-07-29'),
            (4,1,NULL,'CARRINHA','Entulho Limpo','ENTREGUE','op',38.7,-9.1,NULL,'2026-07-29 10:00','RECOLHIDO','op','2026-07-29 12:00','CONCLUIDO','2026-07-29 13:00',0,0,'2026-07-29','2026-07-29'),
            (5,1,NULL,'CARRINHA','Entulho Limpo','ENTREGUE',NULL,NULL,NULL,NULL,NULL,'PENDENTE',NULL,NULL,'EM_ANDAMENTO',NULL,0,0,'2026-07-29','2026-07-29'),
            (6,1,'77','CONTENTOR','Entulho Limpo','ENTREGUE','op',38.7,-9.1,NULL,'2026-07-29 10:00','RECOLHIDO','op','2026-07-29 12:00','CONCLUIDO','2026-07-29 13:00',0,0,'2026-07-29','2026-07-29')
        """))

    ensure_alugueres_contentor_schema(engine)
    ensure_alugueres_contentor_schema(engine)

    with engine.connect() as connection:
        estados = dict(connection.execute(text(
            "SELECT id, status_operacional_carrinha FROM pedido_contentores ORDER BY id"
        )).all())
        eloisa = connection.execute(text("""
            SELECT p.valor_global, p.status_pagamento, p.forma_pagamento,
                   pc.chegada_carrinha_data_hora, pc.partida_carrinha_data_hora,
                   pc.chegada_carrinha_feita_por, pc.partida_carrinha_feita_por
              FROM pedidos p JOIN pedido_contentores pc ON pc.pedido_id=p.id
             WHERE pc.id=1
        """)).mappings().one()

    assert estados[1] == "AGUARDANDO_CHEGADA"
    assert estados[2] == "EM_ATENDIMENTO"
    assert estados[3] == "AGUARDANDO_DESPEJO"
    assert estados[4] == "CONCLUIDA"
    assert estados[5] == "AGUARDANDO_CHEGADA"
    assert estados[6] == "AGUARDANDO_CHEGADA"
    assert float(eloisa["valor_global"]) == 300
    assert eloisa["status_pagamento"] == "PENDENTE"
    assert eloisa["forma_pagamento"] is None
    assert eloisa["chegada_carrinha_data_hora"] is None
    assert eloisa["partida_carrinha_data_hora"] is None
    assert eloisa["chegada_carrinha_feita_por"] is None
    assert eloisa["partida_carrinha_feita_por"] is None
