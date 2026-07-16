from datetime import datetime, timedelta, timezone
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.core.db import Base
from app.core.db import get_db
from app.core.schema_migrations import ensure_alugueres_contentor_schema
from app.core.time import utcnow
from app.integrations.whatsapp.client import send_whatsapp_message
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    TipoEquipamentoPedido,
)
from app.models.whatsapp_dedup import WhatsAppProcessedMessage
from app.services.pedido_service import PedidoService
from app.services.whatsapp_dedup_service import (
    PROCESSING_CLAIM_TTL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    WhatsAppMessageDedupService,
)
from app.routes import webhook


PHONE = "351900009900"


def _msg(text=None, *, kind="text", media=None, phone=PHONE, message_id="m"):
    return NormalizedWhatsAppMessage(
        telefone=phone,
        tipo=kind,
        texto=text,
        media_id=media,
        message_id=message_id,
    )


def _liberar_operadores(monkeypatch):
    for name in (
        "WHATSAPP_OWNER_PHONE",
        "AUTHORIZED_OPERATOR_PHONE",
        "AUTHORIZED_OPERATOR_PHONES",
        "OWNER_WHATSAPP",
    ):
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()


def _correcao_ctx(*, tipo, pago):
    is_carrinha = tipo == TipoEquipamentoPedido.CARRINHA.value
    item = {
        "tipo_equipamento": tipo,
        "residuo_contratado": "Entulho Limpo",
        "precisa_mao_de_obra": True,
        "horario_agendado": "09:30" if is_carrinha else None,
    }
    return {
        "tipo_solicitacao": tipo,
        "quantidade": 1,
        "itens": [item],
        "residuos": ["Entulho Limpo"],
        "nome": "Cliente Correcao",
        "telefone": "351912345678",
        "data": datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc).isoformat(),
        "horario_agendado": "09:30" if is_carrinha else None,
        "precisa_mao_de_obra": True,
        "valor": "250",
        "pago": pago,
        "forma": "MBWay" if pago else None,
        "endereco": "Rua da Obra",
        "referencia": "Portao azul",
    }


def _conversa_correcao(db_session, *, tipo, pago, telefone=PHONE):
    conversa = ConversaWhatsApp(
        telefone=telefone,
        estado_atual="v24_cadastro_corrigir",
        contexto_json=_correcao_ctx(tipo=tipo, pago=pago),
    )
    db_session.add(conversa)
    db_session.commit()
    db_session.refresh(conversa)
    return conversa


def _expected_state(agent, field):
    return agent._edit_state_for(field)


def _assert_context_preserved(ctx_before, ctx_after, *, except_keys=("editing_field",)):
    for key, value in ctx_before.items():
        if key in except_keys:
            continue
        assert ctx_after[key] == value


@pytest.mark.parametrize("pago", [True, False])
def test_correcao_carrinha_status_pagamento_por_numero_textual(db_session, pago):
    conversa = _conversa_correcao(
        db_session,
        tipo=TipoEquipamentoPedido.CARRINHA.value,
        pago=pago,
    )
    agent = PedidoV24Agent(db_session)
    fields = agent._corrigir_fields(conversa.contexto_json)
    status_index = [field for field, _ in fields].index("status_pagamento") + 1
    ctx_before = dict(conversa.contexto_json)

    response = agent.handle(conversa, _msg(str(status_index)))
    db_session.refresh(conversa)

    assert "o pedido ja esta pago?" in agent._norm(response)
    assert conversa.estado_atual == "v24_cadastro_edicao_opcao"
    assert conversa.contexto_json["editing_field"] == "status_pagamento"
    _assert_context_preserved(ctx_before, conversa.contexto_json)


