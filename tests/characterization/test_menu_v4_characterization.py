"""Testes de Caracterização - Menu V4 (Pedidos v2.4).

Documenta o comportamento atual do fluxo de menu principal v2.4 antes de refatorações.
Cobre: texto do menu, navegação por opções numéricas e textuais, comandos de menu/cancelamento,
e diferenças de autorização entre perfis GESTOR e FUNCIONARIO.
"""

from unittest.mock import MagicMock

import pytest

from app.agents.pedido_v24_agent import PedidoV24Agent
from app.agents.whatsapp_router_agent import (
    WhatsappRouterAgent,
    MAIN_MENU,
    MENU_COMMANDS,
    CANCEL_COMMANDS,
    APP_DISPLAY_NAME,
)
from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.operador import PerfilOperador
from app.models.conversa import ConversaWhatsApp


def msg(text=None, *, kind="text", media=None, lat=None, lon=None, phone="351900009900"):
    return NormalizedWhatsAppMessage(
        telefone=phone, tipo=kind, texto=text, media_id=media,
        latitude=lat, longitude=lon, message_id=media or "m",
    )


def contact_msg(name=None, contact_phone=None, *, phone="351900009900"):
    return NormalizedWhatsAppMessage(
        telefone=phone,
        tipo="contacts",
        message_id="m-contact",
        contact_name=name,
        contact_phone=contact_phone,
    )


def liberar_operadores(monkeypatch):
    """Configura operadores autorizados para testes."""
    from types import SimpleNamespace
    import app.services.operador_service as operador_service_module

    settings = SimpleNamespace(
        authorized_operator_phone="",
        authorized_operator_phones=",".join((
            "351900000000", "351900000222", "351900000333", "351900009900",
            "351900009901", "351900010001", "351900010002", "351900010003",
            "351900010004", "351900010005", "351900010006", "351900010007",
            "351900010008", "351900010009",
        )),
    )
    monkeypatch.setattr(operador_service_module, "get_settings", lambda: settings)


def mock_decisao_funcionario(_self, telefone):
    """Retorna decisão de acesso para perfil FUNCIONARIO."""
    from types import SimpleNamespace
    from app.models.operador import PerfilOperador
    return SimpleNamespace(
        autorizado=True,
        perfil=PerfilOperador.FUNCIONARIO,
        operador=SimpleNamespace(
            telefone_whatsapp=telefone,
            nome="Motorista Teste",
            ativo=True,
            perfil=PerfilOperador.FUNCIONARIO
        )
    )


def mock_decisao_gestor(_self, telefone):
    """Retorna decisão de acesso para perfil GESTOR."""
    from types import SimpleNamespace
    from app.models.operador import PerfilOperador
    return SimpleNamespace(
        autorizado=True,
        perfil=PerfilOperador.GESTOR,
        operador=SimpleNamespace(
            telefone_whatsapp=telefone,
            nome="Gestor Teste",
            ativo=True,
            perfil=PerfilOperador.GESTOR
        )
    )


def mock_buscar_funcionario(_self, telefone):
    """Retorna operador FUNCIONARIO mock."""
    from types import SimpleNamespace
    from app.models.operador import PerfilOperador
    return SimpleNamespace(
        telefone_whatsapp=telefone,
        nome="Motorista Teste",
        ativo=True,
        perfil=PerfilOperador.FUNCIONARIO
    )


def mock_buscar_gestor(_self, telefone):
    """Retorna operador GESTOR mock."""
    from types import SimpleNamespace
    from app.models.operador import PerfilOperador
    return SimpleNamespace(
        telefone_whatsapp=telefone,
        nome="Gestor Teste",
        ativo=True,
        perfil=PerfilOperador.GESTOR
    )


def criar_mock_pedido_com_pendentes(nome="Cliente Teste"):
    """Cria mock de pedido com estrutura necessária para testes v24."""
    return MagicMock(
        id=1,
        nome_cliente=nome,
        data_planejada=MagicMock(strftime=lambda x: "01/01/2024"),
        contentores=[],
        status_pagamento="PENDENTE",
        valor_global=100,
    )


def reset_conversa(db_session, telefone="351900009900"):
    """Reseta estado da conversa para idle."""
    conversa = db_session.query(ConversaWhatsApp).filter(
        ConversaWhatsApp.telefone == telefone
    ).first()
    if conversa:
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        db_session.commit()


