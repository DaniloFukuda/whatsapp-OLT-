from app.agents.whatsapp_router_agent import WhatsappRouterAgent

from tests.system.helpers.conversation_driver import ConversationDriver
from tests.system.helpers.fake_whatsapp_user import FakeWhatsAppUser
from tests.system.helpers.scenario_runner import ScenarioRunner


def _driver(system_app, phone, scenario_id):
    app, SessionLocal, _session_ids = system_app
    return ConversationDriver(app, SessionLocal, FakeWhatsAppUser(phone, scenario_id))


def _assert_common_invariants(db_assertions, fake_meta):
    assert fake_meta.external_attempts == []
    db_assertions.assert_quick_check_ok()
    db_assertions.assert_no_active_queue()


def test_sys_001_menu_do_gestor_autorizado(system_app, gestor, fake_meta, db_assertions):
    driver = _driver(system_app, gestor, "SYS-001")

    response = driver.send_text("menu")

    assert response.status_code == 200
    assert len(fake_meta.sent) == 1
    body = fake_meta.last_body()
    for expected in ["Novo pedido", "Entrega", "Recolha", "Despejo", "Resumo"]:
        assert expected in body
    db_assertions.assert_pedido_count(0)
    db_assertions.assert_contentor_count(0)
    db_assertions.assert_conversation(gestor, "idle", {})
    db_assertions.assert_all_dedup_completed()
    _assert_common_invariants(db_assertions, fake_meta)


def test_sys_003_telefone_nao_autorizado_bloqueado(system_app, gestor, unauthorized_phone, fake_meta, db_assertions):
    assert gestor != unauthorized_phone
    driver = _driver(system_app, unauthorized_phone, "SYS-003")

    response = driver.send_text("1")

    assert response.status_code == 200
    assert len(fake_meta.sent) == 1
    assert "Telefone" in fake_meta.last_body()
    assert "autorizado" in fake_meta.last_body()
    db_assertions.assert_pedido_count(0)
    db_assertions.assert_contentor_count(0)
    db_assertions.assert_conversation(unauthorized_phone, "idle", {})
    db_assertions.assert_all_dedup_completed()
    _assert_common_invariants(db_assertions, fake_meta)


def test_sys_005_cancelamento_durante_cadastro_de_contentor(system_app, gestor, fake_meta, db_assertions):
    driver = _driver(system_app, gestor, "SYS-005")
    runner = ScenarioRunner("SYS-005", "Cancelamento durante cadastro de contentor")
    runner.add_step("abre cadastro", lambda: driver.send_text("1"), lambda r: r.status_code == 200)
    runner.add_step("seleciona contentor", lambda: driver.send_text("1"))
    runner.add_step("informa cliente", lambda: driver.send_text("Cliente SYS 005"))
    runner.add_step("cancela", lambda: driver.send_text("cancelar"))

    runner.run()

    assert any("cancelada" in message["body"].lower() for message in fake_meta.sent)
    assert "Menu principal" in fake_meta.last_body()
    db_assertions.assert_pedido_count(0)
    db_assertions.assert_contentor_count(0)
    db_assertions.assert_conversation(gestor, "idle", {})
    db_assertions.assert_all_dedup_completed()
    _assert_common_invariants(db_assertions, fake_meta)


def test_sys_006_cadastro_completo_de_um_contentor_pago(system_app, gestor, fake_meta, db_assertions):
    driver = _driver(system_app, gestor, "SYS-006")
    steps = [
        ("1", "v24_cadastro_tipo_solicitacao", "tipo"),
        ("1", "v24_cadastro_nome", "nome"),
        ("Cliente SYS 006", "v24_cadastro_telefone", "telefone"),
        ("351999100006", "v24_cadastro_quantidade", "Quantos contentores"),
        ("1", "v24_cadastro_mao_obra", "pessoal"),
        ("2", "v24_cadastro_residuo", "Res"),
        ("1", "v24_cadastro_data", "Quando"),
        ("3", "v24_cadastro_data_manual", "DD/MM/AAAA"),
        ("31/12/2030", "v24_cadastro_valor", "valor"),
        ("100", "v24_cadastro_pago", "pago"),
        ("1", "v24_cadastro_forma", "forma"),
        ("2", "v24_cadastro_endereco", "endere"),
        ("Rua Ficticia SYS 006, 100", "v24_cadastro_referencia_opcao", "refer"),
        ("2", "v24_cadastro_confirmacao", "Confirme"),
        ("1", "idle", "Pedido #"),
    ]

    for text, expected_state, expected_text in steps:
        response = driver.send_text(text)
        assert response.status_code == 200
        assert driver.current_state() == expected_state
        bodies = [message["body"] for message in response.json()["messages"]]
        assert any(expected_text.lower() in body.lower() for body in bodies)
        assert isinstance(driver.context(), dict)

    db_assertions.assert_pedido_count(1)
    db_assertions.assert_contentor_count(1)
    db_assertions.assert_single_paid_contentor_order(
        cliente="Cliente SYS 006",
        telefone="351999100006",
        forma="Transferência",
        residuo="Entulho Limpo",
    )
    db_assertions.assert_conversation(gestor, "idle", {})
    db_assertions.assert_all_dedup_completed()
    _assert_common_invariants(db_assertions, fake_meta)


def test_sys_046_mesmo_message_id_processado_uma_vez(system_app, gestor, fake_meta, db_assertions, monkeypatch):
    calls = []
    original = WhatsappRouterAgent.handle

    def wrapped_handle(self, message):
        calls.append(message.message_id)
        return original(self, message)

    monkeypatch.setattr(WhatsappRouterAgent, "handle", wrapped_handle)
    driver = _driver(system_app, gestor, "SYS-046")

    first = driver.send_text("menu")
    second = driver.repeat_last_message_id("menu")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == ["wamid.test.sys046.001"]
    assert len(fake_meta.sent) == 1
    db_assertions.assert_pedido_count(0)
    db_assertions.assert_contentor_count(0)
    db_assertions.assert_dedup_completed("wamid.test.sys046.001")
    db_assertions.assert_conversation(gestor, "idle", {})
    _assert_common_invariants(db_assertions, fake_meta)
