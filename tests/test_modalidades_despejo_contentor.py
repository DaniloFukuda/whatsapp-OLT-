"""FEATURE_CONTENTORES_ENABLED aplicada ao despejo operacional de Contentor."""

from datetime import datetime, timezone

import pytest

import app.core.config as config_module
from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import Settings, get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    StatusCicloPedido,
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
        telefone=f"35190005{db_session.query(ConversaWhatsApp).count():04d}",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto=None, *, tipo="text", media=None):
    return NormalizedWhatsAppMessage(
        telefone="351900050000",
        tipo=tipo,
        texto=texto,
        media_id=media,
        message_id=media or "modalidades-despejo-contentor",
    )


def criar_contentor_recolhido(db_session, *, avariado=False):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Contentor Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua do Vazadouro",
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
        [{"contentor_id": item.id, "numero_adesivo": "701", "fotos": ["foto-entrega"]}],
    )
    service.confirmar_recolha(
        item.id,
        "motorista-recolha",
        avariado,
        "Avaria antiga preservada" if avariado else None,
        ["foto-recolha"],
    )
    return pedido


def criar_carrinha_aguardando_despejo(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Carrinha Despejo",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[{
            "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
            "residuo_contratado": "Entulho Limpo",
            "horario_agendado": "14:00",
            "precisa_mao_de_obra": False,
        }],
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
    service.confirmar_partida_carrinha(
        item.id, "motorista-partida", False, None, ["foto-partida"]
    )
    db_session.commit()
    return pedido


def contexto_confirmacao(pedido, *, foto="foto-despejo", divergencia=False):
    item = pedido.contentores[0]
    return {
        "pedido_id": pedido.id,
        "contentores": [item.id],
        "contentor_id": item.id,
        "despejos": [],
        "fotos_despejo": [foto],
        "residuo_contratado": "Entulho Limpo",
        "residuo_efetivo": "Entulho Misto" if divergencia else "Entulho Limpo",
        "residuo_assumido": "Entulho Limpo",
        "carga_errada": divergencia,
        "relato_carga": "Material divergente observado" if divergencia else None,
    }


def snapshot(item, pedido, db_session):
    return {
        "status_ciclo": item.status_ciclo,
        "residuo": item.residuo_efetivo_vazadouro,
        "carga_errada": item.carga_errada,
        "relato": item.relato_carga,
        "resolucao": item.status_resolucao_carga,
        "operador": item.despejo_feito_por,
        "data": item.despejo_data_hora,
        "avariado": item.contentor_avariado,
        "relato_avaria": item.relato_avaria,
        "valor": pedido.valor_global,
        "pagamento": pedido.status_pagamento,
        "forma": pedido.forma_pagamento,
        "recebido_em": pedido.pagamento_recebido_em,
        "recebido_por": pedido.pagamento_recebido_por,
        "fotos": db_session.query(ContentorFoto).filter_by(
            pedido_contentor_id=item.id, tipo_foto=TipoFoto.DESPEJO.value
        ).count(),
    }


def test_contentor_on_aparece_para_despejo(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_recolhido(db_session)
    resposta = PedidoV24Agent(db_session).start_despejo(conversa(db_session))
    assert f"#{pedido.id}" in resposta
    assert "Contentor" in resposta


def test_contentor_on_despejo_completo_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_recolhido(db_session)
    atual = conversa(db_session, estado="v24_despejo_confirmacao", contexto=contexto_confirmacao(pedido))
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    db_session.refresh(pedido.contentores[0])
    assert "processado no vazadouro" in resposta
    assert pedido.contentores[0].status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert pedido.contentores[0].residuo_efetivo_vazadouro == "Entulho Limpo"


def test_contentor_off_nao_aparece_e_antigo_fica_bloqueado(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_recolhido(db_session)
    configurar(monkeypatch, contentores=False)
    atual = conversa(db_session)
    resposta = PedidoV24Agent(db_session).start_despejo(atual)
    assert f"#{pedido.id}" not in resposta
    assert atual.estado_atual == "idle"


@pytest.mark.parametrize("estado", ["v24_despejo_ativo", "v24_despejo_contentor"])
def test_contentor_off_selecao_direta_nao_permite_bypass(db_session, monkeypatch, estado):
    pedido = criar_contentor_recolhido(db_session)
    item = pedido.contentores[0]
    configurar(monkeypatch, contentores=False)
    contexto = {"pedido_id": pedido.id, "ids": [item.id], "contentores": [item.id], "despejos": []}
    atual = conversa(db_session, estado=estado, contexto=contexto)
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem(str(item.id)))
    assert "não está habilitado" in resposta
    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


@pytest.mark.parametrize(
    "estado,texto,tipo,media",
    [
        ("v24_despejo_residuo", "1", "text", None),
        ("v24_despejo_conformidade", "1", "text", None),
        ("v24_despejo_relato", "Relato suficientemente longo", "text", None),
        ("v24_despejo_foto", None, "image", "foto-nova"),
        ("v24_despejo_foto_acao", "2", "text", None),
        ("v24_despejo_confirmacao", "1", "text", None),
    ],
)
def test_contexto_residual_off_bloqueia_todos_handlers(
    db_session, monkeypatch, estado, texto, tipo, media
):
    pedido = criar_contentor_recolhido(db_session)
    antes = snapshot(pedido.contentores[0], pedido, db_session)
    configurar(monkeypatch, contentores=False)
    contexto = contexto_confirmacao(pedido, divergencia=True)
    contexto["residuos_disponiveis"] = ["Entulho Limpo", "Entulho Misto"]
    atual = conversa(db_session, estado=estado, contexto=contexto)
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem(texto, tipo=tipo, media=media))
    db_session.refresh(pedido.contentores[0])
    db_session.refresh(pedido)
    assert "não está habilitado" in resposta
    assert atual.estado_atual == "idle"
    assert snapshot(pedido.contentores[0], pedido, db_session) == antes


