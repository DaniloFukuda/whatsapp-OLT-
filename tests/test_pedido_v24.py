from datetime import datetime, timedelta, timezone

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.agents.whatsapp_router_agent import MAIN_MENU
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.aluguer import ContentorFoto
from app.models.operador import Operador, PerfilOperador
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
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
    assert all(item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value for item in pedido.contentores)
    assert all(item.horario_agendado is None for item in pedido.contentores)
    assert all(item.precisa_mao_de_obra is False for item in pedido.contentores)

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


def test_service_cria_itens_contentor_e_carrinha(db_session):
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Hibrido",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="600",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": "CONTENTOR",
                "residuo_contratado": "Entulho Limpo",
                "precisa_mao_de_obra": False,
            },
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": True,
            },
        ],
    )

    contentor, carrinha = pedido.contentores
    assert contentor.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
    assert contentor.horario_agendado is None
    assert contentor.precisa_mao_de_obra is False
    assert carrinha.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert carrinha.horario_agendado == "14:00"
    assert carrinha.precisa_mao_de_obra is True


def operador(db_session, telefone, perfil):
    db_session.add(
        Operador(
            telefone_whatsapp=telefone,
            nome_operador=f"Operador {telefone}",
            perfil=perfil,
            ativo=True,
        )
    )
    db_session.commit()


def entregar_pedido(pedido, db_session, entrega_em, numeros=None):
    numeros = numeros or []
    for index, item in enumerate(pedido.contentores):
        item.status_entrega = StatusEntregaPedido.ENTREGUE.value
        item.status_recolha = StatusRecolhaPedido.PENDENTE.value
        item.entrega_data_hora = entrega_em
        item.entrega_latitude = 38.7
        item.entrega_longitude = -9.1
        if index < len(numeros):
            item.numero_adesivo_contentor = numeros[index]
    db_session.commit()


def test_service_rejeita_carrinha_sem_horario_valido(db_session):
    service = PedidoService(db_session)

    try:
        service.criar(
            nome_cliente="Cliente Carrinha",
            telefone_cliente="351912345678",
            data_planejada=datetime.now(timezone.utc),
            valor_global="300",
            pago=False,
            forma_pagamento=None,
            pedido_feito_por="gestor",
            endereco_aproximado="Rua",
            ponto_referencia=None,
            itens=[{"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "25:99"}],
        )
    except ValueError as exc:
        assert "horario agendado" in str(exc)
    else:
        raise AssertionError("Carrinha sem horario valido deveria falhar")


def test_menu_principal_usa_texto_com_emojis_sem_list_message():
    result = send_whatsapp_message("351900000000", MAIN_MENU, force_mock=True)

    assert result["status"] == "mocked"
    assert result["body"] == MAIN_MENU
    assert "interactive_type" not in result
    assert "1. 🟢 Novo pedido" in result["body"]


def test_cadastro_v24_carrinha_valida_horario_e_mao_de_obra(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    for text in ["novo pedido", "Cliente Carrinha", "351912345678", "Hoje", "1"]:
        response = router.handle(msg(text))
    assert "Carrinha" in response
    horario_prompt = router.handle(msg("2"))
    assert "HH:MM" in horario_prompt
    invalid = router.handle(msg("99:99"))
    assert "Horário inválido" in invalid
    mao_obra = router.handle(msg("09:30"))
    assert mao_obra.startswith("O cliente solicitou pessoal para carregamento do resíduo?")
    assert "Sim, com pessoal" in mao_obra
    assert "Não, apenas equipamento" in mao_obra
    residuo_prompt = router.handle(msg("1"))
    assert "Entulho Limpo" in residuo_prompt
    for text in ["1", "250", "Não, pendente", "Rua da Carrinha", "Não"]:
        response = router.handle(msg(text))

    item = db_session.query(PedidoContentor).one()
    assert "Pedido #1 criado" in response
    assert item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert item.horario_agendado == "09:30"
    assert item.precisa_mao_de_obra is True
    assert item.residuo_contratado == "Entulho Limpo"


def test_cadastro_v24_salva_mao_de_obra_false(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "Cliente Sem Pessoal", "351912345678", "Hoje", "1",
        "1", "2", "2", "120", "Não, pendente", "Rua", "Não",
    ]
    for text in steps:
        response = router.handle(msg(text))

    item = db_session.query(PedidoContentor).one()
    assert "Pedido #1 criado" in response
    assert item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
    assert item.precisa_mao_de_obra is False


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


def test_entrega_v24_carrinha_aceita_frota_zero_e_grava_so_no_gps(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Carrinha Entrega", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300",
        pago=True, forma_pagamento="MBWay", pedido_feito_por="gestor",
        endereco_aproximado="Rua", ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": True,
            }
        ],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("2"))
    prompt = router.handle(msg("1"))
    assert "frota da carrinha" in prompt
    foto_prompt = router.handle(msg("0"))
    assert "Carrinha sem frota" in foto_prompt
    router.handle(msg(kind="image", media="foto-carrinha"))
    router.handle(msg("2"))
    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].numero_adesivo_contentor is None
    assert db_session.query(ContentorFoto).count() == 0

    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    response = router.handle(msg("Portao azul"))

    db_session.refresh(pedido.contentores[0])
    assert "Entrega do lote registrada com sucesso" in response
    assert pedido.contentores[0].numero_adesivo_contentor is None
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.ENTREGA.value).count() == 1


