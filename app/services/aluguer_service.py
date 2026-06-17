from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.aluguer import AluguerContentor, StatusAluguer
from app.models.contentor import StatusContentor
from app.repositories.aluguer_repository import AluguerRepository
from app.repositories.cliente_repository import ClienteRepository
from app.repositories.contentor_repository import ContentorRepository


class AluguerService:
    def __init__(self, db: Session):
        self.db = db
        self.alugueres = AluguerRepository(db)
        self.clientes = ClienteRepository(db)
        self.contentores = ContentorRepository(db)

    def registrar_novo_aluguer(
        self,
        nome_cliente: str,
        telefone_cliente: str,
        valor: Decimal | float | str,
        forma_pagamento: str | None,
        pago: bool,
        contentor_id: int | None = None,
        foto_entrega_path: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        observacoes: str | None = None,
        data_entrega: datetime | None = None,
    ) -> AluguerContentor:
        entrega = data_entrega or utcnow()
        cliente = self.clientes.get_or_create(nome=nome_cliente, telefone=telefone_cliente)
        contentor = self.contentores.get(contentor_id) if contentor_id else self.contentores.first_available()
        if contentor is None:
            raise ValueError("Nenhum contentor disponivel")

        aluguer = self.alugueres.create(
            contentor_id=contentor.id,
            cliente_id=cliente.id,
            telefone_cliente=telefone_cliente,
            nome_cliente=nome_cliente,
            data_entrega=entrega,
            data_vencimento=entrega + timedelta(days=5),
            valor=Decimal(str(valor)),
            forma_pagamento=forma_pagamento,
            pago=pago,
            status=StatusAluguer.ATIVO,
            foto_entrega_path=foto_entrega_path,
            latitude=latitude,
            longitude=longitude,
            observacoes=observacoes,
        )
        contentor.status = StatusContentor.ALUGADO
        self.alugueres.add_event(aluguer.id, "entrega", "Contentor entregue no local indicado")
        self.alugueres.add_event(
            aluguer.id,
            "pagamento_informado",
            f"Pagamento informado: {'pago' if pago else 'nao pago'}; forma: {forma_pagamento or 'nao informada'}",
        )
        self.alugueres.add_event(aluguer.id, "criado", "Aluguer criado pelo fluxo WhatsApp")
        self.db.commit()
        self.db.refresh(aluguer)
        return aluguer

    def renovar_por_mais_5_dias(self, aluguer_id: int) -> AluguerContentor:
        aluguer = self._get_or_raise(aluguer_id)
        aluguer.data_vencimento = aluguer.data_vencimento + timedelta(days=5)
        aluguer.status = StatusAluguer.RENOVADO
        self.alugueres.add_event(aluguer.id, "renovado", "Aluguer renovado por mais 5 dias")
        return self.alugueres.save(aluguer)

    def marcar_recolha(self, aluguer_id: int) -> AluguerContentor:
        aluguer = self._get_or_raise(aluguer_id)
        aluguer.status = StatusAluguer.AGUARDANDO_RECOLHA
        aluguer.contentor.status = StatusContentor.AGUARDANDO_RECOLHA
        self.alugueres.add_event(aluguer.id, "aguardando_recolha", "Aluguer marcado para recolha")
        return self.alugueres.save(aluguer)

    def listar_vencendo_amanha(self, now: datetime | None = None) -> list[AluguerContentor]:
        base = now or utcnow()
        tomorrow = (base + timedelta(days=1)).date()
        start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=base.tzinfo)
        end = start + timedelta(days=1)
        return self.alugueres.list_by_due_range(start, end)

    def listar_atrasados(self, now: datetime | None = None) -> list[AluguerContentor]:
        return self.alugueres.list_overdue(now or utcnow())

    def _get_or_raise(self, aluguer_id: int) -> AluguerContentor:
        aluguer = self.alugueres.get(aluguer_id)
        if not aluguer:
            raise ValueError("Aluguer nao encontrado")
        return aluguer