@pytest.mark.parametrize("divergencia", [False, True])
def test_ultima_guarda_impede_toda_mutacao_operacional_e_financeira(
    db_session, monkeypatch, divergencia
):
    pedido = criar_contentor_recolhido(db_session, avariado=True)
    item = pedido.contentores[0]
    antes = snapshot(item, pedido, db_session)
    configurar(monkeypatch, contentores=False, avarias=not divergencia)
    atual = conversa(db_session, estado="v24_despejo_confirmacao", contexto=contexto_confirmacao(pedido, divergencia=divergencia))
    resposta = PedidoV24Agent(db_session)._confirmar_despejo_atual(atual, dict(atual.contexto_json))
    db_session.refresh(item)
    db_session.refresh(pedido)
    assert "não está habilitado" in resposta
    assert snapshot(item, pedido, db_session) == antes
    assert atual.estado_atual == "idle"


@pytest.mark.parametrize("carrinhas", [True, False])
def test_carrinha_permanece_operacional_independente_da_propria_flag(
    db_session, monkeypatch, carrinhas
):
    configurar(monkeypatch, contentores=False, carrinhas=carrinhas)
    pedido = criar_carrinha_aguardando_despejo(db_session)
    item = pedido.contentores[0]
    atual = conversa(db_session, estado="v24_despejo_confirmacao", contexto=contexto_confirmacao(pedido))
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    db_session.refresh(item)
    assert "processado no vazadouro" in resposta
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value


@pytest.mark.parametrize("avarias", [True, False])
def test_feature_avarias_permanece_independente_e_avaria_preservada(db_session, monkeypatch, avarias):
    pedido = criar_contentor_recolhido(db_session, avariado=True)
    item = pedido.contentores[0]
    antes = (item.contentor_avariado, item.relato_avaria, item.status_resolucao_avaria)
    configurar(monkeypatch, contentores=False, avarias=avarias)
    atual = conversa(db_session, estado="v24_despejo_confirmacao", contexto=contexto_confirmacao(pedido))
    PedidoV24Agent(db_session).handle(atual, mensagem("1"))
    db_session.refresh(item)
    assert (item.contentor_avariado, item.relato_avaria, item.status_resolucao_avaria) == antes


def test_reabilitar_contentor_reabre_fluxo_sem_modificar_registro(db_session, monkeypatch):
    pedido = criar_contentor_recolhido(db_session)
    item = pedido.contentores[0]
    antes = snapshot(item, pedido, db_session)
    configurar(monkeypatch, contentores=False)
    PedidoV24Agent(db_session).start_despejo(conversa(db_session))
    db_session.refresh(item)
    assert snapshot(item, pedido, db_session) == antes
    configurar(monkeypatch, contentores=True)
    resposta = PedidoV24Agent(db_session).start_despejo(conversa(db_session))
    assert f"#{pedido.id}" in resposta


def test_defaults_on_on_preservam_fluxo_anterior(db_session):
    pedido = criar_contentor_recolhido(db_session)
    assert get_settings().feature_contentores_enabled is True
    assert get_settings().feature_carrinhas_enabled is True
    assert f"#{pedido.id}" in PedidoV24Agent(db_session).start_despejo(conversa(db_session))


def test_cancelamento_retorna_idle_sem_mutacao_parcial(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True)
    pedido = criar_contentor_recolhido(db_session)
    antes = snapshot(pedido.contentores[0], pedido, db_session)
    atual = conversa(db_session, estado="v24_despejo_confirmacao", contexto=contexto_confirmacao(pedido, divergencia=True))
    resposta = PedidoV24Agent(db_session).handle(atual, mensagem("3"))
    db_session.refresh(pedido.contentores[0])
    assert "cancelado" in resposta
    assert atual.estado_atual == "idle"
    assert snapshot(pedido.contentores[0], pedido, db_session) == antes


def test_contentor_off_e_carrinha_on_lista_somente_carrinha(db_session, monkeypatch):
    contentor = criar_contentor_recolhido(db_session)
    carrinha = criar_carrinha_aguardando_despejo(db_session)
    configurar(monkeypatch, contentores=False, carrinhas=True)
    resposta = PedidoV24Agent(db_session).start_despejo(conversa(db_session))
    assert f"#{contentor.id}" not in resposta
    assert f"#{carrinha.id}" in resposta
    assert "Carrinha" in resposta
