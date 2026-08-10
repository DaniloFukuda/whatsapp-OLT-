from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import sessionmaker

import app.services.operador_service as operador_service_module
import app.agents.pagamento_pendente_agent as pagamento_agent_module
from app.agents.pagamento_pendente_agent import PagamentoPendenteAgent
from app.agents.whatsapp_router_agent import FORBIDDEN_MESSAGE, MAIN_MENU, WhatsappRouterAgent
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.models.pedido import (
    Pedido, PedidoContentor, StatusOperacionalCarrinha, StatusPagamento,
    StatusResolucaoPedido, TipoEquipamentoPedido,
)
from app.services.pedido_service import PedidoService


GESTOR = "351900020001"
FUNCIONARIO = "351900020002"


def msg(text: str, phone: str = GESTOR) -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=phone, tipo="text", texto=text, message_id=f"pay-{phone}-{text}"
    )


@pytest.fixture(autouse=True)
def sem_fallback(monkeypatch):
    settings = SimpleNamespace(authorized_operator_phone="", authorized_operator_phones="")
    monkeypatch.setattr(operador_service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        pagamento_agent_module,
        "get_settings",
        lambda: SimpleNamespace(timezone="Europe/Lisbon"),
    )


@pytest.fixture
def operadores(db_session):
    gestor = Operador(
        telefone_whatsapp=GESTOR, nome_operador="Gestor Financeiro",
        perfil=PerfilOperador.GESTOR, ativo=True,
    )
    funcionario = Operador(
        telefone_whatsapp=FUNCIONARIO, nome_operador="Motorista",
        perfil=PerfilOperador.FUNCIONARIO, ativo=True,
    )
    db_session.add_all([gestor, funcionario])
    db_session.commit()
    return gestor, funcionario


def criar_pedido(
    db, *, nome="Cliente Teste", valor="300", pago=False,
    tipos=(TipoEquipamentoPedido.CARRINHA.value,), concluido=False,
):
    pedido = Pedido(
        nome_cliente=nome,
        telefone_cliente="351911111111",
        data_planejada=datetime.now(ZoneInfo("Europe/Lisbon")),
        valor_global=Decimal(valor),
        status_pagamento=StatusPagamento.PAGO.value if pago else StatusPagamento.PENDENTE.value,
        forma_pagamento="MBWay" if pago else None,
        pedido_feito_por=GESTOR,
        endereco_aproximado="Local de teste",
        contentores=[
            PedidoContentor(
                tipo_equipamento=tipo,
                horario_agendado="10:00" if tipo == TipoEquipamentoPedido.CARRINHA.value else None,
                residuo_contratado="Entulho Limpo",
                status_operacional_carrinha=(
                    StatusOperacionalCarrinha.CONCLUIDA.value
                    if concluido else StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
                ),
            )
            for tipo in tipos
        ],
    )
    db.add(pedido)
    db.commit()
    db.refresh(pedido)
    return pedido


def iniciar_ate_confirmacao(router, pedido_id, forma="2"):
    assert "Registrar pagamento pendente" in router.handle(msg("pagamentos pendentes"))
    revisao = router.handle(msg(str(pedido_id)))
    assert "Revisão do recebimento" in revisao
    confirmacao = router.handle(msg(forma))
    assert "Confirmar pagamento integral" in confirmacao
    return confirmacao


def test_painel_exibe_acao_so_para_gestor_com_pendencia(db_session, operadores):
    criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    assert "Registrar pagamento pendente" in router.handle(msg("5", GESTOR))
    assert "Registrar pagamento pendente" not in router.handle(msg("5", FUNCIONARIO))
    assert MAIN_MENU.count("Painel de Controle Operacional") == 1


@pytest.mark.parametrize("comando", ["registrar pagamento", "receber pagamento", "pagamentos pendentes"])
def test_comandos_textuais_gestor_iniciam_fluxo(db_session, operadores, comando):
    criar_pedido(db_session)
    assert "Selecione o pedido" in WhatsappRouterAgent(db_session).handle(msg(comando))


@pytest.mark.parametrize("comando", ["registrar pagamento", "receber pagamento", "pagamentos pendentes"])
def test_funcionario_nao_inicia_fluxo_por_comando(db_session, operadores, comando):
    criar_pedido(db_session)
    assert WhatsappRouterAgent(db_session).handle(msg(comando, FUNCIONARIO)) == FORBIDDEN_MESSAGE


