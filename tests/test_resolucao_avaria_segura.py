from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.agents.whatsapp_router_agent import (
    FORBIDDEN_MESSAGE,
    RESOLUCAO_AVARIA_CONTEXT_KEY,
    RESOLUCAO_AVARIA_RELATO_DISPLAY_LIMIT,
    RESOLUCAO_AVARIA_RELATO_TRUNCADO,
    RESOLUCAO_AVARIA_REVISAO_STATE,
    WhatsappRouterAgent,
)
from app.integrations.whatsapp.client import _options_for_body
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, EventoAluguer, StatusResolucao
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusCicloPedido,
    StatusPagamento,
    StatusResolucaoPedido,
)


GESTOR = "351900000700"
FUNCIONARIO = "351900000701"


def mensagem(texto: str, telefone: str = GESTOR, tipo: str = "text"):
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo=tipo,
        texto=texto,
        message_id=f"avaria-{telefone}-{texto}",
    )


@pytest.fixture()
def cenario_avaria(db_session):
    db_session.add_all(
        [
            Operador(
                telefone_whatsapp=GESTOR,
                nome_operador="Gestor Avarias",
                perfil=PerfilOperador.GESTOR,
                ativo=True,
            ),
            Operador(
                telefone_whatsapp=FUNCIONARIO,
                nome_operador="Funcionário",
                perfil=PerfilOperador.FUNCIONARIO,
                ativo=True,
            ),
        ]
    )
    pedido = Pedido(
        id=6,
        nome_cliente="TESTE WORK D7 AVARIA",
        telefone_cliente="351911111111",
        data_planejada=datetime(2026, 7, 20, tzinfo=timezone.utc),
        valor_global=150,
        status_pagamento=StatusPagamento.PENDENTE.value,
        pedido_feito_por=GESTOR,
        endereco_aproximado="Obra D7",
    )
    alvo = PedidoContentor(
        id=7,
        pedido=pedido,
        numero_adesivo_contentor="107",
        residuo_contratado="Entulho Limpo",
        status_entrega="ENTREGUE",
        status_recolha="RECOLHIDO",
        recolha_data_hora=datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc),
        contentor_avariado=True,
        relato_avaria="TESTE WORK D7 - PORTA DO CONTENTOR DANIFICADA PARA VALIDACAO",
        status_resolucao_avaria=StatusResolucaoPedido.PENDENTE.value,
        status_ciclo=StatusCicloPedido.EM_ANDAMENTO.value,
    )
    outro = PedidoContentor(
        id=8,
        pedido=pedido,
        numero_adesivo_contentor="108",
        residuo_contratado="Entulho Misto",
        status_entrega="ENTREGUE",
        status_recolha="RECOLHIDO",
        contentor_avariado=True,
        relato_avaria="RODA DO EQUIPAMENTO COM AVARIA",
        status_resolucao_avaria=StatusResolucaoPedido.PENDENTE.value,
        status_ciclo=StatusCicloPedido.EM_ANDAMENTO.value,
    )
    db_session.add_all([pedido, alvo, outro])
    db_session.commit()
    return pedido, alvo, outro


def conversa_ativa(db_session, estado: str, contexto: dict, telefone: str = GESTOR):
    conversa = ConversaWhatsApp(
        telefone=telefone,
        estado_atual=estado,
        contexto_json=deepcopy(contexto),
    )
    db_session.add(conversa)
    db_session.commit()
    return conversa


@pytest.mark.parametrize(
    "estado",
    ["v24_despejo_pedido", "v24_entrega_foto", "v24_recolha_relato"],
)
def test_comando_global_pausa_fluxos_v24_sem_resolver(
    db_session, cenario_avaria, estado
):
    contexto = {"pedido_id": 6, "contentor_id": 8, "fotos": ["foto-original"]}
    conversa = conversa_ativa(db_session, estado, contexto)

    resposta = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    revisao = conversa.contexto_json["_resolucao_avaria"]
    assert conversa.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE
    assert revisao["estado_anterior"] == estado
    assert revisao["contexto_anterior"] == contexto
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert "fluxo anterior está pausado" in resposta.lower()


