import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.integrations.whatsapp.parser import NormalizedWhatsAppMessage
from app.models.conversa import ConversaWhatsApp
from app.models.pedido import (
    PedidoContentor,
    StatusPagamento,
    TipoEquipamentoPedido,
    TipoFoto,
)
from app.services.pedido_service import PedidoService


class PedidoV24Agent:
    PREFIX = "v24_"

    def __init__(self, db: Session):
        self.db = db
        self.service = PedidoService(db)

    def start_cadastro(self, conversa: ConversaWhatsApp) -> str:
        conversa.estado_atual = "v24_cadastro_tipo_solicitacao"
        conversa.contexto_json = {}
        self.db.commit()
        return self._tipo_solicitacao_prompt()

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        pedidos = self.service.pedidos_pendentes_entrega()
        if not pedidos:
            return self._idle(conversa, "Não existem pedidos pendentes de entrega.")
        return self._advance(
            conversa,
            "v24_entrega_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido pendente:\n\n"
            + "\n".join(f"{i}. {self._pedido_entrega_label(p)}" for i, p in enumerate(pedidos, 1)),
        )

    def start_recolha(self, conversa: ConversaWhatsApp) -> str:
        pedidos = self.service.pedidos_para_recolha()
        if not pedidos:
            return self._idle(conversa, "Não existem contentores aguardando recolha.")
        return self._advance(
            conversa,
            "v24_recolha_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido para recolha:\n\n"
            + "\n".join(f"{i}. {self._pedido_recolha_label(p)}" for i, p in enumerate(pedidos, 1)),
        )

    def start_despejo(self, conversa: ConversaWhatsApp) -> str:
        pedidos = self.service.pedidos_para_despejo()
        if not pedidos:
            return self._idle(conversa, "Não existem contentores recolhidos aguardando despejo.")
        return self._advance(
            conversa,
            "v24_despejo_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido para despejo no vazadouro:\n\n"
            + "\n".join(f"{i}. {self._pedido_despejo_label(p)}" for i, p in enumerate(pedidos, 1)),
        )
    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        ctx = dict(conversa.contexto_json or {})
        raw = (message.texto or "").strip()
        choice = self._norm(raw)

        if state == "v24_cadastro_nome" and message.contact_name:
            name = message.contact_name.strip()
            if len(name) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = name
            phone = self._phone_from_message(message, "")
            if phone:
                ctx["telefone"] = phone
                return self._advance(
                    conversa,
                    "v24_cadastro_quantidade",
                    ctx,
                    self._quantidade_prompt(ctx),
                )
            return self._advance(conversa, "v24_cadastro_telefone", ctx, "Qual é o telefone do cliente?")
        if state == "v24_cadastro_telefone" and message.contact_phone:
            phone = self._phone_from_message(message, raw)
            if not phone:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
            return self._advance(
                conversa,
                "v24_cadastro_quantidade",
                ctx,
                self._quantidade_prompt(ctx),
            )

        if state == "v24_cadastro_nome":
            if len(raw) < 2:
                return "Informe o nome completo do cliente."
            ctx["nome"] = raw
            return self._advance(conversa, "v24_cadastro_telefone", ctx, "Qual é o telefone do cliente?")
        if state == "v24_cadastro_telefone":
            phone = re.sub(r"\D", "", raw)
            if len(phone) < 9:
                return "O telefone informado não é válido."
            ctx["telefone"] = phone
            return self._advance(
                conversa,
                "v24_cadastro_quantidade",
                ctx,
                self._quantidade_prompt(ctx),
            )
        if state == "v24_cadastro_tipo_solicitacao":
            tipo = self._parse_tipo_solicitacao(choice)
            if not tipo:
                return self._tipo_solicitacao_prompt()
            ctx["tipo_solicitacao"] = tipo
            return self._advance(conversa, "v24_cadastro_nome", ctx, "Qual é o nome do cliente?")
        if state == "v24_cadastro_data":
            now = datetime.now(self._lisbon_timezone())
            if choice in {"1", "hoje"}:
                planned = now
            elif choice in {"2", "amanha"}:
                planned = now + timedelta(days=1)
            elif choice in {"3", "outra data"}:
                return self._advance(
                    conversa, "v24_cadastro_data_manual", ctx, "Informe a data no formato DD/MM/AAAA."
                )
            else:
                return "Selecione Hoje, Amanhã ou Outra data."
            ctx["data"] = planned.isoformat()
            return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor global do pedido?")
        if state == "v24_cadastro_data_manual":
            try:
                planned = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=self._lisbon_timezone())
            except ValueError:
                return "Data inválida. Use o formato DD/MM/AAAA."
            ctx["data"] = planned.isoformat()
            return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor global do pedido?")
        if state == "v24_cadastro_quantidade":
            if not raw.isdigit() or not 1 <= int(raw) <= 50:
                return "Informe uma quantidade entre 1 e 50."
            ctx["quantidade"] = int(raw)
            ctx["itens"] = []
            ctx["residuos"] = []
            return self._advance(conversa, "v24_cadastro_tipo_equipamento", ctx, self._tipo_equipamento_prompt(ctx))
        if state == "v24_cadastro_tipo_equipamento":
            legacy_residue = None if choice in {"1", "2"} else self._parse_residue(choice)
            if legacy_residue:
                ctx["item_atual"] = {
                    "tipo_equipamento": TipoEquipamentoPedido.CONTENTOR.value,
                    "horario_agendado": None,
                    "precisa_mao_de_obra": False,
                }
                return self._registrar_item_cadastro(conversa, ctx, legacy_residue)
            equipamento = {
                "1": TipoEquipamentoPedido.CONTENTOR.value,
                "contentor": TipoEquipamentoPedido.CONTENTOR.value,
                "2": TipoEquipamentoPedido.CARRINHA.value,
                "carrinha": TipoEquipamentoPedido.CARRINHA.value,
            }.get(choice)
            if not equipamento:
                return "Selecione Contentor ou Carrinha."
            ctx["item_atual"] = {"tipo_equipamento": equipamento, "horario_agendado": None}
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_horario_carrinha":
            if not self._horario_valido(raw):
                return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
            item_atual = dict(ctx.get("item_atual") or {})
            item_atual["horario_agendado"] = raw
            ctx["item_atual"] = item_atual
            ctx["horario_agendado"] = raw
            if "precisa_mao_de_obra" in ctx:
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_mao_obra":
            mao_obra = self._parse_mao_obra(choice)
            if mao_obra is not None:
                item_atual = dict(ctx.get("item_atual") or {})
                tipo_item = item_atual.get("tipo_equipamento") or ctx.get("tipo_solicitacao")
                if tipo_item:
                    item_atual["tipo_equipamento"] = tipo_item
                item_atual["precisa_mao_de_obra"] = mao_obra
                ctx["item_atual"] = item_atual
                ctx["precisa_mao_de_obra"] = mao_obra
                if tipo_item == TipoEquipamentoPedido.CARRINHA.value and not item_atual.get("horario_agendado"):
                    return self._advance(conversa, "v24_cadastro_horario_carrinha", ctx, self._horario_carrinha_prompt())
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return self._mao_obra_prompt()
        if state == "v24_cadastro_residuo":
            residue = self._parse_residue(choice)
            if not residue:
                return self._residuo_prompt(ctx)
            return self._registrar_item_cadastro(conversa, ctx, residue)
        if state == "v24_cadastro_valor":
            try:
                ctx["valor"] = str(float(raw.replace(",", ".")))
            except ValueError:
                return "Valor inválido."
            return self._advance(
                conversa,
                "v24_cadastro_pago",
                ctx,
                "O pedido já está pago?\n\n1. Sim, já está pago\n2. Não, pendente",
            )
        if state == "v24_cadastro_pago":
            if choice in {"1", "sim", "sim, ja esta pago"}:
                ctx["pago"] = True
                return self._advance(
                    conversa,
                    "v24_cadastro_forma",
                    ctx,
                    "Selecione a forma de pagamento:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
                )
            if choice in {"2", "nao", "nao, pendente"}:
                ctx["pago"] = False
                ctx["forma"] = None
                return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
            return "Selecione uma das opções de pagamento."
        if state == "v24_cadastro_forma":
            forma = {"1": "MBWay", "mbway": "MBWay", "2": "Transferência",
                     "transferencia": "Transferência", "3": "Dinheiro",
                     "dinheiro": "Dinheiro", "4": "Outro", "outro": "Outro"}.get(choice)
            if not forma:
                return "Selecione uma forma de pagamento."
            if forma == "Outro":
                return self._advance(conversa, "v24_cadastro_forma_outro", ctx, "Qual foi a forma de pagamento?")
            ctx["forma"] = forma
            return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
        if state == "v24_cadastro_forma_outro":
            if not raw:
                return "Informe a forma de pagamento."
            ctx["forma"] = raw[:80]
            return self._advance(conversa, "v24_cadastro_endereco", ctx, self._endereco_prompt())
        if state == "v24_cadastro_endereco":
            if message.tipo == "location" and not self._coordinates(message, raw):
                return "Não foi possível ler a localização. Reenvie a localização nativa ou digite o endereço."
            if not raw or len(raw) > 300:
                return "O endereço precisa ter entre 1 e 300 caracteres."
            ctx["endereco"] = raw
            coords = self._coordinates(message, raw)
            if coords:
                ctx["endereco_latitude"], ctx["endereco_longitude"] = coords
            return self._advance(
                conversa, "v24_cadastro_referencia_opcao", ctx,
                "Deseja informar um ponto de referência?\n\n1. Sim\n2. Não",
            )
        if state == "v24_cadastro_referencia_opcao":
            if choice in {"1", "sim"}:
                return self._advance(conversa, "v24_cadastro_referencia", ctx, "Qual é o ponto de referência?")
            if choice in {"2", "nao"}:
                ctx["referencia"] = None
                return self._finish_cadastro(conversa, ctx)
            return "Selecione Sim ou Não."
        if state == "v24_cadastro_referencia":
            if not 1 <= len(raw) <= 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia"] = raw
            return self._finish_cadastro(conversa, ctx)

        if state == "v24_cadastro_confirmacao":
            if choice in {"1", "sim", "confirmar", "confirmar e salvar"}:
                ctx["_confirmado"] = True
                return self._finish_cadastro(conversa, ctx)
            if choice in {"2", "corrigir"}:
                conversa.estado_atual = "v24_cadastro_tipo_solicitacao"
                conversa.contexto_json = {}
                self.db.commit()
                return "Vamos corrigir o pedido desde o inicio.\n\n" + self._tipo_solicitacao_prompt()
            if choice in {"3", "cancelar"}:
                return self._idle(conversa, "Pedido cancelado. Nenhum pedido foi criado.")
            return "Escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar."

        if state == "v24_entrega_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = [c.id for c in pedido.contentores if c.status_entrega == "PENDENTE"]
            if not pendentes:
                return self._idle(conversa, "Esse pedido já não possui ativos pendentes de entrega.")
            ctx.update({"pedido_id": pedido.id, "contentores": pendentes, "indice": 0, "entregas": []})
            return self._advance(conversa, "v24_entrega_adesivo", ctx, self._entrega_numero_prompt(ctx))
        if state == "v24_entrega_adesivo":
            if message.tipo == "interactive":
                return "Digite o número físico do equipamento para continuar."
            number = raw.strip()
            contentor = self.db.get(PedidoContentor, ctx["contentores"][ctx["indice"]])
            if not contentor or contentor.status_entrega != "PENDENTE":
                return self._idle(conversa, "Esse ativo já não está pendente. Reinicie a entrega.")
            is_carrinha = contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value
            if is_carrinha:
                if not re.fullmatch(r"\d{1,6}", number):
                    return "Informe o número da frota da carrinha ou 0 se não houver."
            elif not re.fullmatch(r"\d{1,6}", number) or number == "0":
                return "Informe somente o número visível no contentor."
            if number != "0" and number in [str(item.get("numero_adesivo")) for item in ctx.get("entregas") or []]:
                return "Esse adesivo ja foi informado neste lote."
            if not is_carrinha:
                duplicate = self.db.query(PedidoContentor).filter(
                    PedidoContentor.numero_adesivo_contentor == number,
                    PedidoContentor.status_ciclo == "EM_ANDAMENTO",
                ).first()
                if duplicate:
                    return "Esse adesivo já está em um ciclo ativo."
            entregas = list(ctx.get("entregas") or [])
            entregas.append(
                {
                    "contentor_id": ctx["contentores"][ctx["indice"]],
                    "numero_adesivo": number,
                    "fotos": [],
                }
            )
            ctx["entregas"] = entregas
            label = "Carrinha" if is_carrinha else "Contentor"
            numero_label = number if number != "0" else "sem frota"
            return self._advance(conversa, "v24_entrega_foto", ctx, f"Envie a foto do {label} {numero_label} posicionado no local.")
        if state == "v24_entrega_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            entregas = list(ctx.get("entregas") or [])
            entrega_atual = dict(entregas[-1])
            fotos = list(entrega_atual.get("fotos") or [])
            if photo not in fotos:
                fotos.append(photo)
            entrega_atual["fotos"] = fotos
            entregas[-1] = entrega_atual
            ctx["entregas"] = entregas
            return self._advance(
                conversa, "v24_entrega_foto_acao", ctx,
                "Foto guardada. O que deseja fazer?\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_entrega_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_entrega_foto", ctx, "Envie a próxima foto deste contentor.")
            if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
                return "Selecione Outra Foto ou Próximo Passo."
            ctx["indice"] += 1
            if ctx["indice"] < len(ctx["contentores"]):
                progresso = f"✅ Ativo {ctx['indice']} de {len(ctx['contentores'])} preparado."
                return self._advance(
                    conversa,
                    "v24_entrega_adesivo",
                    ctx,
                    f"{progresso}\n\nVamos registrar o próximo.\n\n{self._entrega_numero_prompt(ctx)}",
                )
            return self._advance(conversa, "v24_entrega_gps", ctx, "Compartilhe a localização GPS da obra.")
        if state == "v24_entrega_gps":
            coords = self._coordinates(message, raw) if message.tipo == "location" else None
            if not coords:
                return "Compartilhe a localização nativa do WhatsApp para confirmar a entrega."
            ctx["latitude"], ctx["longitude"] = coords
            return self._advance(conversa, "v24_entrega_referencia", ctx, "Qual é o ponto de referência? Envie “Não” se não houver.")
        if state == "v24_entrega_referencia":
            if len(raw) > 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia_entrega"] = None if choice == "nao" else raw
            return self._advance(conversa, "v24_entrega_confirmacao", ctx, self._entrega_confirmacao_prompt(ctx))
        if state == "v24_entrega_confirmacao":
            if choice in {"1", "confirmar entrega", "✅ confirmar entrega"}:
                return self._confirmar_entrega_preparada(conversa, ctx)
            if choice in {"2", "cancelar", "❌ cancelar"}:
                return self._idle(conversa, "Entrega cancelada. Nenhum ativo foi marcado como entregue.")
            return "Escolha Confirmar entrega ou Cancelar."
        if state == "v24_entrega_pagou":
            if choice in {"2", "nao", "nao, continua pendente", "🕒 nao, continua pendente"}:
                return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento permanece pendente.")
            if choice in {"1", "sim", "sim, foi pago", "✅ sim, foi pago"}:
                return self._advance(
                    conversa, "v24_entrega_forma", ctx,
                    "Selecione a forma recebida:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro",
                )
            return "Selecione Sim ou Não."
        if state == "v24_entrega_forma":
            forms = {"1": "MBWay", "2": "Transferência", "3": "Dinheiro", "4": "Outro",
                     "mbway": "MBWay", "transferencia": "Transferência", "dinheiro": "Dinheiro", "outro": "Outro"}
            form = forms.get(choice)
            if not form:
                return "Selecione uma forma de pagamento."
            if form == "Outro":
                return self._advance(conversa, "v24_entrega_forma_outro", ctx, "Qual foi a forma recebida?")
            self.service.registrar_pagamento(ctx["pedido_id"], form)
            return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado.")
        if state == "v24_entrega_forma_outro":
            if not raw:
                return "Informe a forma recebida."
            self.service.registrar_pagamento(ctx["pedido_id"], raw[:80])
            return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados. Pagamento registrado.")

        if state == "v24_recolha_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = self._recolha_pendentes(pedido)
            if not pendentes:
                return self._idle(conversa, "Esse pedido ja nao possui ativos pendentes de recolha.")
            ctx.update({"pedido_id": pedido.id, "recolhas": []})
            return self._advance(conversa, "v24_recolha_ativo", ctx, self._recolha_selecao_prompt(ctx, pendentes))
        if state == "v24_recolha_ativo":
            if self._is_recolha_terminar(message, choice, ctx):
                return self._idle(conversa, "✅ Recolhas deste cliente encerradas. Os ativos restantes continuam pendentes.")
            contentor_id = self._selected_recolha_contentor_id(raw, ctx)
            if not contentor_id:
                return "Selecione um ativo da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if not self._is_recolha_pendente_do_pedido(contentor, ctx["pedido_id"]):
                pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
                if pendentes:
                    return self._advance(
                        conversa,
                        "v24_recolha_ativo",
                        ctx,
                        "Esse ativo foi atualizado por outro operador.\n\n"
                        + self._recolha_selecao_prompt(ctx, pendentes),
                    )
                return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de recolha.")
            ctx.update({"contentor_id": contentor_id, "fotos_recolha": [], "avariado": None, "relato_avaria": None})
            return self._advance(conversa, "v24_recolha_foto", ctx, self._recolha_foto_prompt(ctx))
        if state == "v24_recolha_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            ctx.update({"contentor_id": contentor_id, "fotos_recolha": [], "avariado": None, "relato_avaria": None})
            return self._advance(conversa, "v24_recolha_foto", ctx, "Envie a foto do equipamento cheio antes do içamento.")
        if state == "v24_recolha_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            fotos = list(ctx.get("fotos_recolha") or [])
            if photo not in fotos:
                fotos.append(photo)
            ctx["fotos_recolha"] = fotos
            return self._advance(
                conversa, "v24_recolha_foto_acao", ctx,
                "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_recolha_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_recolha_foto", ctx, "Envie a próxima foto.")
            if choice in {"2", "proximo passo", "➡️ proximo passo"}:
                return self._advance(
                    conversa, "v24_recolha_avaria", ctx,
                    "O equipamento sofreu algum estrago ou avaria na obra?\n\n1. ✅ Não, está perfeito\n2. 💥 Sim, está estragado",
                )
            return "Selecione Outra Foto ou Próximo Passo."
        if state == "v24_recolha_avaria":
            if choice in {"1", "nao, esta perfeito", "✅ nao, esta perfeito"}:
                ctx["avariado"] = False
                ctx["relato_avaria"] = None
                return self._advance(conversa, "v24_recolha_confirmacao", ctx, self._recolha_confirmacao_prompt(ctx))
            if choice in {"2", "sim, esta estragado", "💥 sim, esta estragado"}:
                return self._advance(conversa, "v24_recolha_relato", ctx, "Descreva a avaria com pelo menos 10 caracteres.")
            return "Selecione uma das opções de avaria."
        if state == "v24_recolha_relato":
            relato = raw.strip()
            if len(relato) < 10:
                return "O relato da avaria precisa ter pelo menos 10 caracteres."
            ctx["avariado"] = True
            ctx["relato_avaria"] = relato
            return self._advance(conversa, "v24_recolha_confirmacao", ctx, self._recolha_confirmacao_prompt(ctx))

        if state == "v24_recolha_confirmacao":
            if choice in {"1", "confirmar recolha", "confirmar"}:
                return self._confirmar_recolha_atual(conversa, ctx)
            if choice in {"2", "cancelar ativo", "cancelar"}:
                return self._cancelar_recolha_atual(conversa, ctx)
            return "Escolha Confirmar recolha ou Cancelar ativo."

        if state == "v24_despejo_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = self._despejo_pendentes(pedido)
            if not pendentes:
                return self._idle(conversa, "Esse pedido ja nao possui ativos pendentes de despejo.")
            ctx.update({"pedido_id": pedido.id, "despejos": []})
            return self._advance(conversa, "v24_despejo_ativo", ctx, self._despejo_selecao_prompt(ctx, pendentes))
        if state == "v24_despejo_ativo":
            if self._is_despejo_terminar(message, choice, ctx):
                return self._idle(conversa, "✅ Despejos deste cliente encerrados. Os ativos restantes continuam em andamento.")
            contentor_id = self._selected_despejo_contentor_id(raw, ctx)
            if not contentor_id:
                return "Selecione um ativo da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if not self._is_despejo_pendente_do_pedido(contentor, ctx["pedido_id"]):
                pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
                if pendentes:
                    return self._advance(
                        conversa,
                        "v24_despejo_ativo",
                        ctx,
                        "Esse ativo foi atualizado por outro operador.\n\n"
                        + self._despejo_selecao_prompt(ctx, pendentes),
                    )
                return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de despejo.")
            ctx.update(
                {
                    "contentor_id": contentor_id,
                    "fotos_despejo": [],
                    "residuo_contratado": contentor.residuo_contratado,
                    "residuo_efetivo": None,
                    "carga_errada": None,
                    "relato_carga": None,
                }
            )
            return self._advance(conversa, "v24_despejo_foto", ctx, self._despejo_foto_prompt(ctx))
        if state == "v24_despejo_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            contentor = self.db.get(PedidoContentor, contentor_id)
            if not contentor:
                return "Selecione um contentor da lista."
            ctx.update(
                {
                    "contentor_id": contentor_id,
                    "pedido_id": contentor.pedido_id,
                    "fotos_despejo": [],
                    "residuo_contratado": contentor.residuo_contratado,
                    "residuo_efetivo": None,
                    "carga_errada": None,
                    "relato_carga": None,
                }
            )
            return self._advance(conversa, "v24_despejo_foto", ctx, "Envie a foto do entulho espalhado no chão.")
        if state == "v24_despejo_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            fotos = list(ctx.get("fotos_despejo") or [])
            if photo not in fotos:
                fotos.append(photo)
            ctx["fotos_despejo"] = fotos
            return self._advance(
                conversa, "v24_despejo_foto_acao", ctx,
                "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_despejo_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_despejo_foto", ctx, "Envie a próxima foto do despejo.")
            if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
                return "Selecione Outra Foto ou Próximo Passo."
            if not ctx.get("fotos_despejo"):
                return "Envie pelo menos uma imagem para continuar."
            return self._advance(conversa, "v24_despejo_conformidade", ctx, self._despejo_conformidade_prompt(ctx))
        if state in {"v24_despejo_residuo", "v24_despejo_conformidade", "v24_despejo_relato"}:
            self._hydrate_legacy_despejo_context(ctx)
        if state == "v24_despejo_residuo":
            available = ctx.get("residuos_disponiveis") or []
            residue = None
            if choice.isdigit() and 1 <= int(choice) <= len(available):
                residue = available[int(choice) - 1]
            if not residue:
                residue = next((r for r in available if self._norm(r) == choice), None)
            if residue and "pedido_id" in ctx:
                ctx["residuo_efetivo"] = residue
                ctx["carga_errada"] = True
                return self._advance(conversa, "v24_despejo_relato", ctx, "Descreva a divergencia com pelo menos 10 caracteres.")
            if not residue:
                return "Selecione um tipo de resíduo com cota em aberto."
            self.service.confirmar_despejo(ctx["contentor_id"], residue, operador=conversa.telefone)
            return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
        if state == "v24_despejo_conformidade":
            if "pedido_id" in ctx:
                if choice in {"1", "sim", "sim, corresponde", "âœ… sim, corresponde", "sim, tudo certo", "âœ… sim, tudo certo"}:
                    ctx["residuo_efetivo"] = ctx["residuo_contratado"]
                    ctx["carga_errada"] = False
                    ctx["relato_carga"] = None
                    return self._advance(conversa, "v24_despejo_confirmacao", ctx, self._despejo_confirmacao_prompt(ctx))
                if choice in {"2", "nao", "nao, existe divergencia", "âŒ nao, existe divergencia", "nao, esta misturado/errado", "ðŸš¨ nao, esta misturado/errado"}:
                    return self._despejo_residuo_efetivo_prompt(conversa, ctx)
                return "Selecione se o material corresponde ao residuo contratado."
            residue = ctx["residuo_assumido"]
            if choice in {"1", "sim, tudo certo", "✅ sim, tudo certo"}:
                self.service.confirmar_despejo(ctx["contentor_id"], residue, operador=conversa.telefone)
                return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
            if choice in {"2", "nao, esta misturado/errado", "🚨 nao, esta misturado/errado"}:
                return self._advance(conversa, "v24_despejo_relato", ctx, "Justifique a carga errada com pelo menos 10 caracteres.")
            return "Selecione Sim, tudo certo ou Não, está misturado/errado."
        if state == "v24_despejo_relato":
            if "pedido_id" in ctx:
                relato = raw.strip()
                if len(relato) < 10:
                    return "O relato da carga precisa ter pelo menos 10 caracteres."
                ctx["relato_carga"] = relato
                return self._advance(conversa, "v24_despejo_confirmacao", ctx, self._despejo_confirmacao_prompt(ctx))
            if len(raw) < 10:
                return "O relato da carga precisa ter pelo menos 10 caracteres."
            self.service.confirmar_despejo(
                ctx["contentor_id"], ctx["residuo_assumido"], True, raw, operador=conversa.telefone
            )
            return self._idle(conversa, "✅ Ciclo concluído com pendência de carga.")
        if state == "v24_despejo_confirmacao":
            if choice in {"1", "confirmar despejo", "confirmar", "âœ… confirmar despejo"}:
                return self._confirmar_despejo_atual(conversa, ctx)
            if choice in {"2", "voltar", "â†©ï¸ voltar"}:
                return self._advance(conversa, "v24_despejo_conformidade", ctx, self._despejo_conformidade_prompt(ctx))
            if choice in {"3", "cancelar", "âŒ cancelar"}:
                return self._idle(conversa, "Despejo cancelado. Nenhuma foto foi salva e o ativo permanece em andamento.")
            return "Escolha Confirmar despejo, Voltar ou Cancelar."
        return self._idle(conversa, "Fluxo reiniciado. Abra o menu para continuar.")

    def _confirmar_despejo_atual(self, conversa, ctx):
        contentor_id = ctx["contentor_id"]
        fotos = list(ctx.get("fotos_despejo") or [])
        try:
            contentor = self.service.confirmar_despejo(
                contentor_id,
                ctx.get("residuo_efetivo"),
                bool(ctx.get("carga_errada")),
                ctx.get("relato_carga"),
                operador=conversa.telefone,
                pedido_id=ctx.get("pedido_id"),
                fotos=fotos,
            )
        except ValueError:
            pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
            self._limpar_despejo_atual(ctx)
            if pendentes:
                return self._advance(
                    conversa,
                    "v24_despejo_ativo",
                    ctx,
                    "Esse ativo foi atualizado por outro operador.\n\n"
                    + self._despejo_selecao_prompt(ctx, pendentes),
                )
            return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de despejo.")

        despejos = list(ctx.get("despejos") or [])
        despejos.append({"contentor_id": contentor_id, "fotos": len(fotos), "carga_errada": bool(ctx.get("carga_errada"))})
        ctx["despejos"] = despejos
        self._limpar_despejo_atual(ctx)
        pendentes = self._despejo_pendentes_por_pedido(ctx["pedido_id"])
        label = self._equipamento_label(contentor)
        if pendentes:
            return self._advance(
                conversa,
                "v24_despejo_ativo",
                ctx,
                f"✅ {label} processado no vazadouro.\n\nSelecione a proxima unidade deste cliente:\n\n"
                + self._despejo_selecao_prompt(ctx, pendentes),
            )
        return self._idle(conversa, f"✅ {label} processado no vazadouro. Despejo do pedido concluido.")

    def _limpar_despejo_atual(self, ctx):
        for key in (
            "contentor_id",
            "fotos_despejo",
            "residuo_contratado",
            "residuo_efetivo",
            "carga_errada",
            "relato_carga",
            "residuos_disponiveis",
        ):
            ctx.pop(key, None)

    def _selected_despejo_contentor_id(self, raw, ctx) -> int | None:
        return self._selected_id(raw, ctx.get("contentores") or [])

    def _is_despejo_terminar(self, message, choice, ctx) -> bool:
        if choice in {"terminar", "terminar despejos deste cliente", "🏁 terminar despejos deste cliente"}:
            return True
        if not choice.isdigit() or int(choice) != ctx.get("terminar_indice"):
            return False
        if message.tipo == "interactive":
            return True
        return self.db.get(PedidoContentor, int(choice)) is None

    def _is_despejo_pendente_do_pedido(self, contentor, pedido_id: int) -> bool:
        return bool(
            contentor
            and contentor.pedido_id == pedido_id
            and contentor.status_recolha == "RECOLHIDO"
            and contentor.status_ciclo == "EM_ANDAMENTO"
        )

    def _despejo_foto_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else "equipamento"
        return f"Envie a foto do despejo do {label} no vazadouro."

    def _despejo_conformidade_prompt(self, ctx) -> str:
        return (
            f"O material descarregado corresponde a {ctx['residuo_contratado']}?\n\n"
            "1. ✅ Sim, corresponde\n"
            "2. ❌ Não, existe divergência"
        )

    def _despejo_residuo_efetivo_prompt(self, conversa, ctx):
        ctx["residuos_disponiveis"] = ["Entulho Limpo", "Entulho Misto"]
        return self._advance(
            conversa,
            "v24_despejo_residuo",
            ctx,
            "Qual residuo foi efetivamente encontrado?\n\n"
            + "\n".join(f"{index}. {residuo}" for index, residuo in enumerate(ctx["residuos_disponiveis"], 1)),
        )

    def _despejo_confirmacao_prompt(self, ctx) -> str:
        pedido = self.service.get(ctx["pedido_id"])
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else f"Ativo #{ctx['contentor_id']}"
        linhas = [
            "Confirme o despejo deste ativo:",
            "",
            f"Cliente: {pedido.nome_cliente if pedido else ctx.get('pedido_id')}",
            f"Ativo: {label}",
            f"Fotos: {len(ctx.get('fotos_despejo') or [])}",
            f"Residuo contratado: {ctx.get('residuo_contratado')}",
            f"Residuo efetivo: {ctx.get('residuo_efetivo')}",
            f"Divergencia: {'Sim' if ctx.get('carga_errada') else 'Nao'}",
        ]
        if ctx.get("carga_errada"):
            linhas.append(f"Relato: {ctx.get('relato_carga')}")
        linhas.extend(["", "1. ✅ Confirmar despejo", "2. ↩️ Voltar", "3. ❌ Cancelar"])
        return "\n".join(linhas)

    def _despejo_selecao_prompt(self, ctx, pendentes) -> str:
        ctx["contentores"] = [item.id for item in pendentes]
        ctx["terminar_indice"] = len(pendentes) + 1
        linhas = ["Selecione o ativo descarregado:"]
        linhas.extend(f"{index}. {self._equipamento_label(item)}" for index, item in enumerate(pendentes, 1))
        linhas.append(f"{ctx['terminar_indice']}. 🏁 Terminar despejos deste cliente")
        return "\n".join(linhas)

    def _despejo_pendentes_por_pedido(self, pedido_id: int):
        pedido = self.service.get(pedido_id)
        return self._despejo_pendentes(pedido) if pedido else []

    def _despejo_pendentes(self, pedido):
        return [
            item
            for item in sorted(pedido.contentores, key=lambda item: (item.numero_adesivo_contentor or "", item.id))
            if item.status_recolha == "RECOLHIDO" and item.status_ciclo == "EM_ANDAMENTO"
        ]

    def _hydrate_legacy_despejo_context(self, ctx) -> None:
        if "pedido_id" in ctx or not ctx.get("contentor_id"):
            return
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        if not contentor:
            return
        ctx["pedido_id"] = contentor.pedido_id
        ctx.setdefault("residuo_contratado", contentor.residuo_contratado)
        ctx.setdefault("residuo_efetivo", ctx.get("residuo_assumido"))
        ctx.setdefault("carga_errada", None)
        ctx.setdefault("relato_carga", None)
        ctx.setdefault("fotos_despejo", [])

    def _confirmar_recolha_atual(self, conversa, ctx):
        contentor_id = ctx["contentor_id"]
        avariado = bool(ctx.get("avariado"))
        relato = ctx.get("relato_avaria")
        fotos = list(ctx.get("fotos_recolha") or [])
        try:
            self.service.confirmar_recolha(contentor_id, conversa.telefone, avariado, relato, fotos)
        except ValueError:
            pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
            self._limpar_recolha_atual(ctx)
            if pendentes:
                return self._advance(
                    conversa,
                    "v24_recolha_ativo",
                    ctx,
                    "Esse ativo foi atualizado por outro operador.\n\n"
                    + self._recolha_selecao_prompt(ctx, pendentes),
                )
            return self._idle(conversa, "✅ Esse pedido ja nao possui ativos pendentes de recolha.")

        recolhas = list(ctx.get("recolhas") or [])
        recolhas.append({"contentor_id": contentor_id, "avariado": avariado, "fotos": len(fotos)})
        ctx["recolhas"] = recolhas
        self._limpar_recolha_atual(ctx)

        if "pedido_id" not in ctx:
            if avariado:
                return self._idle(conversa, "✅ Recolha registrada com pendencia de avaria.")
            return self._idle(conversa, "✅ Recolha registrada. O contentor aguarda despejo.")

        pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
        if pendentes:
            return self._advance(
                conversa,
                "v24_recolha_ativo",
                ctx,
                "Ativo recolhido. Escolha o proximo ou termine as recolhas deste cliente.\n\n"
                + self._recolha_selecao_prompt(ctx, pendentes),
            )

        if any(item.get("avariado") for item in recolhas):
            return self._idle(conversa, "✅ Recolha do pedido concluida com pendencia de avaria. Ativos aguardam despejo.")
        return self._idle(conversa, "✅ Recolha do pedido concluida. Ativos aguardam despejo.")

    def _cancelar_recolha_atual(self, conversa, ctx):
        self._limpar_recolha_atual(ctx)
        pendentes = self._recolha_pendentes_por_pedido(ctx["pedido_id"])
        if pendentes:
            return self._advance(
                conversa,
                "v24_recolha_ativo",
                ctx,
                "Recolha do ativo cancelada. Nenhuma foto foi salva.\n\n"
                + self._recolha_selecao_prompt(ctx, pendentes),
            )
        return self._idle(conversa, "✅ Nao existem mais ativos pendentes de recolha neste pedido.")

    def _limpar_recolha_atual(self, ctx):
        for key in ("contentor_id", "fotos_recolha", "avariado", "relato_avaria"):
            ctx.pop(key, None)

    def _selected_recolha_contentor_id(self, raw, ctx) -> int | None:
        return self._selected_id(raw, ctx.get("contentores") or [])

    def _is_recolha_terminar(self, message, choice, ctx) -> bool:
        if choice in {"terminar", "terminar recolhas deste cliente", "🏁 terminar recolhas deste cliente"}:
            return True
        if not choice.isdigit() or int(choice) != ctx.get("terminar_indice"):
            return False
        if message.tipo == "interactive":
            return True
        return self.db.get(PedidoContentor, int(choice)) is None

    def _is_recolha_pendente_do_pedido(self, contentor, pedido_id: int) -> bool:
        return bool(
            contentor
            and contentor.pedido_id == pedido_id
            and contentor.status_entrega == "ENTREGUE"
            and contentor.status_recolha == "PENDENTE"
        )

    def _recolha_foto_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else "equipamento"
        return f"Envie a foto de recolha do {label} cheio antes do icamento."

    def _recolha_confirmacao_prompt(self, ctx) -> str:
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        label = self._equipamento_label(contentor) if contentor else f"Ativo #{ctx['contentor_id']}"
        avaria = "Sim" if ctx.get("avariado") else "Nao"
        linhas = [
            "Confirme a recolha deste ativo:",
            "",
            f"Ativo: {label}",
            f"Fotos: {len(ctx.get('fotos_recolha') or [])}",
            f"Avaria: {avaria}",
        ]
        if ctx.get("avariado"):
            linhas.append(f"Relato: {ctx.get('relato_avaria')}")
        linhas.extend(["", "1. Confirmar recolha", "2. Cancelar ativo"])
        return "\n".join(linhas)

    def _recolha_selecao_prompt(self, ctx, pendentes) -> str:
        ctx["contentores"] = [item.id for item in pendentes]
        ctx["terminar_indice"] = len(pendentes) + 1
        linhas = ["Selecione o ativo que esta recolhendo:"]
        linhas.extend(f"{index}. {self._equipamento_label(item)}" for index, item in enumerate(pendentes, 1))
        linhas.append(f"{ctx['terminar_indice']}. 🏁 Terminar recolhas deste cliente")
        return "\n".join(linhas)

    def _recolha_pendentes_por_pedido(self, pedido_id: int):
        pedido = self.service.get(pedido_id)
        return self._recolha_pendentes(pedido) if pedido else []

    def _recolha_pendentes(self, pedido):
        return [
            item
            for item in sorted(pedido.contentores, key=lambda item: (item.numero_adesivo_contentor or "", item.id))
            if item.status_entrega == "ENTREGUE" and item.status_recolha == "PENDENTE"
        ]

    def _despejo_residuo_prompt(self, conversa, ctx):
        contentor = self.db.get(PedidoContentor, ctx["contentor_id"])
        cotas = self.service.cotas_restantes(contentor.pedido_id)
        available = [name for name, amount in cotas.items() if amount > 0]
        if len(available) == 1:
            ctx["residuo_assumido"] = available[0]
            return self._advance(
                conversa, "v24_despejo_conformidade", ctx,
                f"O entulho está correto com o contratado ({available[0]})?\n\n"
                "1. ✅ Sim, tudo certo\n2. 🚨 Não, está misturado/errado",
            )
        ctx["residuos_disponiveis"] = available
        return self._advance(
            conversa, "v24_despejo_residuo", ctx,
            "Qual resíduo caiu no chão?\n\n"
            + "\n".join(f"{i}. 🟢 {item}" for i, item in enumerate(available, 1)),
        )

    def _confirmar_entrega_preparada(self, conversa, ctx):
        try:
            pedido = self.service.confirmar_entrega_lote(
                ctx["pedido_id"],
                conversa.telefone,
                ctx["latitude"],
                ctx["longitude"],
                ctx["referencia_entrega"],
                ctx.get("entregas") or [],
            )
        except ValueError as exc:
            return self._idle(conversa, f"⚠️ {exc}")
        if pedido.status_pagamento == StatusPagamento.PENDENTE.value:
            return self._advance(
                conversa,
                "v24_entrega_pagou",
                ctx,
                "O cliente realizou o pagamento no local?\n\n"
                "1. ✅ Sim, foi pago\n"
                "2. 🕒 Não, continua pendente",
            )
        return self._idle(conversa, "✅ Entrega confirmada com sucesso para todos os ativos processados.")

    def _entrega_confirmacao_prompt(self, ctx):
        pedido = self.service.get(ctx["pedido_id"])
        entregas = ctx.get("entregas") or []
        linhas = [
            "Confirme a entrega preparada:",
            "",
            f"Cliente: {pedido.nome_cliente if pedido else ctx.get('pedido_id')}",
            f"Pedido: #{ctx['pedido_id']}",
            f"Quantidade de ativos: {len(entregas)}",
        ]
        for index, entrega in enumerate(entregas, 1):
            contentor = self.db.get(PedidoContentor, entrega["contentor_id"])
            label = self._equipamento_label(contentor) if contentor else f"Ativo #{entrega['contentor_id']}"
            numero = entrega.get("numero_adesivo") or "sem identificação"
            if contentor and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value and numero == "0":
                numero = "sem frota"
            linhas.append(
                f"{index}. {label} | identificação: {numero} | fotos: {len(entrega.get('fotos') or [])}"
            )
        referencia = ctx.get("referencia_entrega") or "sem referência"
        pagamento = "pendente" if pedido and pedido.status_pagamento == StatusPagamento.PENDENTE.value else "pago"
        linhas.extend(
            [
                f"Ponto de referência: {referencia}",
                f"Pagamento: {pagamento}",
                "",
                "1. ✅ Confirmar entrega",
                "2. ❌ Cancelar",
            ]
        )
        return "\n".join(linhas)

    def _finish_cadastro(self, conversa, ctx):
        if not ctx.get("_confirmado"):
            return self._advance(conversa, "v24_cadastro_confirmacao", ctx, self._format_confirmacao_cadastro(ctx))
        pedido = self.service.criar(
            nome_cliente=ctx["nome"], telefone_cliente=ctx["telefone"],
            data_planejada=datetime.fromisoformat(ctx["data"]), valor_global=ctx["valor"],
            pago=ctx["pago"], forma_pagamento=ctx.get("forma"),
            pedido_feito_por=conversa.telefone, endereco_aproximado=ctx["endereco"],
            ponto_referencia=ctx.get("referencia"), itens=ctx.get("itens") or [],
            endereco_latitude=ctx.get("endereco_latitude"),
            endereco_longitude=ctx.get("endereco_longitude"),
            precisa_mao_de_obra=ctx.get("precisa_mao_de_obra"),
        )
        tipo_label = self._tipo_label(ctx.get("tipo_solicitacao"))
        return self._idle(conversa, f"✅ Pedido #{pedido.id} criado com {len(pedido.contentores)} {tipo_label.lower()}(es).")

    def _registrar_item_cadastro(self, conversa, ctx, residue):
        tipo = ctx.get("tipo_solicitacao") or TipoEquipamentoPedido.CONTENTOR.value
        item = {
            "tipo_equipamento": tipo,
            "horario_agendado": ctx.get("horario_agendado") if tipo == TipoEquipamentoPedido.CARRINHA.value else None,
            "precisa_mao_de_obra": False,
        }
        item_atual = dict(ctx.get("item_atual") or {})
        if item_atual.get("tipo_equipamento"):
            item["tipo_equipamento"] = item_atual["tipo_equipamento"]
        if item_atual.get("horario_agendado"):
            item["horario_agendado"] = item_atual["horario_agendado"]
        item["residuo_contratado"] = residue
        ctx["itens"] = [*(ctx.get("itens") or []), item]
        ctx["residuos"] = [*(ctx.get("residuos") or []), residue]
        ctx.pop("item_atual", None)
        if len(ctx["itens"]) < ctx["quantidade"]:
            return self._advance(conversa, "v24_cadastro_tipo_equipamento", ctx, self._tipo_equipamento_prompt(ctx))
        return self._advance(
            conversa,
            "v24_cadastro_data",
            ctx,
            "Quando está planejada a entrega?\n\n1. Hoje\n2. Amanhã\n3. Outra data",
        )

    def _parse_residue(self, choice):
        return {
            "1": "Entulho Limpo",
            "option_1": "Entulho Limpo",
            "pedido_residuo_limpo": "Entulho Limpo",
            "entulho limpo": "Entulho Limpo",
            "limpo": "Entulho Limpo",
            "2": "Entulho Misto",
            "option_2": "Entulho Misto",
            "pedido_residuo_misto": "Entulho Misto",
            "entulho misto": "Entulho Misto",
            "misto": "Entulho Misto",
        }.get(choice)

    def _parse_mao_obra(self, choice):
        return {
            "1": True,
            "option_1": True,
            "pedido_mao_obra_sim": True,
            "sim": True,
            "✅ sim": True,
            "sim, com pessoal": True,
            "com pessoal": True,
            "2": False,
            "option_2": False,
            "pedido_mao_obra_nao": False,
            "nao": False,
            "não": False,
            "❌ nao": False,
            "nao, apenas equipamento": False,
            "não, apenas equipamento": False,
            "apenas equipamento": False,
        }.get(choice)

    def _pedido_entrega_label(self, pedido) -> str:
        pendentes = [item for item in pedido.contentores if item.status_entrega == "PENDENTE"]
        tipos = {item.tipo_equipamento for item in pendentes}
        if tipos == {TipoEquipamentoPedido.CARRINHA.value}:
            tipo = "Carrinha"
        elif tipos == {TipoEquipamentoPedido.CONTENTOR.value}:
            tipo = "Contentor"
        else:
            tipo = "Equipamento"
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        return f"#{pedido.id} — {pedido.nome_cliente} — {tipo} x{len(pendentes)} — {data}"

    def _pedido_recolha_label(self, pedido) -> str:
        pendentes = self._recolha_pendentes(pedido)
        contentores = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value]
        carrinhas = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value]
        partes = []
        if contentores:
            partes.append(", ".join(self._equipamento_label(item) for item in contentores))
        if carrinhas:
            partes.append(", ".join(self._equipamento_label(item) for item in carrinhas))
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        resumo = " | ".join(partes) if partes else "sem ativos pendentes"
        return f"#{pedido.id} - {pedido.nome_cliente} - {resumo} - {data}"

    def _pedido_despejo_label(self, pedido) -> str:
        pendentes = self._despejo_pendentes(pedido)
        contentores = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CONTENTOR.value]
        carrinhas = [item for item in pendentes if item.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value]
        partes = []
        if contentores:
            partes.append(f"{len(contentores)} Contentor(es)")
        if carrinhas:
            partes.append(f"{len(carrinhas)} Carrinha(s)")
        data = pedido.data_planejada.strftime("%d/%m/%Y") if pedido.data_planejada else "sem data"
        resumo = " e ".join(partes) if partes else "sem ativos"
        return f"#{pedido.id} - {pedido.nome_cliente} - {resumo} aguardando despejo - {data}"

    def _equipamento_label(self, contentor: PedidoContentor) -> str:
        if contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            horario = contentor.horario_agendado or "sem horario"
            return f"🚛 Carrinha ({horario})"
        return f"📦 Contentor {contentor.numero_adesivo_contentor or contentor.id}"

    def _entrega_numero_prompt(self, ctx):
        contentor = self.db.get(PedidoContentor, ctx["contentores"][ctx["indice"]])
        if contentor and contentor.tipo_equipamento == TipoEquipamentoPedido.CARRINHA.value:
            return "Confirme o número da frota da carrinha alocada (ou digite 0 se não houver):"
        return "Digite o número do contentor que está a descarregar agora:"

    def _tipo_equipamento_prompt(self, ctx):
        index = len(ctx.get("itens") or []) + 1
        return f"Tipo de equipamento do item {index}/{ctx['quantidade']}:\n\n1. 📦 Contentor\n2. 🚛 Carrinha"

    def _parse_tipo_solicitacao(self, choice):
        return {
            "1": TipoEquipamentoPedido.CONTENTOR.value,
            "contentor": TipoEquipamentoPedido.CONTENTOR.value,
            "contentores": TipoEquipamentoPedido.CONTENTOR.value,
            "2": TipoEquipamentoPedido.CARRINHA.value,
            "carrinha": TipoEquipamentoPedido.CARRINHA.value,
            "carrinhas": TipoEquipamentoPedido.CARRINHA.value,
        }.get(choice)

    def _tipo_solicitacao_prompt(self):
        return "🚛 Qual é o tipo de solicitação?\n\n1️⃣ Contentor\n2️⃣ Carrinha"

    def _horario_carrinha_prompt(self):
        return "Qual o horário agendado da carrinha? Envie no formato HH:MM. Ex: 14:00"

    def _quantidade_prompt(self, ctx):
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            return "🔢 Quantas carrinhas são necessárias para este pedido?"
        return "🔢 Quantos contentores são necessários para este pedido?"

    def _mao_obra_prompt(self):
        return (
            "👷 Este pedido necessita de mão de obra?\n\n"
            "1. Sim\n"
            "2. Não"
        )

    def _residuo_prompt(self, ctx):
        index = len(ctx["residuos"]) + 1
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            equipamento = f"da carrinha {index}/{ctx['quantidade']}"
        else:
            equipamento = f"do contentor {index}/{ctx['quantidade']}"
        return f"Resíduo {equipamento}:\n\n1. Entulho Limpo\n2. Entulho Misto"

    def _format_confirmacao_cadastro(self, ctx):
        tipo = self._tipo_label(ctx.get("tipo_solicitacao"))
        mao_obra = "Sim" if ctx.get("precisa_mao_de_obra") else "Não"
        quantidade_label = (
            "Quantidade de carrinhas"
            if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value
            else "Quantidade de contentores"
        )
        linhas = [
            "Confirme os dados do pedido:",
            "",
            f"Tipo da solicitação: {tipo}",
            f"{quantidade_label}: {ctx.get('quantidade')}",
            f"Mão de obra: {mao_obra}",
        ]
        if ctx.get("tipo_solicitacao") == TipoEquipamentoPedido.CARRINHA.value:
            linhas.append(f"Horário da carrinha: {ctx.get('horario_agendado') or 'sem horário'}")
        linhas.extend(
            [
                f"Cliente: {ctx.get('nome')}",
                f"Telefone: {ctx.get('telefone')}",
                f"Valor: {self._format_money(ctx.get('valor'))}",
                f"Pagamento: {'Pago' if ctx.get('pago') else 'Pendente'}",
                f"Endereço: {ctx.get('endereco')}",
                "",
                "1. Confirmar e salvar",
                "2. Corrigir",
                "3. Cancelar",
            ]
        )
        return "\n".join(linhas)

    def _tipo_label(self, tipo):
        return "Carrinha" if tipo == TipoEquipamentoPedido.CARRINHA.value else "Contentor"

    def _endereco_prompt(self):
        return "Informe o endereço aproximado (até 300 caracteres) ou envie um link do Google Maps."

    def _format_money(self, value):
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0.00")
        formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        return f"{formatted} €"

    def _advance(self, conversa, state, ctx, response):
        if state == "v24_cadastro_tipo_solicitacao":
            response = self._tipo_solicitacao_prompt()
        elif state == "v24_cadastro_quantidade":
            response = self._quantidade_prompt(ctx)
        elif state == "v24_cadastro_tipo_equipamento" and ctx.get("tipo_solicitacao"):
            if (
                "precisa_mao_de_obra" in ctx
                and ctx["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value
                and not ctx.get("horario_agendado")
            ):
                state = "v24_cadastro_horario_carrinha"
                response = self._horario_carrinha_prompt()
            elif "precisa_mao_de_obra" in ctx:
                state = "v24_cadastro_residuo"
                response = self._residuo_prompt(ctx)
            elif ctx["tipo_solicitacao"] == TipoEquipamentoPedido.CARRINHA.value:
                state = "v24_cadastro_mao_obra"
                response = self._mao_obra_prompt()
            else:
                state = "v24_cadastro_mao_obra"
                response = self._mao_obra_prompt()
        elif state == "v24_cadastro_residuo" and ctx.get("tipo_solicitacao"):
            response = self._residuo_prompt(ctx)
        conversa.estado_atual = state
        conversa.contexto_json = ctx
        self.db.commit()
        return response

    def _idle(self, conversa, response):
        conversa.estado_atual = "idle"
        conversa.contexto_json = {}
        self.db.commit()
        return response

    def _selected_id(self, raw, ids):
        value = self._norm(raw)
        if value.isdigit():
            number = int(value)
            if 1 <= number <= len(ids):
                return ids[number - 1]
            if number in ids:
                return number
        match = re.search(r"#?(\d+)", value)
        return int(match.group(1)) if match and int(match.group(1)) in ids else None

    def _photo(self, message):
        return message.media_id or message.filename or message.message_id if message.tipo == "image" else None

    def _phone_from_message(self, message, raw):
        value = message.contact_phone or raw
        phone = re.sub(r"\D", "", value or "")
        return phone if len(phone) >= 9 else None

    def _coordinates(self, message, raw):
        if message.tipo == "location" and message.latitude is not None and message.longitude is not None:
            return float(message.latitude), float(message.longitude)
        match = re.search(r"(?:q=|@|!3d)(-?\d{1,2}\.\d+)[,!3d]*[,\s!4d]+(-?\d{1,3}\.\d+)", raw)
        return (float(match.group(1)), float(match.group(2))) if match else None

    def _norm(self, value):
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(c for c in normalized if not unicodedata.combining(c)).strip().lower()

    def _horario_valido(self, value):
        if not re.fullmatch(r"\d{2}:\d{2}", (value or "").strip()):
            return False
        hour, minute = [int(part) for part in value.split(":")]
        return 0 <= hour <= 23 and 0 <= minute <= 59

    def _lisbon_timezone(self):
        try:
            return ZoneInfo("Europe/Lisbon")
        except ZoneInfoNotFoundError:
            return timezone.utc
