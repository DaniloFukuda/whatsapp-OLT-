from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.recolha_agent import RecolhaAgent
from app.agents.whatsapp_router_agent import AVARIAS_DISABLED_MESSAGE, WhatsappRouterAgent
from app.core.config import Settings, get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import (
    ContentorFoto,
    ContentorFotoRecolha,
    EventoAluguer,
    StatusCiclo,
    StatusResolucao,
)
from app.models.conversa import ConversaWhatsApp
from app.models.operador import Operador, PerfilOperador
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
)
from app.services.pedido_service import PedidoService
from app.services.aluguer_service import AluguerService
from app.services.seed_service import SeedService


PHONE = "351900001001"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.delenv("FEATURE_AVARIAS_ENABLED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def message(text: str):
    return NormalizedWhatsAppMessage(
        telefone=PHONE, tipo="text", texto=text, message_id=f"feature-{text}"
    )


def pending_contentor(db_session, *, historical=True):
    pedido = Pedido(
        nome_cliente="Cliente Flag",
        telefone_cliente="351911111111",
        data_planejada=datetime(2026, 8, 3, tzinfo=timezone.utc),
        valor_global=100,
        status_pagamento=StatusPagamento.PENDENTE.value,
        pedido_feito_por=PHONE,
        endereco_aproximado="Obra da flag",
    )
    contentor = PedidoContentor(
        pedido=pedido,
        numero_adesivo_contentor="FLAG-1",
        residuo_contratado="Entulho Limpo",
        status_entrega=StatusEntregaPedido.ENTREGUE.value,
        status_recolha=StatusRecolhaPedido.PENDENTE.value,
        status_ciclo=StatusCicloPedido.EM_ANDAMENTO.value,
        contentor_avariado=historical,
        relato_avaria="avaria historica preservada" if historical else None,
        status_resolucao_avaria=(
            StatusResolucaoPedido.PENDENTE.value
            if historical
            else StatusResolucaoPedido.NAO_APLICA.value
        ),
    )
    db_session.add_all([pedido, contentor])
    db_session.commit()
    return pedido, contentor


def legacy_aluguer(db_session, *, historical=False, resolved=False, paid=True):
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Legado Flag",
        telefone_cliente="351922222222",
        valor="180",
        forma_pagamento="MBWay" if paid else None,
        pago=paid,
        tipo_residuo="Entulho Limpo",
        operador_telefone=PHONE,
        status_entrega=StatusEntregaPedido.ENTREGUE.value,
        entrega_feita_por=PHONE,
    )
    if historical:
        aluguer.contentor_avariado = True
        aluguer.relato_avaria = "relato historico legado preservado"
        aluguer.status_resolucao_avaria = (
            StatusResolucao.RESOLVIDO.value if resolved else StatusResolucao.PENDENTE.value
        )
        db_session.add_all(
            [
                EventoAluguer(
                    aluguer_id=aluguer.id,
                    tipo="pendencia_avaria_resolvida" if resolved else "pendencia_avaria",
                    descricao="evento historico imutavel",
                ),
                ContentorFotoRecolha(
                    aluguer_id=aluguer.id,
                    url_foto_recolha="midia-historica-legada",
                ),
            ]
        )
        db_session.commit()
    return aluguer


def test_configuracao_ausente_e_true_e_valores_validos(monkeypatch):
    assert Settings(_env_file=None).feature_avarias_enabled is True
    assert Settings(feature_avarias_enabled="true", _env_file=None).feature_avarias_enabled is True
    assert Settings(feature_avarias_enabled="false", _env_file=None).feature_avarias_enabled is False
    with pytest.raises(ValidationError):
        Settings(feature_avarias_enabled="talvez", _env_file=None)


