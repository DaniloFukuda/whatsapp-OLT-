import re
import unicodedata
from datetime import datetime, timedelta, timezone
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
        conversa.estado_atual = "v24_cadastro_nome"
        conversa.contexto_json = {}
        self.db.commit()
        return "Qual é o nome do cliente?"

    def start_entrega(self, conversa: ConversaWhatsApp) -> str:
        pedidos = self.service.pedidos_pendentes_entrega()
        if not pedidos:
            return self._idle(conversa, "Não existem pedidos pendentes de entrega.")
        return self._advance(
            conversa,
            "v24_entrega_pedido",
            {"ids": [p.id for p in pedidos]},
            "Selecione o pedido pendente:\n\n"
            + "\n".join(f"{i}. #{p.id} — {p.nome_cliente}" for i, p in enumerate(pedidos, 1)),
        )

    def start_recolha(self, conversa: ConversaWhatsApp) -> str:
        itens = self.service.contentores_para_recolha()
        if not itens:
            return self._idle(conversa, "Não existem contentores aguardando recolha.")
        return self._advance(
            conversa,
            "v24_recolha_contentor",
            {"ids": [c.id for c in itens]},
            "Selecione o equipamento que será içado:\n\n"
            + "\n".join(
                f"{i}. {self._equipamento_label(c)} — {c.pedido.nome_cliente}"
                for i, c in enumerate(itens, 1)
            ),
        )

    def start_despejo(self, conversa: ConversaWhatsApp) -> str:
        itens = self.service.contentores_para_despejo()
        if not itens:
            return self._idle(conversa, "Não existem contentores recolhidos aguardando despejo.")
        return self._advance(
            conversa,
            "v24_despejo_contentor",
            {"ids": [c.id for c in itens]},
            "Selecione o equipamento no camião:\n\n"
            + "\n".join(
                f"{i}. {self._equipamento_label(c)} — {c.pedido.nome_cliente}"
                for i, c in enumerate(itens, 1)
            ),
        )

    def handle(self, conversa: ConversaWhatsApp, message: NormalizedWhatsAppMessage) -> str:
        state = conversa.estado_atual
        ctx = dict(conversa.contexto_json or {})
        raw = (message.texto or "").strip()
        choice = self._norm(raw)

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
                "v24_cadastro_data",
                ctx,
                "Quando está planejada a entrega?\n\n1. Hoje\n2. Amanhã\n3. Outra data",
            )
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
            return self._advance(conversa, "v24_cadastro_quantidade", ctx, "Quantos contentores fazem parte do pedido?")
        if state == "v24_cadastro_data_manual":
            try:
                planned = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=self._lisbon_timezone())
            except ValueError:
                return "Data inválida. Use o formato DD/MM/AAAA."
            ctx["data"] = planned.isoformat()
            return self._advance(conversa, "v24_cadastro_quantidade", ctx, "Quantos contentores fazem parte do pedido?")
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
            if equipamento == TipoEquipamentoPedido.CARRINHA.value:
                return self._advance(
                    conversa,
                    "v24_cadastro_horario_carrinha",
                    ctx,
                    "Qual o horário agendado da carrinha? Envie no formato HH:MM. Ex: 14:00",
                )
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_horario_carrinha":
            if not self._horario_valido(raw):
                return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
            item_atual = dict(ctx.get("item_atual") or {})
            item_atual["horario_agendado"] = raw
            ctx["item_atual"] = item_atual
            return self._advance(conversa, "v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())
        if state == "v24_cadastro_mao_obra":
            if choice in {"1", "sim", "sim, com pessoal", "com pessoal"}:
                item_atual = dict(ctx.get("item_atual") or {})
                item_atual["precisa_mao_de_obra"] = True
                ctx["item_atual"] = item_atual
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            if choice in {"2", "nao", "não", "nao, apenas equipamento", "não, apenas equipamento", "apenas equipamento"}:
                item_atual = dict(ctx.get("item_atual") or {})
                item_atual["precisa_mao_de_obra"] = False
                ctx["item_atual"] = item_atual
                return self._advance(conversa, "v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
            return "Selecione Sim, com pessoal ou Não, apenas equipamento."
        if state == "v24_cadastro_residuo":
            residue = self._parse_residue(choice)
            if not residue:
                return "Selecione Entulho Limpo ou Entulho Misto."
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

        if state == "v24_entrega_pedido":
            pedido_id = self._selected_id(raw, ctx["ids"])
            pedido = self.service.get(pedido_id) if pedido_id else None
            if not pedido:
                return "Selecione um pedido da lista."
            pendentes = [c.id for c in pedido.contentores if c.status_entrega == "PENDENTE"]
            ctx.update({"pedido_id": pedido.id, "contentores": pendentes, "indice": 0, "entregas": []})
            return self._advance(conversa, "v24_entrega_adesivo", ctx, self._entrega_numero_prompt(ctx))
        if state == "v24_entrega_adesivo":
            number = raw.strip()
            contentor = self.db.get(PedidoContentor, ctx["contentores"][ctx["indice"]])
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
            entrega_atual["fotos"] = [*(entrega_atual.get("fotos") or []), photo]
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
                return self._advance(conversa, "v24_entrega_adesivo", ctx, self._entrega_numero_prompt(ctx))
            return self._advance(conversa, "v24_entrega_gps", ctx, "Compartilhe a localização GPS da obra.")
        if state == "v24_entrega_gps":
            coords = self._coordinates(message, raw)
            if not coords:
                return "Compartilhe uma localização nativa ou um link válido do Google Maps."
            ctx["latitude"], ctx["longitude"] = coords
            return self._advance(conversa, "v24_entrega_referencia", ctx, "Qual é o ponto de referência? Envie “Não” se não houver.")
        if state == "v24_entrega_referencia":
            if len(raw) > 50:
                return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia_entrega"] = None if choice == "nao" else raw
            pedido = self.service.confirmar_entrega_lote(
                ctx["pedido_id"],
                conversa.telefone,
                ctx["latitude"],
                ctx["longitude"],
                ctx["referencia_entrega"],
                ctx.get("entregas") or [],
            )
            if pedido.status_pagamento == StatusPagamento.PENDENTE.value:
                return self._advance(
                    conversa, "v24_entrega_pagou", ctx,
                    "O cliente pagou no ato da entrega?\n\n1. Sim\n2. Não",
                )
            return self._idle(conversa, "✅ Entrega do lote registrada com sucesso.")
        if state == "v24_entrega_pagou":
            if choice in {"2", "nao"}:
                return self._idle(conversa, "✅ Entrega registrada. O pagamento permanece pendente.")
            if choice in {"1", "sim"}:
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
            return self._idle(conversa, "✅ Entrega e pagamento registrados.")
        if state == "v24_entrega_forma_outro":
            if not raw:
                return "Informe a forma recebida."
            self.service.registrar_pagamento(ctx["pedido_id"], raw[:80])
            return self._idle(conversa, "✅ Entrega e pagamento registrados.")

        if state == "v24_recolha_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            ctx.update({"contentor_id": contentor_id, "fotos": 0})
            return self._advance(conversa, "v24_recolha_foto", ctx, "Envie a foto do equipamento cheio antes do içamento.")
        if state == "v24_recolha_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            self.service.adicionar_foto(ctx["contentor_id"], photo, TipoFoto.RECOLHA)
            ctx["fotos"] += 1
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
                self.service.confirmar_recolha(ctx["contentor_id"], conversa.telefone, False, None)
                return self._idle(conversa, "✅ Recolha registrada. O contentor aguarda despejo.")
            if choice in {"2", "sim, esta estragado", "💥 sim, esta estragado"}:
                return self._advance(conversa, "v24_recolha_relato", ctx, "Descreva a avaria com pelo menos 10 caracteres.")
            return "Selecione uma das opções de avaria."
        if state == "v24_recolha_relato":
            if len(raw) < 10:
                return "O relato da avaria precisa ter pelo menos 10 caracteres."
            self.service.confirmar_recolha(ctx["contentor_id"], conversa.telefone, True, raw)
            return self._idle(conversa, "✅ Recolha registrada com pendência de avaria.")

        if state == "v24_despejo_contentor":
            contentor_id = self._selected_id(raw, ctx["ids"])
            if not contentor_id:
                return "Selecione um contentor da lista."
            ctx.update({"contentor_id": contentor_id, "fotos": 0})
            return self._advance(conversa, "v24_despejo_foto", ctx, "Envie a foto do entulho espalhado no chão.")
        if state == "v24_despejo_foto":
            photo = self._photo(message)
            if not photo:
                return "Envie uma imagem para continuar."
            self.service.adicionar_foto(ctx["contentor_id"], photo, TipoFoto.DESPEJO)
            ctx["fotos"] += 1
            return self._advance(
                conversa, "v24_despejo_foto_acao", ctx,
                "Foto guardada.\n\n1. ➕ Outra Foto\n2. ➡️ Próximo Passo",
            )
        if state == "v24_despejo_foto_acao":
            if choice in {"1", "outra foto", "➕ outra foto"}:
                return self._advance(conversa, "v24_despejo_foto", ctx, "Envie a próxima foto do despejo.")
            if choice not in {"2", "proximo passo", "➡️ proximo passo"}:
                return "Selecione Outra Foto ou Próximo Passo."
            return self._despejo_residuo_prompt(conversa, ctx)
        if state == "v24_despejo_residuo":
            available = ctx.get("residuos_disponiveis") or []
            residue = None
            if choice.isdigit() and 1 <= int(choice) <= len(available):
                residue = available[int(choice) - 1]
            if not residue:
                residue = next((r for r in available if self._norm(r) == choice), None)
            if not residue:
                return "Selecione um tipo de resíduo com cota em aberto."
            self.service.confirmar_despejo(ctx["contentor_id"], residue)
            return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
        if state == "v24_despejo_conformidade":
            residue = ctx["residuo_assumido"]
            if choice in {"1", "sim, tudo certo", "✅ sim, tudo certo"}:
                self.service.confirmar_despejo(ctx["contentor_id"], residue)
                return self._idle(conversa, "✅ Despejo auditado e ciclo concluído.")
            if choice in {"2", "nao, esta misturado/errado", "🚨 nao, esta misturado/errado"}:
                return self._advance(conversa, "v24_despejo_relato", ctx, "Justifique a carga errada com pelo menos 10 caracteres.")
            return "Selecione Sim, tudo certo ou Não, está misturado/errado."
        if state == "v24_despejo_relato":
            if len(raw) < 10:
                return "O relato da carga precisa ter pelo menos 10 caracteres."
            self.service.confirmar_despejo(ctx["contentor_id"], ctx["residuo_assumido"], True, raw)
            return self._idle(conversa, "✅ Ciclo concluído com pendência de carga.")
        return self._idle(conversa, "Fluxo reiniciado. Abra o menu para continuar.")

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

    def _finish_cadastro(self, conversa, ctx):
        pedido = self.service.criar(
            nome_cliente=ctx["nome"], telefone_cliente=ctx["telefone"],
            data_planejada=datetime.fromisoformat(ctx["data"]), valor_global=ctx["valor"],
            pago=ctx["pago"], forma_pagamento=ctx.get("forma"),
            pedido_feito_por=conversa.telefone, endereco_aproximado=ctx["endereco"],
            ponto_referencia=ctx.get("referencia"), itens=ctx.get("itens") or [],
            endereco_latitude=ctx.get("endereco_latitude"),
            endereco_longitude=ctx.get("endereco_longitude"),
        )
        return self._idle(conversa, f"✅ Pedido #{pedido.id} criado com {len(pedido.contentores)} contentor(es).")

    def _registrar_item_cadastro(self, conversa, ctx, residue):
        item = dict(ctx.get("item_atual") or {})
        item.setdefault("tipo_equipamento", TipoEquipamentoPedido.CONTENTOR.value)
        item.setdefault("horario_agendado", None)
        item.setdefault("precisa_mao_de_obra", False)
        item["residuo_contratado"] = residue
        ctx["itens"] = [*(ctx.get("itens") or []), item]
        ctx["residuos"] = [*(ctx.get("residuos") or []), residue]
        ctx.pop("item_atual", None)
        if len(ctx["itens"]) < ctx["quantidade"]:
            return self._advance(conversa, "v24_cadastro_tipo_equipamento", ctx, self._tipo_equipamento_prompt(ctx))
        return self._advance(conversa, "v24_cadastro_valor", ctx, "Qual é o valor global do pedido?")

    def _parse_residue(self, choice):
        return {
            "1": "Entulho Limpo",
            "entulho limpo": "Entulho Limpo",
            "limpo": "Entulho Limpo",
            "2": "Entulho Misto",
            "entulho misto": "Entulho Misto",
            "misto": "Entulho Misto",
        }.get(choice)

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

    def _mao_obra_prompt(self):
        return (
            "O cliente solicitou pessoal para carregamento do resíduo?\n\n"
            "1. Sim, com pessoal\n"
            "2. Não, apenas equipamento"
        )

    def _residuo_prompt(self, ctx):
        index = len(ctx["residuos"]) + 1
        return f"Resíduo do contentor {index}/{ctx['quantidade']}:\n\n1. Entulho Limpo\n2. Entulho Misto"

    def _endereco_prompt(self):
        return "Informe o endereço aproximado (até 300 caracteres) ou envie um link do Google Maps."

    def _advance(self, conversa, state, ctx, response):
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
