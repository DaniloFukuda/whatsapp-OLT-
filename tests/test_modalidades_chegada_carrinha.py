"""FEATURE_CARRINHAS_ENABLED aplicada à chegada operacional de Carrinha."""

from datetime import datetime, timezone

import pytest

import app.core.config as config_module
from app.agents.pedido_v24.modality import resolve_operational_modality
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import Settings, get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusEntregaPedido,
    StatusOperacionalCarrinha,
    TipoEquipamentoPedido,
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
        telefone="351900039900",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto=None, *, tipo="text", media=None, latitude=None, longitude=None):
    return NormalizedWhatsAppMessage(
        telefone="351900039900",
        tipo=tipo,
        texto=texto,
        media_id=media,
        latitude=latitude,
        longitude=longitude,
        message_id=media or "modalidades-chegada-carrinha",
    )


def criar_pedido(db_session, tipo):
    comuns = dict(
        nome_cliente=f"Cliente {tipo.title()}",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Operacional",
        ponto_referencia=None,
    )
    if tipo == TipoEquipamentoPedido.CONTENTOR.value:
        return PedidoService(db_session).criar(
            **comuns,
            residuos=["Entulho Limpo"],
        )
    return PedidoService(db_session).criar(
        **comuns,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "14:00",
                "precisa_mao_de_obra": False,
            }
        ],
    )


def concluir_operacao(agente, atual, *, numero):
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
                "numero_adesivo": "707",
                "fotos": ["foto-707"],
            }
        ],
        "latitude": 38.7,
        "longitude": -9.1,
        "referencia_entrega": "Portão",
    }


@pytest.fixture
def carrinha_bloqueada(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    configurar(monkeypatch, carrinhas=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_confirmacao",
        contexto=contexto_preparado(pedido),
    )
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    item = pedido.contentores[0]
    db_session.refresh(item)
    return pedido, item, atual, resposta


def test_carrinha_on_aparece_na_opcao_2(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)

    resposta = PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert pedido.nome_cliente in resposta
    assert "Carrinha" in resposta


def test_carrinha_on_chegada_completa_funciona(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)

    resposta = concluir_operacao(
        PedidoV24Agent(db_session),
        conversa(db_session),
        numero="77",
    )

    item = pedido.contentores[0]
    db_session.refresh(item)
    assert "Chegada da carrinha confirmada" in resposta
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value


def test_carrinha_off_nao_aparece_na_opcao_2(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    configurar(monkeypatch, carrinhas=False)

    resposta = PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert pedido.nome_cliente not in resposta
    assert "não existem pedidos pendentes" in resposta.lower()


def test_carrinha_antiga_pendente_e_bloqueada(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    configurar(monkeypatch, carrinhas=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_pedido",
        contexto={"ids": [pedido.id]},
    )

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem(str(pedido.id)))

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_carrinha_off_nao_muda_status_operacional(carrinha_bloqueada):
    _, item, _, _ = carrinha_bloqueada
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value


def test_carrinha_off_nao_persiste_frota(carrinha_bloqueada):
    _, item, _, _ = carrinha_bloqueada
    assert item.frota_carrinha is None


def test_carrinha_off_nao_persiste_foto(db_session, carrinha_bloqueada):
    assert db_session.query(ContentorFoto).count() == 0


def test_carrinha_off_nao_persiste_gps(carrinha_bloqueada):
    _, item, _, _ = carrinha_bloqueada
    assert item.chegada_carrinha_latitude is None
    assert item.chegada_carrinha_longitude is None


def test_carrinha_off_nao_persiste_partida_prevista(carrinha_bloqueada):
    _, item, _, _ = carrinha_bloqueada
    assert item.partida_prevista_carrinha_data_hora is None


def test_carrinha_off_nao_altera_campos_legados(carrinha_bloqueada):
    _, item, _, _ = carrinha_bloqueada
    assert item.status_entrega == StatusEntregaPedido.PENDENTE.value
    assert item.entrega_feita_por is None
    assert item.entrega_latitude is None
    assert item.entrega_longitude is None
    assert item.entrega_data_hora is None


def test_contexto_residual_de_carrinha_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    configurar(monkeypatch, carrinhas=False)
    item = pedido.contentores[0]
    atual = conversa(
        db_session,
        estado="v24_entrega_adesivo",
        contexto={
            "pedido_id": pedido.id,
            "contentores": [item.id],
            "indice": 0,
            "entregas": [],
        },
    )

    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("808"))

    assert "não está habilitada" in resposta.lower()
    assert atual.estado_atual == "idle"
    assert item.frota_carrinha is None


def test_handler_direto_nao_confirma_carrinha(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    configurar(monkeypatch, carrinhas=False)
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
    assert pedido.contentores[0].status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value


def test_contentor_on_carrinha_off_contentor_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CONTENTOR.value)

    concluir_operacao(PedidoV24Agent(db_session), conversa(db_session), numero="101")

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.ENTREGUE.value


def test_contentor_off_carrinha_on_carrinha_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)

    concluir_operacao(PedidoV24Agent(db_session), conversa(db_session), numero="88")

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value