def mock_servicos_v24_entrega(router):
    """Configura mocks nos DOIS services (router e v24_agent) para entrega."""
    mock_pedido = criar_mock_pedido_com_pendentes()
    # Router usa pedido_service para DECIDIR chamar v24
    router.pedido_service.pedidos_pendentes_entrega = MagicMock(return_value=[mock_pedido])
    router.pedido_service.carrinhas_aguardando_chegada = MagicMock(return_value=[])
    # v24_agent usa SEU PRÓPRIO service (instância separada) para EXECUTAR
    router.pedido_v24_agent.service.pedidos_pendentes_entrega = MagicMock(return_value=[mock_pedido])
    router.pedido_v24_agent.service.carrinhas_aguardando_chegada = MagicMock(return_value=[])
    router.pedido_v24_agent.service.get = MagicMock(return_value=mock_pedido)


def mock_servicos_v24_recolha(router):
    """Configura mocks nos DOIS services para recolha/partida."""
    mock_pedido = criar_mock_pedido_com_pendentes()
    # Router CHECK (usa estes nomes de método):
    router.pedido_service.contentores_para_recolha = MagicMock(return_value=[mock_pedido])
    router.pedido_service.carrinhas_aguardando_partida = MagicMock(return_value=[])
    # v24_agent EXECUÇÃO (usa estes nomes de método):
    router.pedido_v24_agent.service.pedidos_para_recolha = MagicMock(return_value=[mock_pedido])
    router.pedido_v24_agent.service.pedidos_carrinha_aguardando_partida = MagicMock(return_value=[])
    router.pedido_v24_agent.service.get = MagicMock(return_value=mock_pedido)


def mock_servicos_v24_despejo(router):
    """Configura mocks nos DOIS services para despejo."""
    mock_pedido = criar_mock_pedido_com_pendentes()
    # Router chama v24_agent.start_despejo diretamente (sem check prévio no router)
    # v24_agent EXECUÇÃO:
    router.pedido_v24_agent.service.pedidos_para_despejo = MagicMock(return_value=[mock_pedido])
    router.pedido_v24_agent.service.pedidos_carrinha_aguardando_despejo = MagicMock(return_value=[])
    router.pedido_v24_agent.service.get = MagicMock(return_value=mock_pedido)


class TestMenuV4TextoExato:
    """Caracteriza o texto exato do Menu Principal V4."""

    def test_menu_v4_texto_completo_com_emojis_e_app_name(self):
        """Menu V4 contém nome da aplicação, 5 opções numeradas com emojis e instrução final."""
        expected = (
            f"🤖 Menu Principal • {APP_DISPLAY_NAME}\n\n"
            "1. 🟢 Novo Pedido\n"
            "2. 🚛 Confirmar Chegada / Entrega\n"
            "3. 📦 Confirmar Recolha / Partida\n"
            "4. ♻️ Confirmar Despejo no Vazadouro\n"
            "5. 📊 Painel de Controle Operacional\n\n"
            "Digite o número da opção desejada."
        )
        assert MAIN_MENU == expected

    def test_menu_v4_contem_todas_opcoes_numeradas_1_a_5(self):
        """Menu contém exatamente as 5 opções numeradas de 1 a 5."""
        for i in range(1, 6):
            assert f"{i}. " in MAIN_MENU

    def test_menu_v4_opcao_1_novo_pedido_com_emoji_verde(self):
        assert "1. 🟢 Novo Pedido" in MAIN_MENU

    def test_menu_v4_opcao_2_chegada_entrega_com_emoji_caminhao(self):
        assert "2. 🚛 Confirmar Chegada / Entrega" in MAIN_MENU

    def test_menu_v4_opcao_3_recolha_partida_com_emoji_caixa(self):
        assert "3. 📦 Confirmar Recolha / Partida" in MAIN_MENU

    def test_menu_v4_opcao_4_despejo_com_emoji_reciclagem(self):
        assert "4. ♻️ Confirmar Despejo no Vazadouro" in MAIN_MENU

    def test_menu_v4_opcao_5_painel_com_emoji_grafico(self):
        assert "5. 📊 Painel de Controle Operacional" in MAIN_MENU


