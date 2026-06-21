from datetime import datetime, timedelta

from app.agents.aluguer_agent import AluguerAgent
from app.core.config import get_settings
from app.core.time import utcnow
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor
from app.models.conversa import ConversaWhatsApp
from app.services.seed_service import SeedService
from app.agents.whatsapp_router_agent import WhatsappRouterAgent


def text_message(texto: str, telefone: str = "351955000000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=telefone, tipo="text", texto=texto, message_id="m-text")


def image_message(telefone: str = "351955000000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="image",
        message_id="m-image",
        media_id="foto-contentor",
        mime_type="image/jpeg",
    )


def location_message(telefone: str = "351955000000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="location",
        message_id="m-location",
        latitude=38.7223,
        longitude=-9.1393,
    )


def liberar_operadores(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()


def setup_router(db_session, monkeypatch) -> WhatsappRouterAgent:
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    return WhatsappRouterAgent(db_session)


def advance_to_confirmation(router: WhatsappRouterAgent, telefone: str = "351955000000") -> str:
    router.handle(text_message("1", telefone))
    router.handle(text_message(" OLT-12 ", telefone))
    router.handle(image_message(telefone))
    router.handle(location_message(telefone))
    router.handle(text_message("Cliente Confirmacao", telefone))
    router.handle(text_message("912345678", telefone))
    router.handle(text_message("pular", telefone))
    router.handle(text_message("hoje", telefone))
    router.handle(text_message("2", telefone))
    router.handle(text_message("75€", telefone))
    router.handle(text_message("4", telefone))
    router.handle(text_message("Cheque", telefone))
    return router.handle(text_message("2", telefone))


def conversa(db_session, telefone: str = "351955000000") -> ConversaWhatsApp:
    return db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()


def test_menu_inicial_mostra_opcao_de_cadastrar_contentor(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    response = router.handle(text_message("ola"))

    assert "Cadastrar entrega de contentor" in response
    assert "4. Ver resumo" in response


def test_opcao_1_inicia_cadastro_e_texto_na_foto_nao_avanca(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    start = router.handle(text_message("1"))
    invalid = router.handle(text_message(""))
    photo_prompt = router.handle(text_message(" CNT-001 "))
    response = router.handle(text_message("texto qualquer"))

    assert start == "🚛 Qual o numero do contentor?"
    assert "Informe o numero do contentor" in invalid
    assert "foto do contentor" in photo_prompt
    assert conversa(db_session).contexto_json["numero_contentor"] == "CNT-001"
    assert "Ainda nao recebi a imagem" in response
    assert conversa(db_session).estado_atual == "aguardando_foto_entrega"


def test_imagem_avanca_e_texto_na_localizacao_nao_avanca(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("12"))
    image_response = router.handle(image_message())
    text_response = router.handle(text_message("Rua sem pin"))

    assert "localizacao GPS" in image_response
    assert "precisao do mapa" in text_response
    assert conversa(db_session).estado_atual == "aguardando_localizacao"


def test_localizacao_nome_telefone_e_email_validam_antes_de_avancar(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("C12"))
    router.handle(image_message())
    loc_response = router.handle(location_message())
    invalid_name = router.handle(text_message("Al"))
    valid_name = router.handle(text_message("Cliente Campo"))
    phone_response = router.handle(text_message("+351 912 345 678"))
    invalid_email = router.handle(text_message("email-invalido"))
    skip_email = router.handle(text_message("pular"))

    context = conversa(db_session).contexto_json
    assert "nome do cliente" in loc_response
    assert "entre 3 e 50 caracteres" in invalid_name
    assert "telefone do cliente" in valid_name
    assert "e-mail do cliente" in phone_response
    assert "E-mail invalido" in invalid_email
    assert "Confirma a entrega" in skip_email
    assert context["latitude"] == 38.7223
    assert context["longitude"] == -9.1393
    assert context["telefone_cliente"] == "351912345678"


def test_telefone_por_contato_nativo_e_aceito(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("C12"))
    router.handle(image_message())
    router.handle(location_message())
    router.handle(text_message("Cliente Contato"))
    response = router.handle(
        NormalizedWhatsAppMessage(
            telefone="351955000000",
            tipo="contacts",
            message_id="m-contact",
            contact_phone="+351 913 000 111",
        )
    )

    assert "e-mail do cliente" in response
    assert conversa(db_session).contexto_json["telefone_cliente"] == "351913000111"


def test_data_hoje_calcula_retirada_tipo_e_valor_sao_controlados(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("C12"))
    router.handle(image_message())
    router.handle(location_message())
    router.handle(text_message("Cliente Valores"))
    router.handle(text_message("912345678"))
    router.handle(text_message("pular"))
    tipo_prompt = router.handle(text_message("1"))
    invalid_tipo = router.handle(text_message("madeira"))
    valor_prompt = router.handle(text_message("1"))
    invalid_valor = router.handle(text_message("a combinar"))
    pagamento_prompt = router.handle(text_message("75€"))

    context = conversa(db_session).contexto_json
    assert "Entulho Limpo" in tipo_prompt
    assert "Opcao invalida" in invalid_tipo
    assert "Qual o valor do servico?" in valor_prompt
    assert "valor valido" in invalid_valor
    assert "MBWay" in pagamento_prompt
    assert context["valor"] == "75.00"
    assert context["data_retirada_prevista"] == (datetime.fromisoformat(context["data_entrega"]) + timedelta(days=5)).isoformat()


def test_confirmacao_corrige_numero_contentor_salva_e_limpa_sessao(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    confirmation = advance_to_confirmation(router)
    correction_menu = router.handle(text_message("2"))
    correction_prompt = router.handle(text_message("1"))
    confirmation_after_correction = router.handle(text_message(" CNT-999 "))
    save_response = router.handle(text_message("1"))
    aluguer = db_session.query(AluguerContentor).one()

    assert "✅ Confirmacao dos Dados" in confirmation
    assert "🚛 Contentor: OLT-12" in confirmation
    assert "Numero do contentor" in correction_menu
    assert "numero do contentor" in correction_prompt
    assert "🚛 Contentor: CNT-999" in confirmation_after_correction
    assert "Cadastro salvo com sucesso" in save_response
    assert aluguer.numero_contentor == "CNT-999"
    assert aluguer.nome_cliente == "Cliente Confirmacao"
    assert aluguer.valor == 75
    assert aluguer.forma_pagamento == "Cheque"
    assert aluguer.pago is False
    assert conversa(db_session).estado_atual == "idle"
    assert conversa(db_session).contexto_json == {}


def test_cancelar_tudo_na_confirmacao_limpa_sessao_sem_salvar(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    advance_to_confirmation(router)
    response = router.handle(text_message("3"))

    assert "Cadastro cancelado" in response
    assert db_session.query(AluguerContentor).count() == 0
    assert conversa(db_session).estado_atual == "idle"


def test_timeout_de_30_minutos_pergunta_se_deseja_continuar(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("C12"))
    router.handle(image_message())
    current = conversa(db_session)
    context = dict(current.contexto_json)
    context["updated_at"] = (utcnow() - timedelta(minutes=31)).isoformat()
    current.contexto_json = context
    db_session.commit()

    response = router.handle(location_message())

    assert "nao terminou o cadastro" in response
    assert "Sim, continuar" in response
    assert conversa(db_session).estado_atual == "cadastro_expirado"


def test_timeout_de_30_minutos_permite_recomecar(db_session, monkeypatch):
    router = setup_router(db_session, monkeypatch)

    router.handle(text_message("1"))
    router.handle(text_message("C12"))
    current = conversa(db_session)
    context = dict(current.contexto_json)
    context["updated_at"] = (utcnow() - timedelta(minutes=31)).isoformat()
    current.contexto_json = context
    db_session.commit()

    router.handle(image_message())
    response = router.handle(text_message("2"))
    current = conversa(db_session)

    assert response == "🚛 Qual o numero do contentor?"
    assert current.estado_atual == "aguardando_numero_contentor"
    assert "numero_contentor" not in current.contexto_json
