"""Caracterização operacional fundamental da Carrinha V2.4."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sqlalchemy.orm import sessionmaker

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.core.config import get_settings
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.aluguer import ContentorFoto
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusOperacionalCarrinha,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


def criar_pedido_carrinha(service: PedidoService, residuo="Entulho Limpo"):
    return service.criar(
        nome_cliente="Cliente Carrinha C2",
        telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc),
        valor_global="300",
        pago=False,
        forma_pagamento=None,
        pedido_feito_por="gestor-c2",
        endereco_aproximado="Rua da Carrinha",
        ponto_referencia=None,
        itens=[
            {
                "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
                "residuo_contratado": residuo,
                "horario_agendado": "10:00",
            }
        ],
    )


def confirmar_chegada(service: PedidoService, pedido, frota="17"):
    carrinha = pedido.contentores[0]
    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "motorista-chegada-c2",
        38.7223,
        -9.1393,
        "Portao principal",
        [{"contentor_id": carrinha.id, "numero_adesivo": frota, "fotos": [f"foto-chegada-{frota}"]}],
    )
    service.db.commit()
    service.db.refresh(carrinha)
    return carrinha


def ids(itens):
    return {item.id for item in itens}


def definir_avarias(monkeypatch, habilitadas):
    monkeypatch.setenv("FEATURE_AVARIAS_ENABLED", "true" if habilitadas else "false")
    get_settings.cache_clear()


def partir(service, carrinha, foto="foto-partida"):
    return service.confirmar_partida_carrinha(carrinha.id, "motorista-partida", False, None, [foto])


def preparar_despejo(service, pedido, frota="30"):
    carrinha = confirmar_chegada(service, pedido, frota)
    partir(service, carrinha, f"foto-partida-{frota}")
    return carrinha


def mensagem(telefone, texto, identificador):
    return NormalizedWhatsAppMessage(
        telefone=telefone, tipo="text", texto=texto, message_id=identificador
    )


def financeiro(pedido):
    return Decimal(str(pedido.valor_global)), pedido.status_pagamento, pedido.forma_pagamento


def test_pedido_novo_cria_carrinha_no_estado_operacional_inicial(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    db_session.refresh(carrinha)

    assert carrinha.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
    assert pedido.id not in {item.id for item in service.pedidos_pendentes_entrega()}


def test_carrinha_nova_aparece_na_fila_de_chegada(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    assert carrinha.id in ids(service.carrinhas_aguardando_chegada())
    assert pedido.id in {item.id for item in service.pedidos_carrinha_aguardando_chegada()}


def test_carrinha_aguardando_chegada_nao_aparece_em_partida_despejo_ou_filas_de_contentor(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]

    assert carrinha.id not in ids(service.carrinhas_aguardando_partida())
    assert carrinha.id not in ids(service.carrinhas_aguardando_despejo())
    assert carrinha.id not in ids(service.contentores_para_recolha())
    assert carrinha.id not in ids(service.contentores_para_despejo())


def test_confirmacao_valida_da_chegada_muda_estado_e_remove_da_fila(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido)

    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert persistida.id == carrinha.id
    assert persistida.id not in ids(service.carrinhas_aguardando_chegada())
    assert pedido.id not in {item.id for item in service.pedidos_carrinha_aguardando_chegada()}


def test_carrinha_em_atendimento_aparece_na_partida_mas_nao_no_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "18")

    assert carrinha.id in ids(service.carrinhas_aguardando_partida())
    assert pedido.id in {item.id for item in service.pedidos_carrinha_aguardando_partida()}
    assert carrinha.id not in ids(service.carrinhas_aguardando_despejo())


def test_confirmacao_valida_da_partida_muda_filas_para_despejo(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "19")

    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida-c2", False, None, ["foto-partida-19"]
    )
    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert persistida.id not in ids(service.carrinhas_aguardando_partida())
    assert persistida.id in ids(service.carrinhas_aguardando_despejo())
    assert persistida.id not in ids(service.contentores_para_recolha())
    assert persistida.id not in ids(service.contentores_para_despejo())


def test_confirmacao_valida_do_despejo_conclui_e_remove_das_filas(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "20")
    service.confirmar_partida_carrinha(
        carrinha.id, "motorista-partida-c2", False, None, ["foto-partida-20"]
    )

    service.confirmar_despejo_carrinha(
        carrinha.id,
        "Entulho Limpo",
        False,
        None,
        "motorista-despejo-c2",
        ["foto-despejo-20"],
    )
    db_session.expire_all()
    persistida = service.get(pedido.id).contentores[0]

    assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.CONCLUIDA.value
    assert persistida.status_ciclo == StatusCicloPedido.CONCLUIDO.value
    assert persistida.id not in ids(service.carrinhas_aguardando_chegada())
    assert persistida.id not in ids(service.carrinhas_aguardando_partida())
    assert persistida.id not in ids(service.carrinhas_aguardando_despejo())


def test_cancelamento_antes_do_commit_da_chegada_nao_persiste_dados_parciais(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha_id = pedido.contentores[0].id

    service.confirmar_chegada_carrinha_lote_transacional(
        pedido.id,
        "motorista-nao-persistir",
        38.7,
        -9.1,
        "Nao persistir",
        [{"contentor_id": carrinha_id, "numero_adesivo": "21", "fotos": ["foto-nao-persistir"]}],
    )
    db_session.rollback()

    SessionLocal = sessionmaker(bind=db_session.get_bind())
    with SessionLocal() as verificacao:
        persistida = verificacao.get(PedidoContentor, carrinha_id)
        assert persistida.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
        assert persistida.frota_carrinha is None
        assert persistida.chegada_carrinha_data_hora is None
        assert persistida.chegada_carrinha_latitude is None
        assert persistida.chegada_carrinha_longitude is None
        assert verificacao.query(ContentorFoto).filter_by(
            pedido_contentor_id=carrinha_id, tipo_foto=TipoFoto.ENTREGA.value
        ).count() == 0


def test_chegada_persiste_frota_carrinha(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "31")
    assert carrinha.frota_carrinha == "31"


def test_chegada_mantem_numero_adesivo_contentor_vazio(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "32")
    assert carrinha.numero_adesivo_contentor is None


def test_foto_de_chegada_fica_vinculada_a_carrinha(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "33")
    fotos = db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=carrinha.id, tipo_foto=TipoFoto.ENTREGA.value
    ).all()
    assert [foto.url_midia for foto in fotos] == ["foto-chegada-33"]


def test_chegada_persiste_gps(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "34")
    assert (carrinha.chegada_carrinha_latitude, carrinha.chegada_carrinha_longitude) == (38.7223, -9.1393)


def test_chegada_preserva_ponto_referencia(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "35")
    assert carrinha.chegada_carrinha_ponto_referencia == "Portao principal"


def test_chegada_registra_operador_e_data_hora(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "36")
    assert carrinha.chegada_carrinha_feita_por == "motorista-chegada-c2"
    assert carrinha.chegada_carrinha_data_hora is not None


def test_chegada_calcula_partida_prevista(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "37")
    assert carrinha.partida_prevista_carrinha_data_hora is not None


def test_partida_prevista_corresponde_a_chegada_mais_duas_horas(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "38")
    assert carrinha.partida_prevista_carrinha_data_hora == carrinha.chegada_carrinha_data_hora + timedelta(hours=2)


def test_regra_de_duas_horas_e_informativa_e_nao_bloqueia_partida(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "39")
    agora_atrasado = carrinha.chegada_carrinha_data_hora + timedelta(hours=3)
    assert service.carrinha_atrasada(carrinha, agora_atrasado) is True
    partir(service, carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value


def test_partida_persiste_foto_operacional(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "40")
    partir(service, carrinha, "foto-partida-40")
    assert db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=carrinha.id, tipo_foto=TipoFoto.RECOLHA.value,
        url_midia="foto-partida-40",
    ).count() == 1


def test_partida_preserva_dados_especificos_da_chegada(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "41")
    chegada = (carrinha.frota_carrinha, carrinha.chegada_carrinha_data_hora, carrinha.chegada_carrinha_latitude, carrinha.partida_prevista_carrinha_data_hora)
    partir(service, carrinha)
    db_session.refresh(carrinha)
    assert (carrinha.frota_carrinha, carrinha.chegada_carrinha_data_hora, carrinha.chegada_carrinha_latitude, carrinha.partida_prevista_carrinha_data_hora) == chegada


def test_partida_de_uma_carrinha_nao_altera_outra(db_session):
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Duas Carrinhas", telefone_cliente="351912345678",
        data_planejada=datetime.now(timezone.utc), valor_global="500", pago=False,
        forma_pagamento=None, pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, itens=[
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "10:00"},
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Misto", "horario_agendado": "11:00"},
        ],
    )
    primeira, segunda = pedido.contentores
    service.confirmar_chegada_carrinha_lote_transacional(pedido.id, "motorista", 38.7, -9.1, None, [
        {"contentor_id": primeira.id, "numero_adesivo": "42", "fotos": ["c42"]},
        {"contentor_id": segunda.id, "numero_adesivo": "43", "fotos": ["c43"]},
    ])
    db_session.commit()
    partir(service, primeira)
    db_session.refresh(segunda)
    assert segunda.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert segunda.partida_carrinha_data_hora is None


def test_cancelar_partida_na_confirmacao_nao_grava_partida_ou_foto(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "44")
    conversa = ConversaWhatsApp(
        telefone="351900009944", estado_atual="v24_recolha_confirmacao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": carrinha.id, "recolhas": [], "fotos_recolha": ["nao-salvar"], "avariado": False, "relato_avaria": None},
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = PedidoV24Agent(db_session).handle(conversa, mensagem(conversa.telefone, "2", "cancel-partida"))
    db_session.refresh(carrinha)
    assert "cancelada" in resposta.lower()
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.EM_ATENDIMENTO.value
    assert carrinha.partida_carrinha_data_hora is None
    assert db_session.query(ContentorFoto).filter_by(tipo_foto=TipoFoto.RECOLHA.value).count() == 0


def test_partida_sem_avaria_nao_cria_pendencia_com_flag_on(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "45")
    partir(service, carrinha)
    assert carrinha.contentor_avariado is False
    assert carrinha.status_resolucao_avaria == StatusResolucaoPedido.NAO_APLICA.value


def test_partida_com_avaria_persiste_relato_e_status_com_flag_on(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "46")
    service.confirmar_partida_carrinha(carrinha.id, "motorista", True, "porta lateral muito danificada", ["p46"])
    assert carrinha.contentor_avariado is True
    assert carrinha.relato_avaria == "porta lateral muito danificada"
    assert carrinha.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value


def test_avaria_de_carrinha_fica_somente_no_item_correto(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Avaria isolada", telefone_cliente="351912345678", data_planejada=datetime.now(timezone.utc),
        valor_global="500", pago=False, forma_pagamento=None, pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, itens=[
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "10:00"},
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Misto", "horario_agendado": "11:00"},
        ],
    )
    primeira, segunda = pedido.contentores
    service.confirmar_chegada_carrinha_lote_transacional(pedido.id, "op", 38.7, -9.1, None, [
        {"contentor_id": primeira.id, "numero_adesivo": "47", "fotos": ["c47"]},
        {"contentor_id": segunda.id, "numero_adesivo": "48", "fotos": ["c48"]},
    ])
    db_session.commit()
    service.confirmar_partida_carrinha(primeira.id, "op", True, "porta traseira muito danificada", ["p47"])
    db_session.refresh(segunda)
    assert primeira.contentor_avariado is True
    assert segunda.contentor_avariado is False


def test_flag_off_pula_etapa_de_avaria_da_partida(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = confirmar_chegada(service, pedido, "49")
    conversa = ConversaWhatsApp(
        telefone="351900009949", estado_atual="v24_recolha_foto_acao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": carrinha.id, "recolhas": [], "fotos_recolha": ["p49"]},
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = PedidoV24Agent(db_session).handle(conversa, mensagem(conversa.telefone, "2", "off-skip"))
    assert conversa.estado_atual == "v24_recolha_confirmacao"
    assert "avaria" not in resposta.lower()


def test_flag_off_permite_partida(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "50")
    partir(service, carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value


def test_flag_off_nao_cria_nova_avaria(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "51")
    partir(service, carrinha)
    assert carrinha.contentor_avariado is False
    assert carrinha.relato_avaria is None


def test_flag_off_preserva_historico_existente_da_carrinha(db_session, monkeypatch):
    definir_avarias(monkeypatch, False)
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "52")
    carrinha.contentor_avariado = True
    carrinha.relato_avaria = "avaria historica preservada"
    carrinha.status_resolucao_avaria = StatusResolucaoPedido.PENDENTE.value
    db_session.commit()
    partir(service, carrinha)
    assert carrinha.contentor_avariado is True
    assert carrinha.relato_avaria == "avaria historica preservada"


def test_despejo_persiste_residuo_efetivo(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(service, criar_pedido_carrinha(service), "53")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d53"])
    assert carrinha.residuo_efetivo_vazadouro == "Entulho Limpo"


def test_despejo_conforme_mantem_campos_sem_divergencia(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(service, criar_pedido_carrinha(service), "54")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d54"])
    assert carrinha.carga_errada is False
    assert carrinha.relato_carga is None


def test_despejo_divergente_persiste_carga_e_relato(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(
        service, criar_pedido_carrinha(service, "Entulho Misto"), "55"
    )
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Misto", True, "carga misturada no vazadouro", "op", ["d55"])
    assert carrinha.carga_errada is True
    assert carrinha.relato_carga == "carga misturada no vazadouro"


def test_foto_de_despejo_fica_vinculada_a_carrinha(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(service, criar_pedido_carrinha(service), "56")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d56"])
    assert db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=carrinha.id, tipo_foto=TipoFoto.DESPEJO.value, url_midia="d56"
    ).count() == 1


def test_cancelar_despejo_na_confirmacao_nao_conclui_carrinha(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = preparar_despejo(service, pedido, "57")
    conversa = ConversaWhatsApp(
        telefone="351900009957", estado_atual="v24_despejo_confirmacao",
        contexto_json={"pedido_id": pedido.id, "contentor_id": carrinha.id, "despejos": [], "fotos_despejo": ["nao-salvar"], "residuo_contratado": "Entulho Limpo", "residuo_efetivo": "Entulho Limpo", "carga_errada": False, "relato_carga": None},
    )
    db_session.add(conversa)
    db_session.commit()
    resposta = PedidoV24Agent(db_session).handle(conversa, mensagem(conversa.telefone, "3", "cancel-dump"))
    db_session.refresh(carrinha)
    assert "cancelado" in resposta.lower()
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
    assert carrinha.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value


def test_falha_de_validacao_da_chegada_nao_deixa_lote_parcial(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    carrinha = pedido.contentores[0]
    with pytest.raises(ValueError, match="foto na chegada"):
        service.confirmar_chegada_carrinha_lote_transacional(
            pedido.id, "op", 38.7, -9.1, None,
            [{"contentor_id": carrinha.id, "numero_adesivo": "58", "fotos": []}],
        )
    db_session.rollback()
    db_session.refresh(carrinha)
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_CHEGADA.value
    assert carrinha.frota_carrinha is None


def test_retry_de_despejo_concluido_e_rejeitado_sem_duplicar(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(service, criar_pedido_carrinha(service), "59")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d59"])
    with pytest.raises(ValueError, match="disponível para despejo|disponivel para despejo"):
        service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d59"])
    assert db_session.query(ContentorFoto).filter_by(
        pedido_contentor_id=carrinha.id, tipo_foto=TipoFoto.DESPEJO.value
    ).count() == 1


def test_duas_carrinhas_mantem_frota_fotos_avaria_e_residuo_separados(db_session, monkeypatch):
    definir_avarias(monkeypatch, True)
    service = PedidoService(db_session)
    pedido = service.criar(
        nome_cliente="Separadas", telefone_cliente="351912345678", data_planejada=datetime.now(timezone.utc),
        valor_global="500", pago=False, forma_pagamento=None, pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, itens=[
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Limpo", "horario_agendado": "10:00"},
            {"tipo_equipamento": "CARRINHA", "residuo_contratado": "Entulho Misto", "horario_agendado": "11:00"},
        ],
    )
    primeira, segunda = pedido.contentores
    service.confirmar_chegada_carrinha_lote_transacional(pedido.id, "op", 38.7, -9.1, None, [
        {"contentor_id": primeira.id, "numero_adesivo": "60", "fotos": ["c60"]},
        {"contentor_id": segunda.id, "numero_adesivo": "61", "fotos": ["c61"]},
    ])
    db_session.commit()
    service.confirmar_partida_carrinha(primeira.id, "op", True, "porta lateral muito danificada", ["p60"])
    service.confirmar_partida_carrinha(segunda.id, "op", False, None, ["p61"])
    service.confirmar_despejo_carrinha(primeira.id, "Entulho Limpo", False, None, "op", ["d60"])
    service.confirmar_despejo_carrinha(segunda.id, "Entulho Misto", False, None, "op", ["d61"])
    db_session.expire_all()
    primeira, segunda = service.get(pedido.id).contentores
    assert (primeira.frota_carrinha, segunda.frota_carrinha) == ("60", "61")
    assert (primeira.contentor_avariado, segunda.contentor_avariado) == (True, False)
    assert (primeira.residuo_efetivo_vazadouro, segunda.residuo_efetivo_vazadouro) == ("Entulho Limpo", "Entulho Misto")
    assert {f.url_midia for f in primeira.fotos} == {"c60", "p60", "d60"}
    assert {f.url_midia for f in segunda.fotos} == {"c61", "p61", "d61"}


def test_operacao_de_carrinha_nao_modifica_contentor(db_session):
    service = PedidoService(db_session)
    pedido_contentor = service.criar(
        nome_cliente="Contentor isolado", telefone_cliente="351900000001", data_planejada=datetime.now(timezone.utc),
        valor_global="100", pago=False, forma_pagamento=None, pedido_feito_por="gestor", endereco_aproximado="Rua",
        ponto_referencia=None, residuos=["Entulho Limpo"],
    )
    contentor = pedido_contentor.contentores[0]
    pedido_carrinha = criar_pedido_carrinha(service)
    carrinha = preparar_despejo(service, pedido_carrinha, "62")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d62"])
    db_session.refresh(contentor)
    assert contentor.status_entrega == StatusEntregaPedido.PENDENTE.value
    assert contentor.status_recolha == StatusRecolhaPedido.PENDENTE.value
    assert contentor.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value


def test_carrinha_nunca_recebe_numero_adesivo_no_ciclo_normal(db_session):
    service = PedidoService(db_session)
    carrinha = preparar_despejo(service, criar_pedido_carrinha(service), "63")
    assert carrinha.numero_adesivo_contentor is None
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d63"])
    assert carrinha.numero_adesivo_contentor is None


def test_ciclo_da_carrinha_nao_altera_valor_global(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    valor = Decimal(str(pedido.valor_global))
    carrinha = preparar_despejo(service, pedido, "64")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d64"])
    db_session.refresh(pedido)
    assert Decimal(str(pedido.valor_global)) == valor


def test_ciclo_da_carrinha_nao_altera_status_pagamento(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    status = pedido.status_pagamento
    carrinha = preparar_despejo(service, pedido, "65")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d65"])
    db_session.refresh(pedido)
    assert pedido.status_pagamento == status == StatusPagamento.PENDENTE.value


def test_ciclo_da_carrinha_nao_altera_forma_pagamento(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    forma = pedido.forma_pagamento
    carrinha = preparar_despejo(service, pedido, "66")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d66"])
    db_session.refresh(pedido)
    assert pedido.forma_pagamento == forma is None


def test_ciclo_da_carrinha_nao_registra_recebimento_financeiro(db_session):
    service = PedidoService(db_session)
    pedido = criar_pedido_carrinha(service)
    antes = financeiro(pedido)
    carrinha = preparar_despejo(service, pedido, "67")
    service.confirmar_despejo_carrinha(carrinha.id, "Entulho Limpo", False, None, "op", ["d67"])
    db_session.refresh(pedido)
    assert financeiro(pedido) == antes
    assert pedido.pagamento_recebido_em is None


def test_campos_legados_sao_espelhados_sem_substituir_estado_operacional(db_session):
    service = PedidoService(db_session)
    carrinha = confirmar_chegada(service, criar_pedido_carrinha(service), "68")
    assert carrinha.status_entrega == StatusEntregaPedido.ENTREGUE.value
    assert carrinha.entrega_data_hora == carrinha.chegada_carrinha_data_hora
    partir(service, carrinha)
    assert carrinha.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
    assert carrinha.recolha_data_hora == carrinha.partida_carrinha_data_hora
    assert carrinha.status_operacional_carrinha == StatusOperacionalCarrinha.AGUARDANDO_DESPEJO.value
