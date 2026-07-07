from datetime import datetime, timezone

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.agents.whatsapp_router_agent import MAIN_MENU
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.aluguer import ContentorFoto
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoFoto,
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


def test_menu_principal_usa_texto_com_emojis_sem_list_message():
    result = send_whatsapp_message("351900000000", MAIN_MENU, force_mock=True)

    assert result["status"] == "mocked"
    assert result["body"] == MAIN_MENU
    assert "interactive_type" not in result
    assert "1. 🟢 Novo pedido" in result["body"]


def test_entrega_v24_guarda_lote_no_contexto_ate_gps(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Entrega", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300",
        pago=True, forma_pagamento="MBWay", pedido_feito_por="gestor",
        endereco_aproximado="Rua", ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("2"))
    router.handle(msg("1"))
    router.handle(msg("101"))
    router.handle(msg(kind="image", media="foto-101"))
    router.handle(msg("2"))

    db_session.refresh(pedido.contentores[0])
    conversa = db_session.query(ConversaWhatsApp).one()
    assert pedido.contentores[0].numero_adesivo_contentor is None
    assert db_session.query(ContentorFoto).count() == 0
    assert conversa.contexto_json["entregas"][0]["numero_adesivo"] == "101"
    assert conversa.contexto_json["entregas"][0]["fotos"] == ["foto-101"]

    router.handle(msg("202"))
    router.handle(msg(kind="image", media="foto-202"))
    router.handle(msg("2"))
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    response = router.handle(msg("Portao azul"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Entrega do lote registrada com sucesso" in response
    assert [c.numero_adesivo_contentor for c in pedido.contentores] == ["101", "202"]
    assert all(c.status_entrega == StatusEntregaPedido.ENTREGUE.value for c in pedido.contentores)
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.ENTREGA.value).count() == 2
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_despejo_v24_mapeia_indice_para_residuo_do_contexto(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Despejo", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300",
        pago=True, forma_pagamento="MBWay", pedido_feito_por="gestor",
        endereco_aproximado="Rua", ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    for index, contentor in enumerate(pedido.contentores, 1):
        contentor.numero_adesivo_contentor = str(index)
    db_session.commit()
    service.confirmar_entrega_lote(pedido.id, "motorista", 38.7, -9.1, None)
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    service.confirmar_recolha(pedido.contentores[1].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo"))
    prompt = router.handle(msg("2"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.contexto_json["residuos_disponiveis"] == ["Entulho Limpo", "Entulho Misto"]
    assert "Entulho Limpo" in prompt
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "Despejo auditado" in response
    assert pedido.contentores[0].residuo_efetivo_vazadouro == "Entulho Limpo"
