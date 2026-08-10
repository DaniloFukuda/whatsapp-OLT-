"""FEATURE_CONTENTORES_ENABLED aplicada à entrega operacional de Contentor."""

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
        telefone="351900029900",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto=None, *, tipo="text", media=None, latitude=None, longitude=None):
    return NormalizedWhatsAppMessage(
        telefone="351900029900",
        tipo=tipo,
        texto=texto,
        media_id=media,
        latitude=latitude,
        longitude=longitude,
        message_id=media or "modalidades-entrega-contentor",
    )


def criar_contentor(db_session, *, pago=True):
    return PedidoService(db_session).criar(
        nome_cliente="Cliente Contentor Antigo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=pago,
        forma_pagamento="MBWay" if pago else None,
        pedido_feito_por="gestor",
        endereco_aproximado="Rua do Contentor",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )


def criar_carrinha(db_session):
    return PedidoService(db_session).criar(
        nome_cliente="Cliente Carrinha",
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


def concluir_entrega(agente, atual, *, numero="101"):
    agente.start_entrega(atual)
    agente.handle(atual, mensagem("1"))
    agente.handle(atual, mensagem(numero))
    agente.handle(atual, mensagem(tipo="image", media=f"foto-{numero}"))
    agente.handle(atual, mensagem("2"))
    agente.handle(
        atual,
        mensagem(tipo="location", latitude=38.7, longitude=-9.1),
    )
    agente.handle(atual, mensagem("Não"))
    return agente.handle(atual, mensagem("1"))


def contexto_preparado(pedido):
    item = pedido.contentores[0]
    return {
        "pedido_id": pedido.id,
        "contentores": [item.id],
        "indice": 1,
        "entregas": [
            {
                "contentor_id": item.id,
                "numero_adesivo": "909",
                "fotos": ["foto-909"],
            }
        ],
        "latitude": 38.7,
        "longitude": -9.1,
        "referencia_entrega": None,
    }


def tentar_confirmar_desabilitado(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_confirmacao",
        contexto=contexto_preparado(pedido),
    )
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    db_session.refresh(pedido.contentores[0])
    return pedido, atual, resposta


def test_contentor_on_entrega_disponivel_normalmente(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)

    resposta = PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert pedido.nome_cliente in resposta
    assert "Contentor" in resposta


def test_contentor_on_entrega_completa_segue_fluxo_atual(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    atual = conversa(db_session)

    resposta = concluir_entrega(PedidoV24Agent(db_session), atual)

    db_session.refresh(pedido.contentores[0])
    assert "Entrega confirmada" in resposta
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert pedido.contentores[0].numero_adesivo_contentor == "101"
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.ENTREGA.value).count() == 1


def test_contentor_off_bloqueia_inicio_da_entrega(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    criar_contentor(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_entrega(atual)

    assert "não existem pedidos pendentes" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_contentor_antigo_pendente_tambem_e_bloqueado(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_pedido",
        contexto={"ids": [pedido.id]},
    )

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem(str(pedido.id)))

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_contentor_off_nao_altera_status_entrega(db_session, monkeypatch):
    pedido, _, _ = tentar_confirmar_desabilitado(db_session, monkeypatch)

    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value


def test_contentor_off_nao_persiste_adesivo(db_session, monkeypatch):
    pedido, _, _ = tentar_confirmar_desabilitado(db_session, monkeypatch)

    assert pedido.contentores[0].numero_adesivo_contentor is None


def test_contentor_off_nao_persiste_foto(db_session, monkeypatch):
    tentar_confirmar_desabilitado(db_session, monkeypatch)

    assert db_session.query(ContentorFoto).count() == 0


def test_contentor_off_nao_persiste_gps(db_session, monkeypatch):
    pedido, _, _ = tentar_confirmar_desabilitado(db_session, monkeypatch)

    assert pedido.contentores[0].entrega_latitude is None
    assert pedido.contentores[0].entrega_longitude is None


def test_contexto_residual_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_adesivo",
        contexto={
            "pedido_id": pedido.id,
            "contentores": [pedido.contentores[0].id],
            "indice": 0,
            "entregas": [],
        },
    )

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("808"))

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}
    assert pedido.contentores[0].numero_adesivo_contentor is None


def test_chamada_direta_nao_permite_conclusao(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_confirmacao",
        contexto=contexto_preparado(pedido),
    )

    resposta = PedidoV24Agent(db_session)._confirmar_entrega_preparada(
        atual,
        dict(atual.contexto_json),
    )

    db_session.refresh(pedido.contentores[0])
    assert "não está habilitada" in resposta.lower()
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value
    assert db_session.query(ContentorFoto).count() == 0


@pytest.mark.parametrize("carrinhas_enabled", [True, False])
def test_contentor_off_nao_bloqueia_chegada_de_carrinha(
    db_session,
    monkeypatch,
    carrinhas_enabled,
):
    configurar(
        monkeypatch,
        contentores=False,
        carrinhas=carrinhas_enabled,
    )
    pedido = criar_carrinha(db_session)
    atual = conversa(db_session)

    resposta = concluir_entrega(PedidoV24Agent(db_session), atual, numero="77")

    db_session.refresh(pedido.contentores[0])
    assert "Chegada da carrinha confirmada" in resposta
    assert (
        pedido.contentores[0].status_operacional_carrinha
        == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    )


def test_contentor_on_carrinha_off_mantem_entrega_contentor(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    pedido = criar_contentor(db_session)

    concluir_entrega(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.ENTREGUE.value


def test_desligar_contentor_nao_altera_feature_avarias(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, avarias=True)

    PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert get_settings().feature_avarias_enabled is True


def test_defaults_on_on_preservam_comportamento_anterior(db_session, monkeypatch):
    configurar(monkeypatch)
    pedido = criar_contentor(db_session)

    resposta = PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert pedido.nome_cliente in resposta


def test_reabilitar_contentor_retorna_disponibilidade_sem_alterar_registro(
    db_session,
    monkeypatch,
):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor(db_session)
    item = pedido.contentores[0]
    configurar(monkeypatch, contentores=False)
    PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    configurar(monkeypatch, contentores=True)
    resposta = PedidoV24Agent(db_session).start_entrega(
        db_session.query(ConversaWhatsApp).one()
    )

    db_session.refresh(item)
    assert pedido.nome_cliente in resposta
    assert item.status_entrega == StatusEntregaPedido.PENDENTE.value
    assert item.numero_adesivo_contentor is None
    assert item.entrega_latitude is None
    assert db_session.query(ContentorFoto).count() == 0