@pytest.mark.parametrize(
    ("tipo", "pago", "expected_count", "unexpected_field"),
    [
        (TipoEquipamentoPedido.CARRINHA.value, True, 12, None),
        (TipoEquipamentoPedido.CARRINHA.value, False, 11, "forma_pagamento"),
        (TipoEquipamentoPedido.CONTENTOR.value, True, 11, None),
        (TipoEquipamentoPedido.CONTENTOR.value, False, 10, "forma_pagamento"),
    ],
)
def test_correcao_fields_por_tipo_e_status_de_pagamento(
    db_session,
    tipo,
    pago,
    expected_count,
    unexpected_field,
):
    conversa = _conversa_correcao(db_session, tipo=tipo, pago=pago)
    agent = PedidoV24Agent(db_session)

    fields = agent._corrigir_fields(conversa.contexto_json)
    field_names = [field for field, _ in fields]
    status_index = field_names.index("status_pagamento") + 1

    assert len(fields) == expected_count
    assert "status_pagamento" in field_names
    if unexpected_field:
        assert unexpected_field not in field_names

    response = agent.handle(conversa, _msg(str(status_index)))
    db_session.refresh(conversa)

    assert conversa.estado_atual == "v24_cadastro_edicao_opcao"
    assert conversa.contexto_json["editing_field"] == "status_pagamento"
    assert "pago" in agent._norm(response)


def test_correcao_status_pagamento_por_id_interativo_equivale_ao_textual(db_session):
    textual = _conversa_correcao(
        db_session,
        tipo=TipoEquipamentoPedido.CARRINHA.value,
        pago=True,
        telefone="351900009901",
    )
    interactive = _conversa_correcao(
        db_session,
        tipo=TipoEquipamentoPedido.CARRINHA.value,
        pago=True,
        telefone="351900009902",
    )
    agent = PedidoV24Agent(db_session)
    fields = agent._corrigir_fields(textual.contexto_json)
    status_index = [field for field, _ in fields].index("status_pagamento") + 1

    text_response = agent.handle(textual, _msg(str(status_index), phone=textual.telefone))
    interactive_response = agent.handle(
        interactive,
        _msg(
            "corrigir_pedido:status_pagamento",
            kind="interactive",
            phone=interactive.telefone,
            message_id="interactive-status",
        ),
    )
    db_session.refresh(textual)
    db_session.refresh(interactive)

    assert textual.estado_atual == interactive.estado_atual == "v24_cadastro_edicao_opcao"
    assert textual.contexto_json["editing_field"] == interactive.contexto_json["editing_field"]
    assert text_response == interactive_response


@pytest.mark.parametrize(
    ("tipo", "pago"),
    [
        (TipoEquipamentoPedido.CONTENTOR.value, True),
        (TipoEquipamentoPedido.CONTENTOR.value, False),
        (TipoEquipamentoPedido.CARRINHA.value, True),
        (TipoEquipamentoPedido.CARRINHA.value, False),
    ],
)
def test_correcao_todas_opcoes_entram_no_estado_correto_por_numero_e_id(db_session, tipo, pago):
    base = _correcao_ctx(tipo=tipo, pago=pago)
    agent = PedidoV24Agent(db_session)
    fields = agent._corrigir_fields(base)

    for index, (field, _title) in enumerate(fields, 1):
        numeric = ConversaWhatsApp(
            telefone=f"35191{index:07d}",
            estado_atual="v24_cadastro_corrigir",
            contexto_json=dict(base),
        )
        interactive = ConversaWhatsApp(
            telefone=f"35192{index:07d}",
            estado_atual="v24_cadastro_corrigir",
            contexto_json=dict(base),
        )
        db_session.add_all([numeric, interactive])
        db_session.commit()
        numeric_ctx_before = dict(numeric.contexto_json)
        interactive_ctx_before = dict(interactive.contexto_json)

        numeric_response = agent.handle(numeric, _msg(str(index), phone=numeric.telefone))
        interactive_response = agent.handle(
            interactive,
            _msg(f"corrigir_pedido:{field}", kind="interactive", phone=interactive.telefone),
        )
        db_session.refresh(numeric)
        db_session.refresh(interactive)

        assert numeric.estado_atual == _expected_state(agent, field)
        assert interactive.estado_atual == _expected_state(agent, field)
        assert numeric.contexto_json["editing_field"] == field
        assert interactive.contexto_json["editing_field"] == field
        assert numeric_response
        assert interactive_response
        _assert_context_preserved(numeric_ctx_before, numeric.contexto_json)
        _assert_context_preserved(interactive_ctx_before, interactive.contexto_json)


