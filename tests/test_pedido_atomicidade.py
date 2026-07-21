from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.db import Base
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import Pedido, PedidoContentor, StatusPagamento, TipoEquipamentoPedido
from app.services.pedido_service import PedidoService
from app.services.webhook_dedup_service import WebhookDedupService


def pedido_kwargs(**overrides):
    values = {
        "nome_cliente": "Cliente Atomico",
        "telefone_cliente": "351900000000",
        "data_planejada": datetime.now(timezone.utc),
        "valor_global": "250.50",
        "pago": False,
        "forma_pagamento": None,
        "pedido_feito_por": "351911000000",
        "endereco_aproximado": "Rua de Teste",
        "ponto_referencia": None,
        "residuos": ["Entulho Limpo"],
    }
    values.update(overrides)
    return values


def contexto_confirmacao(**overrides):
    values = {
        "_confirmado": True,
        "tipo_solicitacao": "CONTENTOR",
        "nome": "Cliente Atomico",
        "telefone": "351900000000",
        "data": datetime.now(timezone.utc).isoformat(),
        "valor": "250.50",
        "pago": False,
        "forma": None,
        "endereco": "Rua de Teste",
        "referencia": None,
        "itens": [{"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"}],
    }
    values.update(overrides)
    return values


def conversa_confirmavel(db_session, **context_overrides):
    contexto = contexto_confirmacao(**context_overrides)
    conversa = ConversaWhatsApp(
        telefone="351911000000",
        estado_atual="v24_cadastro_confirmacao",
        contexto_json=contexto,
    )
    db_session.add(conversa)
    db_session.commit()
    db_session.refresh(conversa)
    return conversa, contexto


def mensagem_claim(message_id):
    return NormalizedWhatsAppMessage(
        telefone="351900000000",
        tipo="text",
        message_id=message_id,
        texto="confirmar",
        raw={"id": message_id, "type": "text", "text": {"body": "confirmar"}},
    )


def executar_confirmacoes_concorrentes(tmp_path, suffix):
    db_path = tmp_path / f"atomicidade-concorrente-{suffix}.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    contexto = contexto_confirmacao()
    claim_ids = (f"wamid.atomic.{suffix}.a", f"wamid.atomic.{suffix}.b")

    with factory() as setup:
        conversa = ConversaWhatsApp(
            telefone="351900000000",
            estado_atual="v24_cadastro_confirmacao",
            contexto_json=contexto,
        )
        setup.add(conversa)
        setup.commit()
        conversa_id = conversa.id
        claims = [WebhookDedupService(setup).adquirir(mensagem_claim(claim_id)) for claim_id in claim_ids]
        assert all(claim.deve_processar for claim in claims)
        assert len({claim.registro.message_id for claim in claims}) == 2

    barrier = threading.Barrier(2)

    class AgentSincronizado(PedidoV24Agent):
        def _reservar_confirmacao(self, conversa):
            barrier.wait(timeout=10)
            return super()._reservar_confirmacao(conversa)

    def worker(claim_id):
        with factory() as session:
            conversa = session.get(ConversaWhatsApp, conversa_id)
            assert conversa is not None
            session.rollback()
            connection = session.connection()
            dbapi_connection_id = id(connection.connection.driver_connection)
            response = AgentSincronizado(session)._finish_cadastro(
                conversa,
                {**contexto, "message_id": claim_id},
            )
            winner = response.startswith("✅ Pedido #")
            reusable = session.execute(text("SELECT 1")).scalar_one() == 1
            conversation_state = session.get(ConversaWhatsApp, conversa_id).estado_atual
            observed_orders = session.query(Pedido).count()
            return {
                "thread_id": threading.get_ident(),
                "claim_id": claim_id,
                "connection_id": dbapi_connection_id,
                "winner": winner,
                "response": response,
                "reusable": reusable,
                "conversation_state": conversation_state,
                "observed_orders": observed_orders,
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, claim_ids))

    with factory() as verification:
        final_conversation = verification.get(ConversaWhatsApp, conversa_id)
        final = {
            "orders": verification.query(Pedido).count(),
            "items": verification.query(PedidoContentor).count(),
            "state": final_conversation.estado_atual,
            "context": final_conversation.contexto_json,
        }
    engine.dispose()
    return results, final, claim_ids, db_path