class TestMenuV4Opcao1NovoPedido:
    """Caracteriza comportamento da opção 1 - Novo Pedido."""

    def test_opcao_1_numerica_inicia_cadastro_v24_para_gestor(self, db_session, monkeypatch):
        """Opção '1' inicia cadastro v2.4 para perfil GESTOR."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("1"))
        assert "tipo de solicitação" in response.lower() or "tipo de solicita" in response.lower()
        assert "contentor" in response.lower() or "carrinha" in response.lower()

    def test_opcao_1_texto_novo_pedido_inicia_cadastro_para_gestor(self, db_session, monkeypatch):
        """Alias textual 'novo pedido' inicia cadastro v2.4 para GESTOR."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("novo pedido"))
        assert "tipo de solicitação" in response.lower() or "tipo de solicita" in response.lower()

    def test_opcao_1_alias_cadastrar_pedido_inicia_cadastro_para_gestor(self, db_session, monkeypatch):
        """Alias textual 'cadastrar pedido' inicia cadastro v2.4 para GESTOR."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("cadastrar pedido"))
        assert "tipo de solicitação" in response.lower() or "tipo de solicita" in response.lower()

    def test_opcao_1_bloqueada_para_funcionario_retorna_mensagem_permissao(self, db_session, monkeypatch):
        """Perfil FUNCIONARIO recebe mensagem de permissão ao tentar opção 1."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("1", phone="351900010001"))
        assert "perfil de motorista" in response.lower()
        # Texto real: "não possui permissão" - matching flexível para encoding ascii
        assert "permite" not in response.lower() or "não possui permiss" in response.lower()
        assert "cadastrar pedidos" in response.lower()

    def test_opcao_1_funcionario_nao_direciona_para_recolha_apenas_mensagem_erro(self, db_session, monkeypatch):
        """FUNCIONARIO que envia '1' recebe apenas mensagem de erro (não inicia recolha)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("1", phone="351900010001"))
        # Comportamento atual: apenas mensagem de erro, NÃO inicia fluxo de recolha
        assert "recolha" not in response.lower()


class TestMenuV4Opcao2ChegadaEntrega:
    """Caracteriza comportamento da opção 2 - Confirmar Chegada / Entrega."""

    def test_opcao_2_numerica_inicia_entrega_v24_com_pedidos_pendentes(self, db_session, monkeypatch):
        """Opção '2' inicia entrega v2.4 quando há pedidos pendentes de entrega."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router)

        response = router.handle(msg("2"))
        # Prompt real do v24_agent.start_entrega: "Selecione o cliente para confirmar a chegada / entrega"
        assert "selecione o cliente" in response.lower()
        assert "chegada" in response.lower()

    def test_opcao_2_alias_confirmar_entrega_contentor(self, db_session, monkeypatch):
        """Alias 'confirmar entrega de contentor' inicia entrega v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router)

        response = router.handle(msg("confirmar entrega de contentor"))
        assert "selecione o cliente" in response.lower()
        assert "chegada" in response.lower()

    def test_opcao_2_alias_confirmar_chegada(self, db_session, monkeypatch):
        """Alias 'confirmar chegada' inicia entrega v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router)

        response = router.handle(msg("confirmar chegada"))
        assert "selecione o cliente" in response.lower()
        assert "chegada" in response.lower()

    def test_opcao_2_alias_chegada(self, db_session, monkeypatch):
        """Alias 'chegada' inicia entrega v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router)

        response = router.handle(msg("chegada"))
        assert "selecione o cliente" in response.lower()
        assert "chegada" in response.lower()

    def test_opcao_2_sem_pedidos_v24_retorna_mensagem_v24_nao_legacy(self, db_session, monkeypatch):
        """Sem pedidos v2.4 pendentes (router check vazio), retorna mensagem do v24_agent (não cai para legacy)."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        # Router check vazio -> v24_agent.start_entrega retorna "Não existem pedidos pendentes de entrega."
        router.pedido_service.pedidos_pendentes_entrega = MagicMock(return_value=[])
        router.pedido_service.carrinhas_aguardando_chegada = MagicMock(return_value=[])

        response = router.handle(msg("2"))
        # Comportamento ATUAL: v24_agent retorna sua própria mensagem, NÃO cai para legacy
        # Match flexível para encoding: "não" pode vir como "nao"
        assert "existem pedidos pendentes de entrega" in response.lower()