def test_tela_exibe_dados_operacionais_sem_confundir_id_interno(
    db_session, cenario_avaria
):
    resposta = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    assert "TESTE WORK D7 AVARIA" in resposta
    assert "Pedido: 6" in resposta
    assert "Equipamento Nº 107" in resposta
    assert "PORTA DO CONTENTOR DANIFICADA" in resposta
    assert "Estado atual: PENDENTE" in resposta
    assert "contentor #7" not in resposta.lower()
    assert "Equipamento Nº 7\n" not in resposta


@pytest.mark.parametrize(
    ("acao", "trecho"),
    [
        ("2", "fluxo anterior foi retomado"),
        ("3", "pendência foi preservada"),
        ("resolucao_avaria:voltar", "fluxo anterior foi retomado"),
        ("resolucao_avaria:cancelar", "pendência foi preservada"),
    ],
)
def test_voltar_e_cancelar_restauram_exatamente_o_fluxo(
    db_session, cenario_avaria, acao, trecho
):
    contexto = {"pedido_id": 6, "despejos": [{"contentor_id": 8}], "nested": {"a": 1}}
    conversa = conversa_ativa(db_session, "v24_despejo_ativo", contexto)
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))

    resposta = router.handle(mensagem(acao, tipo="interactive" if ":" in acao else "text"))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    assert trecho in resposta
    assert conversa.estado_atual == "v24_despejo_ativo"
    assert conversa.contexto_json == contexto
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_confirmacao_resolve_so_alvo_com_auditoria_e_preserva_pedido(
    db_session, cenario_avaria
):
    pedido, alvo, outro = cenario_avaria
    conversa = conversa_ativa(
        db_session, "v24_despejo_pedido", {"ids": [pedido.id]}
    )
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))

    resposta = router.handle(mensagem("1"))

    db_session.refresh(alvo)
    db_session.refresh(outro)
    db_session.refresh(pedido)
    db_session.refresh(conversa)
    assert resposta == "Pendência de avaria do Equipamento Nº 107 resolvida."
    assert alvo.status_resolucao_avaria == StatusResolucaoPedido.RESOLVIDO.value
    assert alvo.avaria_estado_anterior == StatusResolucaoPedido.PENDENTE.value
    assert alvo.avaria_resolvida_em is not None
    assert alvo.avaria_resolvida_por == GESTOR
    assert alvo.relato_avaria.startswith("TESTE WORK D7")
    assert outro.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert alvo.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert conversa.estado_atual == "idle"


@pytest.mark.parametrize(
    "falha_em",
    ["auditoria", "conversa"],
)
def test_falha_na_auditoria_ou_conversa_reverte_resolucao(
    db_session, cenario_avaria, monkeypatch, falha_em
):
    alvo = cenario_avaria[1]
    conversa = conversa_ativa(db_session, "v24_despejo_pedido", {"ids": [6]})
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))

    metodo = (
        "_registrar_auditoria_avaria_atual"
        if falha_em == "auditoria"
        else "_finalizar_conversa_resolucao"
    )
    monkeypatch.setattr(router, metodo, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("falha")))
    resposta = router.handle(mensagem("1"))

    db_session.expire_all()
    alvo_db = db_session.get(PedidoContentor, alvo.id)
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    assert "Nenhuma alteração foi realizada" in resposta
    assert alvo_db.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert alvo_db.avaria_resolvida_em is None
    assert conversa_db.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE


def test_confirmacao_repetida_por_botao_e_idempotente(db_session, cenario_avaria):
    alvo = cenario_avaria[1]
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))
    router.handle(mensagem("resolucao_avaria:confirmar", tipo="interactive"))
    db_session.refresh(alvo)
    resolvida_em = alvo.avaria_resolvida_em

    resposta = router.handle(mensagem("resolucao_avaria:confirmar", tipo="interactive"))

    db_session.refresh(alvo)
    assert "já está resolvida" in resposta
    assert alvo.avaria_resolvida_em == resolvida_em