def test_fallback_textual_correcao_12_opcoes_permite_responder_9(db_session, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v25.0")
    get_settings.cache_clear()
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        if json["type"] == "interactive":
            return httpx.Response(400, json={"error": {"message": "list failed"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.text"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    conversa = _conversa_correcao(
        db_session,
        tipo=TipoEquipamentoPedido.CARRINHA.value,
        pago=True,
    )
    agent = PedidoV24Agent(db_session)
    menu = agent._corrigir_prompt(conversa.contexto_json)

    result = send_whatsapp_message(conversa.telefone, menu)

    assert calls[0]["type"] == "interactive"
    assert calls[0]["interactive"]["type"] == "list"
    assert calls[1]["type"] == "text"
    assert result["fallback_from"] == "list"
    assert "9. Status do pagamento" in calls[1]["text"]["body"]
    assert "12. ponto de referencia" in agent._norm(calls[1]["text"]["body"])
    db_session.refresh(conversa)
    assert conversa.estado_atual == "v24_cadastro_corrigir"

    response = agent.handle(conversa, _msg("9"))
    db_session.refresh(conversa)

    assert conversa.estado_atual == "v24_cadastro_edicao_opcao"
    assert conversa.contexto_json["editing_field"] == "status_pagamento"
    assert "pago" in agent._norm(response)


def _pedido_para_recolha(service, *, nome="Cliente Recolha", quantidade=1, adesivo_base=100):
    residuos = ["Entulho Limpo" for _ in range(quantidade)]
    pedido = service.criar(
        nome_cliente=nome,
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="100",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua",
        ponto_referencia=None,
        residuos=residuos,
    )
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [
            {"contentor_id": item.id, "numero_adesivo": str(adesivo_base + index), "fotos": [f"foto-{adesivo_base}-{index}"]}
            for index, item in enumerate(pedido.contentores)
        ],
    )
    return pedido


def test_recolha_segundo_comando_3_e_selecao_valida_posterior(db_session, monkeypatch):
    _liberar_operadores(monkeypatch)
    service = PedidoService(db_session)
    _pedido_para_recolha(service)
    router = WhatsappRouterAgent(db_session)

    primeira = router.handle(_msg("3", message_id="m-1"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
    ids_originais = list(conversa.contexto_json["ids"])
    segunda = router.handle(_msg("3", message_id="m-2"))
    db_session.refresh(conversa)
    avancou = router.handle(_msg("1", message_id="m-3"))
    db_session.refresh(conversa)

    assert "Selecione o pedido para recolha" in primeira
    assert conversa.contexto_json["pedido_id"] == ids_originais[0]
    assert "Selecione um pedido da lista" in segunda
    assert conversa.estado_atual == "v24_recolha_ativo"
    assert "Selecione o ativo" in avancou


def test_falha_http_lista_recolha_envia_fallback_e_numero_avanca_fluxo(db_session, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1148807428322172")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v25.0")
    get_settings.cache_clear()
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        if json["type"] == "interactive":
            return httpx.Response(400, json={"error": {"message": "list failed"}})
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(calls)}"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    service = PedidoService(db_session)
    _pedido_para_recolha(service, nome="Cliente A", adesivo_base=100)
    _pedido_para_recolha(service, nome="Cliente B", adesivo_base=200)
    _pedido_para_recolha(service, nome="Cliente C", adesivo_base=300)
    _pedido_para_recolha(service, nome="Cliente D", adesivo_base=400)
    router = WhatsappRouterAgent(db_session)

    prompt = router.handle(_msg("3"))
    result = send_whatsapp_message(PHONE, prompt)
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
    ids = list(conversa.contexto_json["ids"])
    response = router.handle(_msg("1"))
    db_session.refresh(conversa)

    assert calls[0]["type"] == "interactive"
    assert calls[0]["interactive"]["type"] == "list"
    assert result["fallback_from"] == "list"
    assert all(f"{index}." in calls[1]["text"]["body"] for index in range(1, len(ids) + 1))
    assert conversa.contexto_json["pedido_id"] == ids[0]
    assert conversa.estado_atual == "v24_recolha_ativo"
    assert "Selecione o ativo" in response


def _webhook_payload(text, *, phone=PHONE, message_id="wamid.1"):
    message = {
        "from": phone,
        "type": "text",
        "text": {"body": text},
    }
    if message_id is not None:
        message["id"] = message_id
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [message]
                        }
                    }
                ]
            }
        ]
    }


