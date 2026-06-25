from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, ContentorFoto, StatusEntrega
from app.models.contentor import Contentor, StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.services.seed_service import SeedService


def text_message(texto: str, telefone: str = "351900009000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=telefone, tipo="text", texto=texto, message_id="m1")


def image_message(media_id: str = "media-entrega-1", telefone: str = "351900009000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="image",
        media_id=media_id,
        mime_type="image/jpeg",
        message_id=media_id,
    )


def location_message(latitude: float, longitude: float, telefone: str = "351900009000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="location",
        latitude=latitude,
        longitude=longitude,
        message_id="loc1",
    )


def liberar_operadores(monkeypatch):
    for env_name in (
        "WHATSAPP_OWNER_" + "PHONE",
        "AUTHORIZED_OPERATOR_" + "PHONE",
        "AUTHORIZED_OPERATOR_" + "PHONES",
        "OWNER_" + "WHATSAPP",
    ):
        monkeypatch.setenv(env_name, "")
    get_settings.cache_clear()


def avancar_ate_valor(router: WhatsappRouterAgent, telefone: str = "351900009000"):
    router.handle(text_message("novo", telefone=telefone))
    router.handle(text_message("Cliente Pedido", telefone=telefone))
    router.handle(text_message("+351 912 345 678", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    return router.handle(text_message("2", telefone=telefone))


def test_cadastro_pedido_atendente_salva_entrega_pendente_sem_foto_ou_gps_real(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    states = []
    responses = []
    telefone = "351900009000"

    responses.append(router.handle(text_message("novo", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("Cliente Pedido", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("+351 912 345 678", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("1", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("2", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("150,50", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("2", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("2", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("Rua Direita, proximo ao numero 50", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("2", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)
    responses.append(router.handle(text_message("1", telefone=telefone)))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one().estado_atual)

    aluguer = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).one()

    assert states == [
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_confirmacao_data_entrega",
        "aguardando_tipo_residuo",
        "aguardando_valor",
        "aguardando_pago",
        "aguardando_tipo_endereco_pedido",
        "aguardando_endereco_pedido_texto",
        "aguardando_ponto_referencia_opcao",
        "aguardando_confirmacao_final",
        "idle",
    ]
    assert "Cadastro de pedido iniciado" in responses[0]
    assert "Pedido salvo com sucesso" in responses[-1]
    assert aluguer.nome_cliente == "Cliente Pedido"
    assert aluguer.telefone_cliente == "351912345678"
    assert aluguer.tipo_residuo == "Entulho Misto"
    assert aluguer.pago is False
    assert aluguer.forma_pagamento is None
    assert aluguer.status_entrega == StatusEntrega.PENDENTE.value
    assert aluguer.numero_contentor == "A definir"
    assert db_session.query(Contentor).filter_by(codigo="C01").one().status == StatusContentor.DISPONIVEL
    assert aluguer.pedido_feito_por == telefone
    assert aluguer.entrega_feita_por is None
    assert aluguer.pedido_endereco_tipo == "TEXTO"
    assert aluguer.pedido_endereco_texto == "Rua Direita, proximo ao numero 50"
    assert aluguer.pedido_ponto_referencia is None
    assert aluguer.foto_entrega_path is None
    assert aluguer.latitude is None
    assert aluguer.longitude is None
    assert "pedido_criado" in {evento.tipo for evento in aluguer.eventos}
    assert "entrega" not in {evento.tipo for evento in aluguer.eventos}


def test_entrega_vincula_contentor_somente_na_entrega(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)
    telefone = "351900009010"

    avancar_ate_valor(router, telefone=telefone)
    router.handle(text_message("120", telefone=telefone))
    router.handle(text_message("2", telefone=telefone))
    router.handle(text_message("2", telefone=telefone))
    router.handle(text_message("Rua da Entrega", telefone=telefone))
    router.handle(text_message("2", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    aluguer = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).one()

    assert aluguer.status_entrega == StatusEntrega.PENDENTE.value
    assert aluguer.numero_contentor == "A definir"
    assert db_session.query(Contentor).filter_by(codigo="C02").one().status == StatusContentor.DISPONIVEL

    start = router.handle(text_message("2", telefone=telefone))
    prompt_contentor = router.handle(text_message("1", telefone=telefone))
    foto_prompt = router.handle(text_message("C02", telefone=telefone))
    mais_foto = router.handle(image_message("media-entrega-1", telefone=telefone))
    gps_prompt = router.handle(text_message("2", telefone=telefone))
    referencia_prompt = router.handle(location_message(38.7223, -9.1393, telefone=telefone))
    referencia_texto_prompt = router.handle(text_message("1", telefone=telefone))
    pagamento_prompt = router.handle(text_message("Portao azul", telefone=telefone))
    forma_prompt = router.handle(text_message("1", telefone=telefone))
    final = router.handle(text_message("1", telefone=telefone))
    db_session.refresh(aluguer)

    assert "Entrega de contentor" in start
    assert "Informe o contentor entregue" in prompt_contentor
    assert "envie a foto" in foto_prompt
    assert "Deseja adicionar mais uma foto" in mais_foto
    assert "localizacao GPS exata" in gps_prompt
    assert "ponto de referencia" in referencia_prompt
    assert "Digite o ponto de referencia" in referencia_texto_prompt
    assert "pagamento no ato" in pagamento_prompt
    assert "forma de pagamento" in forma_prompt
    assert "Entrega do contentor registrada" in final
    assert aluguer.status_entrega == StatusEntrega.ENTREGUE.value
    assert aluguer.numero_contentor == "C02"
    assert aluguer.contentor.codigo == "C02"
    assert aluguer.entrega_feita_por == telefone
    assert aluguer.entrega_latitude == 38.7223
    assert aluguer.entrega_longitude == -9.1393
    assert aluguer.entrega_ponto_referencia == "Portao azul"
    assert aluguer.pago is True
    assert aluguer.forma_pagamento == "MBWay"
    assert db_session.query(ContentorFoto).filter_by(aluguer_id=aluguer.id).count() == 1
    assert db_session.query(Contentor).filter_by(codigo="C02").one().status == StatusContentor.ALUGADO


def test_cadastro_valor_rejeita_valor_absurdo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)
    telefone = "351900009001"

    avancar_ate_valor(router, telefone=telefone)
    invalid = router.handle(text_message("80000000000000000.00", telefone=telefone))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()

    assert "Valor invalido" in invalid
    assert conversa.estado_atual == "aguardando_valor"

    still_invalid = router.handle(text_message("1000", telefone=telefone))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()

    assert "Valor invalido" in still_invalid
    assert conversa.estado_atual == "aguardando_valor"

    valid = router.handle(text_message("999,99", telefone=telefone))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()

    assert "pedido ja esta pago" in valid
    assert conversa.estado_atual == "aguardando_pago"


def test_cadastro_endereco_aceita_link_google_maps_com_coordenadas(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)
    telefone = "351900009002"

    avancar_ate_valor(router, telefone=telefone)
    router.handle(text_message("120", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    router.handle(text_message("mbway", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    response = router.handle(text_message("https://www.google.com/maps?q=38.7223,-9.1393", telefone=telefone))

    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()
    assert "ponto de referencia" in response
    assert conversa.contexto_json["pedido_endereco_tipo"] == "LOCALIZACAO"
    assert conversa.contexto_json["pedido_latitude"] == 38.7223
    assert conversa.contexto_json["pedido_longitude"] == -9.1393
    assert conversa.estado_atual == "aguardando_ponto_referencia_opcao"


def test_cadastro_endereco_aceita_link_google_maps_com_3d_4d(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)
    telefone = "351900009003"

    avancar_ate_valor(router, telefone=telefone)
    router.handle(text_message("120", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    router.handle(text_message("mbway", telefone=telefone))
    router.handle(text_message("1", telefone=telefone))
    response = router.handle(
        text_message("https://www.google.com/maps/place/OLT/@38.7223,-9.1393,17z/data=!3d38.7223!4d-9.1393", telefone=telefone)
    )

    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()
    assert "ponto de referencia" in response
    assert conversa.contexto_json["pedido_latitude"] == 38.7223
    assert conversa.contexto_json["pedido_longitude"] == -9.1393


def test_menu_funcionario_mostra_entrega_e_recolha(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    telefone = "351900009004"
    db_session.add(
        Operador(
            telefone_whatsapp=telefone,
            nome_operador="Motorista",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=True,
        )
    )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("menu", telefone=telefone))

    assert "1. Confirmar entrega de contentor" in response
    assert "2. Confirmar recolha de contentor" in response
    assert "Novo pedido" not in response


def test_funcionario_opcao_1_abre_entrega_nao_cadastro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    telefone = "351900009005"
    db_session.add(
        Operador(
            telefone_whatsapp=telefone,
            nome_operador="Motorista",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=True,
        )
    )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("1", telefone=telefone))

    assert "Nao existem pedidos pendentes de entrega" in response
    assert "Cadastro de pedido iniciado" not in response
