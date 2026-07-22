from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.operador_service as operador_service_module
from app.agents.pedido_v24_agent import PedidoV24Agent
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
    settings = SimpleNamespace(
        authorized_operator_phone="",
        authorized_operator_phones=",".join(
            (
                "351900000000", "351900000222", "351900000333", "351900009900",
                "351900009901", "351900010001", "351900010002", "351900010003",
                "351900010004", "351900010005", "351900010006", "351900010007",
                "351900010008", "351900010009",
            )
        ),
    )
    monkeypatch.setattr(operador_service_module, "get_settings", lambda: settings)


@pytest.mark.parametrize(
    ("residuo_contratado", "residuo_efetivo", "carga_errada", "esperado"),
    [
        pytest.param("Entulho Misto", "Entulho Limpo", False, True, id="residuos-diferentes"),
        pytest.param("Entulho Limpo", "Entulho Limpo", False, False, id="residuos-iguais"),
        pytest.param("Entulho Limpo", "Entulho Limpo", True, True, id="nao-corresponde"),
        pytest.param("Entulho Limpo", "Entulho Limpo", False, False, id="conformidade-confirmada"),
    ],
)
def test_despejo_v24_mensagem_e_persistencia_usam_mesma_regra_de_divergencia(
    residuo_contratado, residuo_efetivo, carga_errada, esperado
):
    agent = PedidoV24Agent.__new__(PedidoV24Agent)
    contentor = SimpleNamespace(
        id=10,
        numero_adesivo_contentor="501",
        tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
    )
    agent.db = MagicMock()
    agent.db.get.return_value = contentor
    agent.service = MagicMock()
    agent.service.get.return_value = SimpleNamespace(nome_cliente="Cliente Divergencia")
    agent.service.confirmar_despejo.return_value = contentor
    agent._despejo_pendentes_por_pedido = MagicMock(return_value=[])
    agent._idle = MagicMock(return_value="concluido")
    ctx = {
        "pedido_id": 1,
        "contentor_id": contentor.id,
        "fotos_despejo": ["foto-despejo"],
        "residuo_contratado": residuo_contratado,
        "residuo_efetivo": residuo_efetivo,
        "carga_errada": carga_errada,
        "relato_carga": "carga nao corresponde" if carga_errada else None,
    }

    confirmacao = agent._despejo_confirmacao_prompt(ctx)
    agent._confirmar_despejo_atual(SimpleNamespace(telefone="operador"), ctx)

    assert f"Divergencia: {'Sim' if esperado else 'Nao'}" in confirmacao
    assert agent.service.confirmar_despejo.call_args.args[2] is esperado


def avancar_cadastro_v24_ate_mao_obra(router, *, tipo="contentor", quantidade="1", phone="351900009900"):
    if tipo == "carrinha":
        steps = ["novo pedido", tipo, quantidade, "Cliente Hotfix", "351912345678", "Hoje", "09:30", "limpo"]
    else:
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
    liberar_operadores(monkeypatch)
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
        ("50", "Valor total: 50,00 €"),
        ("50.5", "Valor total: 50,50 €"),
        ("1250.75", "Valor total: 1.250,75 €"),
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
    assert "Hora da entrega: 14:00" in response


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
    assert "pessoal para carregamento" in prompt.lower()
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

    assert conversa.estado_atual == "v24_cadastro_valor"
    assert "valor" in response.lower()


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
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    for text in ["novo pedido", "carrinha", "1", "Cliente Carrinha", "351912345678", "Hoje"]:
        response = router.handle(msg(text))
    assert "HH:MM" in response
    invalid = router.handle(msg("99:99"))
    assert "Horário inválido" in invalid
    residuo_prompt = router.handle(msg("09:30"))
    assert "Entulho Limpo" in residuo_prompt
    mao_obra = router.handle(msg("1"))
    assert "pessoal para carregamento" in mao_obra.lower()
    for text in ["Sim, com pessoal", "250", "Não, pendente", "Rua da Carrinha", "Não"]:
        response = router.handle(msg(text))
    assert "Tipo da solicita" in response
    assert "Carrinha" in response
    assert "Quantidade de carrinhas: 1" in response
    assert "Pessoal para carregamento: Sim" in response
    assert "Hora da entrega: 09:30" in response
    assert "Valor total: 250,00 €" in response
    response = router.handle(msg("1"))

    pedido = db_session.query(Pedido).one()
    item = db_session.query(PedidoContentor).one()
    assert "Pedido #1 criado" in response
    assert pedido.precisa_mao_de_obra is True
    assert item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert item.horario_agendado == "09:30"
    assert item.precisa_mao_de_obra is False
    assert item.residuo_contratado == "Entulho Limpo"