def test_lista_apenas_pendentes_positivos_sem_duplicar_e_classifica_tipos(db_session, operadores):
    contentor = criar_pedido(db_session, nome="Contentor", tipos=(TipoEquipamentoPedido.CONTENTOR.value,)*2)
    carrinha = criar_pedido(db_session, nome="Carrinha")
    misto = criar_pedido(
        db_session, nome="Legado Misto",
        tipos=(TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value),
        concluido=True,
    )
    pago = criar_pedido(db_session, nome="Já pago", pago=True)
    criar_pedido(db_session, nome="Sem saldo", valor="0")
    response = WhatsappRouterAgent(db_session).handle(msg("pagamentos pendentes"))
    assert response.count(f"Pedido #{contentor.id}") == 1 and "Contentor • CONTENTOR" in response
    assert response.count(f"Pedido #{carrinha.id}") == 1 and "Carrinha • CARRINHA" in response
    assert f"Pedido #{misto.id}" not in response and "Legado Misto" not in response
    assert f"Pedido #{pago.id}" not in response and "Sem saldo" not in response


def test_revisao_e_confirmacao_exibem_campos_obrigatorios(db_session, operadores):
    pedido = criar_pedido(db_session, nome="Cliente Revisão")
    confirmacao = iniciar_ate_confirmacao(WhatsappRouterAgent(db_session), pedido.id)
    for trecho in (
        f"Pedido: #{pedido.id}", "Cliente: Cliente Revisão", "Tipo: CARRINHA",
        "Valor total: € 300,00", "Valor já pago: € 0,00", "Saldo pendente: € 300,00",
        "Status atual: PENDENTE", "Forma selecionada: MBWay", "saldo integral",
    ):
        assert trecho in confirmacao


@pytest.mark.parametrize(("entrada", "esperada"), [("1", "Dinheiro"), ("2", "MBWay")])
def test_pagamento_confirma_dinheiro_e_mbway(db_session, operadores, entrada, esperada):
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id, entrada)
    response = router.handle(msg("1"))
    db_session.refresh(pedido)
    assert "✅ Pagamento registrado" in response
    assert f"Forma: {esperada}" in response and "Operador: Gestor Financeiro" in response
    assert pedido.status_pagamento == StatusPagamento.PAGO.value
    assert pedido.forma_pagamento == esperada
    assert pedido.pagamento_recebido_em is not None
    assert pedido.pagamento_recebido_por == GESTOR


def test_forma_invalida_cancelar_voltar_e_menu_nao_mutam(db_session, operadores):
    for comando in ("cancelar", "voltar", "menu"):
        pedido = criar_pedido(db_session, nome=f"Cliente {comando}")
        router = WhatsappRouterAgent(db_session)
        router.handle(msg("pagamentos pendentes"))
        router.handle(msg(str(pedido.id)))
        assert "inválida" in router.handle(msg("Criptomoeda"))
        router.handle(msg(comando))
        db_session.refresh(pedido)
        assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
        assert pedido.forma_pagamento is None


def test_trocar_forma_nao_muta_antes_de_confirmar(db_session, operadores):
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id, "1")
    assert "Escolha a nova forma" in router.handle(msg("2"))
    db_session.refresh(pedido)
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value


def test_timeout_nao_muta_e_libera_conversa(db_session, operadores):
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    router.handle(msg("pagamentos pendentes"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=GESTOR).one()
    conversa.contexto_json = {
        **conversa.contexto_json,
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    db_session.commit()
    assert "expirou" in router.handle(msg("1"))
    db_session.refresh(pedido)
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert conversa.estado_atual == "idle"


def test_servico_rejeita_inexistente_pago_operador_inativo_nao_gestor_e_forma_invalida(db_session, operadores):
    gestor, _ = operadores
    service = PedidoService(db_session)
    pendente = criar_pedido(db_session)
    pago = criar_pedido(db_session, pago=True)
    with pytest.raises(ValueError, match="não encontrado"):
        service.registrar_pagamento_pendente(999999, "Dinheiro", GESTOR)
    with pytest.raises(ValueError, match="já está marcado"):
        service.registrar_pagamento_pendente(pago.id, "Dinheiro", GESTOR)
    with pytest.raises(PermissionError, match="não permitida"):
        service.registrar_pagamento_pendente(pendente.id, "Dinheiro", FUNCIONARIO)
    gestor.ativo = False
    db_session.commit()
    with pytest.raises(PermissionError, match="não permitida"):
        service.registrar_pagamento_pendente(pendente.id, "Dinheiro", GESTOR)
    gestor.ativo = True
    db_session.commit()
    with pytest.raises(ValueError, match="inválida"):
        service.registrar_pagamento_pendente(pendente.id, "Bitcoin", GESTOR)


def test_segunda_confirmacao_e_duas_sessoes_rejeitam_duplicidade(db_session, operadores):
    pedido = criar_pedido(db_session)
    SessionLocal = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    first, second = SessionLocal(), SessionLocal()
    try:
        PedidoService(first).registrar_pagamento_pendente(pedido.id, "MBWay", GESTOR)
        with pytest.raises(ValueError, match="já está marcado"):
            PedidoService(second).registrar_pagamento_pendente(pedido.id, "Dinheiro", GESTOR)
    finally:
        first.close()
        second.close()
    db_session.expire_all()
    assert db_session.get(Pedido, pedido.id).forma_pagamento == "MBWay"


def test_retry_da_confirmacao_nao_duplica(db_session, operadores):
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id)
    assert "Pagamento registrado" in router.handle(msg("1"))
    assert "Pagamento registrado" not in router.handle(msg("1"))
    db_session.refresh(pedido)
    assert pedido.status_pagamento == StatusPagamento.PAGO.value


def test_outro_gestor_paga_durante_conversa_e_retomada_revalida(db_session, operadores):
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id)
    PedidoService(db_session).registrar_pagamento_pendente(pedido.id, "Dinheiro", GESTOR)
    assert "já está marcado como pago" in router.handle(msg("1"))
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=GESTOR).one()
    assert conversa.estado_atual == "idle"