def test_id_atual_resolvido_nao_faz_fallback_para_legado(
    db_session, cenario_avaria
):
    alvo = cenario_avaria[1]
    alvo.status_resolucao_avaria = StatusResolucaoPedido.RESOLVIDO.value
    legado = AluguerContentor(
        id=7,
        contentor_id=999,
        cliente_id=999,
        telefone_cliente="351922222222",
        nome_cliente="Cliente legado",
        numero_contentor="L-77",
        data_vencimento=datetime(2026, 7, 30, tzinfo=timezone.utc),
        valor=100,
        contentor_avariado=True,
        relato_avaria="AVARIA LEGADA AINDA PENDENTE",
        status_resolucao_avaria=StatusResolucao.PENDENTE.value,
    )
    db_session.add(legado)
    db_session.commit()

    resposta = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    db_session.refresh(legado)
    assert "Equipamento Nº 107 já está resolvida" in resposta
    assert legado.status_resolucao_avaria == StatusResolucao.PENDENTE.value


def test_funcionario_nao_inicia_revisao_nem_altera_fluxo(
    db_session, cenario_avaria
):
    contexto = {"pedido_id": 6}
    conversa = conversa_ativa(
        db_session, "v24_despejo_pedido", contexto, telefone=FUNCIONARIO
    )

    resposta = WhatsappRouterAgent(db_session).handle(
        mensagem("resolver avaria 7", telefone=FUNCIONARIO)
    )

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    assert resposta == FORBIDDEN_MESSAGE
    assert conversa.estado_atual == "v24_despejo_pedido"
    assert conversa.contexto_json == contexto
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_tela_gera_botoes_especificos_e_respostas_digitadas_funcionam(
    db_session, cenario_avaria
):
    router = WhatsappRouterAgent(db_session)
    tela = router.handle(mensagem("resolver avaria 7"))

    assert [opcao["id"] for opcao in _options_for_body(tela)] == [
        "resolucao_avaria:confirmar",
        "resolucao_avaria:voltar",
        "resolucao_avaria:cancelar",
    ]
    resposta = router.handle(mensagem("2"))
    assert "fluxo anterior foi retomado" in resposta


@pytest.mark.parametrize("contexto_invalido", [None, [], "texto", 42, True])
def test_abertura_com_contexto_json_invalido_usa_contexto_anterior_vazio(
    db_session, cenario_avaria, contexto_invalido
):
    conversa = conversa_ativa(db_session, "v24_despejo_pedido", {})
    conversa.contexto_json = contexto_invalido
    db_session.commit()

    WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    revisao = conversa.contexto_json[RESOLUCAO_AVARIA_CONTEXT_KEY]
    assert revisao["contexto_anterior"] == {}
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert cenario_avaria[1].avaria_resolvida_em is None


@pytest.mark.parametrize(
    "contexto_invalido",
    [
        None,
        [],
        "texto",
        42,
        True,
        {},
        {RESOLUCAO_AVARIA_CONTEXT_KEY: {}},
        {RESOLUCAO_AVARIA_CONTEXT_KEY: {"id": 7}},
        {
            RESOLUCAO_AVARIA_CONTEXT_KEY: {
                "id": True,
                "origem": "pedido",
                "estado_anterior": "idle",
                "contexto_anterior": {},
                "gestor": GESTOR,
            }
        },
    ],
)
def test_confirmar_contexto_invalido_nunca_resolve(
    db_session, cenario_avaria, contexto_invalido
):
    conversa = conversa_ativa(
        db_session, RESOLUCAO_AVARIA_REVISAO_STATE, {}, telefone=GESTOR
    )
    conversa.contexto_json = deepcopy(contexto_invalido)
    db_session.commit()

    resposta = WhatsappRouterAgent(db_session).handle(mensagem("1"))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    assert "Revisão inválida" in resposta
    assert conversa.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert cenario_avaria[1].avaria_resolvida_em is None


def test_novo_comando_nao_substitui_revisao_invalida_nem_infere_alvo(
    db_session, cenario_avaria
):
    conversa = conversa_ativa(
        db_session,
        RESOLUCAO_AVARIA_REVISAO_STATE,
        {RESOLUCAO_AVARIA_CONTEXT_KEY: {"id": 8}},
    )

    resposta = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    assert "Revisão inválida" in resposta
    assert conversa.contexto_json == {RESOLUCAO_AVARIA_CONTEXT_KEY: {"id": 8}}
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