class TestMenuV4Opcao3RecolhaPartida:
    """Caracteriza comportamento da opção 3 - Confirmar Recolha / Partida."""

    def test_opcao_3_numerica_inicia_recolha_v24_com_pendentes(self, db_session, monkeypatch):
        """Opção '3' inicia recolha v2.4 quando há contentores/carrinhas pendentes."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router)

        response = router.handle(msg("3"))
        # Prompt real: "Selecione o pedido para confirmar recolha / partida"
        assert "selecione o pedido" in response.lower()
        assert "recolha" in response.lower()

    def test_opcao_3_alias_confirmar_recolha_contentor(self, db_session, monkeypatch):
        """Alias 'confirmar recolha de contentor' inicia recolha v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router)

        response = router.handle(msg("confirmar recolha de contentor"))
        assert "selecione o pedido" in response.lower()
        assert "recolha" in response.lower()

    def test_opcao_3_alias_confirmar_partida(self, db_session, monkeypatch):
        """Alias 'confirmar partida' inicia recolha v2.4 (carrinhas)."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router)

        response = router.handle(msg("confirmar partida"))
        assert "selecione o pedido" in response.lower()
        assert "recolha" in response.lower()

    def test_opcao_3_alias_partida(self, db_session, monkeypatch):
        """Alias 'partida' inicia recolha v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router)

        response = router.handle(msg("partida"))
        assert "selecione o pedido" in response.lower()
        assert "recolha" in response.lower()

    def test_opcao_3_sem_pendentes_v24_retorna_mensagem_v24(self, db_session, monkeypatch):
        """Sem pendentes v2.4 (router check vazio), retorna mensagem do v24_agent (não cai para legacy)."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        router.pedido_service.contentores_para_recolha = MagicMock(return_value=[])
        router.pedido_service.carrinhas_aguardando_partida = MagicMock(return_value=[])

        response = router.handle(msg("3"))
        # Comportamento ATUAL: v24_agent retorna mensagem específica
        # Match flexível: pode ser "não existem equipamentos..." ou "não existem contentores entregues..."
        assert "não existem" in response.lower() or "nao existem" in response.lower()
        assert "recolha" in response.lower()


class TestMenuV4Opcao4Despejo:
    """Caracteriza comportamento da opção 4 - Confirmar Despejo no Vazadouro."""

    def test_opcao_4_numerica_inicia_despejo_v24(self, db_session, monkeypatch):
        """Opção '4' inicia despejo v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_despejo(router)

        response = router.handle(msg("4"))
        # Prompt real: "Selecione o pedido para despejo no vazadouro"
        assert "selecione o pedido" in response.lower()
        assert "vazadouro" in response.lower()

    def test_opcao_4_alias_confirmar_despejo(self, db_session, monkeypatch):
        """Alias 'confirmar despejo' inicia despejo v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_despejo(router)

        response = router.handle(msg("confirmar despejo"))
        assert "selecione o pedido" in response.lower()
        assert "vazadouro" in response.lower()

    def test_opcao_4_alias_confirmar_despejo_no_vazadouro(self, db_session, monkeypatch):
        """Alias 'confirmar despejo no vazadouro' inicia despejo v2.4."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_despejo(router)

        response = router.handle(msg("confirmar despejo no vazadouro"))
        assert "selecione o pedido" in response.lower()
        assert "vazadouro" in response.lower()


class TestMenuV4Opcao5Painel:
    """Caracteriza comportamento da opção 5 - Painel de Controle Operacional."""

    def test_opcao_5_numerica_chama_resumo_operacional(self, db_session, monkeypatch):
        """Opção '5' chama comando operacional 'resumo'."""
        liberar_operadores(monkeypatch)
        reset_conversa(db_session)
        router = WhatsappRouterAgent(db_session)

        response = router.handle(msg("5"))
        # Deve conter informações de resumo (contentores, status, etc.)
        assert "contentor" in response.lower() or "resumo" in response.lower() or "painel" in response.lower()


class TestMenuV4ComandoMenu:
    """Caracteriza comando 'menu' e aliases."""

    def test_comando_menu_reseta_estado_e_mostra_menu(self, db_session, monkeypatch):
        """Comando 'menu' reseta conversa para idle e mostra menu principal."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        # Primeiro iniciar um fluxo
        router.handle(msg("novo pedido"))
        # Depois chamar menu
        response = router.handle(msg("menu"))
        assert response == MAIN_MENU

    def test_comando_inicio_alias_menu(self, db_session, monkeypatch):
        """Alias 'inicio' funciona como menu."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("inicio"))
        assert response == MAIN_MENU

    def test_comando_inicio_com_acento_alias_menu(self, db_session, monkeypatch):
        """Alias 'início' (com acento) funciona como menu."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("início"))
        assert response == MAIN_MENU

    def test_comando_menu_limpa_contexto_antes_de_mostrar(self, db_session, monkeypatch):
        """Menu limpa estado_atual e contexto_json da conversa."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        conversa = db_session.query(ConversaWhatsApp).first()
        assert conversa.estado_atual.startswith("v24_")

        router.handle(msg("menu"))
        db_session.refresh(conversa)
        assert conversa.estado_atual == "idle"
        assert conversa.contexto_json == {}


