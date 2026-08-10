"""FEATURE_CARRINHAS_ENABLED aplicada à partida operacional de Carrinha."""

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
    StatusPagamento,
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
        telefone="351900059900",
        estado_atual=estado,
        contexto_json=contexto or {},
    )
    db_session.add(atual)
    db_session.commit()
    return atual


def mensagem(texto=None, *, tipo="text", media=None):
    return NormalizedWhatsAppMessage(
        telefone="351900059900",
        tipo=tipo,
        texto=texto,
        media_id=media,
        message_id=media or "modalidades-partida-carrinha",
    )


def criar_carrinha_em_atendimento(db_session, *, pago=True):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Carrinha Em Atendimento",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=pago,
        forma_pagamento="MBWay" if pago else None,
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


def criar_contentor_entregue(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Cliente Contentor Recolha",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor",
        endereco_aproximado="Rua do Contentor",
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
        [{"contentor_id": item.id, "numero_adesivo": "601", "fotos": ["foto-entrega"]}],
    )
    return pedido


def concluir_partida(agente, atual):
    agente.start_recolha(atual)
    agente.handle(atual, mensagem("1"))
    agente.handle(atual, mensagem("1"))
    agente.handle(atual, mensagem(tipo="image", media="foto-partida"))
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
        "fotos_recolha": ["foto-partida-preparada"],
        "avariado": avariado,
        "relato_avaria": relato,
    }


@pytest.fixture
def partida_bloqueada(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True, avarias=True)
    pedido = criar_carrinha_em_atendimento(db_session, pago=False)
    configurar(monkeypatch, carrinhas=False, avarias=True)
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
    db_session.refresh(pedido)
    return pedido, item, atual, resposta


def test_carrinha_on_aparece_para_partida(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert pedido.nome_cliente in resposta
    assert "Carrinha" in resposta


def test_carrinha_on_partida_completa_funciona(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)

    concluir_partida(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value


def test_carrinha_off_nao_aparece(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert pedido.nome_cliente not in resposta
    assert "não existem equipamentos" in resposta.lower()


def test_carrinha_antiga_em_atendimento_e_bloqueada(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False)
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


def test_carrinha_off_status_operacional_permanece(partida_bloqueada):
    _, item, _, _ = partida_bloqueada
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value


def test_carrinha_off_nao_persiste_foto_partida(db_session, partida_bloqueada):
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0


def test_carrinha_off_nao_persiste_horario_ou_operador(partida_bloqueada):
    _, item, _, _ = partida_bloqueada
    assert item.partida_carrinha_data_hora is None
    assert item.partida_carrinha_feita_por is None


@pytest.mark.parametrize("avarias_enabled", [True, False])
def test_carrinha_off_nao_cria_avaria(db_session, monkeypatch, avarias_enabled):
    configurar(monkeypatch, carrinhas=True, avarias=avarias_enabled)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False, avarias=avarias_enabled)
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


def test_carrinha_off_preserva_avaria_existente(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True, avarias=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    item = pedido.contentores[0]
    item.contentor_avariado = True
    item.relato_avaria = "Avaria preexistente preservada"
    item.status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    db_session.commit()
    configurar(monkeypatch, carrinhas=False, avarias=True)
    atual = conversa(
        db_session,
        estado="v24_recolha_confirmacao",
        contexto=contexto_preparado(pedido),
    )

    PedidoV24Agent(db_session).handle(atual, mensagem("1"))

    db_session.refresh(item)
    assert item.contentor_avariado is True
    assert item.relato_avaria == "Avaria preexistente preservada"
    assert item.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_contexto_residual_nao_permite_bypass(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False)
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
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False)
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
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    configurar(monkeypatch, carrinhas=False)
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
    assert pedido.contentores[0].status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value


def test_carrinha_off_nao_fica_elegivel_para_despejo(db_session, partida_bloqueada):
    assert PedidoService(db_session).carrinhas_aguardando_despejo() == []


def test_contentor_on_carrinha_off_recolha_contentor_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=True, carrinhas=False)
    pedido = criar_contentor_entregue(db_session)

    concluir_partida(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_recolha == StatusRecolhaPedido.RECOLHIDO.value


def test_contentor_off_carrinha_on_partida_funciona(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)

    concluir_partida(PedidoV24Agent(db_session), conversa(db_session))

    db_session.refresh(pedido.contentores[0])
    assert pedido.contentores[0].status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value


def test_off_off_nao_inicia_operacao(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(db_session)

    resposta = PedidoV24Agent(db_session).start_recolha(atual)

    assert "não há modalidade operacional habilitada" in resposta.lower()
    assert "selecione" not in resposta.lower()


def test_off_off_contexto_seguro_idle(db_session, monkeypatch):
    configurar(monkeypatch, contentores=False, carrinhas=False)
    atual = conversa(
        db_session,
        estado="v24_recolha_foto",
        contexto={"pedido_id": 99, "contentor_id": 88},
    )

    PedidoV24Agent(db_session).start_recolha(atual)

    assert atual.estado_atual == "idle"
    assert atual.contexto_json == {}


def test_feature_avarias_permanece_independente(db_session, monkeypatch):
    configurar(monkeypatch, carrinhas=False, avarias=True)

    PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert get_settings().feature_avarias_enabled is True


def test_defaults_on_on_preservam_comportamento_anterior(db_session, monkeypatch):
    configurar(monkeypatch)
    contentor = criar_contentor_entregue(db_session)
    carrinha = criar_carrinha_em_atendimento(db_session)

    resposta = PedidoV24Agent(db_session).start_recolha(conversa(db_session))

    assert contentor.nome_cliente in resposta
    assert carrinha.nome_cliente in resposta


def test_reabilitar_carrinha_devolve_partida_sem_alterar_registro(
    db_session,
    monkeypatch,
):
    configurar(monkeypatch, carrinhas=True)
    pedido = criar_carrinha_em_atendimento(db_session)
    item = pedido.contentores[0]
    configurar(monkeypatch, carrinhas=False)
    atual = conversa(db_session)
    PedidoV24Agent(db_session).start_recolha(atual)

    configurar(monkeypatch, carrinhas=True)
    resposta = PedidoV24Agent(db_session).start_recolha(atual)

    db_session.refresh(item)
    assert pedido.nome_cliente in resposta
    assert item.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert item.partida_carrinha_data_hora is None


def test_campos_legados_espelhados_nao_mudam(partida_bloqueada):
    _, item, _, _ = partida_bloqueada
    assert item.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert item.recolha_feita_por is None
    assert item.recolha_data_hora is None
    assert item.status_entrega == StatusEntregaPedido.ENTREGUE.value


def test_financeiro_permanece_inalterado(partida_bloqueada):
    pedido, _, _, _ = partida_bloqueada
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert pedido.forma_pagamento is None