def test_recolha_v24_lista_contentor_e_carrinha_com_labels(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Recolha Hibrida",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="500",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[
            {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"},
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "14:00",
            },
        ],
    )
    service = PedidoService(db_session)
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "44", "fotos": []},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "0", "fotos": []},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    response = router.handle(msg("3"))

    assert "📦 Contentor 44" in response
    assert "🚛 Carrinha (14:00)" in response


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


def test_resumo_v32_gestor_ve_blocos_contentores_carrinhas_financeiro_e_menu_separado(db_session):
    gestor = "351900010001"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    service = PedidoService(db_session)
    now = datetime.now(timezone.utc)
    pedido_contentores = service.criar(
        nome_cliente="Cliente Agrupado",
        telefone_cliente="351912345678",
        data_planejada=now,
        valor_global="100",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    entregar_pedido(pedido_contentores, db_session, now - timedelta(days=5), ["11", "12"])
    pedido_carrinha = service.criar(
        nome_cliente="Cliente Carrinha Painel",
        telefone_cliente="351900000222",
        data_planejada=now,
        valor_global="80",
        pago=True,
        forma_pagamento="Dinheiro",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": True,
            }
        ],
    )
    entregar_pedido(pedido_carrinha, db_session, now, ["0"])
    pedido_misto = service.criar(
        nome_cliente="Cliente Misto",
        telefone_cliente="351900000333",
        data_planejada=now,
        valor_global="100",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Mista",
        ponto_referencia=None,
        itens=[
            {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"},
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "15:00",
            },
        ],
    )
    entregar_pedido(pedido_misto, db_session, now, ["21", "0"])

    router = WhatsappRouterAgent(db_session)
    response = router.handle(msg("resumo", phone=gestor))

    assert "PAINEL DE CONTROLE OPERACIONAL OLT" in response
    assert "1. VENCEM AMANHA" in response
    assert "2. RECOLHER HOJE" in response
    assert "3. RECOLHER AMANHA" in response
    assert "4. PENDENCIAS ATIVAS" in response
    assert "5. RESUMO FINANCEIRO DO MES" in response
    assert response.count("Cliente Agrupado") == 1
    assert "11, 12" in response
    assert "Cliente Carrinha Painel: horario 14:00 • ⚠️ Com Pessoal" in response
    assert "https://www.google.com/maps?q=38.7,-9.1" in response
    assert "Receita de Contentores ja paga: EUR 100.00" in response
    assert "Receita de Contentores pendente: EUR 50.00" in response
    assert "Receita de Carrinhas ja paga: EUR 80.00" in response
    assert "Receita de Carrinhas pendente: EUR 50.00" in response
    assert "Faturado Global: EUR 180.00" in response
    assert "A receber Global: EUR 100.00" in response
    assert "Total projetado do mes: EUR 280.00" in response
    assert "Menu principal - OLT Entulhos" not in response
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_resumo_v32_funcionario_oculta_comercial_financeiro_pagamentos_e_carga(db_session):
    funcionario = "351900010002"
    operador(db_session, funcionario, PerfilOperador.FUNCIONARIO)
    service = PedidoService(db_session)
    now = datetime.now(timezone.utc)
    pedido = service.criar(
        nome_cliente="Cliente Funcionario",
        telefone_cliente="351912345678",
        data_planejada=now,
        valor_global="300",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[
            {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"},
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "16:30",
                "precisa_mao_de_obra": True,
            },
        ],
    )
    entregar_pedido(pedido, db_session, now - timedelta(days=5), ["31", "0"])
    pedido.contentores[0].residuo_efetivo_vazadouro = "Entulho Misto"
    pedido.contentores[0].carga_errada = True
    pedido.contentores[0].status_resolucao_carga = StatusResolucaoPedido.PENDENTE.value
    pedido.contentores[1].contentor_avariado = True
    pedido.contentores[1].relato_avaria = "Porta lateral amassada"
    pedido.contentores[1].status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    db_session.commit()

    router = WhatsappRouterAgent(db_session)
    response = router.handle(msg("resumo", phone=funcionario))

    assert "1. VENCEM AMANHA" not in response
    assert "5. RESUMO FINANCEIRO" not in response
    assert "Pagamentos pendentes" not in response
    assert "Divergencias de residuo" not in response
    assert "valor" not in response.lower()
    assert "EUR" not in response
    assert "Avarias em equipamentos" in response
    assert "Carrinha 16:30" in response
    assert "Porta lateral amassada" in response
    assert "resolver avaria" in response
    assert "Carrinha | Cliente Funcionario: horario 16:30 • ⚠️ Com Pessoal" in response
    assert "Menu principal - OLT Entulhos" not in response
    pending = router.pop_pending_messages()
    assert pending == [
        "Ola, sou o Robo de Gestao de Contentores da OLT. O que vamos fazer agora?\n\n"
        "1. Confirmar entrega de contentor\n"
        "2. Confirmar recolha de contentor\n"
        "3. Confirmar Despejo no Vazadouro"
    ]
