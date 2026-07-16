from decimal import Decimal

from sqlalchemy import text

from app.models.conversa import ConversaWhatsApp
from app.models.pedido import Pedido, PedidoContentor
from app.models.whatsapp_dedup import WhatsAppProcessedMessage
from app.models.whatsapp_phone_queue import WhatsAppPhoneQueueItem


class DatabaseAssertions:
    def __init__(self, SessionLocal):
        self.SessionLocal = SessionLocal

    def assert_quick_check_ok(self) -> None:
        with self.SessionLocal() as session:
            result = session.execute(text("PRAGMA quick_check")).scalar()
            assert result == "ok"

    def assert_pedido_count(self, expected: int) -> None:
        with self.SessionLocal() as session:
            assert session.query(Pedido).count() == expected

    def assert_contentor_count(self, expected: int) -> None:
        with self.SessionLocal() as session:
            assert session.query(PedidoContentor).count() == expected

    def assert_queue_empty(self) -> None:
        with self.SessionLocal() as session:
            assert session.query(WhatsAppPhoneQueueItem).count() == 0

    def assert_no_active_queue(self) -> None:
        with self.SessionLocal() as session:
            active = (
                session.query(WhatsAppPhoneQueueItem)
                .filter(WhatsAppPhoneQueueItem.status.in_(["PENDING", "PROCESSING"]))
                .count()
            )
            assert active == 0

    def assert_all_dedup_completed(self) -> None:
        with self.SessionLocal() as session:
            statuses = [row.status for row in session.query(WhatsAppProcessedMessage).all()]
            assert statuses
            assert set(statuses) == {"COMPLETED"}

    def assert_dedup_completed(self, message_id: str, expected_count: int = 1) -> None:
        with self.SessionLocal() as session:
            rows = session.query(WhatsAppProcessedMessage).filter_by(message_id=message_id).all()
            assert len(rows) == expected_count
            assert rows[0].status == "COMPLETED"

    def assert_conversation(self, phone: str, state: str, context: dict | None = None) -> None:
        with self.SessionLocal() as session:
            conversa = session.query(ConversaWhatsApp).filter_by(telefone=phone).one_or_none()
            assert conversa is not None
            assert conversa.estado_atual == state
            if context is not None:
                assert conversa.contexto_json == context

    def assert_single_paid_contentor_order(self, *, cliente: str, telefone: str, forma: str, residuo: str) -> None:
        with self.SessionLocal() as session:
            pedido = session.query(Pedido).one()
            assert pedido.nome_cliente == cliente
            assert pedido.telefone_cliente == telefone
            assert pedido.status_pagamento == "PAGO"
            assert pedido.forma_pagamento == forma
            assert pedido.precisa_mao_de_obra is False
            assert Decimal(str(pedido.valor_global)) == Decimal("100.00")
            item = session.query(PedidoContentor).one()
            assert item.pedido_id == pedido.id
            assert item.tipo_equipamento == "CONTENTOR"
            assert item.residuo_contratado == residuo
            assert item.status_entrega == "PENDENTE"
            assert item.status_recolha == "PENDENTE"
            assert item.status_ciclo == "EM_ANDAMENTO"
