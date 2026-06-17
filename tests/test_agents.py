from datetime import timedelta

from app.agents.aluguer_agent import AluguerAgent
from app.agents.recolha_agent import RecolhaAgent
from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone
from app.integrations.whatsapp.client import send_text_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import StatusAluguer
from app.models.contentor import StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService
from app.services.seed_service import SeedService


def text_message(texto: str, telefone: str = "351900000000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=telefone, tipo="text", texto=texto, message_id="m1")


def test_router_chama_aluguer_agent_quando_mensagem_for_novo(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()

    response = WhatsappRouterAgent(db_session).handle(text_message("novo"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").first()

    assert "foto do contentor" in response
    assert conversa.estado_atual == AluguerAgent.START_STATE
    assert conversa.contexto_json["contentor_codigo"] == "C01"


def test_aluguer_agent_avanca_estado_da_conversa(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    conversa = ConversaWhatsApp(telefone="351900000010", estado_atual="idle", contexto_json={})
    db_session.add(conversa)
    db_session.commit()
    db_session.refresh(conversa)

    agent = AluguerAgent(db_session)
    agent.start(conversa)
    response = agent.handle(
        conversa,
        NormalizedWhatsAppMessage(
            telefone=conversa.telefone,
            tipo="image",
            message_id="m2",
            media_id="media-1",
            mime_type="image/jpeg",
        ),
    )

    assert response == "Agora envie a localizacao."
    assert conversa.estado_atual == "aguardando_localizacao"
    assert conversa.contexto_json["foto_entrega_path"] == "whatsapp://media/media-1"


def test_recolha_agent_marca_aluguer_como_aguardando_recolha(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Recolha",
        telefone_cliente="351900000020",
        valor="110",
        forma_pagamento="dinheiro",
        pago=True,
    )

    response = RecolhaAgent(db_session).marcar_recolha(aluguer.id)

    assert "aguardando recolha" in response
    assert aluguer.status == StatusAluguer.AGUARDANDO_RECOLHA


def test_fluxo_completo_de_novo_aluguer(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    states = []
    responses = []

    responses.append(router.handle(text_message("novo")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(
        router.handle(
            NormalizedWhatsAppMessage(
                telefone="351900000000",
                tipo="image",
                message_id="m2",
                media_id="foto-123",
                mime_type="image/jpeg",
            )
        )
    )
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(
        router.handle(
            NormalizedWhatsAppMessage(
                telefone="351900000000",
                tipo="location",
                message_id="m3",
                latitude=38.7223,
                longitude=-9.1393,
            )
        )
    )
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("Cliente Final")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("351911111111")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("150,50")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("sim")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("mbway")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)

    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()
    aluguer = AluguerService(db_session)._get_or_raise(conversa.contexto_json["aluguer_id"])

    assert states == [
        "aguardando_foto_entrega",
        "aguardando_localizacao",
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_valor",
        "aguardando_pago",
        "aguardando_forma_pagamento",
        "confirmado",
    ]
    assert responses[-1].startswith("Aluguer #")
    assert conversa.estado_atual == "confirmado"
    assert aluguer.nome_cliente == "Cliente Final"
    assert aluguer.telefone_cliente == "351911111111"
    assert aluguer.foto_entrega_path == "whatsapp://media/foto-123"
    assert aluguer.latitude == 38.7223
    assert aluguer.longitude == -9.1393
    assert aluguer.status == StatusAluguer.ATIVO
    assert aluguer.contentor.codigo == "C01"
    assert aluguer.contentor.status == StatusContentor.ALUGADO
    assert aluguer.data_vencimento == aluguer.data_entrega + timedelta(days=5)
    assert {evento.tipo for evento in aluguer.eventos} == {"entrega", "pagamento_informado", "criado"}


def test_whatsapp_client_mock_retorna_mensagem_enviada(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    result = send_text_message("351900000000", "Mensagem de teste")

    assert result == {"to": "351900000000", "body": "Mensagem de teste", "status": "mocked"}


def test_normalizacao_de_telefone():
    assert normalize_phone("+55 (61) 98266-551") == "556198266551"
    assert normalize_phone(" 351-912 345 678 ") == "351912345678"
    assert normalize_phone(None) == ""


def test_autorizacao_com_authorized_operator_phone(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "+55 (61) 98266-551")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()

    assert WhatsappRouterAgent(db_session)._is_authorized("556198266551") is True


def test_autorizacao_com_authorized_operator_phones(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "+55 (61) 98266-551, 351-912345678")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()

    router = WhatsappRouterAgent(db_session)

    assert router._is_authorized("556198266551") is True
    assert router._is_authorized("+351 912 345 678") is True


def test_numero_nao_autorizado(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "556198266551")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()

    response = WhatsappRouterAgent(db_session).handle(text_message("novo", telefone="556100000000"))

    assert response == "Telefone nao autorizado para iniciar alugueres. Contacte o administrador do sistema."


def test_mensagem_novo_de_operador_autorizado_inicia_fluxo(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "+55 (61) 98266-551")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()

    response = WhatsappRouterAgent(db_session).handle(text_message("Novo", telefone="556198266551"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="556198266551").one()

    assert "foto do contentor" in response
    assert conversa.estado_atual == "aguardando_foto_entrega"