class TestMenuV4CancelarAntesConfirmacao:
    """Caracteriza cancelamento antes da confirmação final."""

    def test_cancelar_comando_cancela_fluxo_ativo_v24(self, db_session, monkeypatch):
        """Comando 'cancelar' cancela fluxo v2.4 ativo e retorna mensagem de cancelamento."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("cancelar"))
        assert "operação cancelada" in response.lower()
        assert "nenhuma alteração foi salva" in response.lower()

    def test_cancela_comando_cancela_fluxo_ativo_v24(self, db_session, monkeypatch):
        """Comando 'cancela' (sem r) cancela fluxo v2.4 ativo."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("cancela"))
        assert "operação cancelada" in response.lower()

    def test_sair_comando_cancela_fluxo_ativo_v24(self, db_session, monkeypatch):
        """Comando 'sair' cancela fluxo v2.4 ativo."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("sair"))
        assert "operação cancelada" in response.lower()

    def test_parar_comando_cancela_fluxo_ativo_v24(self, db_session, monkeypatch):
        """Comando 'parar' cancela fluxo v2.4 ativo."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("parar"))
        assert "operação cancelada" in response.lower()

    def test_voltar_comando_cancela_fluxo_ativo_v24(self, db_session, monkeypatch):
        """Comando 'voltar' cancela fluxo v2.4 ativo."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("voltar"))
        assert "operação cancelada" in response.lower()

    def test_zero_comando_cancela_fluxo_ativo_v24_exceto_adesivo(self, db_session, monkeypatch):
        """Comando '0' cancela fluxo v2.4, exceto no estado v24_entrega_adesivo."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        router.handle(msg("novo pedido"))
        response = router.handle(msg("0"))
        assert "operação cancelada" in response.lower()

    def test_cancelar_sem_fluxo_ativo_retorna_menu(self, db_session, monkeypatch):
        """Cancelar sem fluxo ativo retorna mensagem informativa + menu."""
        liberar_operadores(monkeypatch)
        router = WhatsappRouterAgent(db_session)

        response = router.handle(msg("cancelar"))
        assert "nenhuma operação em andamento" in response.lower()
        assert MAIN_MENU in response


class TestMenuV4AliasesTextuais:
    """Caracteriza aliases textuais já existentes para cada opção."""

    def test_aliases_opcao_1_novo_pedido(self, db_session, monkeypatch):
        """Aliases conhecidos para opção 1: 'novo pedido', 'cadastrar pedido'."""
        liberar_operadores(monkeypatch)

        for alias in ["novo pedido", "cadastrar pedido"]:
            reset_conversa(db_session)
            router = WhatsappRouterAgent(db_session)
            response = router.handle(msg(alias))
            assert "tipo de solicitação" in response.lower() or "tipo de solicita" in response.lower()

    def test_aliases_opcao_2_entrega(self, db_session, monkeypatch):
        """Aliases conhecidos para opção 2."""
        liberar_operadores(monkeypatch)

        aliases = [
            "confirmar entrega de contentor",
            "confirmar entrega do lote",
            "confirmar chegada",
            "confirmar chegada / entrega",
            "chegada",
        ]
        for alias in aliases:
            reset_conversa(db_session)
            router = WhatsappRouterAgent(db_session)
            mock_servicos_v24_entrega(router)
            response = router.handle(msg(alias))
            assert "selecione o cliente" in response.lower()
            assert "chegada" in response.lower()

    def test_aliases_opcao_3_recolha(self, db_session, monkeypatch):
        """Aliases conhecidos para opção 3."""
        liberar_operadores(monkeypatch)

        aliases = [
            "confirmar recolha de contentor",
            "confirmar partida",
            "confirmar recolha / partida",
            "partida",
        ]
        for alias in aliases:
            reset_conversa(db_session)
            router = WhatsappRouterAgent(db_session)
            mock_servicos_v24_recolha(router)
            response = router.handle(msg(alias))
            assert "selecione o pedido" in response.lower()
            assert "recolha" in response.lower()

    def test_aliases_opcao_4_despejo(self, db_session, monkeypatch):
        """Aliases conhecidos para opção 4."""
        liberar_operadores(monkeypatch)

        aliases = [
            "confirmar despejo no vazadouro",
            "confirmar despejo",
        ]
        for alias in aliases:
            reset_conversa(db_session)
            router = WhatsappRouterAgent(db_session)
            mock_servicos_v24_despejo(router)
            response = router.handle(msg(alias))
            assert "selecione o pedido" in response.lower()
            assert "vazadouro" in response.lower()