def test_wrapper_cria_pedido_contentor_compativel(db_session):
    """OLT-ATOMIC-001"""
    pedido = PedidoService(db_session).criar(**pedido_kwargs())

    assert pedido.id is not None
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert len(pedido.contentores) == 1
    assert pedido.contentores[0].tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value
    assert pedido.contentores[0].residuo_contratado == "Entulho Limpo"


def test_wrapper_cria_pedido_carrinha_compativel(db_session):
    """OLT-ATOMIC-002"""
    pedido = PedidoService(db_session).criar(
        **pedido_kwargs(
            residuos=None,
            itens=[
                {
                    "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                    "horario_agendado": "14:30",
                    "residuo_contratado": "Entulho Misto",
                    "precisa_mao_de_obra": True,
                }
            ],
        )
    )

    assert pedido.id is not None
    assert pedido.precisa_mao_de_obra is True
    assert pedido.contentores[0].tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert pedido.contentores[0].horario_agendado == "14:30"


def test_criar_transacional_faz_add_e_flush_sem_commit_ou_refresh(db_session, monkeypatch):
    """OLT-ATOMIC-023"""
    calls = {"add": 0, "flush": 0, "commit": 0, "refresh": 0}
    original_add = db_session.add
    original_flush = db_session.flush

    def add(instance):
        calls["add"] += 1
        return original_add(instance)

    def flush(*args, **kwargs):
        calls["flush"] += 1
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "add", add)
    monkeypatch.setattr(db_session, "flush", flush)
    monkeypatch.setattr(db_session, "commit", lambda: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(db_session, "refresh", lambda *_: calls.__setitem__("refresh", calls["refresh"] + 1))

    pedido = PedidoService(db_session).criar_transacional(
        **pedido_kwargs(residuos=["Entulho Limpo", "Entulho Misto"])
    )

    assert calls == {"add": 1, "flush": 1, "commit": 0, "refresh": 0}
    assert pedido.id is not None
    assert len(pedido.contentores) == 2
    assert all(item.id is not None for item in pedido.contentores)


def test_wrapper_delega_e_faz_um_commit_e_refresh():
    """OLT-ATOMIC-024"""
    db = Mock(spec=Session)
    service = PedidoService(db)
    pedido = Pedido(nome_cliente="Mesmo Pedido")
    service.criar_transacional = Mock(return_value=pedido)
    kwargs = pedido_kwargs()

    resultado = service.criar(**kwargs)

    service.criar_transacional.assert_called_once_with(
        **kwargs,
        itens=None,
        endereco_latitude=None,
        endereco_longitude=None,
        precisa_mao_de_obra=None,
    )
    db.commit.assert_called_once_with()
    db.refresh.assert_called_once_with(pedido)
    assert resultado is pedido


def test_regras_distintas_de_contentor_e_carrinha_permanecem(db_session):
    """OLT-ATOMIC-025"""
    service = PedidoService(db_session)
    contentor = service.criar(**pedido_kwargs())
    carrinha = service.criar(
        **pedido_kwargs(
            residuos=None,
            itens=[{
                "tipo_equipamento": "CARRINHA",
                "horario_agendado": "09:15",
                "residuo_contratado": "Entulho Misto",
            }],
        )
    )

    assert contentor.contentores[0].horario_agendado is None
    assert carrinha.contentores[0].horario_agendado == "09:15"
    with pytest.raises(ValueError, match="combinar contentores e carrinhas"):
        service.criar(
            **pedido_kwargs(
                residuos=None,
                itens=[
                    {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"},
                    {
                        "tipo_equipamento": "CARRINHA",
                        "horario_agendado": "10:00",
                        "residuo_contratado": "Entulho Misto",
                    },
                ],
            )
        )


def test_pagamento_pendente_permanece_correto(db_session):
    """OLT-ATOMIC-026"""
    pedido = PedidoService(db_session).criar(**pedido_kwargs(pago=False, forma_pagamento="Dinheiro"))

    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None


def test_pagamento_realizado_permanece_correto(db_session):
    """OLT-ATOMIC-027"""
    pedido = PedidoService(db_session).criar(**pedido_kwargs(pago=True, forma_pagamento="MBWay"))

    assert pedido.status_pagamento == StatusPagamento.PAGO.value
    assert pedido.forma_pagamento == "MBWay"


def test_referencia_opcional_permanece_correta(db_session):
    """OLT-ATOMIC-028"""
    sem_referencia = PedidoService(db_session).criar(**pedido_kwargs())
    com_referencia = PedidoService(db_session).criar(
        **pedido_kwargs(ponto_referencia="Portao azul")
    )

    assert sem_referencia.ponto_referencia is None
    assert com_referencia.ponto_referencia == "Portao azul"


def test_mao_de_obra_permanece_no_pedido(db_session):
    """OLT-ATOMIC-029"""
    pedido = PedidoService(db_session).criar(
        **pedido_kwargs(
            residuos=None,
            precisa_mao_de_obra=True,
            itens=[{
                "tipo_equipamento": "CARRINHA",
                "horario_agendado": "16:00",
                "residuo_contratado": "Entulho Limpo",
                "precisa_mao_de_obra": True,
            }],
        )
    )

    assert pedido.precisa_mao_de_obra is True
    assert PedidoService(db_session).precisa_mao_de_obra(pedido) is True


def test_multiplos_itens_sao_montados_antes_do_commit_externo(db_session):
    """OLT-ATOMIC-030"""
    pedido = PedidoService(db_session).criar_transacional(
        **pedido_kwargs(residuos=["Entulho Limpo", "Entulho Misto"])
    )

    assert pedido.id is not None
    assert [item.residuo_contratado for item in pedido.contentores] == [
        "Entulho Limpo",
        "Entulho Misto",
    ]
    assert all(item.pedido_id == pedido.id for item in pedido.contentores)
    db_session.rollback()
    assert db_session.query(Pedido).count() == 0


def test_falha_antes_da_criacao_nao_deixa_pedido(db_session, monkeypatch):
    """OLT-ATOMIC-003"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    monkeypatch.setattr(
        agent,
        "_reservar_confirmacao",
        lambda *_: (_ for _ in ()).throw(RuntimeError("antes-da-criacao")),
    )

    with pytest.raises(RuntimeError, match="antes-da-criacao"):
        agent._finish_cadastro(conversa, contexto)

    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "v24_cadastro_confirmacao"


def test_falha_depois_do_flush_remove_pedido_e_itens(db_session, monkeypatch):
    """OLT-ATOMIC-004"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    monkeypatch.setattr(
        agent,
        "_aplicar_idle",
        lambda *_: (_ for _ in ()).throw(RuntimeError("depois-do-flush")),
    )

    with pytest.raises(RuntimeError, match="depois-do-flush"):
        agent._finish_cadastro(conversa, contexto)

    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    assert conversa_db.estado_atual == "v24_cadastro_confirmacao"
    assert conversa_db.contexto_json == contexto


def test_falha_durante_segundo_item_nao_deixa_pedido_parcial(db_session, monkeypatch):
    """OLT-ATOMIC-005"""
    conversa, contexto = conversa_confirmavel(
        db_session,
        itens=[
            {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Limpo"},
            {"tipo_equipamento": "CONTENTOR", "residuo_contratado": "Entulho Misto"},
        ],
    )
    agent = PedidoV24Agent(db_session)
    original = agent.service._criar_item
    calls = 0

    def criar_item(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("segundo-item")
        return original(item)

    monkeypatch.setattr(agent.service, "_criar_item", criar_item)

    with pytest.raises(RuntimeError, match="segundo-item"):
        agent._finish_cadastro(conversa, contexto)

    assert calls == 2
    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "v24_cadastro_confirmacao"


def test_falha_antes_do_commit_executa_rollback_total(db_session, monkeypatch):
    """OLT-ATOMIC-006"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    monkeypatch.setattr(
        db_session,
        "commit",
        lambda: (_ for _ in ()).throw(RuntimeError("commit-indisponivel")),
    )

    with pytest.raises(RuntimeError, match="commit-indisponivel"):
        agent._finish_cadastro(conversa, contexto)

    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "v24_cadastro_confirmacao"


def test_pedido_e_idle_sao_confirmados_no_mesmo_commit(db_session, monkeypatch):
    """OLT-ATOMIC-007"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    commits = 0

    def commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    response = agent._finish_cadastro(conversa, contexto)

    assert commits == 1
    assert "Pedido #" in response
    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    assert conversa_db.estado_atual == "idle"
    assert conversa_db.contexto_json == {}


def test_retry_depois_de_rollback_cria_exatamente_um_pedido(db_session, monkeypatch):
    """OLT-ATOMIC-008"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    original = agent._aplicar_idle
    calls = 0

    def falhar_uma_vez(conversa_atual):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("falha-transitoria")
        return original(conversa_atual)

    monkeypatch.setattr(agent, "_aplicar_idle", falhar_uma_vez)
    with pytest.raises(RuntimeError, match="falha-transitoria"):
        agent._finish_cadastro(conversa, contexto)

    response = agent._finish_cadastro(conversa, contexto)

    assert "Pedido #" in response
    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "idle"


def test_confirmacao_depois_do_sucesso_nao_cria_outro_pedido(db_session):
    """OLT-ATOMIC-009"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    agent.service.criar_transacional = Mock(wraps=agent.service.criar_transacional)

    primeira = agent._finish_cadastro(conversa, contexto)
    segunda = agent._finish_cadastro(conversa, {**contexto, "message_id": "outro-id"})

    assert "Pedido #" in primeira
    assert segunda == "Este pedido já foi confirmado ou está sendo processado."
    assert agent.service.criar_transacional.call_count == 1
    assert db_session.query(Pedido).count() == 1


def test_conversa_so_vai_para_idle_com_negocio_concluido(db_session):
    """OLT-ATOMIC-010"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)

    agent._finish_cadastro(conversa, contexto)

    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    assert conversa_db.estado_atual == "idle"
    assert conversa_db.contexto_json == {}


def test_falha_ao_preparar_idle_impede_conclusao_do_pedido(db_session, monkeypatch):
    """OLT-ATOMIC-011"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    monkeypatch.setattr(
        agent,
        "_aplicar_idle",
        lambda *_: (_ for _ in ()).throw(RuntimeError("idle-indisponivel")),
    )

    with pytest.raises(RuntimeError, match="idle-indisponivel"):
        agent._finish_cadastro(conversa, contexto)

    assert db_session.query(Pedido).count() == 0
    assert db_session.query(PedidoContentor).count() == 0
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    assert conversa_db.estado_atual == "v24_cadastro_confirmacao"
    assert conversa_db.contexto_json == contexto


def test_duas_confirmacoes_sequenciais_criam_um_pedido(db_session):
    """OLT-ATOMIC-015"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)

    agent._finish_cadastro(conversa, contexto)
    resposta = agent._finish_cadastro(conversa, contexto)

    assert resposta == "Este pedido já foi confirmado ou está sendo processado."
    assert db_session.query(Pedido).count() == 1
    assert db_session.query(PedidoContentor).count() == 1


def test_duas_confirmacoes_concorrentes_criam_exatamente_um_pedido(tmp_path):
    """OLT-ATOMIC-016"""
    results, final, claim_ids, db_path = executar_confirmacoes_concorrentes(tmp_path, "016")

    assert db_path.is_file()
    assert len({result["thread_id"] for result in results}) == 2
    assert len({result["connection_id"] for result in results}) == 2
    assert {result["claim_id"] for result in results} == set(claim_ids)
    assert [result["winner"] for result in results].count(True) == 1
    assert [result["winner"] for result in results].count(False) == 1
    loser = next(result for result in results if not result["winner"])
    assert loser["response"] == "Este pedido já foi confirmado ou está sendo processado."
    assert loser["reusable"] is True
    assert loser["conversation_state"] == "idle"
    assert loser["observed_orders"] == 1
    for forbidden in ("conversation_id", "message_id", "payload_hash", "351900000000", "sql", "traceback", "lock"):
        assert forbidden not in loser["response"].lower()
    assert final == {"orders": 1, "items": 1, "state": "idle", "context": {}}


def test_ids_diferentes_no_mesmo_estado_nao_criam_dois_pedidos(tmp_path):
    """OLT-ATOMIC-018"""
    results, final, claim_ids, _ = executar_confirmacoes_concorrentes(tmp_path, "018")

    assert claim_ids[0] != claim_ids[1]
    assert {result["claim_id"] for result in results} == set(claim_ids)
    assert sum(result["winner"] for result in results) == 1
    assert final["orders"] == 1
    assert final["items"] == 1
    assert final["state"] == "idle"
    assert final["context"] == {}


def test_session_permanece_utilizavel_depois_do_rollback(db_session, monkeypatch):
    """OLT-ATOMIC-021"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    monkeypatch.setattr(
        agent,
        "_aplicar_idle",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback")),
    )

    with pytest.raises(RuntimeError, match="rollback"):
        agent._finish_cadastro(conversa, contexto)

    assert db_session.execute(text("SELECT 1")).scalar_one() == 1
    assert db_session.query(Pedido).count() == 0
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "v24_cadastro_confirmacao"


def test_confirmacao_final_executa_um_commit_de_negocio(db_session, monkeypatch):
    """OLT-ATOMIC-022"""
    conversa, contexto = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    commits = 0

    def commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    agent._finish_cadastro(conversa, contexto)

    assert commits == 1
    assert db_session.query(Pedido).count() == 1
    assert db_session.get(ConversaWhatsApp, conversa.id).estado_atual == "idle"


def test_reserva_perdida_nao_cria_pedido(db_session):
    conversa, contexto = conversa_confirmavel(db_session)
    conversa.estado_atual = "idle"
    db_session.commit()
    agent = PedidoV24Agent(db_session)
    agent.service.criar_transacional = Mock(wraps=agent.service.criar_transacional)

    response = agent._finish_cadastro(conversa, contexto)

    assert response == "Este pedido já foi confirmado ou está sendo processado."
    assert agent.service.criar_transacional.call_count == 0
    assert db_session.query(Pedido).count() == 0


def test_aplicar_idle_nao_executa_commit(db_session, monkeypatch):
    conversa, _ = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    commit = Mock()
    monkeypatch.setattr(db_session, "commit", commit)

    agent._aplicar_idle(conversa)

    commit.assert_not_called()
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}


