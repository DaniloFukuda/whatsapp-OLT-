from app.models.contentor import Contentor, StatusContentor
from app.services.seed_service import SeedService


def test_seed_dos_20_contentores_e_idempotente(db_session):
    service = SeedService(db_session)

    first_run = service.seed_contentores_iniciais()
    second_run = service.seed_contentores_iniciais()
    contentores = db_session.query(Contentor).order_by(Contentor.codigo).all()

    assert first_run == [f"C{index:02d}" for index in range(1, 21)]
    assert second_run == []
    assert [contentor.codigo for contentor in contentores] == [f"C{index:02d}" for index in range(1, 21)]
    assert {contentor.status for contentor in contentores} == {StatusContentor.DISPONIVEL}
