from app.agents.whatsapp_router_agent import WhatsappRouterAgent
from app.core.config import get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import AluguerContentor, ContentorFotoRecolha, StatusCiclo, StatusEntrega, StatusResolucao
from app.models.contentor import StatusContentor
from app.services.aluguer_service import AluguerService
from app.services.seed_service import SeedService


def text_message(texto: str, telefone: str = "351900009100") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(telefone=telefone, tipo="text", texto=texto, message_id=f"text-{texto}")


def image_message(media_id: str = "media-recolha-1", telefone: str = "351900009100") -> NormalizedWhatsAppMessage:
    return NormalizedWhatsAppMessage(
        telefone=telefone,
        tipo="image",
        media_id=media_id,
        mime_type="image/jpeg",
        message_id=media_id,
    )


def liberar_operadores(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()


def autorizar_gestor(monkeypatch, telefone: str):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "")
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONE", telefone)
    monkeypatch.setenv("AUTHORIZED_OPERATOR_PHONES", "")
    monkeypatch.setenv("OWNER_WHATSAPP", "")
    get_settings.cache_clear()


def criar_aluguer_entregue(db_session, pago: bool = True) -> AluguerContentor:
    SeedService(db_session).seed_contentores_iniciais()
    service = AluguerService(db_session)
    return service.registrar_novo_aluguer(
        nome_cliente="Cliente Recolha",
        telefone_cliente="351912345678",
        valor="180",
        forma_pagamento="MBWay" if pago else None,
        pago=pago,
        tipo_residuo="Entulho Limpo",
        operador_telefone="351900000001",
        status_entrega=StatusEntrega.ENTREGUE.value,
        entrega_feita_por="351900000002",
        entrega_latitude=38.7223,
        entrega_longitude=-9.1393,
        entrega_ponto_referencia="Portao azul",
    )


def test_recolha_registra_fotos_triagem_pendencias_e_libera_contentor(db_session, monkeypatch):
    liberar_operadores(monkeypatch)
    aluguer = criar_aluguer_entregue(db_session)
    router = WhatsappRouterAgent(db_session)
    telefone = "351900009100"

    inicio = router.handle(text_message("recolha", telefone=telefone))
    selecao = router.handle(text_message("1", telefone=telefone))
    foto = router.handle(image_message("media-recolha-1", telefone=telefone))
    triagem_carga = router.handle(text_message("2", telefone=telefone))
    relato_carga_prompt = router.handle(text_message("2", telefone=telefone))
    relato_carga = router.handle(text_message("Tinha sacos de lixo domestico e gesso", telefone=telefone))
    relato_avaria_prompt = router.handle(text_message("2", telefone=telefone))
    final = router.handle(text_message("Lateral direita amassada por retroescavadeira", telefone=telefone))

    db_session.refresh(aluguer)
    assert "Confirmar recolha de contentor" in inicio
    assert "Envie a foto" in selecao
    assert "Deseja adicionar mais uma foto" in foto
    assert "contratado" in triagem_carga
    assert "material incorreto" in relato_carga_prompt
    assert "estrago ou avaria" in relato_carga
    assert "Descreva o estrago" in relato_avaria_prompt
    assert "Recolha do contentor registrada" in final
    assert aluguer.status_ciclo == StatusCiclo.RECOLHIDO.value
    assert aluguer.status.name == "RECOLHIDO"
    assert aluguer.recolha_feita_por == telefone
    assert aluguer.recolha_data_hora is not None
    assert aluguer.carga_errada is True
    assert aluguer.status_resolucao_carga == StatusResolucao.PENDENTE.value
    assert aluguer.contentor_avariado is True
    assert aluguer.status_resolucao_avaria == StatusResolucao.PENDENTE.value
    assert aluguer.contentor.status == StatusContentor.DISPONIVEL
    assert db_session.query(ContentorFotoRecolha).filter_by(aluguer_id=aluguer.id).count() == 1


def test_resumo_gestor_exibe_e_resolve_pendencia_de_carga(db_session, monkeypatch):
    telefone = "351900009101"
    autorizar_gestor(monkeypatch, telefone)
    aluguer = criar_aluguer_entregue(db_session, pago=False)
    service = AluguerService(db_session)
    service.confirmar_recolha(
        aluguer.id,
        operador_telefone=telefone,
        fotos_recolha=["media-recolha-1"],
        carga_errada=True,
        relato_carga="Tinha gesso e sacos de lixo domestico",
        contentor_avariado=False,
    )
    router = WhatsappRouterAgent(db_session)

    resumo = router.handle(text_message("resumo", telefone=telefone))
    resolvido = router.handle(text_message(f"resolver carga {aluguer.id}", telefone=telefone))
    db_session.refresh(aluguer)

    assert "PAINEL DE CONTROLE OPERACIONAL OLT" in resumo
    assert "PENDENCIAS ATIVAS" in resumo
    assert "PENDENCIAS OPERACIONAIS CRITICAS" not in resumo
    assert f"resolver carga {aluguer.id}" not in resumo
    assert "resolvida" in resolvido
    assert aluguer.status_resolucao_carga == StatusResolucao.RESOLVIDO.value