def test_recolha_desabilitada_pula_avaria_e_preserva_historico(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    pedido, contentor = pending_contentor(db_session)
    conversa = ConversaWhatsApp(
        telefone=PHONE,
        estado_atual="v24_recolha_foto_acao",
        contexto_json={
            "pedido_id": pedido.id,
            "contentor_id": contentor.id,
            "recolhas": [],
            "fotos_recolha": ["foto-operacional"],
            "avariado": None,
            "relato_avaria": None,
        },
    )
    db_session.add(conversa)
    db_session.commit()

    resposta = PedidoV24Agent(db_session).handle(conversa, message("2"))

    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert "avaria" not in resposta.lower()
    assert "avariado" not in conversa.contexto_json
    assert "relato_avaria" not in conversa.contexto_json

    PedidoService(db_session).confirmar_recolha(
        contentor.id, PHONE, False, None, ["foto-operacional"]
    )
    db_session.refresh(contentor)
    assert contentor.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert contentor.contentor_avariado is True
    assert contentor.relato_avaria == "avaria historica preservada"
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value


def test_servico_bloqueia_criacao_e_resolucao_sem_mutacao(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    _, contentor = pending_contentor(db_session, historical=False)
    service = PedidoService(db_session)

    with pytest.raises(ValueError, match="não está disponível"):
        service.confirmar_recolha(contentor.id, PHONE, True, "porta muito danificada", [])
    with pytest.raises(ValueError, match="não está disponível"):
        service.resolver("avaria", contentor.id)

    db_session.refresh(contentor)
    assert contentor.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert contentor.contentor_avariado is False
    assert contentor.relato_avaria is None
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.NAO_APLICA.value


def test_comando_painel_e_revisao_antiga_desabilitados(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    db_session.add(
        Operador(
            telefone_whatsapp=PHONE,
            nome_operador="Gestor Flag",
            perfil=PerfilOperador.GESTOR,
            ativo=True,
        )
    )
    _, contentor = pending_contentor(db_session)
    conversa = ConversaWhatsApp(
        telefone=PHONE,
        estado_atual="resolucao_avaria_revisao",
        contexto_json={
            "_resolucao_avaria": {
                "id": contentor.id,
                "origem": "pedido",
                "estado_anterior": "idle",
                "contexto_anterior": {"preservar": True},
                "gestor": PHONE,
            }
        },
    )
    db_session.add(conversa)
    db_session.commit()
    router = WhatsappRouterAgent(db_session)

    recuperacao = router.handle(message("resolucao_avaria:confirmar"))
    assert recuperacao.startswith(AVARIAS_DISABLED_MESSAGE)
    assert conversa.estado_atual == "idle"
    assert conversa.contexto_json == {"preservar": True}
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value

    assert router.handle(message(f"resolver avaria {contentor.id}")) == AVARIAS_DISABLED_MESSAGE
    painel = router._painel_v32_pendencias(PerfilOperador.GESTOR)
    assert "avaria" not in painel.lower()
    assert "resolver avaria" not in painel.lower()


def test_flag_true_preserva_criacao_de_avaria(db_session):
    _, contentor = pending_contentor(db_session, historical=False)
    PedidoService(db_session).confirmar_recolha(
        contentor.id, PHONE, True, "porta lateral danificada", ["foto-recolha"]
    )
    db_session.refresh(contentor)
    assert contentor.contentor_avariado is True
    assert contentor.relato_avaria == "porta lateral danificada"
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_fluxo_legado_false_pula_avaria_preserva_historico_e_financeiro(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    aluguer = legacy_aluguer(db_session, historical=True, paid=False)
    valor, pago = aluguer.valor, aluguer.pago
    conversa = ConversaWhatsApp(telefone=PHONE, estado_atual="idle", contexto_json={})
    db_session.add(conversa)
    db_session.commit()
    agent = RecolhaAgent(db_session)

    assert "Confirmar recolha" in agent.start(conversa)
    assert "Envie a foto" in agent.handle(conversa, message("1"))
    image = NormalizedWhatsAppMessage(
        telefone=PHONE, tipo="image", media_id="foto-operacional-legada", message_id="foto-legada"
    )
    assert "adicionar mais" in agent.handle(conversa, image)
    assert "contratado" in agent.handle(conversa, message("2"))
    final = agent.handle(conversa, message("1"))

    db_session.expire_all()
    persistido = db_session.get(type(aluguer), aluguer.id)
    assert "avaria" not in final.lower()
    assert "relato" not in final.lower()
    assert conversa.estado_atual == "idle"
    assert persistido.status_ciclo == StatusCiclo.RECOLHIDO.value
    assert persistido.contentor_avariado is True
    assert persistido.relato_avaria == "relato historico legado preservado"
    assert persistido.status_resolucao_avaria == StatusResolucao.PENDENTE.value
    assert (persistido.valor, persistido.pago) == (valor, pago)


def test_aluguer_service_false_bloqueia_criacao_resolucao_e_preserva_evidencias(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    aluguer = legacy_aluguer(db_session, historical=True)
    evento = db_session.query(EventoAluguer).filter_by(
        aluguer_id=aluguer.id, descricao="evento historico imutavel"
    ).one()
    foto = db_session.query(ContentorFotoRecolha).filter_by(aluguer_id=aluguer.id).one()
    snapshot = (aluguer.contentor_avariado, aluguer.relato_avaria, aluguer.status_resolucao_avaria)
    evento_snapshot = (evento.tipo, evento.descricao, evento.criado_em)
    foto_snapshot = (foto.url_foto_recolha, foto.criado_em)
    service = AluguerService(db_session)

    with pytest.raises(ValueError, match="não está disponível"):
        service.confirmar_recolha(
            aluguer.id, PHONE, ["nova-foto"], contentor_avariado=True,
            relato_avaria="nova avaria que deve ser bloqueada",
        )
    with pytest.raises(ValueError, match="não está disponível"):
        service.resolver_pendencia_avaria(aluguer.id, PHONE)

    db_session.expire_all()
    persistido = db_session.get(type(aluguer), aluguer.id)
    evento_db = db_session.get(EventoAluguer, evento.id)
    foto_db = db_session.get(ContentorFotoRecolha, foto.id)
    assert (persistido.contentor_avariado, persistido.relato_avaria, persistido.status_resolucao_avaria) == snapshot
    assert (evento_db.tipo, evento_db.descricao, evento_db.criado_em) == evento_snapshot
    assert (foto_db.url_foto_recolha, foto_db.criado_em) == foto_snapshot
    assert db_session.query(ContentorFotoRecolha).filter_by(aluguer_id=aluguer.id).count() == 1


def test_aluguer_service_true_mantem_criacao_e_resolucao(db_session):
    aluguer = legacy_aluguer(db_session)
    service = AluguerService(db_session)
    service.confirmar_recolha(
        aluguer.id, PHONE, ["foto-nova"], contentor_avariado=True,
        relato_avaria="porta traseira bastante danificada",
    )
    service.resolver_pendencia_avaria(aluguer.id, PHONE)
    db_session.expire_all()
    persistido = db_session.get(type(aluguer), aluguer.id)
    assert persistido.contentor_avariado is True
    assert persistido.relato_avaria == "porta traseira bastante danificada"
    assert persistido.status_resolucao_avaria == StatusResolucao.RESOLVIDO.value
    assert db_session.query(EventoAluguer).filter_by(
        aluguer_id=aluguer.id, tipo="pendencia_avaria_resolvida"
    ).count() == 1


def test_painel_v4_false_oculta_avarias_e_true_reexibe(db_session, monkeypatch):
    db_session.add(Operador(
        telefone_whatsapp=PHONE, nome_operador="Gestor Painel",
        perfil=PerfilOperador.GESTOR, ativo=True,
    ))
    pedido, contentor = pending_contentor(db_session)
    router = WhatsappRouterAgent(db_session)

    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    oculto = router._painel_v4(PerfilOperador.GESTOR)
    assert "PAINEL DE CONTROLE OPERACIONAL" in oculto
    assert "RESUMO FINANCEIRO" in oculto
    assert "FATURAMENTO CONTENTORES" in oculto
    assert "FATURAMENTO CARRINHAS" in oculto
    assert "Avarias em Equipamentos" not in oculto
    assert "avaria historica preservada" not in oculto
    assert "resolver avaria" not in oculto

    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "true")
    get_settings.cache_clear()
    visivel = router._painel_v4(PerfilOperador.GESTOR)
    assert "Avarias em Equipamentos" in visivel
    assert "avaria historica preservada" in visivel
    assert "resolver%20avaria" in visivel
    assert pedido.status_pagamento == StatusPagamento.PENDENTE.value
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_historico_resolvido_com_foto_evento_datas_e_responsavel_permanece_intacto(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    db_session.add(Operador(
        telefone_whatsapp=PHONE, nome_operador="Gestor Histórico",
        perfil=PerfilOperador.GESTOR, ativo=True,
    ))
    _, atual = pending_contentor(db_session)
    resolvida_em = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    atual.status_resolucao_avaria = StatusResolucaoPedido.RESOLVIDO.value
    atual.avaria_estado_anterior = StatusResolucaoPedido.PENDENTE.value
    atual.avaria_resolvida_em = resolvida_em
    atual.avaria_resolvida_por = PHONE
    foto_atual = ContentorFoto(
        pedido_contentor_id=atual.id, url_midia="midia-historica-atual",
        url_foto="midia-historica-atual", tipo_foto="RECOLHA", tipo="recolha",
    )
    legado = legacy_aluguer(db_session, historical=True, resolved=True)
    db_session.add(foto_atual)
    db_session.commit()
    router = WhatsappRouterAgent(db_session)
    snapshot_atual = (
        atual.contentor_avariado, atual.relato_avaria, atual.status_resolucao_avaria,
        atual.avaria_estado_anterior, atual.avaria_resolvida_em, atual.avaria_resolvida_por,
    )
    eventos_legados = db_session.query(EventoAluguer).filter_by(aluguer_id=legado.id).count()
    fotos_legadas = db_session.query(ContentorFotoRecolha).filter_by(aluguer_id=legado.id).count()
    assert router._painel_v4(PerfilOperador.GESTOR).lower().count("avaria") == 0
    assert router.handle(message(f"resolver avaria {atual.id}")) == AVARIAS_DISABLED_MESSAGE

    db_session.expire_all()
    atual_db = db_session.get(PedidoContentor, atual.id)
    legado_db = db_session.get(type(legado), legado.id)
    assert (
        atual_db.contentor_avariado, atual_db.relato_avaria, atual_db.status_resolucao_avaria,
        atual_db.avaria_estado_anterior, atual_db.avaria_resolvida_em, atual_db.avaria_resolvida_por,
    ) == snapshot_atual
    assert db_session.get(ContentorFoto, foto_atual.id).url_midia == "midia-historica-atual"
    assert legado_db.status_resolucao_avaria == StatusResolucao.RESOLVIDO.value
    assert db_session.query(EventoAluguer).filter_by(aluguer_id=legado.id).count() == eventos_legados
    assert db_session.query(ContentorFotoRecolha).filter_by(aluguer_id=legado.id).count() == fotos_legadas


@pytest.mark.parametrize("state", ["v24_recolha_avaria", "v24_recolha_relato"])
def test_estados_v24_antigos_recuperam_para_confirmacao_sem_persistir_parcial(db_session, monkeypatch, state):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    pedido, contentor = pending_contentor(db_session)
    conversa = ConversaWhatsApp(
        telefone=PHONE, estado_atual=state,
        contexto_json={
            "pedido_id": pedido.id, "contentor_id": contentor.id, "recolhas": [],
            "fotos_recolha": ["foto-operacional"], "avariado": True,
            "relato_avaria": "parcial nao persistido",
        },
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = PedidoV24Agent(db_session).handle(conversa, message("entrada tardia"))
    db_session.refresh(contentor)
    assert resposta.startswith(AVARIAS_DISABLED_MESSAGE)
    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert "avariado" not in conversa.contexto_json and "relato_avaria" not in conversa.contexto_json
    assert contentor.relato_avaria == "avaria historica preservada"
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


@pytest.mark.parametrize("state", ["recolha_aguardando_triagem_avaria", "recolha_aguardando_relato_avaria"])
def test_estados_legados_antigos_concluem_sem_persistir_avaria_parcial(db_session, monkeypatch, state):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    aluguer = legacy_aluguer(db_session, historical=True)
    conversa = ConversaWhatsApp(
        telefone=PHONE, estado_atual=state,
        contexto_json={
            "aluguer_id": aluguer.id, "fotos_recolha": ["foto-operacional"],
            "carga_errada": False, "relato_carga": None,
            "contentor_avariado": True, "relato_avaria": "parcial descartado",
        },
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = RecolhaAgent(db_session).handle(conversa, message("entrada tardia"))
    db_session.expire_all()
    persistido = db_session.get(type(aluguer), aluguer.id)
    assert resposta.startswith(AVARIAS_DISABLED_MESSAGE)
    assert conversa.estado_atual == "idle"
    assert persistido.relato_avaria == "relato historico legado preservado"
    assert persistido.status_resolucao_avaria == StatusResolucao.PENDENTE.value


def test_revisao_invalida_e_confirmacao_tardia_false_usam_fallback_seguro(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    db_session.add(Operador(
        telefone_whatsapp=PHONE, nome_operador="Gestor Contexto",
        perfil=PerfilOperador.GESTOR, ativo=True,
    ))
    conversa = ConversaWhatsApp(
        telefone=PHONE, estado_atual="resolucao_avaria_revisao",
        contexto_json={"_resolucao_avaria": {"contexto_anterior": "invalido"}},
    )
    db_session.add(conversa)
    db_session.commit()
    router = WhatsappRouterAgent(db_session)
    resposta = router.handle(message("resolucao_avaria:confirmar"))
    assert resposta.startswith(AVARIAS_DISABLED_MESSAGE)
    assert conversa.estado_atual == "idle" and conversa.contexto_json == {}
    assert router.handle(message("resolucao_avaria:confirmar")) == AVARIAS_DISABLED_MESSAGE
    assert conversa.estado_atual == "idle"


def test_transicao_em_foto_operacional_false_segue_sem_estado_de_avaria(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "false")
    get_settings.cache_clear()
    pedido, contentor = pending_contentor(db_session)
    conversa = ConversaWhatsApp(
        telefone=PHONE, estado_atual="v24_recolha_foto_acao",
        contexto_json={
            "pedido_id": pedido.id, "contentor_id": contentor.id,
            "recolhas": [], "fotos_recolha": ["foto-operacional"],
        },
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = PedidoV24Agent(db_session).handle(conversa, message("2"))
    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert "avaria" not in resposta.lower()
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_ciclo_true_consolidado_cria_exibe_revisa_resolve_e_audita(db_session):
    db_session.add(Operador(
        telefone_whatsapp=PHONE, nome_operador="Gestor Integrado",
        perfil=PerfilOperador.GESTOR, ativo=True,
    ))
    pedido, contentor = pending_contentor(db_session, historical=False)
    conversa = ConversaWhatsApp(
        telefone=PHONE, estado_atual="v24_recolha_avaria",
        contexto_json={
            "pedido_id": pedido.id, "contentor_id": contentor.id,
            "recolhas": [], "fotos_recolha": ["foto-integrada"],
        },
    )
    db_session.add(conversa)
    db_session.commit()
    agent = PedidoV24Agent(db_session)
    assert "Descreva a avaria" in agent.handle(conversa, message("2"))
    assert "Avaria: Sim" in agent.handle(conversa, message("porta lateral muito danificada"))
    final = agent.handle(conversa, message("1"))
    assert "pendencia de avaria" in final
    db_session.refresh(contentor)
    assert contentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value

    router = WhatsappRouterAgent(db_session)
    painel = router._painel_v4(PerfilOperador.GESTOR)
    assert "Avarias em Equipamentos" in painel and "resolver%20avaria" in painel
    revisao = router.handle(message(f"resolver avaria {contentor.id}"))
    assert "Revisão de resolução de avaria" in revisao
    resolucao = router.handle(message("resolucao_avaria:confirmar"))
    assert "resolvida" in resolucao
    db_session.expire_all()
    persistido = db_session.get(PedidoContentor, contentor.id)
    assert persistido.status_resolucao_avaria == StatusResolucaoPedido.RESOLVIDO.value
    assert persistido.avaria_estado_anterior == StatusResolucaoPedido.PENDENTE.value
    assert persistido.avaria_resolvida_em is not None
    assert persistido.avaria_resolvida_por == PHONE
