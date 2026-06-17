from app.models.aluguer import StatusAluguer
from app.models.contentor import Contentor, StatusContentor
from app.models.conversa import ConversaWhatsApp
from app.services.aluguer_service import AluguerService
from app.services.demo_reset_service import DemoResetService
from app.services.seed_service import SeedService


def test_reset_recoloca_contentores_como_disponivel(db_session):
    SeedService(db_session).seed_contentores_iniciais()
    aluguer = AluguerService(db_session).registrar_novo_aluguer(
        nome_cliente="Cliente Demo",
        telefone_cliente="351900000001",
        valor="70",
        forma_pagamento="transferencia",
        pago=True,
    )
    db_session.add(ConversaWhatsApp(telefone="556198266551", estado_atual="confirmado", contexto_json={}))
    db_session.commit()

    assert aluguer.contentor.status == StatusContentor.ALUGADO

    result = DemoResetService(db_session).reset()
    contentores = db_session.query(Contentor).filter(Contentor.codigo.in_([f"C{index:02d}" for index in range(1, 21)])).all()

    assert result["alugueres_removidos"] == 1
    assert result["conversas_removidas"] == 1
    assert db_session.query(ConversaWhatsApp).count() == 0
    assert len(contentores) == 20
    assert {contentor.status for contentor in contentores} == {StatusContentor.DISPONIVEL}