def test_cadastro_v24_carrinha_ordem_quantidade_cliente_data_hora_residuo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)

    assert "tipo de solicita" in router.handle(msg("novo pedido")).lower()
    assert "carrinhas" in router.handle(msg("carrinha")).lower()
    assert "nome do cliente" in router.handle(msg("2")).lower()
    assert "telefone do cliente" in router.handle(msg("Cliente Ordem")).lower()
    assert "planejada a entrega" in router.handle(msg("351912345678")).lower()
    assert "HH:MM" in router.handle(msg("Hoje"))
    assert "Resíduo da carrinha 1/2" in router.handle(msg("08:45"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert conversa.contexto_json["quantidade"] == 2
    assert conversa.contexto_json["nome"] == "Cliente Ordem"


def test_cadastro_v24_corrigir_abre_lista_com_ids_e_edita_quantidade_sem_salvar(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)
    steps = [
        "novo pedido", "carrinha", "1", "Cliente Corrigir", "351912345678",
        "Hoje", "09:30", "limpo", "Sim, com pessoal", "250",
        "Sim, já está pago", "MBWay", "Rua", "Não",
    ]
    for text in steps:
        response = router.handle(msg(text))
    assert "Quantidade de carrinhas: 1" in response

    corrigir = router.handle(msg("Corrigir"))
    result = send_whatsapp_message("351900009900", corrigir, force_mock=True)
    assert result["interactive_type"] == "list"
    assert result["list_rows"][0]["id"] == "corrigir_pedido:quantidade"
    assert "corrigir_pedido:hora_entrega" in [row["id"] for row in result["list_rows"]]

    prompt = router.handle(msg("corrigir_pedido:quantidade"))
    assert "carrinhas" in prompt.lower()
    confirmacao = router.handle(msg("2"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_confirmacao"
    assert "Quantidade de carrinhas: 2" in confirmacao
    assert "Cliente: Cliente Corrigir" in confirmacao
    assert db_session.query(Pedido).count() == 0


def test_cadastro_v24_corrigir_pagamento_pendente_limpa_forma(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    router = WhatsappRouterAgent(db_session)
    steps = [
        "novo pedido", "carrinha", "1", "Cliente Pagamento", "351912345678",
        "Hoje", "09:30", "limpo", "Sim, com pessoal", "250",
        "Sim, já está pago", "MBWay", "Rua", "Não", "Corrigir",
        "corrigir_pedido:status_pagamento", "Não, pendente",
    ]
    for text in steps:
        response = router.handle(msg(text))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.estado_atual == "v24_cadastro_confirmacao"
    assert conversa.contexto_json["pago"] is False
    assert conversa.contexto_json["forma"] is None
    assert "Status do pagamento: Pendente" in response
    assert "Forma de pagamento:" not in response


def test_cadastro_v24_salva_mao_de_obra_false(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
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

    steps = ["novo pedido", "carrinha", "2", "Cliente Carrinhas", "351912345678", "Hoje", "09:30"]
    for text in steps:
        response = router.handle(msg(text))
    assert "Entulho Limpo" in response

    response = router.handle(msg("1"))
    assert "Entulho Limpo" in response
    response = router.handle(msg("2"))
    assert "pessoal para carregamento" in response.lower()
    for text in ["Sim, com pessoal", "500", "Não, pendente", "Rua Carrinhas", "Não"]:
        response = router.handle(msg(text))
    assert response.count("Pessoal para carregamento") == 1
    response = router.handle(msg("1"))

    assert "Pedido #1 criado" in response
    pedido = db_session.query(Pedido).one()
    itens = db_session.query(PedidoContentor).all()
    assert len(itens) == 2
    assert pedido.precisa_mao_de_obra is True
    assert all(item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value for item in itens)
    assert all(item.precisa_mao_de_obra is False for item in itens)


def test_entrega_v24_guarda_lote_no_contexto_ate_gps(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
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
    router.handle(msg("Sim"))
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
    liberar_operadores(monkeypatch)
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
    router.handle(msg("Sim"))
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
    assert "Contentor 1 de 2 registrado." in proximo
    assert "Selecione o pedido" not in proximo
    router.handle(msg("77"))
    router.handle(msg(kind="image", media="foto-carrinha-2"))
    router.handle(msg("2"))
    invalid_gps = router.handle(msg("https://www.google.com/maps?q=38.7,-9.1"))
    assert "localização nativa do WhatsApp" in invalid_gps
    router.handle(msg(kind="location", lat=38.7, lon=-9.1))
    router.handle(msg("entrega_referencia:sim"))
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


def test_entrega_v24_selecao_de_pedido_usa_lista_com_cliente_inteiro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    nome_cliente = "Cliente Entrega"
    pedido = PedidoService(db_session).criar(
        nome_cliente=nome_cliente,
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
    router = WhatsappRouterAgent(db_session)

    body = router.handle(msg("2"))
    sent = send_whatsapp_message("351900009900", body, force_mock=True)
    rows = sent["list_rows"]
    assert sent["interactive_type"] == "list"
    assert rows == [
        {
            "id": f"entrega_pedido:{pedido.id}",
            "title": nome_cliente,
            "description": "Quantidade: 2 equipamentos",
        }
    ]
    visible_text = rows[0]["title"] + " " + rows[0]["description"]
    assert "Contentor" not in visible_text
    assert pedido.data_planejada.strftime("%d/%m/%Y") not in visible_text
    assert "entrega_pedido" not in visible_text
    assert str(pedido.id) not in visible_text

    prompt = router.handle(msg(f"entrega_pedido:{pedido.id}"))
    assert "número do contentor" in prompt


def test_entrega_v24_nome_longo_usa_fallback_seguro_sem_id_interno(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    nome_cliente = "Cliente Empresarial Nome Muito Longo Para Entrega"
    pedido = PedidoService(db_session).criar(
        nome_cliente=nome_cliente,
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
    router = WhatsappRouterAgent(db_session)

    body = router.handle(msg("2"))
    sent = send_whatsapp_message("351900009900", body, force_mock=True)

    assert "list_rows" not in sent
    assert nome_cliente in sent["body"]
    assert "Quantidade: 2 equipamentos" in sent["body"]
    assert "entrega_pedido" not in sent["body"]
    assert f"ID: {pedido.id}" not in sent["body"]
    assert "Contentor x2" not in sent["body"]
    assert pedido.data_planejada.strftime("%d/%m/%Y") not in sent["body"]


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
    router.handle(msg("Sim"))
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
    router.handle(msg("Sim"))
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
    router.handle(msg("Não"))
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
    router.handle(msg("Sim"))
    router.handle(msg("Portao"))
    conversa_antes = db_session.query(ConversaWhatsApp).one()
    contexto_antes = dict(conversa_antes.contexto_json)
    pedido.contentores[0].status_entrega = StatusEntregaPedido.ENTREGUE.value
    db_session.commit()
    response = router.handle(msg("1"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert "lista de ativos pendentes mudou" in response.lower()
    assert conversa.estado_atual == "v24_entrega_confirmacao"
    assert conversa.contexto_json == contexto_antes
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
    liberar_operadores(monkeypatch)
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
    liberar_operadores(monkeypatch)
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
    prompt = router.handle(msg("1"))
    conversa = db_session.query(ConversaWhatsApp).one()

    assert conversa.contexto_json["residuos_disponiveis"] == ["Entulho Limpo", "Entulho Misto"]
    assert "Entulho Limpo" in prompt
    foto_prompt = router.handle(msg("despejo_residuo:limpo"))
    router.handle(msg(kind="image", media="foto-despejo"))
    router.handle(msg("2"))
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    assert "Contentor 1" in foto_prompt
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
    residuos = router.handle(msg("1"))
    foto_prompt = router.handle(msg("despejo_residuo:limpo"))
    router.handle(msg(kind="image", media="foto-despejo-201-a"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-201-a"))
    router.handle(msg("1"))
    router.handle(msg(kind="image", media="foto-despejo-201-b"))
    confirmacao = router.handle(msg("2"))
    restantes = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Selecione o pedido para despejo" in lista
    assert "Selecione o ativo descarregado" in ativos
    assert "Entulho Limpo" in residuos
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


def test_despejo_v24_prompts_usam_ids_interativos_estaveis(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido_misto = service.criar(
        nome_cliente="Cliente IDs Despejo",
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
        pedido_misto.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": pedido_misto.contentores[0].id, "numero_adesivo": "601", "fotos": ["foto-601"]},
            {"contentor_id": pedido_misto.contentores[1].id, "numero_adesivo": "602", "fotos": ["foto-602"]},
        ],
    )
    for contentor in pedido_misto.contentores:
        service.confirmar_recolha(contentor.id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    router.handle(msg("4"))
    router.handle(msg("1"))
    residuo_prompt = router.handle(msg("1"))
    residuo_message = send_whatsapp_message("351900009900", residuo_prompt, force_mock=True)

    assert residuo_message["buttons"] == [
        {"id": "despejo_residuo:limpo", "title": "Entulho Limpo"},
        {"id": "despejo_residuo:misto", "title": "Entulho Misto"},
    ]

    pedido_limpo = service.criar(
        nome_cliente="Cliente ID Conformidade",
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
        pedido_limpo.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido_limpo.contentores[0].id, "numero_adesivo": "603", "fotos": ["foto-603"]}],
    )
    service.confirmar_recolha(pedido_limpo.contentores[0].id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)
    outro_telefone = "351900009901"

    router.handle(msg("4", phone=outro_telefone))
    router.handle(msg("2", phone=outro_telefone))
    conformidade_prompt = router.handle(msg("1", phone=outro_telefone))
    conformidade_message = send_whatsapp_message("351900009900", conformidade_prompt, force_mock=True)

    assert conformidade_message["buttons"] == [
        {"id": "despejo_conformidade:sim", "title": "✅ Sim"},
        {"id": "despejo_conformidade:nao", "title": "🚨 Não"},
    ]


def test_despejo_v24_cota_esgotada_entre_escolha_e_confirmacao_nao_salva(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Cota Concorrente",
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
            {"contentor_id": pedido.contentores[0].id, "numero_adesivo": "611", "fotos": ["foto-611"]},
            {"contentor_id": pedido.contentores[1].id, "numero_adesivo": "612", "fotos": ["foto-612"]},
        ],
    )
    for contentor in pedido.contentores:
        service.confirmar_recolha(contentor.id, "motorista", False, None)
    router = WhatsappRouterAgent(db_session)

    for item in ["4", "1", "1", "despejo_residuo:limpo"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-cota-esgotada"))
    confirmacao = router.handle(msg("2"))
    service.confirmar_despejo(
        pedido.contentores[1].id,
        "Entulho Limpo",
        operador="outro",
        pedido_id=pedido.id,
        fotos=["foto-outro-cota"],
    )
    response = router.handle(msg("1"))

    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido.contentores[1])
    assert "Residuo efetivo: Entulho Limpo" in confirmacao
    assert "Não existe cota em aberto" in response
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert pedido.contentores[0].despejo_data_hora is None
    assert pedido.contentores[0].residuo_efetivo_vazadouro is None
    fotos = db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).all()
    assert [foto.url_midia for foto in fotos] == ["foto-outro-cota"]


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

    for item in ["4", "1", "1", "despejo_conformidade:sim"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-despejo-203"))
    router.handle(msg("2"))
    response = router.handle(msg("1"))

    conversa = db_session.query(ConversaWhatsApp).one()
    assert (
        "✅ Contentor 203 processado no vazadouro. Pedido do cliente Cliente Ultimo Despejo "
        "concluído. Nenhum contentor pendente."
    ) == response
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
    assert "Envie a foto do despejo" in response
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
    pergunta = router.handle(msg("despejo_conformidade:nao"))
    curto = router.handle(msg("curto"))
    final = router.handle(msg("  havia plastico misturado  "))

    db_session.refresh(pedido.contentores[0])
    assert "pelo menos 10" in pergunta
    assert "pelo menos 10" in curto
    assert "Divergência registrada" in final
    assert pedido.contentores[0].residuo_efetivo_vazadouro is None
    assert pedido.contentores[0].carga_errada is True
    assert pedido.contentores[0].relato_carga == "havia plastico misturado"
    assert pedido.contentores[0].status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert pedido.contentores[0].despejo_data_hora is None


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

    for item in ["4", "1", "1", "despejo_residuo:limpo"]:
        router.handle(msg(item))
    router.handle(msg(kind="image", media="foto-nao-salvar"))
    router.handle(msg("2"))
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
    entregar_pedido(pedido_contentores, db_session, now - timedelta(days=4), ["11", "12"])
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
    assert "*1. AÇÕES PARA HOJE*" in response
    assert "*2. AÇÕES AGENDADAS PARA OS PRÓXIMOS DIAS*" in response
    assert "*3. PENDÊNCIAS ATIVAS*" in response
    assert "*4. RESUMO FINANCEIRO DO MÊS*" in response
    assert "🔄 *Renovações:*" in response
    assert response.count("Cliente Agrupado") == 2
    assert "• Cliente Agrupado (2 un) • Valor: 100,00 €" in response
    assert "Entrar em contacto: https://wa.me/351912345678" in response
    assert "🚛 *FATURAMENTO CARRINHAS*" in response
    assert "Pago: 100,00 € | Pendente: 0,00 €" in response
    assert "Pago: 80,00 € | Pendente: 0,00 €" in response
    assert "Total faturado — caixa: 180,00 €" in response
    assert "Total a receber: 0,00 €" in response
    assert "Total projetado: 180,00 €" in response
    assert "[Abrir endereço]" not in response
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
    assert "Avarias em Equipamentos" in response
    assert "Equipamento Nº 0" in response
    assert "Porta lateral amassada" in response
    assert "https://wa.me/?text=resolver%20avaria%202" in response
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
    entregar_pedido(pedido, db_session, now - timedelta(days=6), ["44", "45", "45", None])
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

    assert response.count("Cliente Sem Duplicar") == 3
    assert "44" in response
    assert "45" in response
    assert "45, 45" not in response
    assert "foto-" not in response
    assert "maps?q=0,0" not in response
    assert "Avarias em Equipamentos" in response
    assert "https://wa.me/?text=resolver%20avaria%202" in response


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

    assert "📦 *FATURAMENTO CONTENTORES*" in response
    assert "• Pago: 1.000,50 € | Pendente: 0,00 €" in response
    assert "🚛 *FATURAMENTO CARRINHAS*" in response
    assert "• Pago: 0,00 € | Pendente: 200,00 €" in response
    assert "Total faturado — caixa: 1.000,50 €" in response
    assert "Total a receber: 200,00 €" in response
    assert "Total projetado: 1.200,50 €" in response


def test_resumo_v4_entregas_hoje_contentor_carrinha_endereco_e_horario_seguro(db_session):
    gestor = "351900010005"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    service = PedidoService(db_session)
    today = datetime.now(timezone.utc)
    pedido_contentor = service.criar(
        nome_cliente="Cliente Entrega Maps",
        telefone_cliente="351912345680",
        data_planejada=today,
        valor_global="120",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Maps",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto"],
        endereco_latitude=38.7,
        endereco_longitude=-9.1,
    )
    pedido_carrinha = service.criar(
        nome_cliente="Cliente Carrinha Antiga",
        telefone_cliente="351912345681",
        data_planejada=today,
        valor_global="90",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Textual",
        ponto_referencia=None,
        itens=[{"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "10:30"}],
    )
    pedido_carrinha.contentores[0].horario_agendado = None
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=gestor))

    assert "📦 *Entrega de Contentores:*" in response
    assert "• Cliente Entrega Maps (2 un)" in response
    assert "📍 Abrir endereço: https://www.google.com/maps?q=38.7,-9.1" in response
    assert "🚛 *Envio de Carrinhas:*" in response
    assert "• Cliente Carrinha Antiga (1 un)" in response
    assert "⏰ Horário: Horário não informado" in response
    assert "📍 Endereço: Rua Textual" in response
    assert "[Abrir endereço]" not in response
    assert "None" not in response


def test_resumo_v4_financeiro_nao_duplica_multiequipamento_nem_sum_distinct(db_session):
    gestor = "351900010006"
    funcionario = "351900010007"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    operador(db_session, funcionario, PerfilOperador.FUNCIONARIO)
    service = PedidoService(db_session)
    today = datetime.now(timezone.utc)
    service.criar(
        nome_cliente="Cliente Multi 450",
        telefone_cliente="351912345682",
        data_planejada=today,
        valor_global="450",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=["Entulho Limpo", "Entulho Misto", "Madeira"],
    )
    for suffix in ("A", "B"):
        service.criar(
            nome_cliente=f"Cliente Mesmo Valor {suffix}",
            telefone_cliente=f"35191234568{3 if suffix == 'A' else 4}",
            data_planejada=today,
            valor_global="100",
            pago=True,
            forma_pagamento="Dinheiro",
            pedido_feito_por="gestor",
            endereco_aproximado="Rua",
            ponto_referencia=None,
            residuos=["Entulho Limpo"],
        )
    service.criar(
        nome_cliente="Cliente Carrinha Pendente",
        telefone_cliente="351912345685",
        data_planejada=today,
        valor_global="80",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        itens=[{"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "09:00"}],
    )

    response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=gestor))
    funcionario_response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=funcionario))

    assert "• Pago: 650,00 € | Pendente: 0,00 €" in response
    assert "• Pago: 0,00 € | Pendente: 80,00 €" in response
    assert "Total faturado — caixa: 650,00 €" in response
    assert "Total a receber: 80,00 €" in response
    assert "Total projetado: 730,00 €" in response
    assert "RESUMO FINANCEIRO" not in funcionario_response
    assert "Total faturado" not in funcionario_response
    assert "Em dívida" not in funcionario_response


def test_resumo_v4_secoes_vazias_e_financeiro_zerado(db_session):
    gestor = "351900010008"
    funcionario = "351900010009"
    operador(db_session, gestor, PerfilOperador.GESTOR)
    operador(db_session, funcionario, PerfilOperador.FUNCIONARIO)

    gestor_response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=gestor))
    funcionario_response = WhatsappRouterAgent(db_session).handle(msg("resumo", phone=funcionario))

    assert "• Nenhuma ação para hoje." in gestor_response
    assert "• Nenhuma ação agendada." in gestor_response
    assert "• Nenhuma pendência ativa." in gestor_response
    assert "• Pago: 0,00 € | Pendente: 0,00 €" in gestor_response
    assert "Entrega de Contentores:" not in gestor_response
    assert "Envio de Carrinhas:" not in gestor_response
    assert "None" not in gestor_response
    assert "RESUMO FINANCEIRO" not in funcionario_response


def test_resumo_v4_cabecalho_usa_data_operacional_de_portugal(db_session, monkeypatch):
    import app.agents.whatsapp_router_agent as router_module

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 14, 0, 30, tzinfo=tz)

    monkeypatch.setattr(router_module, "datetime", FakeDateTime)

    response = WhatsappRouterAgent(db_session)._painel_v32(PerfilOperador.FUNCIONARIO)

    assert "📊 *PAINEL DE CONTROLE OPERACIONAL OLT*" in response
    assert "📅 Data: 14/07/2026" in response