@pytest.mark.parametrize("acao", ["2", "3", "resolucao_avaria:voltar", "resolucao_avaria:cancelar"])
@pytest.mark.parametrize(
    "contexto_invalido",
    [None, [], "texto", 42, True, {}, {RESOLUCAO_AVARIA_CONTEXT_KEY: {"id": 7}}],
)
def test_voltar_cancelar_contexto_invalido_recupera_idle_sem_inferir_alvo(
    db_session, cenario_avaria, contexto_invalido, acao
):
    conversa = conversa_ativa(db_session, RESOLUCAO_AVARIA_REVISAO_STATE, {})
    conversa.contexto_json = deepcopy(contexto_invalido)
    db_session.commit()

    resposta = WhatsappRouterAgent(db_session).handle(mensagem(acao))

    db_session.refresh(conversa)
    db_session.refresh(cenario_avaria[1])
    assert resposta == (
        "Revisão inválida cancelada com segurança. Nenhuma avaria foi alterada."
    )
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {}
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


@pytest.mark.parametrize("acao", ["2", "3"])
def test_falha_ao_persistir_voltar_cancelar_mantem_revisao(
    db_session, cenario_avaria, monkeypatch, acao
):
    conversa = conversa_ativa(
        db_session, "v24_despejo_pedido", {"pedido_id": 6}
    )
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))
    contexto_revisao = deepcopy(conversa.contexto_json)

    monkeypatch.setattr(
        db_session,
        "commit",
        lambda: (_ for _ in ()).throw(RuntimeError("falha controlada")),
    )
    resposta = router.handle(mensagem(acao))

    db_session.expire_all()
    conversa_db = db_session.get(ConversaWhatsApp, conversa.id)
    alvo_db = db_session.get(PedidoContentor, 7)
    assert "revisão permanece ativa" in resposta
    assert conversa_db.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE
    assert conversa_db.contexto_json == contexto_revisao
    assert alvo_db.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


@pytest.mark.parametrize("mudanca", ["inativo", "funcionario"])
def test_autorizacao_e_revalidada_ao_confirmar(
    db_session, cenario_avaria, mudanca
):
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))
    operador = db_session.scalar(
        select(Operador).where(Operador.telefone_whatsapp == GESTOR)
    )
    if mudanca == "inativo":
        operador.ativo = False
    else:
        operador.perfil = PerfilOperador.FUNCIONARIO
    db_session.commit()

    resposta = router.handle(mensagem("1"))

    db_session.expire_all()
    alvo = db_session.get(PedidoContentor, 7)
    assert resposta in {FORBIDDEN_MESSAGE, "Telefone não autorizado."}
    assert alvo.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert alvo.avaria_resolvida_em is None


def test_outro_telefone_nao_confirma_revisao_do_gestor(db_session, cenario_avaria):
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))

    resposta = router.handle(
        mensagem(
            "resolucao_avaria:confirmar",
            telefone=FUNCIONARIO,
            tipo="interactive",
        )
    )

    db_session.refresh(cenario_avaria[1])
    assert resposta
    assert "resolvida" not in resposta.lower()
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert cenario_avaria[1].avaria_resolvida_em is None