class TestMenuV4AutorizacaoGestorFuncionario:
    """Caracteriza diferenças de autorização entre GESTOR e FUNCIONARIO."""

    def test_gestor_pode_acessar_todas_opcoes_menu(self, db_session, monkeypatch):
        """GESTOR tem acesso a todas as 5 opções do menu."""
        liberar_operadores(monkeypatch)

        # Opção 1 - usa telefone padrão (351900009900) que é GESTOR
        reset_conversa(db_session)
        router1 = WhatsappRouterAgent(db_session)
        r1 = router1.handle(msg("1"))
        assert "tipo de solicitação" in r1.lower() or "tipo de solicita" in r1.lower()

        # Opção 2
        reset_conversa(db_session)
        router2 = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router2)
        r2 = router2.handle(msg("2"))
        assert "selecione o cliente" in r2.lower()
        assert "chegada" in r2.lower()

        # Opção 3
        reset_conversa(db_session)
        router3 = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router3)
        r3 = router3.handle(msg("3"))
        assert "selecione o pedido" in r3.lower()
        assert "recolha" in r3.lower()

        # Opção 4
        reset_conversa(db_session)
        router4 = WhatsappRouterAgent(db_session)
        mock_servicos_v24_despejo(router4)
        r4 = router4.handle(msg("4"))
        assert "selecione o pedido" in r4.lower()
        assert "vazadouro" in r4.lower()

        # Opção 5
        reset_conversa(db_session)
        router5 = WhatsappRouterAgent(db_session)
        r5 = router5.handle(msg("5"))
        assert "contentor" in r5.lower() or "resumo" in r5.lower() or "painel" in r5.lower()

    def test_funcionario_bloqueado_opcao_1_novo_pedido(self, db_session, monkeypatch):
        """FUNCIONARIO não pode criar novo pedido (opção 1)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        reset_conversa(db_session, "351900010001")
        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("1", phone="351900010001"))
        assert "perfil de motorista" in response.lower()
        # Match flexível para encoding: "não possui permissão" pode vir como "nao possui permissao"
        assert "permiss" in response.lower()

    def test_funcionario_pode_acessar_opcao_2_entrega(self, db_session, monkeypatch):
        """FUNCIONARIO pode acessar opção 2 (entrega/chegada)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        reset_conversa(db_session, "351900010001")
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_entrega(router)

        response = router.handle(msg("2", phone="351900010001"))
        assert "selecione o cliente" in response.lower()
        assert "chegada" in response.lower()

    def test_funcionario_pode_acessar_opcao_3_recolha(self, db_session, monkeypatch):
        """FUNCIONARIO pode acessar opção 3 (recolha/partida)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        reset_conversa(db_session, "351900010001")
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_recolha(router)

        response = router.handle(msg("3", phone="351900010001"))
        assert "selecione o pedido" in response.lower()
        assert "recolha" in response.lower()

    def test_funcionario_pode_acessar_opcao_4_despejo(self, db_session, monkeypatch):
        """FUNCIONARIO pode acessar opção 4 (despejo)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        reset_conversa(db_session, "351900010001")
        router = WhatsappRouterAgent(db_session)
        mock_servicos_v24_despejo(router)

        response = router.handle(msg("4", phone="351900010001"))
        assert "selecione o pedido" in response.lower()
        assert "vazadouro" in response.lower()

    def test_funcionario_pode_acessar_opcao_5_painel(self, db_session, monkeypatch):
        """FUNCIONARIO pode acessar opção 5 (painel/resumo operacional)."""
        import app.services.operador_service as operador_service_module

        monkeypatch.setattr(operador_service_module.OperadorService, "decidir_acesso", mock_decisao_funcionario)
        monkeypatch.setattr(operador_service_module.OperadorService, "buscar_por_telefone", mock_buscar_funcionario)

        reset_conversa(db_session, "351900010001")
        router = WhatsappRouterAgent(db_session)
        response = router.handle(msg("5", phone="351900010001"))
        assert "contentor" in response.lower() or "resumo" in response.lower() or "painel" in response.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])