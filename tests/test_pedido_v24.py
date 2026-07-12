from datetime import datetime, timedelta, timezone

import pytest

from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.agents.whatsapp_router_agent import MAIN_MENU
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage, parse_whatsapp_payload
from app.models.conversa import ConversaWhatsApp
from app.models.aluguer import ContentorFoto
from app.models.operador import Operador, PerfilOperador
from app.models.pedido import (
    Pedido,
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


def contact_msg(name=None, contact_phone=None, *, phone="351900009900"):
    return NormalizedWhatsAppMessage(
        telefone=phone,
        tipo="contacts",
        message_id="m-contact",
        contact_name=name,
        contact_phone=contact_phone,
    )


def liberar_operadores(monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()


def avancar_cadastro_v24_ate_mao_obra(router, *, tipo="contentor", quantidade="1", phone="351900009900"):
    steps = ["novo pedido", tipo, "Cliente Hotfix", "351912345678", quantidade]
    response = ""
    for text in steps:
        response = router.handle(msg(text, phone=phone))
    return response


def parsed_location_message(latitude, longitude, *, name=None, address=None, phone="351900009900"):
    location = {"latitude": latitude, "longitude": longitude}
    if name is not None:
        location["name"] = name
    if address is not None:
        location["address"] = address
    return parse_whatsapp_payload(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": phone,
                                        "id": "wamid.location",
                                        "type": "location",
                                        "location": location,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )[0]


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
    primeiro, segundo = pedido.contentores
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        "Portão",
        [
            {"contentor_id": primeiro.id, "numero_adesivo": "1", "fotos": ["foto-1"]},
            {"contentor_id": segundo.id, "numero_adesivo": "2", "fotos": ["foto-2"]},
        ],
    )
    service.confirmar_recolha(primeiro.id, "motorista", False, None)
    service.confirmar_recolha(segundo.id, "motorista", True, "Lateral bastante amassada")
    service.confirmar_despejo(primeiro.id, "Entulho Limpo")
    assert service.cotas_restantes(pedido.id) == {"Entulho Misto": 1}
    service.confirmar_despejo(segundo.id, "Entulho Misto", True, "Havia lixo doméstico misturado")
    assert segundo.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert segundo.status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value
    assert segundo.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_service_vincula_fotos_ao_ativo_correto(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Fotos",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="250",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua das Fotos",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores

    service.adicionar_foto(primeiro.id, "whatsapp://media/foto-ativo-1", TipoFoto.ENTREGA)

    db_session.refresh(primeiro)
    db_session.refresh(segundo)
    assert [foto.url_midia for foto in primeiro.fotos] == ["whatsapp://media/foto-ativo-1"]
    assert segundo.fotos == []


def test_service_confirma_despejo_com_auditoria_sem_status_despejo(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Auditoria",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="250",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Auditoria",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    contentor = pedido.contentores[0]
    contentor.numero_adesivo_contentor = "700"
    db_session.commit()
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista-entrega",
        38.7,
        -9.1,
        None,
        [{"contentor_id": contentor.id, "numero_adesivo": "700", "fotos": ["foto-700"]}],
    )
    service.confirmar_recolha(contentor.id, "motorista-recolha", False, None)

    service.confirmar_despejo(contentor.id, "Entulho Limpo", operador="motorista-despejo")

    db_session.refresh(contentor)
    assert contentor.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert contentor.despejo_feito_por == "motorista-despejo"
    assert contentor.despejo_data_hora is not None
    assert not hasattr(contentor, "status_despejo")


def test_service_lista_ativos_por_status_operacional(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Status",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="450",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Status",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores

    assert service.pedidos_pendentes_entrega() == [pedido]
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": primeiro.id, "numero_adesivo": "501", "fotos": ["foto-501"]},
            {"contentor_id": segundo.id, "numero_adesivo": "502", "fotos": ["foto-502"]},
        ],
    )
    recolha_ids = {item.id for item in service.contentores_para_recolha()}
    assert {primeiro.id, segundo.id} <= recolha_ids

    service.confirmar_recolha(primeiro.id, "motorista", False, None)

    despejo_ids = {item.id for item in service.contentores_para_despejo()}
    recolha_ids = {item.id for item in service.contentores_para_recolha()}
    assert primeiro.id in despejo_ids
    assert segundo.id in recolha_ids


def test_fluxo_cadastro_v24_cria_lote(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "contentor", "Cliente Lote", "351912345678", "2",
        "Não", "Entulho Limpo", "Entulho Misto", "Hoje", "500", "Não, pendente",
        "Rua Principal 10", "Não", "1",
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


def test_cadastro_v24_primeira_pergunta_apos_novo_pedido_e_tipo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    response = router.handle(msg("novo pedido"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "tipo de solicita" in response.lower()
    assert "Contentor" in response
    assert "Carrinha" in response
    assert conversa.estado_atual == "v24_cadastro_tipo_solicitacao"
    assert conversa.contexto_json == {}


def test_cadastro_v24_contato_na_etapa_nome_salva_nome_telefone_e_avanca(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    router.handle(msg("contentor"))
    response = router.handle(contact_msg("Cliente Contacto", "+351 913 000 111"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "contentores" in response.lower()
    assert conversa.estado_atual == "v24_cadastro_quantidade"
    assert conversa.contexto_json["nome"] == "Cliente Contacto"
    assert conversa.contexto_json["telefone"] == "351913000111"


def test_cadastro_v24_contato_na_etapa_nome_sem_telefone_valido_pede_telefone(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    router.handle(msg("contentor"))
    response = router.handle(contact_msg("Cliente Sem Telefone", "abc"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "telefone do cliente" in response
    assert conversa.estado_atual == "v24_cadastro_telefone"
    assert conversa.contexto_json["nome"] == "Cliente Sem Telefone"
    assert "telefone" not in conversa.contexto_json


def test_cadastro_v24_contato_na_etapa_telefone_valida_e_avanca(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    router.handle(msg("contentor"))
    router.handle(msg("Cliente Telefone"))
    response = router.handle(contact_msg("Outro Nome", "+351 914 000 222"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "contentores" in response.lower()
    assert conversa.estado_atual == "v24_cadastro_quantidade"
    assert conversa.contexto_json["nome"] == "Cliente Telefone"
    assert conversa.contexto_json["telefone"] == "351914000222"


def test_cadastro_v24_contato_invalido_na_etapa_telefone_mantem_mensagem_atual(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    router.handle(msg("contentor"))
    router.handle(msg("Cliente Invalido"))
    response = router.handle(contact_msg("Contato Invalido", "123"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert response == "O telefone informado não é válido."
    assert conversa.estado_atual == "v24_cadastro_telefone"
    assert "telefone" not in conversa.contexto_json


def test_cadastro_v24_texto_nome_e_telefone_continuam_funcionando(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    router.handle(msg("contentor"))
    phone_prompt = router.handle(msg("Cliente Texto"))
    response = router.handle(msg("+351 912 345 678"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "telefone do cliente" in phone_prompt
    assert "contentores" in response.lower()
    assert conversa.estado_atual == "v24_cadastro_quantidade"
    assert conversa.contexto_json["nome"] == "Cliente Texto"
    assert conversa.contexto_json["telefone"] == "351912345678"


def test_cadastro_v24_tipo_invalido_nao_avanca_fluxo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("novo pedido"))
    response = router.handle(msg("retroescavadora"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "tipo de solicita" in response.lower()
    assert conversa.estado_atual == "v24_cadastro_tipo_solicitacao"
    assert "tipo_solicitacao" not in conversa.contexto_json


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("50", "Valor: 50,00 €"),
        ("50.5", "Valor: 50,50 €"),
        ("1250.75", "Valor: 1.250,75 €"),
    ],
)
def test_cadastro_v24_resumo_formata_valor_em_euros(db_session, valor, esperado):
    agent = WhatsappRouterAgent(db_session).pedido_v24_agent
    response = agent._format_confirmacao_cadastro(
        {
            "tipo_solicitacao": TipoEquipamentoPedido.CONTENTOR.value,
            "quantidade": 2,
            "precisa_mao_de_obra": False,
            "nome": "Cliente",
            "telefone": "351912345678",
            "valor": valor,
            "pago": False,
            "endereco": "Rua",
        }
    )

    assert esperado in response
    assert "Quantidade de contentores: 2" in response
    assert "Quantidade:" not in response


def test_cadastro_v24_resumo_mostra_quantidade_de_carrinhas(db_session):
    agent = WhatsappRouterAgent(db_session).pedido_v24_agent

    response = agent._format_confirmacao_cadastro(
        {
            "tipo_solicitacao": TipoEquipamentoPedido.CARRINHA.value,
            "quantidade": 1,
            "precisa_mao_de_obra": True,
            "horario_agendado": "14:00",
            "nome": "Cliente",
            "telefone": "351912345678",
            "valor": "50",
            "pago": False,
            "endereco": "Rua",
        }
    )

    assert "Quantidade de carrinhas: 1" in response
    assert "Horário da carrinha: 14:00" in response


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("pedido_mao_obra_sim", True),
        ("1", True),
        ("sim", True),
        ("✅ Sim", True),
        ("pedido_mao_obra_nao", False),
        ("2", False),
        ("não", False),
        ("nao", False),
        ("❌ Não", False),
    ],
)
def test_cadastro_v24_mao_obra_aceita_botoes_e_fallbacks(db_session, monkeypatch, choice, expected):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    prompt = avancar_cadastro_v24_ate_mao_obra(router)
    assert "mão de obra" in prompt.lower()
    result = send_whatsapp_message("351900009900", prompt, force_mock=True)
    assert result["interactive_type"] == "button"
    assert [button["id"] for button in result["buttons"]] == [
        "pedido_mao_obra_sim",
        "pedido_mao_obra_nao",
    ]

    response = router.handle(msg(choice))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_residuo"
    assert conversa.contexto_json["precisa_mao_de_obra"] is expected
    assert "Resíduo do contentor 1/1" in response
    assert db_session.query(Pedido).count() == 0


def test_cadastro_v24_mao_obra_invalida_mantem_estado_e_reenvia_botoes(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    avancar_cadastro_v24_ate_mao_obra(router)
    response = router.handle(msg("talvez"))
    conversa = db_session.query(ConversaWhatsApp).one()
    result = send_whatsapp_message("351900009900", response, force_mock=True)

    assert conversa.estado_atual == "v24_cadastro_mao_obra"
    assert "precisa_mao_de_obra" not in conversa.contexto_json
    assert result["interactive_type"] == "button"
    assert [button["id"] for button in result["buttons"]] == [
        "pedido_mao_obra_sim",
        "pedido_mao_obra_nao",
    ]


@pytest.mark.parametrize("choice", ["pedido_mao_obra_sim", "pedido_mao_obra_nao"])
def test_cadastro_v24_carrinha_mao_obra_continua_avancando_para_residuo(db_session, monkeypatch, choice):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    avancar_cadastro_v24_ate_mao_obra(router, tipo="carrinha")
    response = router.handle(msg(choice))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_horario_carrinha"
    assert "HH:MM" in response


def test_cadastro_v24_residuos_multicontentor_usam_mesmo_prompt_interativo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    avancar_cadastro_v24_ate_mao_obra(router, quantidade="3")
    first = router.handle(msg("pedido_mao_obra_nao"))
    first_buttons = send_whatsapp_message("351900009900", first, force_mock=True)
    second = router.handle(msg("pedido_residuo_limpo"))
    second_buttons = send_whatsapp_message("351900009900", second, force_mock=True)
    third = router.handle(msg("entulho misto"))
    third_buttons = send_whatsapp_message("351900009900", third, force_mock=True)
    invalid = router.handle(msg("madeira"))
    after_invalid = db_session.query(ConversaWhatsApp).one()
    after_invalid_context = dict(after_invalid.contexto_json)
    after_invalid_state = after_invalid.estado_atual
    final_prompt = router.handle(msg("misto"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert "Resíduo do contentor 1/3" in first
    assert "Resíduo do contentor 2/3" in second
    assert "Resíduo do contentor 3/3" in third
    assert "Resíduo do contentor 3/3" in invalid
    assert [button["id"] for button in first_buttons["buttons"]] == [
        "pedido_residuo_limpo",
        "pedido_residuo_misto",
    ]
    assert first_buttons["buttons"] == second_buttons["buttons"] == third_buttons["buttons"]
    assert after_invalid_context["residuos"] == ["Entulho Limpo", "Entulho Misto"]
    assert after_invalid_state == "v24_cadastro_residuo"
    assert conversa.contexto_json["residuos"] == ["Entulho Limpo", "Entulho Misto", "Entulho Misto"]
    assert len(conversa.contexto_json["itens"]) == 3
    assert conversa.estado_atual == "v24_cadastro_data"
    assert "Quando está planejada" in final_prompt


def test_cadastro_v24_endereco_aceita_localizacao_nativa_parseada(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "contentor", "Cliente Localizacao", "351912345678", "1",
        "não", "limpo", "Hoje", "120", "Não, pendente",
    ]
    for text in steps:
        router.handle(msg(text))
    response = router.handle(
        parsed_location_message(38.7223, -9.1393, name="Obra Lisboa", address="Lisboa, Portugal")
    )
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_referencia_opcao"
    assert "ponto de referência" in response
    assert conversa.contexto_json["endereco"] == (
        "Obra Lisboa - Lisboa, Portugal - https://www.google.com/maps?q=38.7223,-9.1393"
    )
    assert conversa.contexto_json["endereco_latitude"] == 38.7223
    assert conversa.contexto_json["endereco_longitude"] == -9.1393


def test_cadastro_v24_endereco_rejeita_location_sem_coordenadas_sem_apagar_contexto(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "contentor", "Cliente Sem GPS", "351912345678", "1",
        "não", "limpo", "Hoje", "120", "Não, pendente",
    ]
    for text in steps:
        router.handle(msg(text))
    before = dict(db_session.query(ConversaWhatsApp).one().contexto_json)
    response = router.handle(NormalizedWhatsAppMessage(telefone="351900009900", tipo="location"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_endereco"
    assert conversa.contexto_json == before
    assert "Não foi possível ler a localização" in response


def test_cadastro_v24_endereco_digitado_e_link_maps_continuam_funcionando(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "contentor", "Cliente Maps", "351912345678", "1",
        "não", "limpo", "Hoje", "120", "Não, pendente",
    ]
    for text in steps:
        router.handle(msg(text))
    response = router.handle(msg("https://www.google.com/maps?q=38.7,-9.1"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_referencia_opcao"
    assert conversa.contexto_json["endereco"] == "https://www.google.com/maps?q=38.7,-9.1"
    assert conversa.contexto_json["endereco_latitude"] == 38.7
    assert conversa.contexto_json["endereco_longitude"] == -9.1
    assert "ponto de referência" in response


def test_service_rejeita_pedido_misto_contentor_e_carrinha(db_session):
    try:
        PedidoService(db_session).criar(
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
    except ValueError as exc:
        assert "combinar contentores e carrinhas" in str(exc)
    else:
        raise AssertionError("Pedido misto deveria ser rejeitado")


def test_service_cria_multiplas_carrinhas_com_mao_de_obra_unica(db_session):
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Carrinhas",
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
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": True,
            },
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": True,
            },
        ],
    )

    assert len(pedido.contentores) == 2
    assert all(item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value for item in pedido.contentores)
    assert all(item.horario_agendado == "14:00" for item in pedido.contentores)
    assert pedido.precisa_mao_de_obra is True
    assert all(item.precisa_mao_de_obra is False for item in pedido.contentores)


def test_mao_de_obra_do_pedido_nao_depende_de_alterar_ou_remover_item(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Mao Obra",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="600",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        precisa_mao_de_obra=True,
        itens=[
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Misto",
                "horario_agendado": "14:00",
            },
            {
                "tipo_equipamento": "CARRINHA",
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
            },
        ],
    )

    pedido.contentores[0].precisa_mao_de_obra = False
    db_session.delete(pedido.contentores[1])
    db_session.commit()
    db_session.refresh(pedido)

    assert pedido.precisa_mao_de_obra is True
    assert service.precisa_mao_de_obra(pedido) is True
    assert len(pedido.contentores) == 1
    assert pedido.contentores[0].precisa_mao_de_obra is False


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


def criar_pedido_legado_misto(
    db_session, *, nome, telefone, data_planejada, valor="100", pago=False, carrinha_horario="15:00"
):
    pedido = Pedido(
        nome_cliente=nome,
        telefone_cliente=telefone,
        data_planejada=data_planejada,
        valor_global=valor,
        status_pagamento=StatusPagamento.PAGO.value if pago else StatusPagamento.PENDENTE.value,
        forma_pagamento="MBWay" if pago else None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua legada",
        contentores=[
            PedidoContentor(
                tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
                residuo_contratado="Entulho Limpo",
            ),
            PedidoContentor(
                tipo_equipamento=TipoEquipamentoPedido.CARRINHA.value,
                residuo_contratado="Entulho Misto",
                horario_agendado=carrinha_horario,
            ),
        ],
    )
    db_session.add(pedido)
    db_session.commit()
    db_session.refresh(pedido)
    return pedido


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
    assert "OLT Gestão de Resíduos & Demolições" in result["body"]
    assert "OLT Entulhos" not in result["body"]
    assert "1. 🟢 Novo pedido" in result["body"]


def test_cadastro_v24_carrinha_valida_horario_e_mao_de_obra(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    for text in ["novo pedido", "carrinha", "Cliente Carrinha", "351912345678", "1"]:
        response = router.handle(msg(text))
    assert "mão de obra" in response.lower()
    assert "Sim" in response
    assert "Não" in response
    horario = router.handle(msg("1"))
    assert "HH:MM" in horario
    invalid = router.handle(msg("99:99"))
    assert "Horário inválido" in invalid
    residuo_prompt = router.handle(msg("09:30"))
    assert "Entulho Limpo" in residuo_prompt
    for text in ["1", "Hoje", "250", "Não, pendente", "Rua da Carrinha", "Não"]:
        response = router.handle(msg(text))
    assert "Tipo da solicita" in response
    assert "Carrinha" in response
    assert "Quantidade de carrinhas: 1" in response
    assert "Mão de obra: Sim" in response
    assert "Horário da carrinha: 09:30" in response
    assert "Valor: 250,00 €" in response
    response = router.handle(msg("1"))

    pedido = db_session.query(Pedido).one()
    item = db_session.query(PedidoContentor).one()
    assert "Pedido #1 criado" in response
    assert pedido.precisa_mao_de_obra is True
    assert item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert item.horario_agendado == "09:30"
    assert item.precisa_mao_de_obra is False
    assert item.residuo_contratado == "Entulho Limpo"


def test_cadastro_v24_salva_mao_de_obra_false(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    router = WhatsappRouterAgent(db_session)

    steps = [
        "novo pedido", "contentor", "Cliente Sem Pessoal", "351912345678", "1",
        "2", "2", "Hoje", "120", "Não, pendente", "Rua", "Não", "1",
    ]
    for text in steps:
        response = router.handle(msg(text))

    pedido = db_session.query(Pedido).one()
    item = db_session.query(PedidoContentor).one()
    assert "Pedido #1 criado" in response
    assert pedido.precisa_mao_de_obra is False
    assert item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
    assert item.precisa_mao_de_obra is False


def test_cadastro_v24_carrinha_multipla_pergunta_mao_de_obra_uma_vez(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    steps = ["novo pedido", "carrinha", "Cliente Carrinhas", "351912345678", "2"]
    for text in steps:
        response = router.handle(msg(text))
    assert "mão de obra" in response.lower()

    response = router.handle(msg("1"))
    assert "HH:MM" in response
    response = router.handle(msg("09:30"))
    assert "Entulho Limpo" in response
    response = router.handle(msg("1"))
    assert "mão de obra" not in response.lower()
    assert "Entulho Limpo" in response
    for text in ["2", "Hoje", "500", "Não, pendente", "Rua Carrinhas", "Não"]:
        response = router.handle(msg(text))
    assert response.count("Mão de obra") == 1
    response = router.handle(msg("1"))

    assert "Pedido #1 criado" in response
    pedido = db_session.query(Pedido).one()
    itens = db_session.query(PedidoContentor).all()
    assert len(itens) == 2
    assert pedido.precisa_mao_de_obra is True
    assert all(item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value for item in itens)
    assert all(item.precisa_mao_de_obra is False for item in itens)


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
    confirmacao = router.handle(msg("Portao azul"))
    assert "Confirmar entrega" in confirmacao
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Entrega confirmada com sucesso" in response
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
    confirmacao = router.handle(msg("Portao azul"))
    assert "Confirmar entrega" in confirmacao
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "Entrega confirmada com sucesso" in response
    assert pedido.contentores[0].numero_adesivo_contentor is None
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.ENTREGA.value).count() == 1


def test_entrega_v24_duas_carrinhas_separa_frota_e_fotos(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Duas Carrinhas",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="500",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "09:00"},
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Misto", "horario_agendado": "11:00"},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    lista = router.handle(msg("2"))
    assert "Cliente Duas Carrinhas" in lista
    assert "Carrinha x2" in lista
    router.handle(msg("1"))
    router.handle(msg("0"))
    router.handle(msg(kind="image", media="foto-carrinha-1a"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-carrinha-1b"))
    proximo = router.handle(msg("2"))
    assert "Ativo 1 de 2 preparado" in proximo
    assert "Selecione o pedido" not in proximo
    router.handle(msg("77"))
    router.handle(msg(kind="image", media="foto-carrinha-2"))
    router.handle(msg("2"))
    invalid_gps = router.handle(msg("https://www.google.com/maps?q=38.7,-9.1"))
    assert "localização nativa do WhatsApp" in invalid_gps
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    confirmacao = router.handle(msg("Portao norte"))
    assert "fotos: 2" in confirmacao
    assert "fotos: 1" in confirmacao
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Entrega confirmada com sucesso" in response
    assert pedido.contentores[0].numero_adesivo_contentor is None
    assert pedido.contentores[1].numero_adesivo_contentor == "77"
    assert [foto.url_midia for foto in pedido.contentores[0].fotos] == [
        "foto-carrinha-1a",
        "foto-carrinha-1b",
    ]
    assert [foto.url_midia for foto in pedido.contentores[1].fotos] == ["foto-carrinha-2"]


def test_entrega_v24_exige_foto_e_cancela_sem_persistir(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Cancela Foto",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("2"))
    router.handle(msg("1"))
    router.handle(msg("123"))
    invalid = router.handle(msg("texto em vez de foto"))
    cancelado = router.handle(msg("cancelar"))

    db_session.refresh(pedido.contentores[0])
    assert "Envie uma imagem" in invalid
    assert "Operação cancelada" in cancelado
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).count() == 0


def test_entrega_v24_cancelamento_na_confirmacao_nao_persiste(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Cancela Confirmacao",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    router = WhatsappRouterAgent(db_session)

    for item in ["2", "1", "123"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-123"))
    router.handle(msg("2"))
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    confirmacao = router.handle(msg("Portao"))
    response = router.handle(msg("2"))

    db_session.refresh(pedido.contentores[0])
    assert "Confirmar entrega" in confirmacao
    assert "Entrega cancelada" in response
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).count() == 0


def test_entrega_v24_pagamento_pendente_pode_ser_pago_no_local(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Paga Local",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    router = WhatsappRouterAgent(db_session)

    for item in ["2", "1", "123"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-123"))
    router.handle(msg("2"))
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    router.handle(msg("Portao"))
    pergunta_pagamento = router.handle(msg("1"))
    pagamento = router.handle(msg("1"))
    response = router.handle(msg("3"))

    db_session.refresh(pedido)
    assert "pagamento no local" in pergunta_pagamento
    assert "forma recebida" in pagamento
    assert "Pagamento registrado" in response
    assert pedido.status_pagamento == StatusPagamento.PAGO.value
    assert pedido.forma_pagamento == "Dinheiro"


def test_entrega_v24_pagamento_pendente_pode_continuar_pendente(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Continua Pendente",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    router = WhatsappRouterAgent(db_session)

    for item in ["2", "1", "123"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-123"))
    router.handle(msg("2"))
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    router.handle(msg("Portao"))
    pergunta_pagamento = router.handle(msg("1"))
    response = router.handle(msg("2"))

    db_session.refresh(pedido)
    assert "pagamento no local" in pergunta_pagamento
    assert "permanece pendente" in response.lower()
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None


def test_entrega_v24_conflito_concorrente_cancela_estado_sem_duplicar_fotos(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    pedido = PedidoService(db_session).criar(
        nome_cliente="Cliente Concorrencia",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    router = WhatsappRouterAgent(db_session)

    for item in ["2", "1", "123"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-123"))
    router.handle(msg(kind="image", media="foto-123"))
    router.handle(msg("2"))
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    router.handle(msg("Portao"))
    pedido.contentores[0].status_entrega = StatusEntregaPedido.ENTREGUE.value
    db_session.commit()
    response = router.handle(msg("1"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "lista de ativos pendentes mudou" in response.lower()
    assert conversa.estado_atual == "idle"
    assert db_session.query(ContentorFoto).count() == 0


def test_service_rejeita_entrega_com_ativo_de_outro_pedido(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Um",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    outro = service.criar(
        nome_cliente="Cliente Dois",
        telefone_cliente="351912345679",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )

    with pytest.raises(ValueError, match="ativos pendentes mudou"):
        service.confirmar_entrega_lote(
            pedido.id,
            "motorista",
            38.7,
            -9.1,
            None,
            [{"contentor_id": outro.contentores[0].id, "numero_adesivo": "999", "fotos": ["foto"]}],
        )


def test_recolha_v24_lista_contentor_e_carrinha_com_labels(db_session, monkeypatch):
    for name in ("WHATSAPP_OWNER_PHONE", "AUTHORIZED_OPERATOR_PHONE",
                 "AUTHORIZED_OPERATOR_PHONES", "OWNER_WHATSAPP"):
        monkeypatch.setenv(name, "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    pedido = criar_pedido_legado_misto(
        db_session,
        nome="Cliente Recolha Hibrida",
        telefone="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor="500",
        pago=True,
        carrinha_horario="14:00",
    )
    service = PedidoService(db_session)
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "44", "fotos": ["foto-44"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "0", "fotos": ["foto-carrinha"]},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    response = router.handle(msg("3"))

    assert "📦 Contentor 44" in response
    assert "🚛 Carrinha (14:00)" in response


def test_recolha_v24_confirma_todos_ativos_do_pedido_em_loop(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Loop Recolha",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "81", "fotos": ["foto-81"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "82", "fotos": ["foto-82"]},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    lista = router.handle(msg("3"))
    ativos = router.handle(msg("1"))
    primeiro_prompt = router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-recolha-81"))
    router.handle(msg("2"))
    confirmacao_primeiro = router.handle(msg("1"))
    segundo_prompt = router.handle(msg("1"))
    segundo_foto = router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-recolha-82"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    final = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Selecione o pedido para recolha" in lista
    assert "Selecione o ativo" in ativos
    assert "Contentor 81" in primeiro_prompt
    assert "Confirme a recolha" in confirmacao_primeiro
    assert "Ativo recolhido" in segundo_prompt
    assert "Contentor 81" not in segundo_prompt
    assert "Contentor 82" in segundo_prompt
    assert "Contentor 82" in segundo_foto
    assert "Recolha do pedido concluida" in final
    assert router.pop_pending_messages() == [MAIN_MENU]
    assert all(item.status_recolha == StatusRecolhaPedido.RECOLHIDO.value for item in pedido.contentores)
    fotos_recolha = (
        db_session.query(ContentorFoto)
        .filter(ContentorFoto.tipo_foto == TipoFoto.RECOLHA.value)
        .order_by(ContentorFoto.url_midia)
        .all()
    )
    assert [foto.url_midia for foto in fotos_recolha] == ["foto-recolha-81", "foto-recolha-82"]


def test_recolha_v24_termino_parcial_mantem_restante_pendente_e_menu_separado(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Parcial",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "91", "fotos": ["foto-91"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "92", "fotos": ["foto-92"]},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    ativos = router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-recolha-91"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    restantes = router.handle(msg("1"))
    response = router.handle(msg("Terminar recolhas deste cliente"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    conversa = db_session.query(ConversaWhatsApp).one()
    assert "Terminar recolhas deste cliente" in ativos
    assert "Contentor 92" in restantes
    assert "encerradas" in response
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert pedido.contentores[1].status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_recolha_v24_cancelamento_durante_fotos_nao_deixa_foto_orfa(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Cancela Recolha",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "93", "fotos": ["foto-93"]}],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-recolha-cancelada"))
    response = router.handle(msg("cancelar"))

    db_session.refresh(pedido.contentores[0])
    conversa = db_session.query(ConversaWhatsApp).one()
    assert "cancelada" in response.lower()
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert router.pop_pending_messages() == [MAIN_MENU]


@pytest.mark.parametrize(
    "stage",
    ["pedido", "ativo", "avaria", "relato", "confirmacao"],
)
def test_recolha_v24_cancelamento_limpa_contexto_sem_marcar_ativo(db_session, monkeypatch, stage):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente=f"Cliente Cancela {stage}",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "94", "fotos": ["foto-94"]}],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    if stage != "pedido":
        router.handle(msg("1"))
    if stage not in {"pedido", "ativo"}:
        router.handle(msg("1"))
        router.handle(msg(kind="image", media=f"foto-{stage}"))
        router.handle(msg("2"))
    if stage == "relato":
        router.handle(msg("2"))
    if stage == "confirmacao":
        router.handle(msg("1"))
    response = router.handle(msg("cancelar"))

    db_session.refresh(pedido.contentores[0])
    conversa = db_session.query(ConversaWhatsApp).one()
    assert "cancelada" in response.lower()
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_recolha_v24_avaria_exige_relato_curto_rejeita_e_valido_cria_pendencia(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Avaria",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "95", "fotos": ["foto-95"]}],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-avaria"))
    router.handle(msg("2"))
    router.handle(msg("2"))
    curto = router.handle(msg("  curto  "))
    confirmacao = router.handle(msg("  porta lateral amassada  "))
    final = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "pelo menos 10" in curto
    assert "Relato: porta lateral amassada" in confirmacao
    assert "pendencia de avaria" in final
    assert pedido.contentores[0].contentor_avariado is True
    assert pedido.contentores[0].relato_avaria == "porta lateral amassada"
    assert pedido.contentores[0].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_recolha_v24_sem_avaria_nao_cria_pendencia(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Sem Avaria",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "96", "fotos": ["foto-96"]}],
    )
    router = WhatsappRouterAgent(db_session)

    for item in ["3", "1", "1"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-sem-avaria"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].contentor_avariado is False
    assert pedido.contentores[0].relato_avaria is None
    assert pedido.contentores[0].status_resolucao_avaria == StatusResolucaoPedido.NAO_APLICA.value


def test_recolha_v24_conflito_concorrente_recarrega_restantes_sem_duplicar_fotos(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Conflito Recolha",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "97", "fotos": ["foto-97"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "98", "fotos": ["foto-98"]},
        ],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-nao-deve-salvar"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    service.confirmar_recolha(pedido.contentores[0].id, "outro", False, None, ["foto-outro"])
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "atualizado por outro operador" in response
    assert "Contentor 98" in response
    assert "Contentor 97" not in response
    assert pedido.contentores[1].status_recolha == StatusRecolhaPedido.PENDENTE.value
    fotos = db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).all()
    assert [foto.url_midia for foto in fotos] == ["foto-outro"]


def test_recolha_v24_rejeita_id_de_ativo_de_outro_pedido(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Um Ativo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    outro = service.criar(
        nome_cliente="Cliente Outro Ativo",
        telefone_cliente="351912345679",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    for item, numero in [(pedido.contentores[0], "99"), (outro.contentores[0], "100")]:
        service.confirmar_entrega_lote(
            item.pedido_id,
            "motorista",
            38.7,
            -9.1,
            None,
            [{"contentor_id": item.id, "numero_adesivo": numero, "fotos": [f"foto-{numero}"]}],
        )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    router.handle(msg("1"))
    response = router.handle(msg(str(outro.contentores[0].id)))

    assert "Selecione um ativo da lista" in response
    db_session.refresh(outro.contentores[0])
    assert outro.contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value


def test_recolha_v24_mais_de_tres_ativos_usa_lista_interativa(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Lista Ativos",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="400",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto", "Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": item.id, "numero_adesivo": str(110 + index), "fotos": [f"foto-{index}"]}
            for index, item in enumerate(pedido.contentores)
        ],
    )
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("3"))
    ativos = router.handle(msg("1"))
    result = send_whatsapp_message("351900009900", ativos, force_mock=True)

    assert result["interactive_type"] == "list"
    assert [row["id"] for row in result["list_rows"]] == ["option_1", "option_2", "option_3", "option_4", "option_5"]


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
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "1", "fotos": ["foto-1"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "2", "fotos": ["foto-2"]},
        ],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    service.confirmar_recolha(pedido.contentores[1].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo"))
    router.handle(msg("2"))
    prompt = router.handle(msg("2"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.contexto_json["residuos_disponiveis"] == ["Entulho Limpo", "Entulho Misto"]
    assert "Entulho Limpo" in prompt
    router.handle(msg("1"))
    router.handle(msg("Havia lixo domestico misturado"))
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "processado no vazadouro" in response
    assert pedido.contentores[0].residuo_efetivo_vazadouro == "Entulho Limpo"


def test_despejo_v24_loop_confirma_um_ativo_e_mostra_restantes_sem_novo_cliente(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Loop Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "201", "fotos": ["foto-201"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "202", "fotos": ["foto-202"]},
        ],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    service.confirmar_recolha(pedido.contentores[1].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    lista = router.handle(msg("4"))
    ativos = router.handle(msg("1"))
    foto_prompt = router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-201-a"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-201-a"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-201-b"))
    router.handle(msg("2"))
    confirmacao = router.handle(msg("1"))
    restantes = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Selecione o pedido para despejo" in lista
    assert "Selecione o ativo descarregado" in ativos
    assert "Contentor 201" in foto_prompt
    assert "Fotos: 2" in confirmacao
    assert "Contentor 202" in restantes
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert pedido.contentores[1].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert pedido.contentores[0].despejo_feito_por == "351900009900"
    assert pedido.contentores[0].despejo_data_hora is not None
    fotos = (
        db_session.query(ContentorFoto)
        .filter_by(pedido_contentor_id=pedido.contentores[0].id, tipo_foto=TipoFoto.DESPEJO.value)
        .order_by(ContentorFoto.url_midia)
        .all()
    )
    assert [foto.url_midia for foto in fotos] == ["foto-despejo-201-a", "foto-despejo-201-b"]
    assert router.pop_pending_messages() == []


def test_despejo_v24_ultimo_ativo_encerra_fluxo_e_menu_separado(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Ultimo Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "203", "fotos": ["foto-203"]}],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    for item in ["4", "1", "1"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-despejo-203"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    response = router.handle(msg("1"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "Despejo do pedido concluido" in response
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_despejo_v24_termino_parcial_preserva_ativos_em_andamento(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Parcial Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "204", "fotos": ["foto-204"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "205", "fotos": ["foto-205"]},
        ],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    service.confirmar_recolha(pedido.contentores[1].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    router.handle(msg("1"))
    response = router.handle(msg("Terminar despejos deste cliente"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "encerrados" in response
    assert all(item.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value for item in pedido.contentores)
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_despejo_v24_cancelamento_durante_fotos_nao_deixa_foto_orfa(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Cancela Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "206", "fotos": ["foto-206"]}],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    router.handle(msg("1"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-cancelada"))
    response = router.handle(msg("cancelar"))

    db_session.refresh(pedido.contentores[0])
    conversa = db_session.query(ConversaWhatsApp).one()
    assert "cancelada" in response.lower()
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).count() == 0
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_despejo_v24_estado_legado_nao_persiste_sem_confirmacao_final(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Estado Legado",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "2061", "fotos": ["foto-2061"]}],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    db_session.add(
        ConversaWhatsApp(
            telefone="351900009900",
            estado_atual="v24_despejo_conformidade",
            contexto_json={
                "contentor_id": pedido.contentores[0].id,
                "residuo_assumido": "Entulho Limpo",
            },
        )
    )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "Confirme o despejo deste ativo" in response
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).count() == 0


def test_despejo_v24_divergencia_valida_cria_pendencia_e_rejeita_relato_curto(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Divergencia Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "207", "fotos": ["foto-207"]}],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    for item in ["4", "1", "1"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-divergencia"))
    router.handle(msg("2"))
    residuos = router.handle(msg("2"))
    router.handle(msg("2"))
    curto = router.handle(msg("curto"))
    confirmacao = router.handle(msg("  havia plastico misturado  "))
    final = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "Entulho Misto" in residuos
    assert "pelo menos 10" in curto
    assert "Relato: havia plastico misturado" in confirmacao
    assert "concluido" in final
    assert pedido.contentores[0].residuo_efetivo_vazadouro == "Entulho Misto"
    assert pedido.contentores[0].carga_errada is True
    assert pedido.contentores[0].relato_carga == "havia plastico misturado"
    assert pedido.contentores[0].status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value


def test_despejo_v24_conflito_concorrente_recarrega_restantes_sem_duplicar_fotos(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Conflito Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "208", "fotos": ["foto-208"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "209", "fotos": ["foto-209"]},
        ],
    )
    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None)
    service.confirmar_recolha(pedido.contentores[1].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    for item in ["4", "1", "1"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-nao-salvar"))
    router.handle(msg("2"))
    router.handle(msg("1"))
    service.confirmar_despejo(
        pedido.contentores[0].id,
        "Entulho Limpo",
        operador="outro",
        pedido_id=pedido.id,
        fotos=["foto-outro"],
    )
    response = router.handle(msg("1"))

    assert "atualizado por outro operador" in response
    assert "Contentor 209" in response
    assert "Contentor 208" not in response
    fotos = db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).all()
    assert [foto.url_midia for foto in fotos] == ["foto-outro"]


def test_despejo_v24_rejeita_ativo_de_outro_pedido_e_mais_de_tres_usa_lista(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Lista Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="400",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto", "Entulho Limpo", "Entulho Misto"],
    )
    outro = service.criar(
        nome_cliente="Cliente Outro Despejo",
        telefone_cliente="351912345679",
        data_planejada=datetime.now(timezone.utc),
        valor_global="100",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": item.id, "numero_adesivo": str(301 + index), "fotos": [f"foto-{index}"]}
            for index, item in enumerate(pedido.contentores)
        ],
    )
    service.confirmar_entrega_lote(
        outro.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": outro.contentores[0].id, "numero_adesivo": "399", "fotos": ["foto-outro"]}],
    )
    for item in [*pedido.contentores, outro.contentores[0]]:
        service.confirmar_recolha(item.id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    ativos = router.handle(msg("1"))
    result = send_whatsapp_message("351900009900", ativos, force_mock=True)
    response = router.handle(msg(str(outro.contentores[0].id)))

    assert result["interactive_type"] == "list"
    assert "Selecione um ativo da lista" in response
    db_session.refresh(outro.contentores[0])
    assert outro.contentores[0].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value


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
    pedido_misto = criar_pedido_legado_misto(
        db_session,
        nome="Cliente Misto",
        telefone="351900000333",
        data_planejada=now,
        valor="100",
        pago=False,
        carrinha_horario="15:00",
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
    assert "Carrinha | Cliente Carrinha Painel: horario 14:00 | Com Pessoal" in response
    assert "https://www.google.com/maps?q=38.7,-9.1" in response
    assert "Receita de Contentores ja paga: 100,00 €" in response
    assert "Receita de Contentores pendente: 0,00 €" in response
    assert "Receita de Carrinhas ja paga: 80,00 €" in response
    assert "Receita de Carrinhas pendente: 0,00 €" in response
    assert "Faturado Global: 180,00 €" in response
    assert "A receber Global: 100,00 €" in response
    assert "Total projetado do mes: 280,00 €" in response
    assert "Menu principal" not in response
    assert router.pop_pending_messages() == [MAIN_MENU]


def test_resumo_v32_funcionario_oculta_comercial_financeiro_pagamentos_e_carga(db_session):
    funcionario = "351900010002"
    operador(db_session, funcionario, PerfilOperador.FUNCIONARIO)
    service = PedidoService(db_session)
    now = datetime.now(timezone.utc)
    pedido = criar_pedido_legado_misto(
        db_session,
        nome="Cliente Funcionario",
        telefone="351912345678",
        data_planejada=now,
        valor="300",
        pago=False,
        carrinha_horario="16:30",
    )
    pedido.contentores[1].precisa_mao_de_obra = True
    db_session.commit()
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
    assert "Carrinha | Cliente Funcionario: horario 16:30 | Com Pessoal" in response
    assert "Menu principal" not in response
    pending = router.pop_pending_messages()
    assert pending == [
        "Olá, sou o Robô de Gestão de Contentores da OLT Gestão de Resíduos & Demolições. O que vamos fazer agora?\n\n"
        "1. Confirmar entrega de contentor\n"
        "2. Confirmar recolha de contentor\n"
        "3. Confirmar Despejo no Vazadouro"
    ]


def test_resumo_v33_agrupa_pedido_sem_duplicar_ativos_fotos_ou_pendencias(db_session):
    gestor = "351900010003"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    service = PedidoService(db_session)
    now = datetime.now(timezone.utc)
    pedido = service.criar(
        nome_cliente="Cliente Sem Duplicar",
        telefone_cliente="351912345678",
        data_planejada=now,
        valor_global="240",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto", "Madeira", "Plastico"],
    )
    entregar_pedido(pedido, db_session, now - timedelta(days=5), ["44", "45", "45", None])
    pedido.contentores[0].entrega_latitude = 0
    pedido.contentores[0].entrega_longitude = 0
    pedido.contentores[1].contentor_avariado = True
    pedido.contentores[1].relato_avaria = "Tampa partida na obra"
    pedido.contentores[1].status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    for item in pedido.contentores[:2]:
        db_session.add(
            ContentorFoto(
                pedido_contentor_id=item.id,
                tipo_foto=TipoFoto.ENTREGA.value,
                url_midia=f"foto-{item.id}",
            )
        )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=gestor))

    assert response.count("- Contentores | Cliente Sem Duplicar") == 1
    assert "- Contentores | Cliente Sem Duplicar: 44, 45, Ativo" in response
    assert "foto-" not in response
    assert "maps?q=0,0" not in response
    assert "Avarias em equipamentos" in response
    assert "resolver avaria" in response


def test_resumo_v33_financeiro_usa_criado_em_e_misto_so_no_global(db_session):
    gestor = "351900010004"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    service = PedidoService(db_session)
    now = datetime.now(timezone.utc)
    fora_do_mes = now - timedelta(days=40)
    pedido_contentor = service.criar(
        nome_cliente="Cliente Mes Contentor",
        telefone_cliente="351912345670",
        data_planejada=fora_do_mes,
        valor_global="1000.50",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
    )
    pedido_carrinha = service.criar(
        nome_cliente="Cliente Mes Carrinha",
        telefone_cliente="351912345671",
        data_planejada=fora_do_mes,
        valor_global="200",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[{"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "09:00"}],
    )
    pedido_misto = criar_pedido_legado_misto(
        db_session,
        nome="Cliente Mes Misto",
        telefone="351912345672",
        data_planejada=fora_do_mes,
        valor="300",
        pago=False,
    )
    pedido_antigo = service.criar(
        nome_cliente="Cliente Mes Antigo",
        telefone_cliente="351912345673",
        data_planejada=now,
        valor_global="999",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    pedido_antigo.criado_em = fora_do_mes
    db_session.commit()
    for pedido in (pedido_contentor, pedido_carrinha, pedido_misto, pedido_antigo):
        entregar_pedido(pedido, db_session, now - timedelta(days=5), ["51", "52"])

    response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=gestor))

    assert "Receita de Contentores ja paga: 1.000,50 €" in response
    assert "Receita de Contentores pendente: 0,00 €" in response
    assert "Receita de Carrinhas ja paga: 0,00 €" in response
    assert "Receita de Carrinhas pendente: 200,00 €" in response
    assert "Faturado Global: 1.000,50 €" in response
    assert "A receber Global: 500,00 €" in response
    assert "Total projetado do mes: 1.500,50 €" in response