def test_contexto_de_revisao_inserido_em_conversa_de_funcionario_nao_confirma(
    db_session, cenario_avaria
):
    conversa = conversa_ativa(
        db_session, RESOLUCAO_AVARIA_REVISAO_STATE, {}, telefone=FUNCIONARIO
    )
    conversa.contexto_json = {
        RESOLUCAO_AVARIA_CONTEXT_KEY: {
            "id": 7,
            "origem": "pedido",
            "estado_anterior": "idle",
            "contexto_anterior": {},
            "gestor": FUNCIONARIO,
        }
    }
    db_session.commit()

    resposta = WhatsappRouterAgent(db_session).handle(
        mensagem("1", telefone=FUNCIONARIO)
    )

    db_session.refresh(cenario_avaria[1])
    assert resposta == FORBIDDEN_MESSAGE
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_telefone_do_contexto_divergente_nao_confirma(db_session, cenario_avaria):
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 7"))
    conversa = db_session.scalar(
        select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
    )
    contexto = deepcopy(conversa.contexto_json)
    contexto[RESOLUCAO_AVARIA_CONTEXT_KEY]["gestor"] = FUNCIONARIO
    conversa.contexto_json = contexto
    db_session.commit()

    resposta = router.handle(mensagem("1"))

    db_session.refresh(cenario_avaria[1])
    assert "Revisão inválida" in resposta
    assert cenario_avaria[1].status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert cenario_avaria[1].avaria_resolvida_em is None


def test_duas_sessoes_impedem_segunda_resolucao_atual(
    db_session, cenario_avaria
):
    WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))
    fabrica = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    primeira = fabrica()
    segunda = fabrica()
    try:
        conversa_1 = primeira.scalar(
            select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
        )
        conversa_2 = segunda.scalar(
            select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
        )
        alvo_1 = primeira.get(PedidoContentor, 7)
        alvo_2 = segunda.get(PedidoContentor, 7)
        assert alvo_1.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
        assert alvo_2.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value

        resposta_1 = WhatsappRouterAgent(primeira)._handle_revisao_avaria(
            conversa_1, "1", GESTOR
        )
        primeira.refresh(alvo_1)
        instante = alvo_1.avaria_resolvida_em
        resolvedor = alvo_1.avaria_resolvida_por
        resposta_2 = WhatsappRouterAgent(segunda)._handle_revisao_avaria(
            conversa_2, "1", GESTOR
        )

        segunda.expire_all()
        final = segunda.get(PedidoContentor, 7)
        assert "resolvida" in resposta_1
        assert "já está resolvida" in resposta_2
        assert final.avaria_resolvida_em == instante
        assert final.avaria_resolvida_por == resolvedor == GESTOR
    finally:
        primeira.close()
        segunda.close()


def criar_avaria_legada(db_session, identificador: int = 70):
    legado = AluguerContentor(
        id=identificador,
        contentor_id=999,
        cliente_id=999,
        telefone_cliente="351922222222",
        nome_cliente="Cliente legado",
        numero_contentor="LEG-107",
        data_vencimento=datetime(2026, 7, 30, tzinfo=timezone.utc),
        valor=100,
        recolha_data_hora=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        contentor_avariado=True,
        relato_avaria="AVARIA LEGADA",
        status_resolucao_avaria=StatusResolucao.PENDENTE.value,
    )
    db_session.add(legado)
    db_session.commit()
    return legado


def test_legado_confirma_com_um_evento_e_identificacao_operacional(
    db_session, cenario_avaria
):
    legado = criar_avaria_legada(db_session)
    router = WhatsappRouterAgent(db_session)
    tela = router.handle(mensagem("resolver avaria 70"))
    resposta = router.handle(mensagem("1"))

    db_session.refresh(legado)
    eventos = db_session.scalars(
        select(EventoAluguer).where(
            EventoAluguer.aluguer_id == legado.id,
            EventoAluguer.tipo == "pendencia_avaria_resolvida",
        )
    ).all()
    assert "Equipamento Nº LEG-107" in tela
    assert resposta == "Pendência de avaria do Equipamento Nº LEG-107 resolvida."
    assert legado.status_resolucao_avaria == StatusResolucao.RESOLVIDO.value
    assert len(eventos) == 1


