from datetime import datetime

from sqlalchemy.orm import Session

from app.services.aluguer_service import AluguerService


class ReminderService:
    def __init__(self, db: Session):
        self.aluguer_service = AluguerService(db)

    def mensagens_vencendo_amanha(self, now: datetime | None = None) -> list[str]:
        mensagens = []
        for aluguer in self.aluguer_service.listar_vencendo_amanha(now=now):
            mensagens.append(
                "Lembrete: aluguer "
                f"#{aluguer.id} de {aluguer.nome_cliente} ({aluguer.telefone_cliente}) "
                f"vence em {aluguer.data_vencimento:%d/%m/%Y}. Confirmar renovacao ou recolha."
            )
        return mensagens
