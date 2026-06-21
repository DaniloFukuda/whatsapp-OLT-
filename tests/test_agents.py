from datetime import datetime, timedelta

from app.agents.aluguer_agent import AluguerAgent
from app.agents.recolha_agent import RecolhaAgent
from app.agents.whatsapp_router_agent import CANCELLED_MENU_MESSAGE, WhatsappRouterAgent
from app.core.config import get_settings
from app.core.phone import normalize_phone, normalize_portugal_phone, whatsapp_link
from app.core.time import utcnow
from app.integrations.whatsapp.client import send_text_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, StatusAluguer
from app.models.contentor import Contentor, StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.services.aluguer_service import AluguerService
from app.services.seed_service import SeedService


def text_message(texto: str, telefone: str = "351900000000") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=telefone, tipo="text", texto=texto, message_id="m1")


def liberar_operadores(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()


def preparar_operacao_demo(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    now = utcnow()
    aluguer_amanha = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Amanhã",
        telefone_cliente="351900000101",
        valor="100",
        forma_pagamento="mbway",
        pago=True,
    )
    aluguer_atrasado = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Atrasado",
        telefone_cliente="351900000102",
        valor="120",
        forma_pagamento="dinheiro",
        pago=False,
    )
    aluguer_regular = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Regular",
        telefone_cliente="351900000103",
        valor="130",
        forma_pagamento="transferencia",
        pago=True,
    )
    aluguer_amanha.data_vencimento = now + timedelta(days=1)
    aluguer_atrasado.data_vencimento = now - timedelta(days=1)
    aluguer_regular.data_vencimento = now + timedelta(days=4)

    contentor_recolha = db_session.query(Contentor).filter_by(codigo="C04").one()
    contentor_recolha.status = StatusContentor.AGUARDANDO_RECOLHA
    contentor_manutencao = db_session.query(Contentor).filter_by(codigo="C05").one()
    contentor_manutencao.status = StatusContentor.MANUTENCAO
    db_session.commit()
    return aluguer_amanha, aluguer_atrasado, aluguer_regular


def preparar_aluguer_gestao(db_session, nome: str, telefone: str, valor: str = "100"):
    return AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente=nome,
        telefone_cliente=telefone,
        valor=valor,
        forma_pagamento="mbway",
        pago=False,
        tipo_residuo="Entulho limpo",
        operador_telefone="351900000000",
        data_entrega=datetime(2026, 1, 1, 10, 0, 0),
    )


def preparar_resumo_paulo(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    now = utcnow()
    hoje = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Retirada Hoje",
        telefone_cliente="912345678",
        valor="100",
        forma_pagamento="mbway",
        pago=True,
        latitude=38.7223,
        longitude=-9.1393,
        data_entrega=now,
    )
    amanha = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Retirada Amanha",
        telefone_cliente="351913333333",
        valor="200",
        forma_pagamento="transferencia",
        pago=False,
        data_entrega=now,
    )
    hoje.data_vencimento = now
    amanha.data_vencimento = now + timedelta(days=1)
    db_session.commit()
    return hoje, amanha


def avancar_cadastro_ate_confirmacao_data(router, db_session, telefone: str = "351900001000"):
    router.handle(text_message("novo", telefone=telefone))
    router.handle(text_message("C12", telefone=telefone))
    router.handle(
        NormalizedWhatsAppMessage(
            telefone=telefone,
            tipo="image",
            message_id="foto-opcoes",
            media_id="foto-opcoes",
            mime_type="image/jpeg",
        )
    )
    router.handle(
        NormalizedWhatsAppMessage(
            telefone=telefone,
            tipo="location",
            message_id="loc-opcoes",
            latitude=38.7223,
            longitude=-9.1393,
        )
    )
    router.handle(text_message("Cliente Opcoes", telefone=telefone))
    router.handle(text_message("912345678", telefone=telefone))
    return router.handle(text_message("pular", telefone=telefone))


def avancar_cadastro_ate_tipo_residuo(router, db_session, telefone: str = "351900001100"):
    prompt_data = avancar_cadastro_ate_confirmacao_data(router, db_session, telefone=telefone)
    prompt_tipo = router.handle(text_message("1", telefone=telefone))
    return prompt_data, prompt_tipo


def avancar_cadastro_ate_pagamento(router, db_session, telefone: str = "351900001200", tipo: str = "1"):
    prompt_data, prompt_tipo = avancar_cadastro_ate_tipo_residuo(router, db_session, telefone=telefone)
    router.handle(text_message(tipo, telefone=telefone))
    router.handle(text_message("150", telefone=telefone))
    prompt_pago = router.handle(text_message("mbway", telefone=telefone))
    return prompt_data, prompt_tipo, prompt_pago