def test_pagamento_preserva_valor_operacao_fotos_e_avarias(db_session, operadores):
    pedido = criar_pedido(db_session)
    item = pedido.contentores[0]
    item.contentor_avariado = True
    item.relato_avaria = "Avaria já registrada"
    item.status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    foto = ContentorFoto(
        pedido_contentor_id=item.id, url_foto="midia-teste", url_midia="midia-teste",
        tipo="entrega", tipo_foto="ENTREGA",
    )
    db_session.add(foto)
    db_session.commit()
    snapshot = (
        pedido.valor_global, item.status_operacional_carrinha, item.status_entrega,
        item.status_recolha, item.status_ciclo, item.contentor_avariado,
        item.relato_avaria, item.status_resolucao_avaria,
    )
    PedidoService(db_session).registrar_pagamento_pendente(pedido.id, "MBWay", GESTOR)
    db_session.refresh(item)
    assert snapshot == (
        pedido.valor_global, item.status_operacional_carrinha, item.status_entrega,
        item.status_recolha, item.status_ciclo, item.contentor_avariado,
        item.relato_avaria, item.status_resolucao_avaria,
    )
    assert db_session.query(ContentorFoto).filter_by(id=foto.id).count() == 1


def test_painel_antes_depois_preserva_projetado(db_session, operadores):
    criar_pedido(db_session, nome="Histórico pago", valor="100", pago=True)
    pendente = criar_pedido(db_session, nome="Equivalente 300", valor="300")
    router = WhatsappRouterAgent(db_session)
    before = router._painel_v4(PerfilOperador.GESTOR)
    assert "100,00 €" in before and "300,00 €" in before and "400,00 €" in before
    PedidoService(db_session).registrar_pagamento_pendente(pendente.id, "MBWay", GESTOR)
    after = router._painel_v4(PerfilOperador.GESTOR)
    assert "400,00 €" in after and "0,00 €" in after
    assert pendente not in router.pedido_service.pedidos_pagamento_pendente()
    assert "Equivalente 300" in after  # o pedido continua no painel operacional


def test_rollback_quando_commit_falha(db_session, operadores, monkeypatch):
    pedido = criar_pedido(db_session)
    original_commit = db_session.commit
    monkeypatch.setattr(
        db_session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("falha infra"))
    )
    with pytest.raises(RuntimeError, match="falha infra"):
        PedidoService(db_session).registrar_pagamento_pendente(pedido.id, "MBWay", GESTOR)
    monkeypatch.setattr(db_session, "commit", original_commit)
    db_session.expire_all()
    persistido = db_session.get(Pedido, pedido.id)
    assert persistido.status_pagamento == StatusPagamento.PENDENTE.value
    assert persistido.forma_pagamento is None


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        (datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc), "15/01/2026 12:30"),
        (datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc), "15/07/2026 13:30"),
        (datetime(2026, 7, 15, 12, 30), "15/07/2026 13:30"),
    ],
)
def test_comprovante_converte_utc_inverno_verao_e_sqlite_naive(instante, esperado):
    local = PagamentoPendenteAgent._to_local_datetime(instante)
    assert local.strftime("%d/%m/%Y %H:%M") == esperado
    assert local.tzinfo is not None