@pytest.mark.parametrize("falha_em", ["evento", "conversa", "commit"])
def test_legado_falha_reverte_status_evento_e_conversa(
    db_session, cenario_avaria, monkeypatch, falha_em
):
    legado = criar_avaria_legada(db_session)
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 70"))
    if falha_em == "commit":
        monkeypatch.setattr(
            db_session,
            "commit",
            lambda: (_ for _ in ()).throw(RuntimeError("falha")),
        )
    else:
        metodo = (
            "_registrar_auditoria_avaria_legada"
            if falha_em == "evento"
            else "_finalizar_conversa_resolucao"
        )
        monkeypatch.setattr(
            router,
            metodo,
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("falha")),
        )

    resposta = router.handle(mensagem("1"))

    db_session.expire_all()
    conversa = db_session.scalar(
        select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
    )
    assert "Nenhuma alteração foi realizada" in resposta
    assert db_session.get(AluguerContentor, legado.id).status_resolucao_avaria == StatusResolucao.PENDENTE.value
    assert conversa.estado_atual == RESOLUCAO_AVARIA_REVISAO_STATE
    assert db_session.scalar(
        select(func.count()).select_from(EventoAluguer).where(
            EventoAluguer.aluguer_id == legado.id
        )
    ) == 0


def test_legado_confirmacao_repetida_nao_cria_segundo_evento(
    db_session, cenario_avaria
):
    legado = criar_avaria_legada(db_session)
    router = WhatsappRouterAgent(db_session)
    router.handle(mensagem("resolver avaria 70"))
    router.handle(mensagem("resolucao_avaria:confirmar", tipo="interactive"))
    resposta = router.handle(mensagem("resolucao_avaria:confirmar", tipo="interactive"))

    assert "já está resolvida" in resposta
    assert db_session.scalar(
        select(func.count()).select_from(EventoAluguer).where(
            EventoAluguer.aluguer_id == legado.id
        )
    ) == 1


def test_duas_sessoes_impedem_segundo_evento_legado(db_session, cenario_avaria):
    legado = criar_avaria_legada(db_session)
    WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 70"))
    fabrica = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    primeira = fabrica()
    segunda = fabrica()
    try:
        conversa_1 = primeira.scalar(
            select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
        )
        conversa_2 = segunda.scalar(
            select(ConversaWhatsApp).where(ConversaWhatsApp.telefone == GESTOR)
        )
        alvo_1 = primeira.get(AluguerContentor, legado.id)
        alvo_2 = segunda.get(AluguerContentor, legado.id)
        assert alvo_1.status_resolucao_avaria == StatusResolucao.PENDENTE.value
        assert alvo_2.status_resolucao_avaria == StatusResolucao.PENDENTE.value

        WhatsappRouterAgent(primeira)._handle_revisao_avaria(conversa_1, "1", GESTOR)
        resposta = WhatsappRouterAgent(segunda)._handle_revisao_avaria(
            conversa_2, "1", GESTOR
        )

        assert "já está resolvida" in resposta
        assert segunda.scalar(
            select(func.count()).select_from(EventoAluguer).where(
                EventoAluguer.aluguer_id == legado.id,
                EventoAluguer.tipo == "pendencia_avaria_resolvida",
            )
        ) == 1
    finally:
        primeira.close()
        segunda.close()


@pytest.mark.parametrize(
    ("relato", "esperado"),
    [
        (None, "Relato: sem relato"),
        ("", "Relato: sem relato"),
        ("curto", "Relato: curto"),
        ("linha 1\nlinha 2 <>& ç", "Relato: linha 1\nlinha 2 <>& ç"),
    ],
)
def test_relato_vazio_curto_e_especial_e_exibido_com_seguranca(
    db_session, cenario_avaria, relato, esperado
):
    alvo = cenario_avaria[1]
    alvo.relato_avaria = relato
    db_session.commit()

    tela = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    assert esperado in tela


def test_relato_longo_e_truncado_so_na_exibicao(db_session, cenario_avaria):
    alvo = cenario_avaria[1]
    relato_integral = ("PORTA\n<danificada>& ç " * 200)
    alvo.relato_avaria = relato_integral
    db_session.commit()

    tela = WhatsappRouterAgent(db_session).handle(mensagem("resolver avaria 7"))

    db_session.refresh(alvo)
    assert RESOLUCAO_AVARIA_RELATO_TRUNCADO in tela
    trecho_exibido = tela.split("Relato: ", 1)[1].split("\nOcorrência:", 1)[0]
    assert len(trecho_exibido) <= RESOLUCAO_AVARIA_RELATO_DISPLAY_LIMIT
    assert alvo.relato_avaria == relato_integral