def test_router_chama_aluguer_agent_quando_mensagem_for_novo(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()

    response = WhatsappRouterAgent(db_session).handle(text_message("novo"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").first()

    assert "numero do contentor" in response
    assert conversa.estado_atual == AluguerAgent.START_STATE
    assert conversa.contexto_json["contentor_codigo"] == "C01"


def test_comandos_de_inicio_disparam_cadastro(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()

    for index, command in enumerate(["iniciar", "cadastrar", "começar", "comecar", "novo"], start=1):
        SeedService(db_session).seed_contentores_iniciais()
        telefone = f"35190000010{index}"
        response = WhatsappRouterAgent(db_session).handle(text_message(command, telefone=telefone))
        conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=telefone).one()

        assert "numero do contentor" in response
        assert conversa.estado_atual == AluguerAgent.START_STATE


def test_aluguer_agent_avanca_estado_da_conversa(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    conversa = ConversaWhatsApp(telefone="351900000010", estado_atual="idle", contexto_json={})
    db_session.add(conversa)
    db_session.commit()
    db_session.refresh(conversa)

    agent = AluguerAgent(db_session)
    agent.start(conversa)
    agent.handle(conversa, text_message("C12", telefone=conversa.telefone))
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

    assert "localizacao GPS" in response
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
    responses.append(router.handle(text_message("OLT-12")))
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
    responses.append(router.handle(text_message("+351 911 111 111")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("pular")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("sim")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("Entulho misto")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("150,50")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("mbway")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("sim")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)
    responses.append(router.handle(text_message("1")))
    states.append(db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one().estado_atual)

    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()
    aluguer = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).one()

    assert states == [
        "aguardando_numero_contentor",
        "aguardando_foto_entrega",
        "aguardando_localizacao",
        "aguardando_nome_cliente",
        "aguardando_telefone_cliente",
        "aguardando_email_cliente",
        "aguardando_confirmacao_data_entrega",
        "aguardando_tipo_residuo",
        "aguardando_valor",
        "aguardando_forma_pagamento",
        "aguardando_pago",
        "aguardando_confirmacao_final",
        "idle",
    ]
    assert "ID/referencia" in responses[-1]
    assert "WhatsApp cliente: https://wa.me/351911111111" in responses[-1]
    assert "Tipo residuo: Entulho Misto" in responses[-1]
    assert "Status pagamento: Pago" in responses[-1]
    assert "Operador: 351900000000" in responses[-1]
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert aluguer.nome_cliente == "Cliente Final"
    assert aluguer.telefone_cliente == "351911111111"
    assert aluguer.email_cliente is None
    assert aluguer.numero_contentor == "OLT-12"
    assert aluguer.tipo_residuo == "Entulho Misto"
    assert aluguer.forma_pagamento == "MBWay"
    assert aluguer.pago is True
    assert aluguer.operador_telefone == "351900000000"
    assert aluguer.criado_por_operador == "351900000000"
    assert aluguer.foto_entrega_path == "whatsapp://media/foto-123"
    assert aluguer.latitude == 38.7223
    assert aluguer.longitude == -9.1393
    assert aluguer.status == StatusAluguer.ATIVO
    assert aluguer.contentor.codigo == "C01"
    assert aluguer.contentor.status == StatusContentor.ALUGADO
    assert aluguer.data_vencimento == aluguer.data_entrega + timedelta(days=5)
    assert {evento.tipo for evento in aluguer.eventos} == {"entrega", "pagamento_informado", "criado"}


def test_cadastro_opcoes_numeradas_confirmacao_data(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    prompt_data = avancar_cadastro_ate_confirmacao_data(router, db_session)
    prompt_tipo = router.handle(text_message("1", telefone="351900001000"))

    assert "1. Sim" in prompt_data
    assert "2. Outra data" in prompt_data
    assert "1. Entulho Limpo" in prompt_tipo
    assert "2. Entulho Misto" in prompt_tipo


def test_cadastro_opcao_2_na_confirmacao_data_pede_data_manual(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    avancar_cadastro_ate_confirmacao_data(router, db_session, telefone="351900001001")
    response = router.handle(text_message("2", telefone="351900001001"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001001").one()

    assert "DD/MM" in response
    assert conversa.estado_atual == "aguardando_data_entrega_manual"


def test_cadastro_tipo_residuo_aceita_1_e_2(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    _, prompt_tipo_1 = avancar_cadastro_ate_tipo_residuo(router, db_session, telefone="351900001002")
    response_1 = router.handle(text_message("1", telefone="351900001002"))
    conversa_1 = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001002").one()

    _, prompt_tipo_2 = avancar_cadastro_ate_tipo_residuo(router, db_session, telefone="351900001003")
    response_2 = router.handle(text_message("2", telefone="351900001003"))
    conversa_2 = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001003").one()

    assert "1. Entulho Limpo" in prompt_tipo_1
    assert "2. Entulho Misto" in prompt_tipo_2
    assert "Qual o valor do servico?" in response_1
    assert "Qual o valor do servico?" in response_2
    assert conversa_1.contexto_json["tipo_residuo"] == "Entulho Limpo"
    assert conversa_2.contexto_json["tipo_residuo"] == "Entulho Misto"


def test_cadastro_pagamento_aceita_1_e_2(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    _, _, prompt_pago_1 = avancar_cadastro_ate_pagamento(router, db_session, telefone="351900001004")
    response_1 = router.handle(text_message("1", telefone="351900001004"))
    conversa_1 = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001004").one()
    assert conversa_1.estado_atual == "aguardando_confirmacao_final"
    response_1 = router.handle(text_message("1", telefone="351900001004"))
    aluguer_1 = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()

    _, _, prompt_pago_2 = avancar_cadastro_ate_pagamento(router, db_session, telefone="351900001005")
    response_2 = router.handle(text_message("2", telefone="351900001005"))
    conversa_2 = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001005").one()
    assert conversa_2.estado_atual == "aguardando_confirmacao_final"
    response_2 = router.handle(text_message("1", telefone="351900001005"))
    aluguer_2 = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()

    assert "1. Pago" in prompt_pago_1
    assert "2. Pendente" in prompt_pago_2
    assert "Status pagamento: Pago" in response_1
    assert "Status pagamento: Pendente" in response_2
    assert aluguer_1.pago is True
    assert aluguer_2.pago is False


def test_cadastro_continua_aceitando_texto_antigo_nas_opcoes(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    avancar_cadastro_ate_confirmacao_data(router, db_session, telefone="351900001006")
    prompt_tipo_limpo = router.handle(text_message("sim", telefone="351900001006"))
    response_limpo = router.handle(text_message("Entulho limpo", telefone="351900001006"))

    avancar_cadastro_ate_confirmacao_data(router, db_session, telefone="351900001007")
    prompt_tipo_misto = router.handle(text_message("sim", telefone="351900001007"))
    response_misto = router.handle(text_message("Entulho misto", telefone="351900001007"))

    _, _, prompt_pago = avancar_cadastro_ate_pagamento(router, db_session, telefone="351900001008")
    response_pago = router.handle(text_message("pago", telefone="351900001008"))

    _, _, prompt_pendente = avancar_cadastro_ate_pagamento(router, db_session, telefone="351900001009")
    response_pendente = router.handle(text_message("pendente", telefone="351900001009"))

    avancar_cadastro_ate_confirmacao_data(router, db_session, telefone="351900001010")
    response_nao = router.handle(text_message("nao", telefone="351900001010"))

    assert "1. Entulho Limpo" in prompt_tipo_limpo
    assert "2. Entulho Misto" in prompt_tipo_misto
    assert "Qual o valor do servico?" in response_limpo
    assert "Qual o valor do servico?" in response_misto
    assert "1. Pago" in prompt_pago
    assert "2. Pendente" in prompt_pendente
    assert "Confirmacao dos Dados" in response_pago
    assert "Confirmacao dos Dados" in response_pendente
    assert "Opcao invalida. Responda 1 para hoje ou 2 para outra data." in response_nao


def test_cancelamento_global_no_cadastro_aguardando_foto_limpa_sessao_sem_salvar(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("novo", telefone="351900001020"))
    router.handle(text_message("1", telefone="351900001020"))
    response = router.handle(text_message("cancelar", telefone="351900001020"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001020").one()

    assert response == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert db_session.query(AluguerContentor).count() == 0


def test_cancelamento_global_no_cadastro_aguardando_localizacao_limpa_sessao_sem_salvar(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("novo", telefone="351900001021"))
    router.handle(text_message("1", telefone="351900001021"))
    router.handle(
        NormalizedWhatsAppMessage(
            telefone="351900001021",
            tipo="image",
            message_id="foto-cancelar",
            media_id="foto-cancelar",
            mime_type="image/jpeg",
        )
    )
    response = router.handle(text_message("cancelar", telefone="351900001021"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001021").one()

    assert response == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert db_session.query(AluguerContentor).count() == 0


def test_cancelamento_global_com_zero_no_cadastro_aguardando_localizacao(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("novo", telefone="351900001022"))
    router.handle(text_message("1", telefone="351900001022"))
    router.handle(
        NormalizedWhatsAppMessage(
            telefone="351900001022",
            tipo="image",
            message_id="foto-zero",
            media_id="foto-zero",
            mime_type="image/jpeg",
        )
    )
    response = router.handle(text_message("0", telefone="351900001022"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900001022").one()

    assert response == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert db_session.query(AluguerContentor).count() == 0


def test_alterar_e_modificar_iniciam_fluxo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Alterar", "351912345600")
    router = WhatsappRouterAgent(db_session)

    alterar = router.handle(text_message("alterar", telefone="351900000001"))
    modificar = router.handle(text_message("modificar", telefone="351900000002"))

    assert "Escolha o registro para alterar:" in alterar
    assert "1. #" in alterar
    assert "Escolha o registro para alterar:" in modificar


def test_alteracao_lista_apenas_registros_dos_ultimos_7_dias(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    recente = preparar_aluguer_gestao(db_session, "Cliente Recente", "351912345601")
    antigo = preparar_aluguer_gestao(db_session, "Cliente Antigo", "351912345602")
    antigo.criado_em = utcnow() - timedelta(days=8)
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("alterar"))

    assert f"#{recente.id}" in response
    assert "Cliente Recente" in response
    assert f"#{antigo.id}" not in response
    assert "Cliente Antigo" not in response


def test_alteracao_escolha_por_numero_seleciona_registro_correto(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    primeiro = preparar_aluguer_gestao(db_session, "Cliente Primeiro", "351912345603")
    segundo = preparar_aluguer_gestao(db_session, "Cliente Segundo", "351912345604")
    segundo.criado_em = utcnow()
    primeiro.criado_em = utcnow() - timedelta(minutes=1)
    db_session.commit()
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    response = router.handle(text_message("2"))

    assert f"ID/referencia: #{primeiro.id}" in response
    assert "Cliente: Cliente Primeiro" in response
    assert "Campos alteraveis:" in response


def test_alteracao_de_nome_do_cliente(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Antes", "351912345605")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("1"))
    response = router.handle(text_message("Cliente Depois"))

    db_session.refresh(aluguer)
    assert "Registro alterado." in response
    assert "Cliente: Cliente Depois" in response
    assert aluguer.nome_cliente == "Cliente Depois"
    assert aluguer.cliente.nome == "Cliente Depois"
    assert aluguer.alterado_por_operador == "351900000000"


def test_cancelamento_global_na_alteracao_nao_altera_registro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Sem Alteracao", "351912345699")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    response = router.handle(text_message("cancelar"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()
    db_session.refresh(aluguer)

    assert response == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert aluguer.nome_cliente == "Cliente Sem Alteracao"
    assert aluguer.alterado_por_operador is None


def test_alteracao_de_telefone_normaliza_portugal_e_atualiza_link(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Telefone", "351912345606")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("2"))
    response = router.handle(text_message("+351 912 345 678"))

    db_session.refresh(aluguer)
    assert aluguer.telefone_cliente == "351912345678"
    assert "Telefone: 351912345678" in response
    assert "WhatsApp cliente: https://wa.me/351912345678" in response


def test_alteracao_de_tipo_residuo_valida_opcoes(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Residuo", "351912345607")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("5"))
    invalid = router.handle(text_message("madeira"))
    response = router.handle(text_message("2"))

    db_session.refresh(aluguer)
    assert invalid == "Opcao invalida. Responda com o numero da opcao."
    assert aluguer.tipo_residuo == "Entulho misto"
    assert "Tipo residuo: Entulho misto" in response


def test_alteracao_tipo_residuo_aceita_opcoes_numeradas(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Residuo Opcao", "351912345627")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    prompt = router.handle(text_message("5"))
    response_1 = router.handle(text_message("1"))
    db_session.refresh(aluguer)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("5"))
    response_2 = router.handle(text_message("2"))
    db_session.refresh(aluguer)

    assert "1 - Entulho limpo" in prompt
    assert "2 - Entulho misto" in prompt
    assert "Tipo residuo: Entulho limpo" in response_1
    assert "Tipo residuo: Entulho misto" in response_2
    assert aluguer.tipo_residuo == "Entulho misto"


def test_alteracao_de_valor_normaliza_formatos(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Valor", "351912345608")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("6"))
    response = router.handle(text_message("€150,00"))

    db_session.refresh(aluguer)
    assert str(aluguer.valor) == "150.00"
    assert "Valor: 150.00" in response


def test_alteracao_de_status_pago_pendente(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Pago", "351912345609")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("8"))
    response = router.handle(text_message("pago"))

    db_session.refresh(aluguer)
    assert aluguer.pago is True
    assert "Status pagamento: pago" in response


def test_alteracao_pagamento_aceita_opcoes_numeradas(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Pago Opcao", "351912345628")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    prompt = router.handle(text_message("8"))
    response_1 = router.handle(text_message("1"))
    db_session.refresh(aluguer)

    router.handle(text_message("alterar"))
    router.handle(text_message("1"))
    router.handle(text_message("8"))
    response_2 = router.handle(text_message("2"))
    db_session.refresh(aluguer)

    assert "1 - Pago" in prompt
    assert "2 - Pendente" in prompt
    assert "Status pagamento: pago" in response_1
    assert "Status pagamento: pendente" in response_2
    assert aluguer.pago is False


def test_excluir_e_deletar_iniciam_fluxo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Excluir", "351912345610")
    router = WhatsappRouterAgent(db_session)

    excluir = router.handle(text_message("excluir", telefone="351900000003"))
    deletar = router.handle(text_message("deletar", telefone="351900000004"))

    assert "Escolha o registro para excluir:" in excluir
    assert "1. #" in excluir
    assert "Escolha o registro para excluir:" in deletar


def test_exclusao_exige_confirmacao(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Confirmacao", "351912345611")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("excluir"))
    response = router.handle(text_message("1"))

    assert f"ID/referencia: #{aluguer.id}" in response
    assert "1 - Sim, continuar" in response
    assert "2 - Nao, cancelar" in response
    assert AluguerService(db_session)._get_or_raise(aluguer.id)


def test_exclusao_com_opcao_2_cancela_sem_apagar(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Cancela", "351912345612")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("excluir"))
    router.handle(text_message("1"))
    response = router.handle(text_message("2"))

    assert response == "Exclusao cancelada. Nenhum registro foi apagado."
    assert AluguerService(db_session)._get_or_raise(aluguer.id).id == aluguer.id


def test_cancelamento_global_na_exclusao_nao_marca_registro_como_excluido(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer_cancelar = preparar_aluguer_gestao(db_session, "Cliente Excluir Cancelar", "351912345700")
    aluguer_zero = preparar_aluguer_gestao(db_session, "Cliente Excluir Zero", "351912345701")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("excluir"))
    router.handle(text_message("1"))
    response_cancelar = router.handle(text_message("cancelar"))

    router.handle(text_message("excluir"))
    router.handle(text_message("1"))
    response_zero = router.handle(text_message("0"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()
    db_session.refresh(aluguer_cancelar)
    db_session.refresh(aluguer_zero)

    assert response_cancelar == CANCELLED_MENU_MESSAGE
    assert response_zero == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert aluguer_cancelar.is_deleted is False
    assert aluguer_zero.is_deleted is False


def test_exclusao_com_opcao_1_pede_justificativa(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Continua Exclusao", "351912345629")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("excluir"))
    router.handle(text_message("1"))
    response = router.handle(text_message("1"))

    assert response == "Informe a justificativa da exclusao com pelo menos 10 caracteres."


def test_exclusao_com_confirmacao_clara_exclui(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = preparar_aluguer_gestao(db_session, "Cliente Apagar", "351912345613")
    aluguer_id = aluguer.id
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("deletar"))
    router.handle(text_message("1"))
    pedido_justificativa = router.handle(text_message("confirmar"))
    curta = router.handle(text_message("curta"))
    response = router.handle(text_message("Cliente pediu cancelamento"))

    db_session.refresh(aluguer)
    assert pedido_justificativa == "Informe a justificativa da exclusao com pelo menos 10 caracteres."
    assert curta == "A justificativa deve ter pelo menos 10 caracteres."
    assert response == f"Registro #{aluguer_id} excluido."
    assert db_session.get(type(aluguer), aluguer_id) is not None
    assert aluguer.is_deleted is True
    assert aluguer.justificativa_exclusao == "Cliente pediu cancelamento"
    assert aluguer.excluido_por_operador == "351900000000"
    assert (
        db_session.query(AluguerContentor)
        .filter(AluguerContentor.id == aluguer_id)
        .filter(AluguerContentor.is_deleted.is_(False))
        .first()
        is None
    )


def test_numero_nao_autorizado_nao_altera_nem_exclui(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "351999999999")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Bloqueado", "351912345614")
    router = WhatsappRouterAgent(db_session)

    alterar = router.handle(text_message("alterar", telefone="351900000000"))
    excluir = router.handle(text_message("excluir", telefone="351900000000"))

    assert alterar == "Telefone nao autorizado para alterar registros. Contacte o administrador do sistema."
    assert excluir == "Telefone nao autorizado para excluir registros. Contacte o administrador do sistema."


def test_renovar_e_prorrogar_iniciam_fluxo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Renovar", "351912345615")
    router = WhatsappRouterAgent(db_session)

    renovar = router.handle(text_message("renovar", telefone="351900000005"))
    prorrogar = router.handle(text_message("prorrogar", telefone="351900000006"))

    assert "Escolha o registro para renovar:" in renovar
    assert "valor 100.00" in renovar
    assert "Escolha o registro para renovar:" in prorrogar


def test_renovacao_lista_apenas_registros_dos_ultimos_7_dias(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    recente = preparar_aluguer_gestao(db_session, "Cliente Renovacao Recente", "351912345616")
    antigo = preparar_aluguer_gestao(db_session, "Cliente Renovacao Antigo", "351912345617")
    antigo.criado_em = utcnow() - timedelta(days=8)
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("renovar"))

    assert f"#{recente.id}" in response
    assert "Cliente Renovacao Recente" in response
    assert f"#{antigo.id}" not in response
    assert "Cliente Renovacao Antigo" not in response


def test_renovacao_escolha_por_numero_seleciona_registro_correto(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    primeiro = preparar_aluguer_gestao(db_session, "Cliente Renovacao Primeiro", "351912345618")
    segundo = preparar_aluguer_gestao(db_session, "Cliente Renovacao Segundo", "351912345619")
    segundo.criado_em = utcnow()
    primeiro.criado_em = utcnow() - timedelta(minutes=1)
    db_session.commit()
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    response = router.handle(text_message("2"))

    assert f"ID/referencia: #{primeiro.id}" in response
    assert "Cliente: Cliente Renovacao Primeiro" in response
    assert "Deseja alterar alguma informacao antes de renovar?" in response
    assert "1 - Sim" in response
    assert "2 - Nao, prosseguir" in response


def test_renovacao_sem_alteracoes_cria_novo_registro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Sem Alteracao", "351912345620", valor="140")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    response = router.handle(text_message("nao"))

    alugueres = db_session.query(AluguerContentor).order_by(AluguerContentor.id).all()
    novo = alugueres[-1]
    assert len(alugueres) == 2
    assert novo.id != origem.id
    assert f"Registro antigo: #{origem.id}" in response
    assert f"Novo registro: #{novo.id}" in response
    assert novo.nome_cliente == origem.nome_cliente
    assert novo.telefone_cliente == origem.telefone_cliente
    assert novo.contentor_id == origem.contentor_id
    assert novo.criado_por_operador == "351900000000"


def test_cancelamento_global_na_renovacao_nao_cria_novo_registro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Renovacao Cancelada", "351912345702")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    response = router.handle(text_message("cancelar"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()
    alugueres = db_session.query(AluguerContentor).order_by(AluguerContentor.id).all()

    assert response == CANCELLED_MENU_MESSAGE
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert [aluguer.id for aluguer in alugueres] == [origem.id]


def test_renovacao_aceita_opcao_2_para_prosseguir(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Renovacao Opcao 2", "351912345630")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    prompt = router.handle(text_message("1"))
    response = router.handle(text_message("2"))

    novo = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
    assert "1 - Sim" in prompt
    assert "2 - Nao, prosseguir" in prompt
    assert f"Registro antigo: #{origem.id}" in response
    assert novo.id != origem.id


def test_renovacao_aceita_opcao_1_para_alterar(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Renovacao Opcao 1", "351912345631")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    response = router.handle(text_message("1"))

    assert "Campos alteraveis antes de renovar:" in response
    assert "1. nome do cliente" in response


def test_renovacao_calcula_novas_datas(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Datas", "351912345621")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    router.handle(text_message("nao"))

    novo = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
    assert novo.data_entrega == origem.data_vencimento + timedelta(days=1)
    assert novo.data_vencimento == novo.data_entrega + timedelta(days=5)


def test_renovacao_nao_altera_registro_antigo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Original", "351912345622", valor="130")
    dados_originais = {
        "nome_cliente": origem.nome_cliente,
        "telefone_cliente": origem.telefone_cliente,
        "data_entrega": origem.data_entrega,
        "data_vencimento": origem.data_vencimento,
        "valor": origem.valor,
        "pago": origem.pago,
    }
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    router.handle(text_message("nao"))
    db_session.refresh(origem)

    assert origem.nome_cliente == dados_originais["nome_cliente"]
    assert origem.telefone_cliente == dados_originais["telefone_cliente"]
    assert origem.data_entrega == dados_originais["data_entrega"]
    assert origem.data_vencimento == dados_originais["data_vencimento"]
    assert origem.valor == dados_originais["valor"]
    assert origem.pago == dados_originais["pago"]


def test_alteracao_antes_da_renovacao_muda_so_o_novo_registro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Antes Renovar", "351912345623")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    router.handle(text_message("sim"))
    router.handle(text_message("1"))
    router.handle(text_message("Cliente Novo Renovado"))
    response = router.handle(text_message("nao"))

    db_session.refresh(origem)
    novo = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
    assert origem.nome_cliente == "Cliente Antes Renovar"
    assert novo.nome_cliente == "Cliente Novo Renovado"
    assert "Cliente: Cliente Novo Renovado" in response


def test_renovacao_tipo_e_pagamento_usam_opcoes_numeradas(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Renovacao Opcoes", "351912345632")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    router.handle(text_message("1"))
    prompt_tipo = router.handle(text_message("5"))
    router.handle(text_message("2"))
    router.handle(text_message("1"))
    prompt_pagamento = router.handle(text_message("8"))
    router.handle(text_message("1"))
    response = router.handle(text_message("2"))

    novo = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
    assert "1 - Entulho limpo" in prompt_tipo
    assert "2 - Entulho misto" in prompt_tipo
    assert "1 - Pago" in prompt_pagamento
    assert "2 - Pendente" in prompt_pagamento
    assert novo.tipo_residuo == "Entulho misto"
    assert novo.pago is True
    assert "Status pagamento: pago" in response


def test_telefone_alterado_antes_da_renovacao_normaliza_e_gera_link(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    origem = preparar_aluguer_gestao(db_session, "Cliente Fone Renovar", "351912345624")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    router.handle(text_message("sim"))
    router.handle(text_message("2"))
    router.handle(text_message("912 345 678"))
    response = router.handle(text_message("nao"))

    db_session.refresh(origem)
    novo = db_session.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
    assert origem.telefone_cliente == "351912345624"
    assert novo.telefone_cliente == "351912345678"
    assert "Telefone: 351912345678" in response
    assert "WhatsApp cliente: https://wa.me/351912345678" in response


def test_resposta_ambigua_na_renovacao_pede_confirmacao_segura(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Ambiguo", "351912345625")
    router = WhatsappRouterAgent(db_session)

    router.handle(text_message("renovar"))
    router.handle(text_message("1"))
    response = router.handle(text_message("talvez"))

    assert response == "Opcao invalida. Responda 1 para Sim ou 2 para Nao."
    assert db_session.query(AluguerContentor).count() == 1


def test_numero_nao_autorizado_nao_renova_nem_prorroga(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "351999999999")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    SeedService(db_session).seed_contentores_iniciais()
    preparar_aluguer_gestao(db_session, "Cliente Renovacao Bloqueada", "351912345626")
    router = WhatsappRouterAgent(db_session)

    renovar = router.handle(text_message("renovar", telefone="351900000000"))
    prorrogar = router.handle(text_message("prorrogar", telefone="351900000000"))

    assert renovar == "Telefone nao autorizado para renovar registros. Contacte o administrador do sistema."
    assert prorrogar == "Telefone nao autorizado para renovar registros. Contacte o administrador do sistema."


def test_whatsapp_client_mock_retorna_mensagem_enviada(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    result = send_text_message("351900000000", "Mensagem de teste")

    assert result == {"to": "351900000000", "body": "Mensagem de teste", "status": "mocked"}


def test_normalizacao_de_telefone():
    assert normalize_phone("+55 (61) 98266-551") == "556198266551"
    assert normalize_phone(" 351-912 345 678 ") == "351912345678"
    assert normalize_phone(None) == ""


def test_telefone_portugal_e_link_wa_me():
    assert normalize_portugal_phone("912345678") == "351912345678"
    assert normalize_portugal_phone("+351 912 345 678") == "351912345678"
    assert normalize_portugal_phone("00351 912 345 678") == "351912345678"
    assert whatsapp_link("912345678") == "https://wa.me/351912345678"


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

    assert "numero do contentor" in response
    assert conversa.estado_atual == AluguerAgent.START_STATE


def test_cancelar_sem_fluxo_ativo_informa_que_nao_ha_operacao(db_session, monkeypatch):
    liberar_operadores(monkeypatch)

    response = WhatsappRouterAgent(db_session).handle(text_message("cancelar"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone="351900000000").one()

    assert response == "Nenhuma operação em andamento para cancelar."
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}


def test_comando_resumo_mostra_contadores_operacionais(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "Resumo dos contentores" in response
    assert "Total: 20" in response
    assert "Disponiveis: 15" in response
    assert "Alugados: 3" in response
    assert "Aguardando recolha: 1" in response
    assert "Manutencao: 1" in response
    assert "Alugueres ativos: 3" in response
    assert "Vencem amanha: 1" in response
    assert "Em atraso: 1" in response
    return

    assert response == "\n".join(
        [
            "📦 Resumo dos contentores",
            "Total: 20",
            "Disponíveis: 15",
            "Alugados: 3",
            "Aguardando recolha: 1",
            "Manutenção: 1",
            "Alugueres ativos: 3",
            "Vencem amanhã: 1",
            "Em atraso: 1",
        ]
    )


def test_comandos_operacionais_continuam_funcionando(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_operacao_demo(db_session)
    router = WhatsappRouterAgent(db_session)

    responses = {
        command: router.handle(text_message(command, telefone=f"35190000200{index}"))
        for index, command in enumerate(["resumo", "lista", "disponiveis", "alugados", "vencendo", "atrasados"], start=1)
    }

    assert "Resumo dos contentores" in responses["resumo"]
    assert "C01 - alugado" in responses["lista"]
    assert responses["disponiveis"].startswith("Contentores dispon")
    assert "Contentores alugados:" in responses["alugados"]
    assert "Alugueres que vencem" in responses["vencendo"]
    assert "Alugueres em atraso:" in responses["atrasados"]


def test_resumo_lista_retirada_de_hoje_com_cliente_e_localizacao(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    hoje, _ = preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "Retiradas hoje:" in response
    assert "Cliente Retirada Hoje" in response
    assert f"#{hoje.id} / C01" in response
    assert "https://www.google.com/maps?q=38.7223,-9.1393" in response
    assert f"retirada {hoje.data_vencimento:%d/%m/%Y}" in response


def test_resumo_lista_retirada_de_amanha_com_cliente(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    _, amanha = preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "Retiradas amanha:" in response
    assert "Cliente Retirada Amanha" in response
    assert f"#{amanha.id} / C02" in response
    assert f"retirada {amanha.data_vencimento:%d/%m/%Y}" in response


def test_resumo_mostra_link_wa_me_quando_ha_telefone(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "telefone: 351912345678 / https://wa.me/351912345678" in response


def test_resumo_lida_com_ausencia_de_localizacao(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "Cliente Retirada Amanha" in response
    assert "localizacao: localizacao nao informada" in response


def test_resumo_calcula_faturado_total_e_recebido_no_mes(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo"))

    assert "Faturamento do mes corrente:" in response
    assert "Faturado total do mes: 300.00" in response
    assert "Recebido/pago no mes: 100.00" in response
    assert "faturado total soma todos os alugueres do mes" in response


def test_resumo_gestor_mostra_financeiro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_resumo_paulo(db_session)
    db_session.add(
        Operador(
            telefone_whatsapp="351900000010",
            nome_operador="Gestor",
            perfil=PerfilOperador.GESTOR,
            ativo=True,
        )
    )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo", telefone="351900000010"))

    assert "Faturamento do mes corrente:" in response
    assert "Faturado total do mes: 300.00" in response


def test_resumo_funcionario_nao_mostra_financeiro(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    preparar_resumo_paulo(db_session)
    db_session.add(
        Operador(
            telefone_whatsapp="351900000011",
            nome_operador="Funcionario",
            perfil=PerfilOperador.FUNCIONARIO,
            ativo=True,
        )
    )
    db_session.commit()

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo", telefone="351900000011"))

    assert "Retiradas hoje:" in response
    assert "Cliente Retirada Hoje" in response
    assert "Faturamento do mes corrente:" not in response
    assert "Faturado total do mes" not in response
    assert "Recebido/pago no mes" not in response


def test_registros_deletados_nao_aparecem_em_listas_e_resumo(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    deletado = preparar_aluguer_gestao(db_session, "Cliente Deletado", "351912345690")
    ativo = preparar_aluguer_gestao(db_session, "Cliente Ativo", "351912345691")
    AluguerService(db_session).excluir(deletado.id, "351900000000", "Duplicidade operacional")

    router = WhatsappRouterAgent(db_session)
    alterar = router.handle(text_message("alterar"))
    alugados = router.handle(text_message("alugados"))
    resumo = router.handle(text_message("resumo"))

    assert f"#{deletado.id}" not in alterar
    assert "Cliente Deletado" not in alugados
    assert "Cliente Deletado" not in resumo
    assert f"#{ativo.id}" in alterar
    assert "Cliente Ativo" in alugados


def test_renovacao_ignora_registros_deletados(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    SeedService(db_session).seed_contentores_iniciais()
    deletado = preparar_aluguer_gestao(db_session, "Cliente Renovacao Deletado", "351912345692")
    ativo = preparar_aluguer_gestao(db_session, "Cliente Renovacao Ativo", "351912345693")
    AluguerService(db_session).excluir(deletado.id, "351900000000", "Contrato encerrado")

    response = WhatsappRouterAgent(db_session).handle(text_message("renovar"))

    assert f"#{deletado.id}" not in response
    assert "Cliente Renovacao Deletado" not in response
    assert f"#{ativo.id}" in response
    assert "Cliente Renovacao Ativo" in response


def test_numero_nao_autorizado_nao_recebe_resumo_detalhado(db_session, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "351999999999")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()
    preparar_resumo_paulo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("resumo", telefone="351900000000"))

    assert response == "Telefone nao autorizado para consultar dados operacionais. Contacte o administrador do sistema."
    assert "Cliente Retirada Hoje" not in response


def test_comando_lista_mostra_todos_os_contentores_com_status(db_session):
    preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("lista"))

    assert "C01 - alugado" in response
    assert "C02 - alugado" in response
    assert "C03 - alugado" in response
    assert "C04 - aguardando recolha" in response
    assert "C05 - manutenção" in response
    assert "C20 - disponível" in response
    assert len(response.splitlines()) == 20


def test_comando_disponiveis_lista_apenas_contentores_disponiveis(db_session):
    preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("disponiveis"))

    assert response.startswith("Contentores disponíveis:\n")
    assert "C06" in response
    assert "C20" in response
    assert "C01" not in response
    assert "C04" not in response


def test_comando_alugados_lista_cliente_vencimento_e_status(db_session):
    aluguer_amanha, aluguer_atrasado, aluguer_regular = preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("alugados"))

    assert "Contentores alugados:" in response
    assert f"C01 - Cliente Amanhã - vencimento {aluguer_amanha.data_vencimento:%d/%m/%Y} - ativo" in response
    assert f"C02 - Cliente Atrasado - vencimento {aluguer_atrasado.data_vencimento:%d/%m/%Y} - ativo" in response
    assert f"C03 - Cliente Regular - vencimento {aluguer_regular.data_vencimento:%d/%m/%Y} - ativo" in response


def test_comando_vencendo_lista_alugueres_que_vencem_amanha(db_session):
    aluguer_amanha, _, _ = preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("vencendo"))

    assert "Alugueres que vencem amanhã:" in response
    assert f"C01 - Cliente Amanhã - vencimento {aluguer_amanha.data_vencimento:%d/%m/%Y} - ativo" in response
    assert "Cliente Atrasado" not in response
    assert "Cliente Regular" not in response


def test_comando_atrasados_lista_alugueres_ativos_em_atraso(db_session):
    _, aluguer_atrasado, _ = preparar_operacao_demo(db_session)

    response = WhatsappRouterAgent(db_session).handle(text_message("atrasados"))

    assert "Alugueres em atraso:" in response
    assert f"C02 - Cliente Atrasado - vencimento {aluguer_atrasado.data_vencimento:%d/%m/%Y} - ativo" in response
    assert "Cliente Amanhã" not in response
    assert "Cliente Regular" not in response