def test_idle_tradicional_mantem_commit_e_limpeza(db_session, monkeypatch):
    conversa, _ = conversa_confirmavel(db_session)
    agent = PedidoV24Agent(db_session)
    original_commit = db_session.commit
    commits = 0

    def commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    response = agent._idle(conversa, "resposta")

    assert response == "resposta"
    assert commits == 1
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}


def test_sqlite_busy_propaga_operational_error_e_preserva_estado(tmp_path):
    db_path = tmp_path / "atomicidade-busy.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    contexto = contexto_confirmacao()

    with factory() as setup:
        conversa = ConversaWhatsApp(
            telefone="351900000000",
            estado_atual="v24_cadastro_confirmacao",
            contexto_json=contexto,
        )
        setup.add(conversa)
        setup.commit()
        conversa_id = conversa.id

    lock_session = factory()
    contender = factory()
    try:
        conversa = contender.get(ConversaWhatsApp, conversa_id)
        contender.expunge(conversa)
        contender.rollback()
        lock_session.execute(
            text(
                "UPDATE conversas_whatsapp "
                "SET estado_atual = 'v24_confirmando' "
                "WHERE id = :id"
            ),
            {"id": conversa_id},
        )

        with pytest.raises(OperationalError, match="locked"):
            PedidoV24Agent(contender)._finish_cadastro(conversa, contexto)

        assert contender.execute(text("SELECT 1")).scalar_one() == 1
        assert contender.query(Pedido).count() == 0
        assert contender.query(PedidoContentor).count() == 0
    finally:
        lock_session.rollback()

    try:
        contender.expire_all()
        assert contender.execute(text("SELECT 1")).scalar_one() == 1
        assert contender.get(ConversaWhatsApp, conversa_id).estado_atual == "v24_cadastro_confirmacao"
        assert contender.query(Pedido).count() == 0
    finally:
        contender.close()
        lock_session.close()
        engine.dispose()
