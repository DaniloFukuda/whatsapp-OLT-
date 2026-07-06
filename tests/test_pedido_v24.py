from datetime import datetime, timezone

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
)
from app.services.pedido_service import PedidoService


def msg(text=None, *, kind="text", media=None, lat=None, lon=None, phone="351900009900"):
    return NormalizedWhatsAppMessage(
        telefone=phone, tipo=kind, texto=text, media_id=media,
        latitude=lat, longitude=lon, message_id=media or "m",
    )


def test_service_cria_pedido_com_varios_contentores_e_consome_cotas(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Obra Central", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="450",
        pago=False, forma_pagamento=None, pedido_feito_por="gestor",
        endereco_aproximado="Rua da Obra", ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None
    assert len(pedido.contentores) == 2

    for index, contentor in enumerate(pedido.contentores, 1):
        contentor.numero_adesivo_contentor = str(index)
    db_session.commit()
    service.confirmar_entrega_lote(pedido.id, "motorista", 38.7, -9.1, "Portão")
    primeiro, segundo = pedido.contentores
    service.confirmar_recolha(primeiro.id, "motorista", False, None)
    service.confirmar_recolha(segundo.id, "motorista", True, "Lateral bastante amassada")
    service.confirmar_despejo(primeiro.id, "Entulho Limpo")
    assert service.cotas_restantes(pedido.id) == {"Entulho Misto": 1}
    service.confirmar_despejo(segundo.id, "Entulho Misto", True, "Havia lixo doméstico misturado")
    assert segundo.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert segundo.status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value
    assert segundo.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_fluxo_cadastro_v24_cria_lote(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "Cliente Lote", "351912345678", "Hoje", "2",
        "Entulho Limpo", "Entulho Misto", "500", "Não, pendente",
        "Rua Principal 10", "Não",
    ]
    response = ""
    for text in steps:
        response = router.handle(msg(text))

    assert "Pedido #1 criado com 2 contentor(es)" in response
    itens = db_session.query(PedidoContentor).all()
    assert [item.residuo_contratado for item in itens] == ["Entulho Limpo", "Entulho Misto"]
    conversa = db_session.query(ConversaWhatsApp).one()
    assert conversa.estado_atual == "idle"
    assert all(item.status_recolha != StatusRecolhaPedido.RECOLHIDO.value for item in itens)
