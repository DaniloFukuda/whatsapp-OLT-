"""FEATURE_CONTENTORES_ENABLED aplicada à recolha operacional de Contentor."""

from datetime import datetime, timezone

import pytest

import app.core.config as config_module
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import Settings, get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    StatusEntregaPedido,
    StatusOperacionalCarrinha,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


@pytest.fixture(autouse=True)
def isolated_modalidade_settings(monkeypatch):
    for nome in (
        "FEATURE_CONTENTORES_ENABLED",
        "FEATURE_CARRINHAS_ENABLED",
        "FEATURE_AVARIAS_ENABLED",
    ):
        monkeypatch.delenv(nome, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def configurar(monkeypatch, *, contentores=True, carrinhas=True, avarias=True):
    settings_real = Settings
    monkeypatch.setattr(
        config_module,
        "Settings",
        lambda: settings_real(
            _env_file=None,
            feature_contentores_enabled=contentores,
            feature_carrinhas_enabled=carrinhas,
            feature_avarias_enabled=avarias,
        ),
    )
    get_settings.cache_clear()


def conversa(db_session, *, estado="idle", contexto=None):
    atual = ConversaWhatsApp(
        telefone="351900049900",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto=None, *, tipo="text", media=None):
    return NormalizedWhatsAppMessage(
        telefone="351900049900",
        tipo=tipo,
        texto=texto,
        media_id=media,
        message_id=media or "modalidades-recolha-contentor",
    )


def criar_contentor_entregue(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Contentor Entregue",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua da Recolha",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )
    item = pedido.contentores[0]
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista-entrega",
        38.7,
        -9.1,
        None,
        [{"contentor_id": item.id, "numero_adesivo": "501", "fotos": ["foto-entrega"]}],
    )
    return pedido


def criar_carrinha_em_atendimento(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Carrinha Partida",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": False,
            }
        ],
    )
    item = pedido.contentores[0]
    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "motorista-chegada",
        38.7,
        -9.1,
        None,
        [{"contentor_id": item.id, "numero_adesivo": "77", "fotos": ["foto-chegada"]}],
    )
    db_session.commit()
    return pedido


def concluir_recolha(agente, atual):
    agente.start_recolha(atual)
    agente.handle(atual, mensagem("1"))
    agente.handle(atual, mensagem("1"))
    agente.handle(atual, mensagem(tipo="image", media="foto-recolha"))
    agente.handle(atual, mensagem("2"))
    agente.handle(atual, mensagem("1"))
    return agente.handle(atual, mensagem("1"))


def contexto_preparado(pedido, *, avariado=False, relato=None):
    item = pedido.contentores[0]
    return {
        "pedido_id": pedido.id,
        "contentores": [item.id],
        "contentor_id": item.id,
        "recolhas": [],
        "fotos_recolha": ["foto-recolha-preparada"],
        "avariado": avariado,
        "relato_avaria": relato,
    }


@pytest.fixture
def tentativa_bloqueada(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, avarias=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False, avarias=True)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(
            pedido,
            avariado=True,
            relato="Lateral bastante danificada",
        ),
    )
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    item = pedido.contentores[0]
    db_session.refresh(item)
    return pedido, item, atual, resposta


def test_contentor_on_aparece_para_recolha(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert pedido.nome_cliente in resposta


def test_contentor_on_recolha_completa_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)

    concluir_recolha(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 1


def test_contentor_off_nao_aparece_na_rotina(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert pedido.nome_cliente not in resposta
    assert "não existem equipamentos" in resposta.lower()


def test_contentor_antigo_entregue_fica_bloqueado(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_ativo",
        contexto={
            "pedido_id": pedido.id,
            "contentores": [pedido.contentores[0].id],
            "recolhas": [],
        },
    )

    resposta = PedidoV24Agent(db_session).handle(
        atual,
        mensagem(str(pedido.contentores[0].id)),
    )

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"


def test_contentor_off_status_recolha_nao_muda(tentativa_bloqueada):
    _, item, _, _ = tentativa_bloqueada
    assert item.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert item.recolha_data_hora is None
    assert item.recolha_feita_por is None


def test_contentor_off_nao_persiste_foto_recolha(db_session, tentativa_bloqueada):
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0


@pytest.mark.parametrize("avarias_enabled", [True, False])
def test_contentor_off_nao_cria_avaria(
    db_session,
    monkeypatch,
    avarias_enabled,
):
    configurar(monkeypatch, contentores=True, avarias=avarias_enabled)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False, avarias=avarias_enabled)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(
            pedido,
            avariado=True,
            relato="Avaria que não deve persistir",
        ),
    )

    PedidoV24Agent(db_session).handle(atual, mensagem("1"))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].contentor_avariado is False
    assert pedido.contentores[0].relato_avaria is None