def _webhook_payload_many(messages, *, phone=PHONE):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": phone,
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                                for message_id, text in messages
                            ]
                        }
                    }
                ]
            }
        ]
    }


def test_webhook_message_id_duplicado_deveria_processar_uma_vez(client, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    test_client = TestClient(client)
    payload = _webhook_payload("novo pedido", message_id="wamid.duplicado")

    first = test_client.post("/webhook/whatsapp", json=payload)
    second = test_client.post("/webhook/whatsapp", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["messages"]
    assert second.json()["messages"] == []


def test_webhook_message_id_duplicado_nao_reexecuta_efeito_funcional(client, db_session, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    test_client = TestClient(client)
    payload = _webhook_payload("novo pedido", message_id="wamid.efeito-unico")

    test_client.post("/webhook/whatsapp", json=payload)
    test_client.post("/webhook/whatsapp", json=payload)

    conversation = db_session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
    assert conversation.estado_atual == "v24_cadastro_tipo_solicitacao"


def test_webhook_duas_mensagens_mesmo_payload_ids_diferentes_preserva_ordem(client, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()

    response = TestClient(client).post(
        "/webhook/whatsapp",
        json=_webhook_payload_many([("wamid.a", "novo pedido"), ("wamid.b", "carrinha")]),
    )

    assert response.status_code == 200
    bodies = [message["body"] for message in response.json()["messages"]]
    assert "tipo de solicitacao" in PedidoV24Agent(None)._norm(bodies[0])
    assert "quantas carrinhas" in PedidoV24Agent(None)._norm(bodies[1])


def test_webhook_duas_mensagens_mesmo_payload_message_id_duplicado_processa_uma_vez(client, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()

    response = TestClient(client).post(
        "/webhook/whatsapp",
        json=_webhook_payload_many([("wamid.same", "novo pedido"), ("wamid.same", "carrinha")]),
    )

    assert response.status_code == 200
    assert len(response.json()["messages"]) == 1


def test_dedup_completed_persiste_em_nova_sessao(db_session):
    service = WhatsAppMessageDedupService(db_session)
    first = service.claim("wamid.persistente")
    assert first.should_process is True
    service.mark_completed("wamid.persistente")

    Session = sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False)
    with Session() as new_session:
        second = WhatsAppMessageDedupService(new_session).claim("wamid.persistente")

    assert second.should_process is False
    assert second.reason == "completed"


def test_dedup_failed_permite_retry_e_completed_bloqueia_terceira_tentativa(client, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    original_handle = WhatsappRouterAgent.handle
    calls = {"count": 0}

    def fail_once(self, message):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("falha simulada")
        return original_handle(self, message)

    monkeypatch.setattr(WhatsappRouterAgent, "handle", fail_once)
    payload = _webhook_payload("novo pedido", message_id="wamid.retry")
    failing_client = TestClient(client, raise_server_exceptions=False)
    ok_client = TestClient(client)

    first = failing_client.post("/webhook/whatsapp", json=payload)
    second = ok_client.post("/webhook/whatsapp", json=payload)
    third = ok_client.post("/webhook/whatsapp", json=payload)

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json()["messages"]
    assert third.json()["messages"] == []
    assert calls["count"] == 2


def test_dedup_processing_recente_nao_processa(db_session):
    now = utcnow()
    db_session.add(
        WhatsAppProcessedMessage(
            message_id="wamid.processing-recente",
            status=STATUS_PROCESSING,
            criado_em=now,
            atualizado_em=now,
        )
    )
    db_session.commit()

    claim = WhatsAppMessageDedupService(db_session).claim("wamid.processing-recente")

    assert claim.should_process is False
    assert claim.reason == "processing"


def test_dedup_processing_expirado_reivindica_novamente(db_session):
    old = utcnow() - PROCESSING_CLAIM_TTL - timedelta(seconds=1)
    db_session.add(
        WhatsAppProcessedMessage(
            message_id="wamid.processing-expirado",
            status=STATUS_PROCESSING,
            criado_em=old,
            atualizado_em=old,
        )
    )
    db_session.commit()

    service = WhatsAppMessageDedupService(db_session)
    claim = service.claim("wamid.processing-expirado")
    service.mark_completed("wamid.processing-expirado")
    record = db_session.query(WhatsAppProcessedMessage).filter_by(message_id="wamid.processing-expirado").one()

    assert claim.should_process is True
    assert claim.reason == "expired_processing_reclaimed"
    assert record.status == STATUS_COMPLETED


def test_dedup_failed_reivindica_novamente(db_session):
    now = utcnow()
    db_session.add(
        WhatsAppProcessedMessage(
            message_id="wamid.failed",
            status=STATUS_FAILED,
            criado_em=now,
            atualizado_em=now,
        )
    )
    db_session.commit()

    claim = WhatsAppMessageDedupService(db_session).claim("wamid.failed")

    assert claim.should_process is True
    assert claim.reason == "failed_reclaimed"


def test_dedup_claim_concorrente_mesmo_message_id_usa_indice_unico(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dedup.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        session = Session()
        try:
            barrier.wait(timeout=5)
            results.append(WhatsAppMessageDedupService(session).claim("wamid.concurrent"))
        except Exception as exc:  # pragma: no cover - assertion reports exact unexpected exception
            errors.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert sum(result.should_process for result in results) == 1
    assert sorted(result.reason for result in results) == ["claimed", "processing"]


def test_webhook_sem_message_id_preserva_comportamento_e_nao_cria_id_vazio(client, db_session, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()

    response = TestClient(client).post(
        "/webhook/whatsapp",
        json=_webhook_payload("novo pedido", message_id=None),
    )

    assert response.status_code == 200
    assert response.json()["messages"]
    assert db_session.query(WhatsAppProcessedMessage).count() == 0


def _concorrencia_realista_app(tmp_path):
    db_path = tmp_path / "concorrencia_realista.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    ensure_alugueres_contentor_schema(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    app = FastAPI(title="olt-concorrencia-realista-test")
    app.include_router(webhook.router)
    session_ids = []
    bind_urls = []

    def override_get_db():
        session = SessionLocal()
        session_ids.append(id(session))
        bind_urls.append(str(session.get_bind().url))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return app, SessionLocal, db_path, session_ids, bind_urls


def _criar_pedido_recolha_em_session(SessionLocal, *, nome="Cliente Concorrencia", adesivo_base=700):
    with SessionLocal() as session:
        pedido = _pedido_para_recolha(PedidoService(session), nome=nome, adesivo_base=adesivo_base)
        pedido_id = pedido.id
    return pedido_id


def _pausar_primeiro_start_recolha(monkeypatch):
    first_reached = threading.Event()
    release_first = threading.Event()
    state = {"paused": False}
    original_start_recolha = PedidoV24Agent.start_recolha

    def synchronized_start_recolha(self, conversa):
        if not state["paused"]:
            state["paused"] = True
            first_reached.set()
            assert release_first.wait(timeout=5), "primeira requisicao nao foi liberada"
        return original_start_recolha(self, conversa)

    monkeypatch.setattr(PedidoV24Agent, "start_recolha", synchronized_start_recolha)
    return first_reached, release_first


def _post_webhook(app, payload, sink, label, done_event=None):
    try:
        response = TestClient(app).post("/webhook/whatsapp", json=payload)
        sink[label] = {"status_code": response.status_code, "json": response.json()}
    except Exception as exc:  # pragma: no cover - assertion reports exact unexpected exception
        sink[label] = {"error": exc}
    finally:
        if done_event is not None:
            done_event.set()


def _assert_concorrencia_schema_visivel(SessionLocal):
    with SessionLocal() as session:
        assert session.query(ConversaWhatsApp).count() >= 0
        assert session.query(WhatsAppProcessedMessage).count() >= 0


def _dedup_statuses(session):
    return {
        row.message_id: row.status
        for row in session.query(WhatsAppProcessedMessage).order_by(WhatsAppProcessedMessage.message_id)
    }


def test_concorrencia_realista_mesmo_telefone_3_e_1_sessoes_independentes(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    app, SessionLocal, db_path, session_ids, bind_urls = _concorrencia_realista_app(tmp_path)
    _criar_pedido_recolha_em_session(SessionLocal)
    first_reached, release_first = _pausar_primeiro_start_recolha(monkeypatch)
    results = {}
    second_done = threading.Event()

    t1 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("3", message_id="wamid.realista.3"), results, "primeira"),
    )
    t1.start()
    assert first_reached.wait(timeout=5)
    t2 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("1", message_id="wamid.realista.1"), results, "segunda", second_done),
    )
    t2.start()
    assert second_done.wait(timeout=5)
    release_first.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "error" not in results.get("primeira", {})
    assert "error" not in results.get("segunda", {})
    assert {results["primeira"]["status_code"], results["segunda"]["status_code"]} == {200}
    assert len(set(session_ids)) >= 2
    assert len(set(bind_urls)) == 1
    assert str(db_path) in bind_urls[0]
    _assert_concorrencia_schema_visivel(SessionLocal)

    with SessionLocal() as session:
        conversas = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).all()
        assert len(conversas) == 1
        conversa = conversas[0]
        assert conversa.estado_atual == "v24_recolha_pedido"
        assert conversa.contexto_json["ids"]
        assert "pedido_id" not in conversa.contexto_json
        statuses = _dedup_statuses(session)
        assert statuses["wamid.realista.3"] == STATUS_COMPLETED
        assert statuses["wamid.realista.1"] == STATUS_COMPLETED

    primeira_body = results["primeira"]["json"]["messages"][0]["body"]
    segunda_body = results["segunda"]["json"]["messages"][0]["body"]
    assert "Selecione o pedido para recolha" in primeira_body
    assert "tipo de solicitacao" in PedidoV24Agent(None)._norm(segunda_body)

    with SessionLocal() as session:
        router = WhatsappRouterAgent(session)
        response = router.handle(_msg("1", message_id="m-continuar-realista"))
        conversa = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        assert conversa.estado_atual == "v24_recolha_ativo"
        assert "Selecione o ativo" in response


def test_concorrencia_realista_duas_mensagens_3_mesmo_telefone(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    app, SessionLocal, db_path, session_ids, bind_urls = _concorrencia_realista_app(tmp_path)
    _criar_pedido_recolha_em_session(SessionLocal, adesivo_base=800)
    first_reached, release_first = _pausar_primeiro_start_recolha(monkeypatch)
    results = {}
    second_done = threading.Event()

    t1 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("3", message_id="wamid.duplo3.a"), results, "primeira"),
    )
    t1.start()
    assert first_reached.wait(timeout=5)
    t2 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("3", message_id="wamid.duplo3.b"), results, "segunda", second_done),
    )
    t2.start()
    assert second_done.wait(timeout=5)
    release_first.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "error" not in results.get("primeira", {})
    assert "error" not in results.get("segunda", {})
    assert len(set(session_ids)) >= 2
    assert len(set(bind_urls)) == 1
    assert str(db_path) in bind_urls[0]

    bodies = [results[label]["json"]["messages"][0]["body"] for label in ("primeira", "segunda")]
    assert all("Selecione o pedido para recolha" in body for body in bodies)

    with SessionLocal() as session:
        conversa = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        assert conversa.estado_atual == "v24_recolha_pedido"
        assert conversa.contexto_json["ids"]
        statuses = _dedup_statuses(session)
        assert statuses["wamid.duplo3.a"] == STATUS_COMPLETED
        assert statuses["wamid.duplo3.b"] == STATUS_COMPLETED
        router = WhatsappRouterAgent(session)
        response = router.handle(_msg("1", message_id="m-selecionar-duplo3"))
        session.refresh(conversa)
        assert conversa.estado_atual == "v24_recolha_ativo"
        assert "Selecione o ativo" in response


