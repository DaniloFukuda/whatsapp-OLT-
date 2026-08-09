"""Caracterização do ciclo operacional V2.4 de contentores.

Os testes exercitam as APIs públicas e conferem o estado persistido e as filas
operacionais, sem depender da organização interna dos módulos de produção.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


def criar_pedido_contentor(service: PedidoService, nome: str = "Cliente C1"):
    return service.criar(
        nome_cliente=nome,
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="200",
        pago=True,
        forma_pagamento="MBWay",
        pedido_feito_por="gestor-c1",
        endereco_aproximado="Rua da Caracterizacao",
        ponto_referencia=None,
        residuos=["Entulho Limpo"],
    )


def criar_pedido_carrinha(service: PedidoService):
    return service.criar(
        nome_cliente="Cliente Carrinha C1",
        telefone_cliente="351987654321",
        data_planejada=datetime.now(timezone.utc),
        valor_global="180",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor-c1",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": "Entulho Limpo",
                "horario_agendado": "10:00",
            }
        ],
    )


def confirmar_entrega(service: PedidoService, pedido, adesivo: str = "41"):
    item = pedido.contentores[0]
    service.confirmar_entrega_lote(
        pedido.id,
        "motorista-entrega-c1",
        38.7223,
        -9.1393,
        "Portao principal",
        [
            {
                "contentor_id": item.id,
                "numero_adesivo": adesivo,
                "fotos": [f"foto-entrega-{adesivo}"],
            }
        ],
    )
    return item


def ids(itens):
    return {item.id for item in itens}


def definir_avarias(monkeypatch, habilitadas: bool):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "true" if habilitadas else "false")
    get_settings.cache_clear()


def preparar_para_despejo(service, pedido, adesivo="50"):
    item = confirmar_entrega(service, pedido, adesivo)
    service.confirmar_recolha(item.id, "motorista-recolha-c1", False, None, [f"foto-recolha-{adesivo}"])
    return item


def test_pedido_novo_cria_item_do_tipo_contentor(db_session):
    pedido = criar_pedido_contentor(PedidoService(db_session))

    db_session.refresh(pedido.contentores[0])

    assert pedido.contentores[0].tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value


def test_item_novo_aparece_na_fila_de_entrega_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)

    fila = service.pedidos_pendentes_entrega()

    assert [item.id for item in fila] == [pedido.id]
    assert pedido.contentores[0].status_entrega == StatusEntregaPedido.PENDENTE.value


def test_carrinha_nao_aparece_na_fila_de_entrega_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido_contentor = criar_pedido_contentor(service)
    pedido_carrinha = criar_pedido_carrinha(service)

    fila_ids = {pedido.id for pedido in service.pedidos_pendentes_entrega()}

    assert pedido_contentor.id in fila_ids
    assert pedido_carrinha.id not in fila_ids


def test_confirmar_entrega_persiste_entregue_e_remove_da_fila(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido)

    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.id == item.id
    assert persistido.status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert pedido.id not in {registro.id for registro in service.pedidos_pendentes_entrega()}


def test_contentor_entregue_torna_se_elegivel_para_recolha(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "42")

    db_session.expire_all()

    assert item.id in ids(service.contentores_para_recolha())
    assert service.get(pedido.id).contentores[0].status_recolha == StatusRecolhaPedido.PENDENTE.value


def test_confirmar_recolha_persiste_remove_da_fila_e_habilita_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "43")

    service.confirmar_recolha(item.id, "motorista-recolha-c1", False, None)
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert persistido.id not in ids(service.contentores_para_recolha())
    assert persistido.id in ids(service.contentores_para_despejo())


def test_confirmar_despejo_conclui_ciclo_e_remove_das_filas(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "44")
    service.confirmar_recolha(item.id, "motorista-recolha-c1", False, None)

    service.confirmar_despejo(
        item.id,
        "Entulho Limpo",
        operador="motorista-despejo-c1",
        pedido_id=pedido.id,
        fotos=["foto-despejo-44"],
    )
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert persistido.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert pedido.id not in {registro.id for registro in service.pedidos_pendentes_entrega()}
    assert persistido.id not in ids(service.contentores_para_recolha())
    assert persistido.id not in ids(service.contentores_para_despejo())


def test_cancelar_entrega_antes_da_confirmacao_nao_persiste_mudanca(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = pedido.contentores[0]
    conversa = ConversaWhatsApp(
        telefone="351900009901",
        estado_atual="idle",
        contexto_json={},
    )
    db_session.add(conversa)
    db_session.commit()
    agent = PedidoV24Agent(db_session)

    agent.start_entrega(conversa)
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="1", message_id="c1-1"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="45", message_id="c1-2"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="image", media_id="foto-nao-persistida", message_id="c1-3"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="c1-4"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone,
        tipo="location",
        latitude=38.7223,
        longitude=-9.1393,
        message_id="c1-5",
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="Sim", message_id="c1-6"
    ))
    agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="Portao", message_id="c1-7"
    ))
    resposta = agent.handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="c1-8"
    ))
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert "cancelada" in resposta.lower()
    assert persistido.id == item.id
    assert persistido.status_entrega == StatusEntregaPedido.PENDENTE.value
    assert persistido.numero_adesivo_contentor is None
    assert persistido.entrega_latitude is None
    assert persistido.entrega_longitude is None
    assert persistido.entrega_ponto_referencia is None
    assert db_session.query(ContentorFoto).count() == 0


def test_entrega_valida_persiste_adesivo_foto_gps_referencia_e_auditoria(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "51")

    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]
    fotos = db_session.query(ContentorFoto).filter_by(pedido_contentor_id=item.id).all()

    assert persistido.numero_adesivo_contentor == "51"
    assert (persistido.entrega_latitude, persistido.entrega_longitude) == (38.7223, -9.1393)
    assert persistido.entrega_ponto_referencia == "Portao principal"
    assert persistido.entrega_feita_por == "motorista-entrega-c1"
    assert persistido.entrega_data_hora is not None
    assert [(foto.tipo_foto, foto.url_midia) for foto in fotos] == [(TipoFoto.ENTREGA.value, "foto-entrega-51")]


def test_entrega_rejeita_item_de_outro_pedido_sem_vincular_dados(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service, "Pedido selecionado")
    outro = criar_pedido_contentor(service, "Outro pedido")

    with pytest.raises(ValueError, match="ativos pendentes mudou"):
        service.confirmar_entrega_lote(
            pedido.id, "motorista", 38.7, -9.1, None,
            [{"contentor_id": outro.contentores[0].id, "numero_adesivo": "52", "fotos": ["foto-errada"]}],
        )
    db_session.rollback()
    db_session.expire_all()

    assert service.get(outro.id).contentores[0].numero_adesivo_contentor is None
    assert db_session.query(ContentorFoto).filter_by(url_midia="foto-errada").count() == 0


def test_recolha_sem_avaria_mantem_campos_inativos_com_flag_on(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "53")

    service.confirmar_recolha(item.id, "motorista", False, None, ["foto-recolha-53"])
    db_session.refresh(item)

    assert item.contentor_avariado is False
    assert item.relato_avaria is None
    assert item.status_resolucao_avaria == StatusResolucaoPedido.NAO_APLICA.value


def test_recolha_com_avaria_persiste_status_relato_e_item_correto(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "54")

    service.confirmar_recolha(item.id, "motorista", True, "porta lateral muito danificada")
    db_session.refresh(item)

    assert item.contentor_avariado is True
    assert item.relato_avaria == "porta lateral muito danificada"
    assert item.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_avaria_e_foto_de_recolha_nao_sao_copiadas_para_outro_item(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Dois contentores", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300", pago=True,
        forma_pagamento="MBWay", pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores
    service.confirmar_entrega_lote(
        pedido.id, "motorista", 38.7, -9.1, None,
        [
            {"contentor_id": primeiro.id, "numero_adesivo": "55", "fotos": ["entrega-55"]},
            {"contentor_id": segundo.id, "numero_adesivo": "56", "fotos": ["entrega-56"]},
        ],
    )

    service.confirmar_recolha(primeiro.id, "motorista", True, "estrutura lateral amassada", ["recolha-55"])
    db_session.expire_all()
    primeiro = service.get(pedido.id).contentores[0]
    segundo = service.get(pedido.id).contentores[1]

    assert primeiro.contentor_avariado is True
    assert segundo.contentor_avariado is False
    assert db_session.query(ContentorFoto).filter_by(pedido_contentor_id=primeiro.id, tipo_foto=TipoFoto.RECOLHA.value).count() == 1
    assert db_session.query(ContentorFoto).filter_by(pedido_contentor_id=segundo.id, tipo_foto=TipoFoto.RECOLHA.value).count() == 0


def test_cancelar_recolha_na_confirmacao_nao_cria_avaria_foto_ou_recolha(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "57")
    conversa = ConversaWhatsApp(
        telefone="351900009902", estado_atual="v24_recolha_confirmacao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": item.id, "recolhas": [], "fotos_recolha": ["nao-salvar"], "avariado": True, "relato_avaria": "porta bastante danificada"},
    )
    db_session.add(conversa)
    db_session.commit()

    resposta = PedidoV24Agent(db_session).handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="cancel-recolha"
    ))
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert "cancelada" in resposta.lower()
    assert persistido.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert persistido.contentor_avariado is False
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0


def test_flag_off_pula_etapa_de_avaria_no_agente(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "58")
    conversa = ConversaWhatsApp(
        telefone="351900009903", estado_atual="v24_recolha_foto_acao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": item.id, "recolhas": [], "fotos_recolha": ["recolha-58"]},
    )
    db_session.add(conversa)
    db_session.commit()

    resposta = PedidoV24Agent(db_session).handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="2", message_id="flag-off"
    ))

    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert "avaria" not in resposta.lower()


def test_flag_off_permite_recolha_sem_criar_avaria(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "59")

    service.confirmar_recolha(item.id, "motorista", False, None, ["recolha-59"])
    db_session.refresh(item)

    assert item.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert item.contentor_avariado is False
    assert item.relato_avaria is None


def test_flag_off_preserva_historico_de_avaria_existente(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = confirmar_entrega(service, pedido, "60")
    item.contentor_avariado = True
    item.relato_avaria = "avaria historica preservada"
    item.status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    db_session.commit()

    service.confirmar_recolha(item.id, "motorista", False, None, ["recolha-60"])
    db_session.refresh(item)

    assert item.contentor_avariado is True
    assert item.relato_avaria == "avaria historica preservada"
    assert item.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_contentor_recolhido_aparece_na_fila_de_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "61")

    assert item.id in ids(service.contentores_para_despejo())


def test_despejo_conforme_persiste_residuo_e_carga_sem_divergencia(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "62")

    service.confirmar_despejo(item.id, "Entulho Limpo", False, None, fotos=["despejo-62"])
    db_session.refresh(item)

    assert item.residuo_efetivo_vazadouro == "Entulho Limpo"
    assert item.carga_errada is False
    assert item.relato_carga is None


def test_despejo_com_divergencia_persiste_carga_e_relato(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "63")

    service.confirmar_despejo(item.id, "Entulho Limpo", True, "havia plastico misturado", fotos=["despejo-63"])
    db_session.refresh(item)

    assert item.carga_errada is True
    assert item.relato_carga == "havia plastico misturado"
    assert item.status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value


def test_foto_de_despejo_fica_vinculada_ao_item_correto(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Dois despejos", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300", pago=True,
        forma_pagamento="MBWay", pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores
    service.confirmar_entrega_lote(pedido.id, "motorista", 38.7, -9.1, None, [
        {"contentor_id": primeiro.id, "numero_adesivo": "64", "fotos": ["e64"]},
        {"contentor_id": segundo.id, "numero_adesivo": "65", "fotos": ["e65"]},
    ])
    service.confirmar_recolha(primeiro.id, "motorista", False, None)
    service.confirmar_recolha(segundo.id, "motorista", False, None)

    service.confirmar_despejo(primeiro.id, "Entulho Limpo", fotos=["despejo-primeiro"])

    assert db_session.query(ContentorFoto).filter_by(pedido_contentor_id=primeiro.id, tipo_foto=TipoFoto.DESPEJO.value).count() == 1
    assert db_session.query(ContentorFoto).filter_by(pedido_contentor_id=segundo.id, tipo_foto=TipoFoto.DESPEJO.value).count() == 0


def test_cancelar_despejo_na_confirmacao_nao_conclui_nem_salva_foto(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "66")
    conversa = ConversaWhatsApp(
        telefone="351900009904", estado_atual="v24_despejo_confirmacao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": item.id, "despejos": [], "fotos_despejo": ["nao-salvar"], "residuo_contratado": "Entulho Limpo", "residuo_efetivo": "Entulho Limpo", "carga_errada": False, "relato_carga": None},
    )
    db_session.add(conversa)
    db_session.commit()

    resposta = PedidoV24Agent(db_session).handle(conversa, NormalizedWhatsAppMessage(
        telefone=conversa.telefone, tipo="text", texto="3", message_id="cancel-despejo"
    ))
    db_session.expire_all()
    persistido = service.get(pedido.id).contentores[0]

    assert "cancelado" in resposta.lower()
    assert persistido.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.DESPEJO.value).count() == 0


def test_contentor_concluido_nao_reaparece_na_fila_de_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "67")
    service.confirmar_despejo(item.id, "Entulho Limpo")

    assert item.id not in ids(service.contentores_para_despejo())


def test_confirmacao_do_despejo_persiste_conclusao_e_auditoria(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "74")

    service.confirmar_despejo(
        item.id,
        "Entulho Limpo",
        operador="motorista-despejo-detalhado",
        fotos=["despejo-74"],
    )
    db_session.refresh(item)

    assert item.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert item.despejo_feito_por == "motorista-despejo-detalhado"
    assert item.despejo_data_hora is not None


def test_falha_de_validacao_da_entrega_nao_deixa_estado_parcial(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Lote invalido", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="300", pago=True,
        forma_pagamento="MBWay", pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores

    with pytest.raises(ValueError, match="pelo menos uma foto"):
        service.confirmar_entrega_lote_transacional(pedido.id, "motorista", 38.7, -9.1, None, [
            {"contentor_id": primeiro.id, "numero_adesivo": "68", "fotos": ["e68"]},
            {"contentor_id": segundo.id, "numero_adesivo": "69", "fotos": []},
        ])
    db_session.rollback()
    db_session.expire_all()

    assert all(item.status_entrega == StatusEntregaPedido.PENDENTE.value for item in service.get(pedido.id).contentores)
    assert db_session.query(ContentorFoto).count() == 0


def test_retry_de_despejo_concluido_e_rejeitado_sem_duplicar_foto(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_contentor(service)
    item = preparar_para_despejo(service, pedido, "70")
    service.confirmar_despejo(item.id, "Entulho Limpo", fotos=["despejo-70"])

    with pytest.raises(ValueError, match="disponível para despejo|disponivel para despejo"):
        service.confirmar_despejo(item.id, "Entulho Limpo", fotos=["despejo-70"])

    assert db_session.query(ContentorFoto).filter_by(pedido_contentor_id=item.id, tipo_foto=TipoFoto.DESPEJO.value).count() == 1
    assert item.status_ciclo == StatusCicloPedido.CONCLUIDO.value


def test_multiplos_itens_mantem_residuos_avarias_e_fotos_separados(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Separacao", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="400", pago=True,
        forma_pagamento="MBWay", pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo", "Entulho Misto"],
    )
    primeiro, segundo = pedido.contentores
    service.confirmar_entrega_lote(pedido.id, "motorista", 38.7, -9.1, None, [
        {"contentor_id": primeiro.id, "numero_adesivo": "71", "fotos": ["entrega-71"]},
        {"contentor_id": segundo.id, "numero_adesivo": "72", "fotos": ["entrega-72"]},
    ])
    service.confirmar_recolha(primeiro.id, "motorista", True, "lateral bastante amassada", ["recolha-71"])
    service.confirmar_recolha(segundo.id, "motorista", False, None, ["recolha-72"])
    service.confirmar_despejo(primeiro.id, "Entulho Limpo", fotos=["despejo-71"])
    service.confirmar_despejo(segundo.id, "Entulho Misto", fotos=["despejo-72"])
    db_session.expire_all()
    primeiro, segundo = service.get(pedido.id).contentores

    assert (primeiro.residuo_efetivo_vazadouro, segundo.residuo_efetivo_vazadouro) == ("Entulho Limpo", "Entulho Misto")
    assert (primeiro.contentor_avariado, segundo.contentor_avariado) == (True, False)
    assert {foto.url_midia for foto in primeiro.fotos} == {"entrega-71", "recolha-71", "despejo-71"}
    assert {foto.url_midia for foto in segundo.fotos} == {"entrega-72", "recolha-72", "despejo-72"}


def test_financeiro_permanece_inalterado_durante_ciclo_operacional(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Financeiro", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="321.45", pago=False,
        forma_pagamento=None, pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo"],
    )
    financeiro = (Decimal(str(pedido.valor_global)), pedido.status_pagamento, pedido.forma_pagamento)
    item = confirmar_entrega(service, pedido, "73")
    service.confirmar_recolha(item.id, "motorista", False, None)
    service.confirmar_despejo(item.id, "Entulho Limpo")
    db_session.expire_all()
    persistido = service.get(pedido.id)

    assert (Decimal(str(persistido.valor_global)), persistido.status_pagamento, persistido.forma_pagamento) == financeiro