def test_comprovante_apos_nova_sessao_preserva_instante_utc_e_hora_local(
    db_session, operadores, monkeypatch
):
    pedido = criar_pedido(db_session)
    instante = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(pagamento_agent_module, "utcnow", lambda: instante)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id)
    resposta = router.handle(msg("1"))
    assert "Data/hora: 15/07/2026 13:30" in resposta

    SessionLocal = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    nova_sessao = SessionLocal()
    try:
        recarregado = nova_sessao.get(Pedido, pedido.id)
        assert recarregado.pagamento_recebido_em == datetime(2026, 7, 15, 12, 30)
        assert PagamentoPendenteAgent._to_local_datetime(
            recarregado.pagamento_recebido_em
        ).strftime("%d/%m/%Y %H:%M") == "15/07/2026 13:30"
    finally:
        nova_sessao.close()


@pytest.mark.parametrize(
    "tipos",
    [
        (TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value),
        (TipoEquipamentoPedido.CONTENTOR.value, "DESCONHECIDO"),
        (),
    ],
)
def test_servico_rejeita_misto_desconhecido_e_sem_itens_sem_mutacao(
    db_session, operadores, tipos
):
    pedido = criar_pedido(db_session, tipos=tipos)
    valor_original = pedido.valor_global
    with pytest.raises(ValueError, match="tipos mistos"):
        PedidoService(db_session).registrar_pagamento_pendente(
            pedido.id, "MBWay", GESTOR
        )
    db_session.refresh(pedido)
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None
    assert pedido.pagamento_recebido_em is None
    assert pedido.pagamento_recebido_por is None
    assert pedido.valor_global == valor_original


def test_comando_direto_nao_seleciona_misto_e_painel_permanece_inalterado(
    db_session, operadores
):
    misto = criar_pedido(
        db_session,
        nome="Misto bloqueado",
        tipos=(TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value),
    )
    router = WhatsappRouterAgent(db_session)
    painel_antes = router._painel_v4(PerfilOperador.GESTOR)
    resposta = router.handle(msg("pagamentos pendentes"))
    assert "Misto bloqueado" not in resposta
    assert f"Pedido #{misto.id}" not in resposta
    assert router._painel_v4(PerfilOperador.GESTOR) == painel_antes
    assert misto.status_pagamento == StatusPagamento.PENDENTE.value


def test_fallback_autorizado_sem_operador_recusa_sem_criar_conversa(
    db_session, monkeypatch
):
    settings = SimpleNamespace(
        authorized_operator_phone=GESTOR, authorized_operator_phones=""
    )
    monkeypatch.setattr(operador_service_module, "get_settings", lambda: settings)
    resposta = WhatsappRouterAgent(db_session).handle(msg("pagamentos pendentes"))
    assert resposta == FORBIDDEN_MESSAGE
    assert db_session.query(ConversaWhatsApp).count() == 0


@pytest.mark.parametrize("remover", [False, True])
def test_operador_desativado_ou_removido_durante_conversa_retorna_estado_seguro(
    db_session, operadores, remover
):
    gestor, _ = operadores
    pedido = criar_pedido(db_session)
    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id)
    if remover:
        db_session.delete(gestor)
    else:
        gestor.ativo = False
    db_session.commit()
    assert router.handle(msg("1")) == FORBIDDEN_MESSAGE
    conversa = db_session.query(ConversaWhatsApp).filter_by(telefone=GESTOR).one()
    db_session.refresh(pedido)
    assert conversa.estado_atual == "idle" and conversa.contexto_json == {}
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None and pedido.pagamento_recebido_em is None


@pytest.mark.parametrize("valor", ["0", "-1"])
def test_servico_rejeita_valor_nao_positivo(db_session, operadores, valor):
    pedido = criar_pedido(db_session, valor=valor)
    with pytest.raises(ValueError, match="saldo pendente"):
        PedidoService(db_session).registrar_pagamento_pendente(
            pedido.id, "Dinheiro", GESTOR
        )


@pytest.mark.parametrize(
    ("entrada", "canonica"),
    [("transferencia", "Transferência"), ("multibanco", "Multibanco")],
)
def test_formas_adicionais_sao_persistidas_canonicamente(
    db_session, operadores, entrada, canonica
):
    pedido = criar_pedido(db_session)
    PedidoService(db_session).registrar_pagamento_pendente(
        pedido.id, entrada, GESTOR
    )
    db_session.refresh(pedido)
    assert pedido.forma_pagamento == canonica


def test_forma_nula_e_pedido_removido_antes_da_confirmacao_sao_seguros(
    db_session, operadores
):
    pedido = criar_pedido(db_session)
    with pytest.raises(ValueError, match="Forma de pagamento inválida"):
        PedidoService(db_session).registrar_pagamento_pendente(pedido.id, None, GESTOR)

    router = WhatsappRouterAgent(db_session)
    iniciar_ate_confirmacao(router, pedido.id)
    db_session.delete(pedido)
    db_session.commit()
    assert "Nenhuma alteração adicional" in router.handle(msg("1"))
