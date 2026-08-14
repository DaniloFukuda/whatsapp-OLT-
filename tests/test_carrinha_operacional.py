from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core.time import utcnow
from app.models.aluguer import ContentorFoto
from app.models.pedido import (
    StatusCicloPedido,
    StatusOperacionalCarrinha,
    StatusPagamento,
    StatusRecolhaPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


def criar_carrinhas(db_session, quantidade=1, pago=False):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Eloisa Compatível",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=pago,
        forma_pagamento="MBWay" if pago else None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua da Obra",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": f"{9 + indice:02d}:00",
            }
            for indice in range(quantidade)
        ],
    )
    return service, pedido


def financeiro(pedido):
    return (
        Decimal(str(pedido.valor_global)),
        pedido.status_pagamento,
        pedido.forma_pagamento,
    )


def confirmar_chegada(service, pedido, item, operador="motorista"):
    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        operador,
        38.7,
        -9.1,
        "Portão azul",
        [{"contentor_id": item.id, "numero_adesivo": "17", "fotos": ["foto-chegada"]}],
    )
    service.db.commit()
    service.db.refresh(item)


def test_ciclo_completo_carrinha_preserva_financeiro_e_separa_evidencias(db_session):
    service, pedido = criar_carrinhas(db_session)
    carrinha = pedido.contentores[0]
    antes = financeiro(pedido)

    confirmar_chegada(service, pedido, carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert carrinha.frota_carrinha == "17"
    assert carrinha.numero_adesivo_contentor is None
    assert carrinha.chegada_carrinha_latitude == 38.7
    assert carrinha.chegada_carrinha_longitude == -9.1
    assert carrinha.partida_prevista_carrinha_data_hora == (
        carrinha.chegada_carrinha_data_hora + timedelta(hours=2)
    )
    assert financeiro(pedido) == antes

    service.confirmar_partida_carrinha(
        carrinha.id, "motorista", False, None, ["foto-partida"]
    )
    db_session.refresh(carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert financeiro(pedido) == antes

    service.confirmar_despejo_carrinha(
        carrinha.id,
        "Entulho Limpo",
        False,
        None,
        "motorista",
        ["foto-despejo"],
    )
    db_session.refresh(carrinha)
    db_session.refresh(pedido)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value
    assert carrinha.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert financeiro(pedido) == (Decimal("300.00"), StatusPagamento.PENDENTE.value, None)
    assert {
        foto.tipo_foto for foto in carrinha.fotos
    } == {TipoFoto.ENTREGA.value, TipoFoto.RECOLHA.value, TipoFoto.DESPEJO.value}


def test_transicoes_invalidas_repeticao_tipo_e_evidencias(db_session):
    service, pedido = criar_carrinhas(db_session)
    carrinha = pedido.contentores[0]

    with pytest.raises(ValueError, match="não disponível"):
        service.confirmar_partida_carrinha(carrinha.id, "op", False, None, ["foto"])
    with pytest.raises(ValueError, match="não disponível"):
        service.confirmar_despejo_carrinha(
            carrinha.id, "Entulho Limpo", False, None, "op", ["foto"]
        )

    confirmar_chegada(service, pedido, carrinha)
    with pytest.raises(ValueError, match="mudou"):
        service.confirmar_chegada_carrinha_lote_transacional(
            pedido.id, "op", 38.7, -9.1, None,
            [{"contentor_id": carrinha.id, "numero_adesivo": "17", "fotos": ["foto"]}],
        )
    with pytest.raises(ValueError, match="foto da partida"):
        service.confirmar_partida_carrinha(carrinha.id, "op", False, None, [])
    with pytest.raises(ValueError, match="pelo menos 10"):
        service.confirmar_partida_carrinha(carrinha.id, "op", True, "curto", ["foto"])

    service.confirmar_partida_carrinha(carrinha.id, "op", True, "Porta lateral avariada", ["foto"])
    with pytest.raises(ValueError, match="não disponível"):
        service.confirmar_partida_carrinha(carrinha.id, "op", False, None, ["foto"])
    with pytest.raises(ValueError, match="pelo menos uma foto"):
        service.confirmar_despejo_carrinha(
            carrinha.id, "Entulho Limpo", False, None, "op", []
        )


def test_contentor_rejeitado_por_servico_carrinha_e_carrinha_por_contentor(db_session):
    service = PedidoService(db_session)
    contentor = service.criar(
        nome_cliente="Contentor",
        telefone_cliente="351900000001",
        data_planejada=datetime.now(timezone.utc),
        valor_global="100",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    item_contentor = contentor.contentores[0]
    with pytest.raises(ValueError, match="mudou"):
        service.confirmar_chegada_carrinha_lote_transacional(
            contentor.id, "op", 38.7, -9.1, None,
            [{"contentor_id": item_contentor.id, "numero_adesivo": "1", "fotos": ["foto"]}],
        )

    _, pedido_carrinha = criar_carrinhas(db_session)
    carrinha = pedido_carrinha.contentores[0]
    with pytest.raises(ValueError, match="apenas contentores"):
        service.confirmar_entrega_lote_transacional(
            pedido_carrinha.id, "op", 38.7, -9.1, None,
            [{"contentor_id": carrinha.id, "numero_adesivo": "1", "fotos": ["foto"]}],
        )
    assert db_session.query(ContentorFoto).count() == 0


def test_prazo_atraso_e_duracao_sao_informativos(db_session):
    service, pedido = criar_carrinhas(db_session)
    carrinha = pedido.contentores[0]
    confirmar_chegada(service, pedido, carrinha)
    chegada = carrinha.chegada_carrinha_data_hora

    assert service.carrinha_atrasada(carrinha, chegada + timedelta(hours=1)) is False
    assert service.carrinha_atrasada(carrinha, chegada + timedelta(hours=2)) is False
    assert service.carrinha_atrasada(carrinha, chegada + timedelta(hours=2, seconds=1)) is True
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value

    carrinha.partida_carrinha_data_hora = chegada + timedelta(hours=3)
    assert service.tempo_operacional_carrinha(carrinha) == timedelta(hours=3)
    assert service.carrinha_atrasada(carrinha, chegada + timedelta(hours=4)) is False


def test_duas_carrinhas_avancam_individualmente(db_session):
    service, pedido = criar_carrinhas(db_session, quantidade=2)
    primeira, segunda = pedido.contentores
    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "op",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": primeira.id, "numero_adesivo": "1", "fotos": ["foto-1"]},
            {"contentor_id": segunda.id, "numero_adesivo": "2", "fotos": ["foto-2"]},
        ],
    )
    db_session.commit()
    service.confirmar_partida_carrinha(primeira.id, "op", False, None, ["partida-1"])
    db_session.refresh(primeira)
    db_session.refresh(segunda)
    assert primeira.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert segunda.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value


def test_falha_no_commit_da_partida_faz_rollback_integral_e_preserva_excecao(
    db_session, monkeypatch
):
    service, pedido = criar_carrinhas(db_session)
    carrinha = pedido.contentores[0]
    confirmar_chegada(service, pedido, carrinha)
    previsao_original = carrinha.partida_prevista_carrinha_data_hora
    financeiro_original = financeiro(pedido)
    commit_original = db_session.commit
    rollback_original = db_session.rollback
    rollback_calls = []
    erro_commit = SQLAlchemyError("falha controlada no commit da partida")

    def commit_com_falha():
        raise erro_commit

    def rollback_monitorado():
        rollback_calls.append(True)
        return rollback_original()

    monkeypatch.setattr(db_session, "commit", commit_com_falha)
    monkeypatch.setattr(db_session, "rollback", rollback_monitorado)

    with pytest.raises(SQLAlchemyError) as exc_info:
        service.confirmar_partida_carrinha(
            carrinha.id,
            "motorista-partida",
            True,
            "Porta lateral avariada",
            ["foto-partida-falhou"],
        )

    assert exc_info.value is erro_commit
    assert rollback_calls == [True]
    assert db_session.in_transaction() is False

    SessionLocal = sessionmaker(bind=db_session.get_bind())
    with SessionLocal() as verificacao:
        persistida = verificacao.get(type(carrinha), carrinha.id)
        pedido_persistido = verificacao.get(type(pedido), pedido.id)
        assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
        assert persistida.partida_carrinha_data_hora is None
        assert persistida.partida_carrinha_feita_por is None
        assert persistida.status_recolha == StatusRecolhaPedido.PENDENTE.value
        assert persistida.recolha_data_hora is None
        assert persistida.recolha_feita_por is None
        assert persistida.contentor_avariado is False
        assert persistida.relato_avaria is None
        assert persistida.partida_prevista_carrinha_data_hora == previsao_original
        assert verificacao.query(ContentorFoto).filter_by(
            pedido_contentor_id=carrinha.id,
            tipo_foto=TipoFoto.RECOLHA.value,
        ).count() == 0
        assert financeiro(pedido_persistido) == financeiro_original

    monkeypatch.setattr(db_session, "commit", commit_original)
    monkeypatch.setattr(db_session, "rollback", rollback_original)
    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida", False, None, ["foto-partida-valida"]
    )
    db_session.refresh(carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value


def test_falha_no_commit_do_despejo_faz_rollback_integral_e_preserva_excecao(
    db_session, monkeypatch
):
    service, pedido = criar_carrinhas(db_session)
    carrinha = pedido.contentores[0]
    confirmar_chegada(service, pedido, carrinha)
    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida", False, None, ["foto-partida"]
    )
    db_session.refresh(carrinha)
    financeiro_original = financeiro(pedido)
    fotos_anteriores = {
        (foto.tipo_foto, foto.url_midia)
        for foto in carrinha.fotos
    }
    commit_original = db_session.commit
    rollback_original = db_session.rollback
    rollback_calls = []
    erro_commit = SQLAlchemyError("falha controlada no commit do despejo")

    def commit_com_falha():
        raise erro_commit

    def rollback_monitorado():
        rollback_calls.append(True)
        return rollback_original()

    monkeypatch.setattr(db_session, "commit", commit_com_falha)
    monkeypatch.setattr(db_session, "rollback", rollback_monitorado)

    with pytest.raises(SQLAlchemyError) as exc_info:
        service.confirmar_despejo_carrinha(
            carrinha.id,
            "Entulho Limpo",
            True,
            "Carga diferente da contratada",
            "motorista-despejo",
            ["foto-despejo-falhou"],
        )

    assert exc_info.value is erro_commit
    assert rollback_calls == [True]
    assert db_session.in_transaction() is False

    SessionLocal = sessionmaker(bind=db_session.get_bind())
    with SessionLocal() as verificacao:
        persistida = verificacao.get(type(carrinha), carrinha.id)
        pedido_persistido = verificacao.get(type(pedido), pedido.id)
        fotos_persistidas = {
            (foto.tipo_foto, foto.url_midia)
            for foto in verificacao.query(ContentorFoto).filter_by(
                pedido_contentor_id=carrinha.id
            )
        }
        assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
        assert persistida.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
        assert persistida.despejo_data_hora is None
        assert persistida.despejo_feito_por is None
        assert persistida.residuo_efetivo_vazadouro is None
        assert persistida.carga_errada is False
        assert persistida.relato_carga is None
        assert fotos_persistidas == fotos_anteriores
        assert all(tipo != TipoFoto.DESPEJO.value for tipo, _url in fotos_persistidas)
        assert financeiro(pedido_persistido) == financeiro_original

    monkeypatch.setattr(db_session, "commit", commit_original)
    monkeypatch.setattr(db_session, "rollback", rollback_original)
    service.confirmar_despejo_carrinha(
        carrinha.id,
        "Entulho Limpo",
        False,
        None,
        "motorista-despejo",
        ["foto-despejo-valida"],
    )
    db_session.refresh(carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value