def test_concorrencia_realista_telefones_diferentes_nao_se_misturam(tmp_path, monkeypatch):
    _liberar_operadores(monkeypatch)
    monkeypatch.setenv("ENV", "test")
    get_settings.cache_clear()
    app, SessionLocal, db_path, session_ids, bind_urls = _concorrencia_realista_app(tmp_path)
    _criar_pedido_recolha_em_session(SessionLocal, adesivo_base=900)
    first_reached, release_first = _pausar_primeiro_start_recolha(monkeypatch)
    other_phone = "351900009901"
    results = {}
    second_done = threading.Event()

    t1 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("3", phone=PHONE, message_id="wamid.telefone.a"), results, "primeira"),
    )
    t1.start()
    assert first_reached.wait(timeout=5)
    t2 = threading.Thread(
        target=_post_webhook,
        args=(app, _webhook_payload("3", phone=other_phone, message_id="wamid.telefone.b"), results, "segunda", second_done),
    )
    t2.start()
    assert second_done.wait(timeout=5)
    release_first.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "error" not in results.get("primeira", {})
    assert "error" not in results.get("segunda", {})
    assert len(set(session_ids)) >= 2
    assert len(set(bind_urls)) == 1
    assert str(db_path) in bind_urls[0]

    with SessionLocal() as session:
        conversa_a = session.query(ConversaWhatsApp).filter_by(telefone=PHONE).one()
        conversa_b = session.query(ConversaWhatsApp).filter_by(telefone=other_phone).one()
        assert conversa_a.estado_atual == "v24_recolha_pedido"
        assert conversa_b.estado_atual == "v24_recolha_pedido"
        assert conversa_a.contexto_json["ids"] == conversa_b.contexto_json["ids"]
        statuses = _dedup_statuses(session)
        assert statuses["wamid.telefone.a"] == STATUS_COMPLETED
        assert statuses["wamid.telefone.b"] == STATUS_COMPLETED


