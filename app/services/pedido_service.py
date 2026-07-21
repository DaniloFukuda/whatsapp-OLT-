from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy.orm import Session, joinedload

from app.core.time import utcnow
from app.models.aluguer import ContentorFoto
from app.models.pedido import (
    Pedido,
    PedidoContentor,
    StatusCicloPedido,
    StatusEntregaPedido,
    StatusPagamento,
    StatusRecolhaPedido,
    StatusResolucaoPedido,
    TipoEquipamentoPedido,
    TipoFoto,
)


RESIDUOS_CANONICOS = ("Entulho Limpo", "Entulho Misto")


class PedidoService:
    def __init__(self, db: Session):
        self.db = db

    def criar(
        self,
        *,
        nome_cliente: str,
        telefone_cliente: str,
        data_planejada: datetime,
        valor_global: Decimal | str | float,
        pago: bool,
        forma_pagamento: str | None,
        pedido_feito_por: str,
        endereco_aproximado: str,
        ponto_referencia: str | None,
        residuos: list[str] | None = None,
        itens: list[dict] | None = None,
        endereco_latitude: float | None = None,
        endereco_longitude: float | None = None,
        precisa_mao_de_obra: bool | None = None,
    ) -> Pedido:
        pedido = self.criar_transacional(
            nome_cliente=nome_cliente,
            telefone_cliente=telefone_cliente,
            data_planejada=data_planejada,
            valor_global=valor_global,
            pago=pago,
            forma_pagamento=forma_pagamento,
            pedido_feito_por=pedido_feito_por,
            endereco_aproximado=endereco_aproximado,
            ponto_referencia=ponto_referencia,
            residuos=residuos,
            itens=itens,
            endereco_latitude=endereco_latitude,
            endereco_longitude=endereco_longitude,
            precisa_mao_de_obra=precisa_mao_de_obra,
        )
        self.db.commit()
        self.db.refresh(pedido)
        return pedido

    def criar_transacional(
        self,
        *,
        nome_cliente: str,
        telefone_cliente: str,
        data_planejada: datetime,
        valor_global: Decimal | str | float,
        pago: bool,
        forma_pagamento: str | None,
        pedido_feito_por: str,
        endereco_aproximado: str,
        ponto_referencia: str | None,
        residuos: list[str] | None = None,
        itens: list[dict] | None = None,
        endereco_latitude: float | None = None,
        endereco_longitude: float | None = None,
        precisa_mao_de_obra: bool | None = None,
    ) -> Pedido:
        itens_normalizados = self._normalizar_itens(residuos=residuos, itens=itens)
        if not itens_normalizados:
            raise ValueError("O pedido precisa de pelo menos um item.")
        pedido_precisa_mao_de_obra = (
            bool(precisa_mao_de_obra)
            if precisa_mao_de_obra is not None
            else any(item["precisa_mao_de_obra"] for item in itens_normalizados)
        )
        try:
            valor = Decimal(str(valor_global).replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("Valor global inválido.") from exc
        if valor < 0 or valor > Decimal("99999999.99"):
            raise ValueError("Valor global inválido.")
        if len(endereco_aproximado.strip()) > 300:
            raise ValueError("O endereço deve ter no máximo 300 caracteres.")
        if ponto_referencia and len(ponto_referencia.strip()) > 50:
            raise ValueError("O ponto de referência deve ter no máximo 50 caracteres.")
        if pago and not forma_pagamento:
            raise ValueError("Informe a forma de pagamento de um pedido pago.")
        pedido = Pedido(
            nome_cliente=nome_cliente.strip(),
            telefone_cliente=telefone_cliente.strip(),
            data_planejada=data_planejada,
            valor_global=valor,
            status_pagamento=StatusPagamento.PAGO.value if pago else StatusPagamento.PENDENTE.value,
            forma_pagamento=forma_pagamento if pago else None,
            pedido_feito_por=pedido_feito_por,
            endereco_aproximado=endereco_aproximado.strip(),
            endereco_latitude=endereco_latitude,
            endereco_longitude=endereco_longitude,
            ponto_referencia=ponto_referencia.strip() if ponto_referencia else None,
            precisa_mao_de_obra=pedido_precisa_mao_de_obra,
            contentores=[self._criar_item(item) for item in itens_normalizados],
        )
        self.db.add(pedido)
        self.db.flush()
        return pedido

    @staticmethod
    def _criar_item(item: dict) -> PedidoContentor:
        return PedidoContentor(
            tipo_equipamento=item["tipo_equipamento"],
            horario_agendado=item["horario_agendado"],
            precisa_mao_de_obra=False,
            residuo_contratado=item["residuo_contratado"],
        )

    def _normalizar_itens(
        self,
        *,
        residuos: list[str] | None,
        itens: list[dict] | None,
    ) -> list[dict]:
        if itens is None:
            itens = [{"residuo_contratado": residuo} for residuo in (residuos or [])]
        normalizados = []
        for item in itens:
            tipo = str(item.get("tipo_equipamento") or TipoEquipamentoPedido.CONTENTOR.value).strip().upper()
            if tipo not in {TipoEquipamentoPedido.CONTENTOR.value, TipoEquipamentoPedido.CARRINHA.value}:
                raise ValueError("Tipo de equipamento invalido.")
            residuo = str(item.get("residuo_contratado") or item.get("residuo") or "").strip()
            if not residuo:
                raise ValueError("Informe o residuo contratado do item.")
            horario = item.get("horario_agendado")
            horario = str(horario).strip() if horario is not None else None
            if tipo == TipoEquipamentoPedido.CARRINHA:
                if not self._horario_valido(horario):
                    raise ValueError("Carrinha precisa de horario agendado no formato HH:MM.")
            else:
                horario = None
            normalizados.append(
                {
                    "tipo_equipamento": tipo,
                    "residuo_contratado": residuo,
                    "horario_agendado": horario,
                    "precisa_mao_de_obra": bool(item.get("precisa_mao_de_obra")),
                }
            )
        tipos = {item["tipo_equipamento"] for item in normalizados}
        if len(tipos) > 1:
            raise ValueError("Um pedido nao pode combinar contentores e carrinhas.")
        return normalizados

    def precisa_mao_de_obra(self, pedido: Pedido) -> bool:
        if bool(getattr(pedido, "precisa_mao_de_obra", False)):
            return True
        return any(item.precisa_mao_de_obra for item in pedido.contentores)

    def _horario_valido(self, value: str | None) -> bool:
        if not value or not re.fullmatch(r"\d{2}:\d{2}", value):
            return False
        hour, minute = [int(part) for part in value.split(":")]
        return 0 <= hour <= 23 and 0 <= minute <= 59

    def get(self, pedido_id: int) -> Pedido | None:
        return (
            self.db.query(Pedido)
            .options(joinedload(Pedido.contentores))
            .filter(Pedido.id == pedido_id)
            .first()
        )

    def pedidos_pendentes_entrega(self) -> list[Pedido]:
        return (
            self.db.query(Pedido)
            .join(PedidoContentor)
            .filter(PedidoContentor.status_entrega == StatusEntregaPedido.PENDENTE.value)
            .distinct()
            .order_by(Pedido.data_planejada, Pedido.id)
            .all()
        )

    def contentores_para_recolha(self) -> list[PedidoContentor]:
        return (
            self.db.query(PedidoContentor)
            .options(joinedload(PedidoContentor.pedido))
            .filter(PedidoContentor.status_entrega == StatusEntregaPedido.ENTREGUE.value)
            .filter(PedidoContentor.status_recolha == StatusRecolhaPedido.PENDENTE.value)
            .order_by(PedidoContentor.numero_adesivo_contentor, PedidoContentor.id)
            .all()
        )

    def pedidos_para_recolha(self) -> list[Pedido]:
        return (
            self.db.query(Pedido)
            .join(PedidoContentor)
            .options(joinedload(Pedido.contentores))
            .filter(PedidoContentor.status_entrega == StatusEntregaPedido.ENTREGUE.value)
            .filter(PedidoContentor.status_recolha == StatusRecolhaPedido.PENDENTE.value)
            .distinct()
            .order_by(Pedido.data_planejada, Pedido.id)
            .all()
        )

    def contentores_para_despejo(self) -> list[PedidoContentor]:
        return (
            self.db.query(PedidoContentor)
            .options(joinedload(PedidoContentor.pedido))
            .filter(PedidoContentor.status_recolha == StatusRecolhaPedido.RECOLHIDO.value)
            .filter(PedidoContentor.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value)
            .order_by(PedidoContentor.numero_adesivo_contentor, PedidoContentor.id)
            .all()
        )

    def pedidos_para_despejo(self) -> list[Pedido]:
        return (
            self.db.query(Pedido)
            .join(PedidoContentor)
            .options(joinedload(Pedido.contentores))
            .filter(PedidoContentor.status_recolha == StatusRecolhaPedido.RECOLHIDO.value)
            .filter(PedidoContentor.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value)
            .distinct()
            .order_by(Pedido.data_planejada, Pedido.id)
            .all()
        )

    def adicionar_foto(self, contentor_id: int, url: str, tipo: TipoFoto | str) -> ContentorFoto:
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not contentor:
            raise ValueError("Contentor do pedido não encontrado.")
        tipo_value = tipo.value if isinstance(tipo, TipoFoto) else str(tipo).upper()
        if tipo_value not in {item.value for item in TipoFoto}:
            raise ValueError("Tipo de foto inválido.")
        foto = ContentorFoto(
            pedido_contentor_id=contentor_id,
            url_midia=url,
            tipo_foto=tipo_value,
            url_foto=url,
            tipo=tipo_value.lower(),
        )
        self.db.add(foto)
        self.db.commit()
        return foto

    def confirmar_entrega_lote(
        self,
        pedido_id: int,
        operador: str,
        latitude: float,
        longitude: float,
        ponto_referencia: str | None,
        entregas: list[dict] | None = None,
    ) -> Pedido:
        pedido = self.get(pedido_id)
        if not pedido:
            raise ValueError("Pedido não encontrado.")
        pendentes = [c for c in pedido.contentores if c.status_entrega == StatusEntregaPedido.PENDENTE.value]
        entregas = entregas or []
        entregas_por_id = {int(item["contentor_id"]): item for item in entregas if item.get("contentor_id")}
        if not entregas:
            raise ValueError("Nenhum ativo preparado para entrega.")
        if {c.id for c in pendentes} != set(entregas_por_id):
            raise ValueError("A lista de ativos pendentes mudou. Reinicie a entrega.")
        if entregas:
            adesivos = []
            for contentor in pendentes:
                item = entregas_por_id[contentor.id]
                adesivo = str(item.get("numero_adesivo") or "").strip()
                fotos = [foto for foto in (item.get("fotos") or []) if foto]
                if not fotos:
                    raise ValueError("Todos os ativos precisam de pelo menos uma foto.")
                if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value and adesivo == "0":
                    continue
                if not adesivo:
                    raise ValueError("Todos os contentores precisam de adesivos validos.")
                adesivos.append(adesivo)
            if len(set(adesivos)) != len(adesivos):
                raise ValueError("Todos os contentores precisam de adesivos sem duplicidade.")
            duplicado = (
                self.db.query(PedidoContentor)
                .filter(PedidoContentor.numero_adesivo_contentor.in_(adesivos))
                .filter(PedidoContentor.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value)
                .filter(~PedidoContentor.id.in_([c.id for c in pendentes]))
                .first()
            )
            if duplicado:
                raise ValueError("Um dos adesivos ja esta em um ciclo ativo.")
        elif any(not c.numero_adesivo_contentor for c in pendentes):
            raise ValueError("Todos os contentores precisam do número do adesivo.")
        if ponto_referencia and len(ponto_referencia) > 50:
            raise ValueError("O ponto de referência deve ter no máximo 50 caracteres.")
        agora = utcnow()
        for contentor in pendentes:
            entrega = entregas_por_id.get(contentor.id)
            if entrega:
                numero = str(entrega["numero_adesivo"]).strip()
                contentor.numero_adesivo_contentor = (
                    None
                    if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value and numero == "0"
                    else numero
                )
            contentor.status_entrega = StatusEntregaPedido.ENTREGUE.value
            contentor.entrega_feita_por = operador
            contentor.entrega_latitude = latitude
            contentor.entrega_longitude = longitude
            contentor.entrega_ponto_referencia = ponto_referencia
            contentor.entrega_data_hora = agora
            for foto_url in (entrega or {}).get("fotos") or []:
                self.db.add(
                    ContentorFoto(
                        pedido_contentor_id=contentor.id,
                        url_midia=foto_url,
                        tipo_foto=TipoFoto.ENTREGA.value,
                        url_foto=foto_url,
                        tipo=TipoFoto.ENTREGA.value.lower(),
                    )
                )
        self.db.commit()
        return pedido

    def registrar_pagamento(self, pedido_id: int, forma: str) -> None:
        pedido = self.get(pedido_id)
        if not pedido:
            raise ValueError("Pedido não encontrado.")
        pedido.status_pagamento = StatusPagamento.PAGO.value
        pedido.forma_pagamento = forma
        self.db.commit()

    def confirmar_recolha(
        self,
        contentor_id: int,
        operador: str,
        avariado: bool,
        relato: str | None,
        fotos: list[str] | None = None,
    ) -> PedidoContentor:
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            not contentor
            or contentor.status_entrega != StatusEntregaPedido.ENTREGUE.value
            or contentor.status_recolha != StatusRecolhaPedido.PENDENTE.value
        ):
            raise ValueError("Contentor não disponível para recolha.")
        relato_limpo = (relato or "").strip()
        if avariado and len(relato_limpo) < 10:
            raise ValueError("O relato da avaria precisa ter pelo menos 10 caracteres.")
        fotos_unicas = []
        for foto in fotos or []:
            foto_limpa = str(foto or "").strip()
            if foto_limpa and foto_limpa not in fotos_unicas:
                fotos_unicas.append(foto_limpa)
        contentor.status_recolha = StatusRecolhaPedido.RECOLHIDO.value
        contentor.recolha_feita_por = operador
        contentor.recolha_data_hora = utcnow()
        contentor.contentor_avariado = avariado
        contentor.relato_avaria = relato_limpo if avariado else None
        contentor.status_resolucao_avaria = (
            StatusResolucaoPedido.PENDENTE.value
            if avariado
            else StatusResolucaoPedido.NAO_APLICA.value
        )
        fotos_existentes = {
            foto.url_midia
            for foto in contentor.fotos
            if foto.tipo_foto == TipoFoto.RECOLHA.value
        }
        for foto_url in fotos_unicas:
            if foto_url in fotos_existentes:
                continue
            self.db.add(
                ContentorFoto(
                    pedido_contentor_id=contentor.id,
                    url_midia=foto_url,
                    tipo_foto=TipoFoto.RECOLHA.value,
                    url_foto=foto_url,
                    tipo=TipoFoto.RECOLHA.value.lower(),
                )
            )
        self.db.commit()
        return contentor

    def cotas_restantes(self, pedido_id: int) -> Counter:
        cotas = self.cotas_residuos(pedido_id)
        return Counter(
            {
                residuo: dados["saldo"]
                for residuo, dados in cotas.items()
                if dados["saldo"] > 0
            }
        )

    def cotas_residuos(self, pedido_id: int) -> dict[str, dict[str, int]]:
        pedido = self.get(pedido_id)
        if not pedido:
            raise ValueError("Pedido não encontrado.")
        contratadas = Counter()
        consumidas = Counter()
        for contentor in pedido.contentores:
            if contentor.residuo_contratado not in RESIDUOS_CANONICOS:
                raise ValueError("Pedido precisa de revisão: resíduo contratado inválido.")
            contratadas[contentor.residuo_contratado] += 1
            if contentor.status_ciclo != StatusCicloPedido.CONCLUIDO.value:
                continue
            if not contentor.residuo_efetivo_vazadouro:
                raise ValueError("Pedido precisa de revisão: despejo concluído sem resíduo efetivo.")
            if contentor.residuo_efetivo_vazadouro not in RESIDUOS_CANONICOS:
                raise ValueError("Pedido precisa de revisão: resíduo efetivo inválido.")
            consumidas[contentor.residuo_efetivo_vazadouro] += 1

        cotas = {}
        for residuo in RESIDUOS_CANONICOS:
            contratado = contratadas[residuo]
            consumido = consumidas[residuo]
            saldo = contratado - consumido
            if saldo < 0:
                raise ValueError("Pedido precisa de revisão: cota de resíduo negativa.")
            cotas[residuo] = {
                "contratado": contratado,
                "consumido": consumido,
                "saldo": saldo,
            }
        return cotas

    def contentores_pendentes_despejo(self, pedido_id: int) -> list[PedidoContentor]:
        pedido = self.get(pedido_id)
        if not pedido:
            return []
        return [
            contentor
            for contentor in pedido.contentores
            if contentor.status_recolha == StatusRecolhaPedido.RECOLHIDO.value
            and contentor.status_ciclo == StatusCicloPedido.EM_ANDAMENTO.value
        ]

    def registrar_divergencia_despejo(
        self,
        contentor_id: int,
        relato: str,
        operador: str | None = None,
        pedido_id: int | None = None,
    ) -> PedidoContentor:
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            not contentor
            or contentor.status_recolha != StatusRecolhaPedido.RECOLHIDO.value
            or contentor.status_ciclo != StatusCicloPedido.EM_ANDAMENTO.value
            or contentor.despejo_data_hora is not None
        ):
            raise ValueError("Contentor não disponível para despejo.")
        if pedido_id is not None and contentor.pedido_id != pedido_id:
            raise ValueError("Ativo nao pertence ao pedido selecionado.")
        relato_limpo = (relato or "").strip()
        if len(relato_limpo) < 10:
            raise ValueError("O relato da carga precisa ter pelo menos 10 caracteres.")
        contentor.carga_errada = True
        contentor.relato_carga = relato_limpo
        contentor.status_resolucao_carga = StatusResolucaoPedido.PENDENTE.value
        self.db.commit()
        return contentor

    def confirmar_despejo(
        self,
        contentor_id: int,
        residuo_efetivo: str,
        carga_errada: bool = False,
        relato: str | None = None,
        operador: str | None = None,
        pedido_id: int | None = None,
        fotos: list[str] | None = None,
    ) -> PedidoContentor:
        contentor = self.db.get(PedidoContentor, contentor_id)
        if (
            not contentor
            or contentor.status_recolha != StatusRecolhaPedido.RECOLHIDO.value
            or contentor.status_ciclo != StatusCicloPedido.EM_ANDAMENTO.value
            or contentor.despejo_data_hora is not None
        ):
            raise ValueError("Contentor não disponível para despejo.")
        if pedido_id is not None and contentor.pedido_id != pedido_id:
            raise ValueError("Ativo nao pertence ao pedido selecionado.")
        if residuo_efetivo not in RESIDUOS_CANONICOS:
            raise ValueError("Residuo efetivo invalido.")
        fotos_unicas = []
        for foto in fotos or []:
            foto_limpa = str(foto or "").strip()
            if foto_limpa and foto_limpa not in fotos_unicas:
                fotos_unicas.append(foto_limpa)
        if fotos is not None and not fotos_unicas:
            raise ValueError("Envie pelo menos uma foto do despejo.")
        cotas = self.cotas_residuos(contentor.pedido_id)
        if cotas[residuo_efetivo]["saldo"] <= 0:
            raise ValueError("Não existe cota em aberto para esse tipo de resíduo.")
        relato_limpo = (relato or "").strip()
        if carga_errada and len(relato_limpo) < 10:
            raise ValueError("O relato da carga precisa ter pelo menos 10 caracteres.")
        try:
            contentor.residuo_efetivo_vazadouro = residuo_efetivo
            contentor.carga_errada = carga_errada
            contentor.relato_carga = relato_limpo if carga_errada else None
            contentor.status_resolucao_carga = (
                StatusResolucaoPedido.PENDENTE.value
                if carga_errada
                else StatusResolucaoPedido.NAO_APLICA.value
            )
            fotos_existentes = {
                foto.url_midia
                for foto in contentor.fotos
                if foto.tipo_foto == TipoFoto.DESPEJO.value
            }
            for foto_url in fotos_unicas:
                if foto_url in fotos_existentes:
                    continue
                self.db.add(
                    ContentorFoto(
                        pedido_contentor_id=contentor.id,
                        url_midia=foto_url,
                        tipo_foto=TipoFoto.DESPEJO.value,
                        url_foto=foto_url,
                        tipo=TipoFoto.DESPEJO.value.lower(),
                    )
                )
            contentor.despejo_feito_por = operador
            contentor.despejo_data_hora = utcnow()
            contentor.status_ciclo = StatusCicloPedido.CONCLUIDO.value
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return contentor

    def resolver(self, tipo: str, contentor_id: int) -> PedidoContentor:
        contentor = self.db.get(PedidoContentor, contentor_id)
        if not contentor:
            raise ValueError("Contentor não encontrado.")
        if tipo == "carga":
            contentor.status_resolucao_carga = StatusResolucaoPedido.RESOLVIDO.value
        elif tipo == "avaria":
            contentor.status_resolucao_avaria = StatusResolucaoPedido.RESOLVIDO.value
        else:
            raise ValueError("Tipo de pendência inválido.")
        self.db.commit()
        return contentor

    def pendencias(self) -> dict[str, list]:
        financeiras = (
            self.db.query(Pedido)
            .filter(Pedido.status_pagamento == StatusPagamento.PENDENTE.value)
            .order_by(Pedido.id)
            .all()
        )
        cargas = (
            self.db.query(PedidoContentor)
            .options(joinedload(PedidoContentor.pedido))
            .filter(PedidoContentor.carga_errada.is_(True))
            .filter(PedidoContentor.status_resolucao_carga == StatusResolucaoPedido.PENDENTE.value)
            .all()
        )
        avarias = (
            self.db.query(PedidoContentor)
            .options(joinedload(PedidoContentor.pedido))
            .filter(PedidoContentor.contentor_avariado.is_(True))
            .filter(PedidoContentor.status_resolucao_avaria == StatusResolucaoPedido.PENDENTE.value)
            .all()
        )
        return {"financeiras": financeiras, "cargas": cargas, "avarias": avarias}
