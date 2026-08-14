from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.aluguer import ContentorFoto
from app.models.pedido import (
    StatusCicloPedido,
    StatusOperacionalCarrinha,
    StatusRecolhaPedido,
    Pedido,
    PedidoContentor,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'r1-cotas.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _pedido(service, tipo):
    itens = [
        {"tipo_equipamento": tipo, "residuo_contratado": "Entulho Limpo"},
        {"tipo_equipamento": tipo, "residuo_contratado": "Entulho Misto"},
    ]
    if tipo == TipoEquipamentoPedido.CARRINHA.value:
        itens[0]["horario_agendado"] = "10:00"
        itens[1]["horario_agendado"] = "11:00"
    return service.criar(
        nome_cliente="R1", telefone_cliente="351900000001",
        data_planejada=datetime.now(timezone.utc),
        valor_global="100", pago=False, forma_pagamento=None,
        pedido_feito_por="teste", endereco_aproximado="Rua", ponto_referencia=None,
        itens=itens,
    )


def _elegiveis(session, tipo):
    service = PedidoService(session)
    pedido = _pedido(service, tipo)
    for item in pedido.contentores:
        if tipo == TipoEquipamentoPedido.CARRINHA.value:
            item.status_operacional_carrinha = StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
        else:
            item.status_recolha = StatusRecolhaPedido.RECOLHIDO.value
            item.status_ciclo = StatusCicloPedido.EM_ANDAMENTO.value
    session.commit()
    return pedido.id, [item.id for item in pedido.contentores]


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_ultima_cota_e_serializada_entre_duas_sessions_file_backed(tmp_path, tipo):
    engine, factory = _factory(tmp_path)
    with factory() as setup:
        pedido_id, ids = _elegiveis(setup, tipo)
    barreira = Barrier(2)

    def confirmar(item_id, foto):
        with factory() as session:
            service = PedidoService(session)
            barreira.wait(timeout=5)
            try:
                if tipo == TipoEquipamentoPedido.CARRINHA.value:
                    service.confirmar_despejo_carrinha(
                        item_id, "Entulho Limpo", False, None, "op", [foto],
                        pedido_id=pedido_id,
                    )
                else:
                    service.confirmar_despejo(
                        item_id, "Entulho Limpo", operador="op",
                        pedido_id=pedido_id, fotos=[foto],
                    )
                return "ok"
            except ValueError as exc:
                session.rollback()
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda args: confirmar(*args), zip(ids, ["a", "b"])))

    assert resultados.count("ok") == 1
    assert sum("cota em aberto" in resultado for resultado in resultados) == 1
    with factory() as verificacao:
        itens = [verificacao.get(PedidoContentor, item_id) for item_id in ids]
        concluidos = [item for item in itens if item.status_ciclo == StatusCicloPedido.CONCLUIDO.value]
        assert len(concluidos) == 1
        rejeitado = next(item for item in itens if item not in concluidos)
        if tipo == TipoEquipamentoPedido.CARRINHA.value:
            assert rejeitado.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
        else:
            assert rejeitado.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
        assert verificacao.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).count() == 1
        assert PedidoService(verificacao).cotas_residuos(pedido_id)["Entulho Limpo"]["saldo"] == 0
    engine.dispose()


@pytest.mark.parametrize("tipo", [TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value])
def test_confirmacao_final_rele_cota_consumida_depois_do_snapshot(tmp_path, tipo):
    engine, factory = _factory(tmp_path)
    with factory() as setup:
        pedido_id, ids = _elegiveis(setup, tipo)
    with factory() as primeira:
        service = PedidoService(primeira)
        if tipo == TipoEquipamentoPedido.CARRINHA.value:
            service.confirmar_despejo_carrinha(ids[0], "Entulho Limpo", False, None, "op", ["a"], pedido_id=pedido_id)
        else:
            service.confirmar_despejo(ids[0], "Entulho Limpo", pedido_id=pedido_id, fotos=["a"])
    with factory() as segunda:
        service = PedidoService(segunda)
        with pytest.raises(ValueError, match="cota em aberto"):
            if tipo == TipoEquipamentoPedido.CARRINHA.value:
                service.confirmar_despejo_carrinha(ids[1], "Entulho Limpo", False, None, "op", ["b"], pedido_id=pedido_id)
            else:
                service.confirmar_despejo(ids[1], "Entulho Limpo", pedido_id=pedido_id, fotos=["b"])
        segunda.rollback()
        rejeitado = segunda.get(PedidoContentor, ids[1])
        assert rejeitado.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
        assert segunda.query(ContentorFoto).filter_by(pedido_contentor_id=ids[1]).count() == 0
    engine.dispose()


def test_carrinha_rejeita_vinculo_com_pedido_diferente_sem_persistir(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as setup:
        pedido_a, _ = _elegiveis(setup, TipoEquipamentoPedido.CARRINHA.value)
        pedido_b, ids_b = _elegiveis(setup, TipoEquipamentoPedido.CARRINHA.value)
    with factory() as session:
        with pytest.raises(ValueError, match="pertence|disponível|disponivel"):
            PedidoService(session).confirmar_despejo_carrinha(
                ids_b[0], "Entulho Limpo", False, None, "op", ["foto"],
                pedido_id=pedido_a,
            )
        session.rollback()
        item = session.get(PedidoContentor, ids_b[0])
        assert item.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
        assert item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
        assert session.query(ContentorFoto).filter_by(pedido_contentor_id=ids_b[0]).count() == 0
    engine.dispose()


def test_lock_sqlite_nao_altera_timestamp_nem_campos_do_pedido(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as setup:
        pedido_id, ids = _elegiveis(setup, TipoEquipamentoPedido.CONTENTOR.value)
        pedido = setup.get(Pedido, pedido_id)
        antes = {
            "atualizado_em": pedido.atualizado_em,
            "nome_cliente": pedido.nome_cliente,
            "telefone_cliente": pedido.telefone_cliente,
            "valor_global": pedido.valor_global,
            "status_pagamento": pedido.status_pagamento,
            "precisa_mao_de_obra": pedido.precisa_mao_de_obra,
        }
    statements = []

    def registrar_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", registrar_sql)
    with factory() as bloqueio:
        PedidoService(bloqueio)._proteger_cotas_despejo(ids[0], pedido_id)
        bloqueio.commit()
    event.remove(engine, "before_cursor_execute", registrar_sql)
    lock_sql = next(sql for sql in statements if sql.lstrip().upper().startswith("UPDATE PEDIDOS"))
    assert "SET id = id" in lock_sql
    assert "atualizado_em" not in lock_sql
    with factory() as verificacao:
        pedido = verificacao.get(Pedido, pedido_id)
        depois = {
            "atualizado_em": pedido.atualizado_em,
            "nome_cliente": pedido.nome_cliente,
            "telefone_cliente": pedido.telefone_cliente,
            "valor_global": pedido.valor_global,
            "status_pagamento": pedido.status_pagamento,
            "precisa_mao_de_obra": pedido.precisa_mao_de_obra,
        }
        assert depois == antes
    engine.dispose()