def test_pedido_legado_com_campos_nulos_nao_quebra_listagens_e_correcao(db_session):
    pedido = Pedido(
        nome_cliente="Cliente Legado",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="150",
        status_pagamento=StatusPagamento.PENDENTE.value,
        forma_pagamento=None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua antiga",
        ponto_referencia=None,
        contentores=[
            PedidoContentor(
                tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
                residuo_contratado="Entulho Limpo",
                horario_agendado=None,
                precisa_mao_de_obra=False,
                despejo_feito_por=None,
                despejo_data_hora=None,
            )
        ],
    )
    db_session.add(pedido)
    db_session.commit()
    service = PedidoService(db_session)
    agent = PedidoV24Agent(db_session)

    assert service.pedidos_pendentes_entrega()[0].id == pedido.id

    service.confirmar_entrega_lote(
        pedido.id,
        "motorista",
        38.7,
        -9.1,
        None,
        [{"contentor_id": pedido.contentores[0].id, "numero_adesivo": "88", "fotos": ["foto-entrega"]}],
    )
    assert service.pedidos_para_recolha()[0].id == pedido.id

    service.confirmar_recolha(pedido.contentores[0].id, "motorista", False, None, ["foto-recolha"])
    assert service.pedidos_para_despejo()[0].id == pedido.id

    ctx = _correcao_ctx(tipo=TipoEquipamentoPedido.CONTENTOR.value, pago=False)
    ctx["forma"] = None
    ctx["referencia"] = None
    prompt = agent._corrigir_prompt(ctx)

    assert "Status do pagamento" in prompt
    assert "Forma de pagamento" not in prompt
    assert "ponto de referencia" in agent._norm(prompt)