def test_tentativa_nao_modifica_avaria_existente(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, avarias=True)
    pedido = criar_contentor_entregue(db_session)
    item = pedido.contentores[0]
    item.contentor_avariado = True
    item.relato_avaria = "Avaria preexistente preservada"
    item.status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    db_session.commit()
    configurar(monkeypatch, contentores=False, avarias=True)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(pedido, avariado=False),
    )

    PedidoV24Agent(db_session).handle(atual, mensagem("1"))

    db_session.refresh(item)
    assert item.contentor_avariado is True
    assert item.relato_avaria == "Avaria preexistente preservada"
    assert item.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_contexto_residual_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_foto",
        contexto=contexto_preparado(pedido),
    )

    resposta = PedidoV24Agent(db_session).handle(
        atual,
        mensagem(tipo="image", media="foto-bypass"),
    )

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_selecao_direta_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_contentor",
        contexto={"ids": [pedido.contentores[0].id]},
    )

    resposta = PedidoV24Agent(db_session).handle(
        atual,
        mensagem(str(pedido.contentores[0].id)),
    )

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"


def test_confirmacao_direta_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(pedido),
    )

    resposta = PedidoV24Agent(db_session)._confirmar_recolha_atual(
        atual,
        dict(atual.contexto_json),
    )

    db_session.refresh(pedido.contentores[0])
    assert "não está habilitada" in resposta.lower()
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value


def test_tentativa_bloqueada_nao_torna_elegivel_para_despejo(
    db_session,
    tentativa_bloqueada,
):
    assert PedidoService(db_session).contentores_para_despejo() == []


@pytest.mark.parametrize("carrinhas_enabled", [True, False])
def test_contentor_off_nao_bloqueia_partida_de_carrinha(
    db_session,
    monkeypatch,
    carrinhas_enabled,
):
    configurar(
        monkeypatch,
        contentores=False,
        carrinhas=carrinhas_enabled,
    )
    pedido = criar_carrinha_em_atendimento(db_session)

    concluir_recolha(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert (
        pedido.contentores[0].status_operacional_carrinha
        == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    )


def test_feature_avarias_permanece_independente(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, avarias=True)

    PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert get_settings().feature_avarias_enabled is True


def test_defaults_on_on_preservam_comportamento_anterior(db_session, monkeypatch):
    configurar(monkeypatch)
    pedido = criar_contentor_entregue(db_session)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert pedido.nome_cliente in resposta


def test_reabilitar_contentor_devolve_recolha_sem_alterar_registro(
    db_session,
    monkeypatch,
):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    item = pedido.contentores[0]
    configurar(monkeypatch, contentores=False)
    atual = conversa(db_session)
    PedidoV24Agent(db_session).start_recolha(atual)

    configurar(monkeypatch, contentores=True)
    resposta = PedidoV24Agent(db_session).start_recolha(atual)

    db_session.refresh(item)
    assert pedido.nome_cliente in resposta
    assert item.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert item.recolha_data_hora is None


def test_cancelamento_seguro_nao_deixa_mutacao_parcial(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_entregue(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(pedido),
    )

    PedidoV24Agent(db_session).handle(atual, mensagem("2"))

    db_session.refresh(pedido.contentores[0])
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0