def test_off_off_opcao_2_nao_inicia_operacao(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_entrega(atual)

    assert "não há modalidade operacional habilitada" in resposta.lower()
    assert "selecione" not in resposta.lower()


def test_off_off_contexto_termina_seguro(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(
        db_session,
        estado="v24_entrega_adesivo",
        contexto={"ids": [99], "tipo": "residual"},
    )

    PedidoV24Agent(db_session).start_entrega(atual)

    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_defaults_on_on_preservam_comportamento_anterior(db_session, monkeypatch):
    configurar(monkeypatch)
    contentor = criar_pedido(db_session, TipoEquipamentoPedido.CONTENTOR.value)
    carrinha = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_entrega(atual)

    assert contentor.nome_cliente in resposta
    assert carrinha.nome_cliente in resposta
    assert atual.contexto_json["ids"] == [contentor.id, carrinha.id]
    assert atual.contexto_json["operational_options"] == [
        {
            "pedido_id": contentor.id,
            "tipos_equipamento": [TipoEquipamentoPedido.CONTENTOR.value],
        },
        {
            "pedido_id": carrinha.id,
            "tipos_equipamento": [TipoEquipamentoPedido.CARRINHA.value],
        },
    ]


def test_pedido_misto_tem_uma_opcao_explicitamente_ambigua(db_session, monkeypatch):
    configurar(monkeypatch)
    pedido = Pedido(
        nome_cliente="Cliente Misto",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        status_pagamento="PAGO",
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua Operacional",
        ponto_referencia=None,
        contentores=[
            PedidoContentor(
                tipo_equipamento=TipoEquipamentoPedido.CONTENTOR.value,
                residuo_contratado="Entulho Limpo",
            ),
            PedidoContentor(
                tipo_equipamento=TipoEquipamentoPedido.CARRINHA.value,
                residuo_contratado="Entulho Limpo",
                horario_agendado="14:00",
            ),
        ],
    )
    db_session.add(pedido)
    db_session.commit()
    atual = conversa(db_session)

    PedidoV24Agent(db_session).start_entrega(atual)

    assert atual.contexto_json["ids"] == [pedido.id]
    assert atual.contexto_json["operational_options"] == [
        {
            "pedido_id": pedido.id,
            "tipos_equipamento": [
                TipoEquipamentoPedido.CARRINHA.value,
                TipoEquipamentoPedido.CONTENTOR.value,
            ],
        }
    ]
    assert resolve_operational_modality(atual.contexto_json, pedido.id) is None


def test_reabilitar_carrinha_retorna_fluxo_sem_modificar_registro(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_pedido(db_session, TipoEquipamentoPedido.CARRINHA.value)
    item = pedido.contentores[0]
    configurar(monkeypatch, carrinhas=False)
    atual = conversa(db_session)
    PedidoV24Agent(db_session).start_entrega(atual)

    configurar(monkeypatch, carrinhas=True)
    resposta = PedidoV24Agent(db_session).start_entrega(atual)

    db_session.refresh(item)
    assert pedido.nome_cliente in resposta
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
    assert item.frota_carrinha is None
    assert db_session.query(ContentorFoto).count() == 0


def test_desligar_carrinha_nao_altera_feature_avarias(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=False, avarias=True)

    PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert get_settings().feature_avarias_enabled is True


def test_desligar_carrinha_nao_altera_configuracao_contentor(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)

    PedidoV24Agent(db_session).start_entrega(conversa(db_session))

    assert get_settings().feature_contentores_enabled is True
