from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.aluguer_service import AluguerService
from app.services.contentor_service import ContentorService
from app.services.reminder_service import ReminderService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/contentores")
def listar_contentores(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {"id": contentor.id, "codigo": contentor.codigo, "status": contentor.status.value}
        for contentor in ContentorService(db).listar_contentores()
    ]


@router.get("/alugueres/vencendo-amanha")
def listar_vencendo_amanha(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": aluguer.id,
            "cliente": aluguer.nome_cliente,
            "telefone": aluguer.telefone_cliente,
            "data_vencimento": aluguer.data_vencimento.isoformat(),
            "status": aluguer.status.value,
        }
        for aluguer in AluguerService(db).listar_vencendo_amanha()
    ]


@router.get("/lembretes")
def listar_lembretes(db: Session = Depends(get_db)) -> dict[str, list[str]]:
    return {"mensagens": ReminderService(db).mensagens_vencendo_amanha()}
