"""Caracterização do ciclo operacional V2.4 de contentores.

Os testes exercitam as APIs públicas e conferem o estado persistido e as filas
operacionais, sem depender da organização interna dos módulos de produção.
"""

from datetime import datetime, timezone

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusRecolhaPedido,
    TipoEquipamentoPedido,
)
from app.services.pedido_service import PedidoService


def criar_pedido_contentor(service: PedidoService, nome: str = "Cliente C1"):
    return service.criar(
        nome_cliente=nome,
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor-c1",
        endereco_aproximado="Rua da Caracterizacao",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )


def criar_pedido_carrinha(service: PedidoService):
    return service.criar(
        nome_cliente="Cliente Carrinha C1",
        telefone_cliente="351987654321",
        data_planejada=datetime.now(timezone.utc),
        valor_global="180",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor-c1",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "10:00",
            }
        ],
    )


def confirmar_entrega(service: PedidoService, pedido, adesivo: str = "41"):
    item = pedido.contentores[0]
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista-entrega-c1",
        38.7223,
        -9.1393,
        "Portao principal",
        [
            {
                "contentor_id": item.id,
                "numero_adesivo": adesivo,
                "fotos": [f"foto-entrega-{adesivo}"],
            }
        ],
    )
    return item


def ids(itens):
    return {item.id for item in itens}


def test_pedido_novo_cria_item_do_tipo_contentor(db_session):
    pedido = criar_pedido_contentor(PedidoService(db_session))

    db_session.refresh(pedido.contentores[0])

    assert pedido.contentores[0].tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value


def test_item_novo_aparece_na_fila_de_entrega_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)

    fila = service.pedidos_pendentes_entrega()

    assert [item.id for item in fila] == [pedido.id]
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value


def test_carrinha_nao_aparece_na_fila_de_entrega_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido_contentor = criar_pedido_contentor(service)
    pedido_carrinha = criar_pedido_carrinha(service)

    fila_ids = {pedido.id for pedido in service.pedidos_pendentes_entrega()}

    assert pedido_contentor.id in fila_ids
    assert pedido_carrinha.id not in fila_ids


def test_confirmar_entrega_persiste_entregue_e_remove_da_fila(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido)

    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.id == item.id
    assert persistido.status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert pedido.id not in {registro.id for registro in service.pedidos_pendentes_entrega()}


def test_contentor_entregue_torna_se_elegivel_para_recolha(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "42")

    db_session.expire_all()

    assert item.id in ids(service.contentores_para_recolha())
    assert service.get(pedido.id).contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value


def test_confirmar_recolha_persiste_remove_da_fila_e_habilita_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "43")

    service.confirmar_recolha(item.id, "motorista-recolha-c1", False, None)
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert persistido.id not in ids(service.contentores_para_recolha())
    assert persistido.id in ids(service.contentores_para_despejo())


def test_confirmar_despejo_conclui_ciclo_e_remove_das_filas(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "44")
    service.confirmar_recolha(item.id, "motorista-recolha-c1", False, None)

    service.confirmar_despejo(
        item.id,
        "Entulho Limpo",
        operador="motorista-despejo-c1",
        pedido_id=pedido.id,
        fotos=["foto-despejo-44"],
    )
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert pedido.id not in {registro.id for registro in service.pedidos_pendentes_entrega()}
    assert persistido.id not in ids(service.contentores_para_recolha())
    assert persistido.id not in ids(service.contentores_para_despejo())


def test_cancelar_entrega_antes_da_confirmacao_nao_persiste_mudanca(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = pedido.contentores[0]
    conversa = ConversaWhatsApp(
        telefone="351900009901",
        estado_atual="idle",
        contexto_json={},
    )
    db_session.add(conversa)
    db_session.commit()
    agent = PedidoV24Agent(db_session)

    agent.start_entrega(conversa)
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="1", message_id="c1-1"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="45", message_id="c1-2"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="image", media_id="foto-nao-persistida", message_id="c1-3"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="c1-4"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone,
        tipo="location",
        latitude=38.7223,
        longitude=-9.1393,
        message_id="c1-5",
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="Sim", message_id="c1-6"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="Portao", message_id="c1-7"
    ))
    resposta = agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="c1-8"
    ))
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert "cancelada" in resposta.lower()
    assert persistido.id == item.id
    assert persistido.status_entrega == StatusEntregaPedido.PENDENTE.value
    assert persistido.numero_adesivo_contentor is None
    assert db_session.query(ContentorFoto).count() == 0
