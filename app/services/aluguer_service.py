from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.aluguer import AluguerContentor, StatusAluguer, StatusEntrega
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
        numero_contentor: str | None = None,
        email_cliente: str | None = None,
        tipo_residuo: str | None = None,
        operador_telefone: str | None = None,
        foto_entrega_path: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        observacoes: str | None = None,
        data_entrega: datetime | None = None,
        status_entrega: str = StatusEntrega.ENTREGUE.value,
        pedido_feito_por: str | None = None,
        entrega_feita_por: str | None = None,
        pedido_endereco_tipo: str | None = None,
        pedido_endereco_texto: str | None = None,
        pedido_latitude: float | None = None,
        pedido_longitude: float | None = None,
        pedido_ponto_referencia: str | None = None,
        entrega_latitude: float | None = None,
        entrega_longitude: float | None = None,
        entrega_ponto_referencia: str | None = None,
    ) -> AluguerContentor:
        entrega = data_entrega or utcnow()
        cliente = self.clientes.get_or_create(nome=nome_cliente, telefone=telefone_cliente)
        contentor = self.contentores.get(contentor_id) if contentor_id else self.contentores.first_available()
        if contentor is None:
            raise ValueError("Nenhum contentor disponivel")
        numero_contentor = (numero_contentor or contentor.codigo).strip()
        if not 1 <= len(numero_contentor) <= 20:
            raise ValueError("Numero do contentor deve ter entre 1 e 20 caracteres")

        status_entrega = status_entrega or StatusEntrega.ENTREGUE.value
        pedido_feito_por = pedido_feito_por or operador_telefone
        if status_entrega == StatusEntrega.ENTREGUE.value:
            entrega_feita_por = entrega_feita_por or operador_telefone
            entrega_latitude = entrega_latitude if entrega_latitude is not None else latitude
            entrega_longitude = entrega_longitude if entrega_longitude is not None else longitude

        aluguer = self.alugueres.create(
            contentor_id=contentor.id,
            cliente_id=cliente.id,
            telefone_cliente=telefone_cliente,
            nome_cliente=nome_cliente,
            email_cliente=email_cliente,
            numero_contentor=numero_contentor,
            data_entrega=entrega,
            data_vencimento=entrega + timedelta(days=5),
            tipo_residuo=tipo_residuo,
            valor=Decimal(str(valor)),
            forma_pagamento=forma_pagamento,
            pago=pago,
            operador_telefone=operador_telefone,
            criado_por_operador=operador_telefone,
            pedido_feito_por=pedido_feito_por,
            entrega_feita_por=entrega_feita_por,
            status_entrega=status_entrega,
            status=StatusAluguer.ATIVO,
            foto_entrega_path=foto_entrega_path,
            latitude=latitude,
            longitude=longitude,
            pedido_endereco_tipo=pedido_endereco_tipo,
            pedido_endereco_texto=pedido_endereco_texto,
            pedido_latitude=pedido_latitude,
            pedido_longitude=pedido_longitude,
            pedido_ponto_referencia=pedido_ponto_referencia,
            entrega_latitude=entrega_latitude,
            entrega_longitude=entrega_longitude,
            entrega_ponto_referencia=entrega_ponto_referencia,
            observacoes=observacoes,
        )
        contentor.status = StatusContentor.ALUGADO
        if operador_telefone and not contentor.criado_por_operador:
            contentor.criado_por_operador = operador_telefone
        if status_entrega == StatusEntrega.PENDENTE.value:
            self.alugueres.add_event(aluguer.id, "pedido_criado", "Pedido cadastrado pelo escritorio; entrega pendente")
        else:
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

    def renovar_criando_novo_registro(
        self,
        aluguer_id: int,
        operador_telefone: str,
        ajustes: dict | None = None,
    ) -> AluguerContentor:
        origem = self._get_or_raise(aluguer_id)
        ajustes = ajustes or {}
        nova_entrega = origem.data_vencimento + timedelta(days=1)
        novo = self.registrar_novo_aluguer(
            contentor_id=origem.contentor_id,
            numero_contentor=ajustes.get("numero_contentor", origem.numero_contentor),
            nome_cliente=ajustes.get("nome_cliente", origem.nome_cliente),
            telefone_cliente=ajustes.get("telefone_cliente", origem.telefone_cliente),
            email_cliente=ajustes.get("email_cliente", origem.email_cliente),
            latitude=ajustes.get("latitude", origem.latitude),
            longitude=ajustes.get("longitude", origem.longitude),
            tipo_residuo=ajustes.get("tipo_residuo", origem.tipo_residuo),
            valor=ajustes.get("valor", origem.valor),
            forma_pagamento=ajustes.get("forma_pagamento", origem.forma_pagamento),
            pago=ajustes.get("pago", origem.pago),
            operador_telefone=operador_telefone or origem.operador_telefone,
            foto_entrega_path=origem.foto_entrega_path,
            observacoes=origem.observacoes,
            data_entrega=nova_entrega,
            pedido_endereco_tipo=ajustes.get("pedido_endereco_tipo", origem.pedido_endereco_tipo),
            pedido_endereco_texto=ajustes.get("pedido_endereco_texto", origem.pedido_endereco_texto),
            pedido_latitude=ajustes.get("pedido_latitude", origem.pedido_latitude),
            pedido_longitude=ajustes.get("pedido_longitude", origem.pedido_longitude),
            pedido_ponto_referencia=ajustes.get("pedido_ponto_referencia", origem.pedido_ponto_referencia),
        )
        self.alugueres.add_event(novo.id, "renovado_de", f"Renovacao criada a partir do aluguer #{origem.id}")
        return self.alugueres.save(novo)

    def marcar_recolha(self, aluguer_id: int) -> AluguerContentor:
        aluguer = self._get_or_raise(aluguer_id)
        aluguer.status = StatusAluguer.AGUARDANDO_RECOLHA
        aluguer.contentor.status = StatusContentor.AGUARDANDO_RECOLHA
        self.alugueres.add_event(aluguer.id, "aguardando_recolha", "Aluguer marcado para recolha")
        return self.alugueres.save(aluguer)

    def listar_pendentes_entrega(self) -> list[AluguerContentor]:
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.status_entrega == StatusEntrega.PENDENTE.value)
            .filter(AluguerContentor.is_deleted.is_(False))
            .order_by(AluguerContentor.data_entrega, AluguerContentor.id)
            .all()
        )

    def listar_vencendo_amanha(self, now: datetime | None = None) -> list[AluguerContentor]:
        base = now or utcnow()
        tomorrow = (base + timedelta(days=1)).date()
        start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=base.tzinfo)
        end = start + timedelta(days=1)
        return self.alugueres.list_by_due_range(start, end)

    def listar_atrasados(self, now: datetime | None = None) -> list[AluguerContentor]:
        return self.alugueres.list_overdue(now or utcnow())

    def listar_cadastrados_nos_ultimos_dias(
        self,
        days: int = 7,
        now: datetime | None = None,
    ) -> list[AluguerContentor]:
        base = now or utcnow()
        since = base - timedelta(days=days)
        return (
            self.db.query(AluguerContentor)
            .filter(AluguerContentor.criado_em >= since)
            .filter(AluguerContentor.is_deleted.is_(False))
            .order_by(AluguerContentor.criado_em.desc(), AluguerContentor.id.desc())
            .all()
        )

    def salvar(self, aluguer: AluguerContentor) -> AluguerContentor:
        return self.alugueres.save(aluguer)

    def excluir(self, aluguer_id: int, operador_telefone: str | None, justificativa: str) -> AluguerContentor:
        if len((justificativa or "").strip()) < 10:
            raise ValueError("Justificativa de exclusao deve ter pelo menos 10 caracteres")
        aluguer = self._get_or_raise(aluguer_id)
        if aluguer.contentor:
            aluguer.contentor.status = StatusContentor.DISPONIVEL
        aluguer.is_deleted = True
        aluguer.justificativa_exclusao = justificativa.strip()
        aluguer.excluido_por_operador = operador_telefone
        self.alugueres.add_event(aluguer.id, "excluido", "Aluguer excluido logicamente")
        return self.alugueres.save(aluguer)

    def _get_or_raise(self, aluguer_id: int) -> AluguerContentor:
        aluguer = self.alugueres.get(aluguer_id)
        if not aluguer:
            raise ValueError("Aluguer nao encontrado")
        return aluguer
