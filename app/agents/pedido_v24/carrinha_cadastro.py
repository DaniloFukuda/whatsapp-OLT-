"""Decisões puras do cadastro moderno de Carrinha."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from app.agents.pedido_v24.transitions import AdvanceTransition, IdleTransition
from app.models.pedido import TipoEquipamentoPedido


class CarrinhaCadastroModality(Enum):
    CARRINHA_INTENT = "CARRINHA_INTENT"
    CARRINHA_PROVEN = "CARRINHA_PROVEN"
    OTHER = "OTHER"
    LEGACY_INDETERMINATE = "LEGACY_INDETERMINATE"
    DIVERGENT = "DIVERGENT"


@dataclass(frozen=True)
class ConfirmarCadastroCarrinha:
    context: dict[str, Any]


def classify_carrinha_cadastro(context):
    ctx = context or {}
    tipo = ctx.get("tipo_solicitacao")
    if tipo == TipoEquipamentoPedido.CONTENTOR.value:
        return CarrinhaCadastroModality.OTHER
    if tipo != TipoEquipamentoPedido.CARRINHA.value:
        return CarrinhaCadastroModality.LEGACY_INDETERMINATE
    itens = ctx.get("itens")
    if itens is not None and not isinstance(itens, list):
        return CarrinhaCadastroModality.LEGACY_INDETERMINATE
    itens = itens or []
    if any(not isinstance(item, dict) or not item.get("tipo_equipamento") for item in itens):
        return CarrinhaCadastroModality.LEGACY_INDETERMINATE
    if any(item["tipo_equipamento"] != TipoEquipamentoPedido.CARRINHA.value for item in itens):
        return CarrinhaCadastroModality.DIVERGENT
    quantidade = ctx.get("quantidade")
    if (
        isinstance(quantidade, int)
        and not isinstance(quantidade, bool)
        and quantidade > 0
        and len(itens) == quantidade
    ):
        required = {"nome", "telefone", "data", "horario_agendado"}
        if not required.issubset(ctx):
            return CarrinhaCadastroModality.LEGACY_INDETERMINATE
        return CarrinhaCadastroModality.CARRINHA_PROVEN
    return CarrinhaCadastroModality.CARRINHA_INTENT


class CarrinhaCadastroAgent:
    """Possui as regras conversacionais Carrinha sem acessar infraestrutura."""

    @staticmethod
    def is_carrinha_selection(value):
        return CarrinhaCadastroAgent._normalize(value) in {"2", "carrinha", "carrinhas"}

    def decide_tipo_solicitacao(self, context):
        ctx = dict(context or {})
        ctx["tipo_solicitacao"] = TipoEquipamentoPedido.CARRINHA.value
        return AdvanceTransition("v24_cadastro_quantidade", ctx, self._quantidade_prompt())

    def decide_quantidade(self, context, message):
        raw = (message.texto or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 50:
            return "Informe uma quantidade entre 1 e 50."
        ctx = dict(context or {})
        ctx.update({"quantidade": int(raw), "itens": [], "residuos": []})
        return AdvanceTransition("v24_cadastro_nome", ctx, "Qual é o nome do cliente?")

    def decide_nome(self, context, message):
        ctx = dict(context or {})
        raw = (message.texto or "").strip()
        name = (message.contact_name or raw).strip()
        if len(name) < 2:
            return "Informe o nome completo do cliente."
        ctx["nome"] = name
        phone = self._phone(message.contact_phone or "")
        if phone:
            ctx["telefone"] = phone
            return AdvanceTransition("v24_cadastro_data", ctx, self._data_prompt())
        return AdvanceTransition("v24_cadastro_telefone", ctx, "Qual é o telefone do cliente?")

    def decide_telefone(self, context, message):
        phone = self._phone(message.contact_phone or message.texto or "")
        if not phone:
            return "O telefone informado não é válido."
        ctx = dict(context or {})
        ctx["telefone"] = phone
        return AdvanceTransition("v24_cadastro_data", ctx, self._data_prompt())

    def decide_data(self, context, message, now):
        choice = self._normalize(message.texto)
        if choice in {"1", "hoje"}:
            planned = now
        elif choice in {"2", "amanha"}:
            planned = now + timedelta(days=1)
        elif choice in {"3", "outra data"}:
            return AdvanceTransition("v24_cadastro_data_manual", dict(context or {}), "Informe a data no formato DD/MM/AAAA.")
        else:
            return "Selecione Hoje, Amanhã ou Outra data."
        ctx = dict(context or {})
        ctx["data"] = planned.isoformat()
        return AdvanceTransition("v24_cadastro_horario_carrinha", ctx, self._horario_prompt())

    def decide_data_manual(self, context, message, timezone):
        try:
            planned = datetime.strptime((message.texto or "").strip(), "%d/%m/%Y").replace(tzinfo=timezone)
        except ValueError:
            return "Data inválida. Use o formato DD/MM/AAAA."
        ctx = dict(context or {})
        ctx["data"] = planned.isoformat()
        return AdvanceTransition("v24_cadastro_horario_carrinha", ctx, self._horario_prompt())

    def decide_horario(self, context, message):
        raw = (message.texto or "").strip()
        if not self._horario_valido(raw):
            return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
        ctx = dict(context or {})
        ctx["horario_agendado"] = raw
        ctx["item_atual"] = {"horario_agendado": raw}
        return AdvanceTransition("v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))

    def decide_residuo(self, context, message):
        ctx = dict(context or {})
        residue = self._parse_residue(self._normalize(message.texto))
        if not residue:
            return self._residuo_prompt(ctx)
        item = {
            "tipo_equipamento": TipoEquipamentoPedido.CARRINHA.value,
            "horario_agendado": ctx.get("horario_agendado"),
            "precisa_mao_de_obra": False,
            "residuo_contratado": residue,
        }
        ctx["itens"] = [*(ctx.get("itens") or []), item]
        ctx["residuos"] = [*(ctx.get("residuos") or []), residue]
        ctx.pop("item_atual", None)
        if len(ctx["itens"]) < ctx["quantidade"]:
            return AdvanceTransition("v24_cadastro_residuo", ctx, self._residuo_prompt(ctx))
        return AdvanceTransition("v24_cadastro_mao_obra", ctx, self._mao_obra_prompt())

    def decide_mao_obra(self, context, message):
        value = self._parse_mao_obra(self._normalize(message.texto))
        if value is None:
            return self._mao_obra_prompt()
        ctx = dict(context or {})
        ctx["precisa_mao_de_obra"] = value
        ctx.pop("item_atual", None)
        return AdvanceTransition("v24_cadastro_valor", ctx, "Qual é o valor comercial total?")

    def decide_valor(self, context, message):
        try:
            value = str(float((message.texto or "").strip().replace(",", ".")))
        except ValueError:
            return "Valor inválido."
        ctx = dict(context or {})
        ctx["valor"] = value
        return AdvanceTransition("v24_cadastro_pago", ctx, "O pedido já está pago?\n\n1. Sim, já está pago\n2. Não, pendente")

    def decide_pago(self, context, message):
        choice = self._normalize(message.texto)
        ctx = dict(context or {})
        if choice in {"1", "sim", "sim, ja esta pago"}:
            ctx["pago"] = True
            return AdvanceTransition("v24_cadastro_forma", ctx, "Selecione a forma de pagamento:\n\n1. MBWay\n2. Transferência\n3. Dinheiro\n4. Outro")
        if choice in {"2", "nao", "nao, pendente"}:
            ctx.update({"pago": False, "forma": None})
            return AdvanceTransition("v24_cadastro_endereco", ctx, self._endereco_prompt())
        return "Selecione uma das opções de pagamento."

    def decide_forma(self, context, message):
        forma = self._parse_forma(self._normalize(message.texto))
        if not forma:
            return "Selecione uma forma de pagamento."
        ctx = dict(context or {})
        if forma == "Outro":
            return AdvanceTransition("v24_cadastro_forma_outro", ctx, "Qual foi a forma de pagamento?")
        ctx["forma"] = forma
        return AdvanceTransition("v24_cadastro_endereco", ctx, self._endereco_prompt())

    def decide_forma_outro(self, context, message):
        raw = (message.texto or "").strip()
        if not raw:
            return "Informe a forma de pagamento."
        ctx = dict(context or {})
        ctx["forma"] = raw[:80]
        return AdvanceTransition("v24_cadastro_endereco", ctx, self._endereco_prompt())

    def decide_endereco(self, context, message, coordinates):
        raw = (message.texto or "").strip()
        if message.tipo == "location" and not coordinates:
            return "Não foi possível ler a localização. Reenvie a localização nativa ou digite o endereço."
        if not raw or len(raw) > 300:
            return "O endereço precisa ter entre 1 e 300 caracteres."
        ctx = dict(context or {})
        ctx["endereco"] = raw
        if coordinates:
            ctx["endereco_latitude"], ctx["endereco_longitude"] = coordinates
        return AdvanceTransition("v24_cadastro_referencia_opcao", ctx, "Deseja informar um ponto de referência?\n\n1. Sim\n2. Não")

    def decide_referencia_opcao(self, context, message):
        choice = self._normalize(message.texto)
        ctx = dict(context or {})
        if choice in {"1", "sim"}:
            return AdvanceTransition("v24_cadastro_referencia", ctx, "Qual é o ponto de referência?")
        if choice in {"2", "nao"}:
            ctx["referencia"] = None
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        return "Selecione Sim ou Não."

    def decide_referencia(self, context, message):
        raw = (message.texto or "").strip()
        if not 1 <= len(raw) <= 50:
            return "O ponto de referência deve ter no máximo 50 caracteres."
        ctx = dict(context or {})
        ctx["referencia"] = raw
        return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")

    def decide_confirmacao(self, context, message):
        choice = self._normalize(message.texto)
        if choice in {"1", "sim", "confirmar", "confirmar e salvar"}:
            return ConfirmarCadastroCarrinha(dict(context or {}))
        if choice in {"2", "corrigir"}:
            return AdvanceTransition("v24_cadastro_corrigir", dict(context or {}), "")
        if choice in {"3", "cancelar"}:
            return IdleTransition("Pedido cancelado. Nenhum pedido foi criado.")
        return "Escolha 1 para confirmar, 2 para corrigir ou 3 para cancelar."

    def decide_corrigir(self, context, field, next_state, prompt):
        if not field:
            return prompt
        ctx = dict(context or {})
        if field == "forma_pagamento" and not ctx.get("pago"):
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        ctx["editing_field"] = field
        return AdvanceTransition(next_state, ctx, prompt)

    def decide_edicao(self, context, message, *, coordinates=None, now=None):
        ctx = dict(context or {})
        field = ctx.get("editing_field")
        raw = (message.texto or "").strip()
        choice = self._normalize(raw)
        if field == "quantidade":
            if not raw.isdigit() or not 1 <= int(raw) <= 50:
                return "Informe uma quantidade entre 1 e 50."
            ctx["quantidade"] = int(raw); self._ajustar_itens_quantidade(ctx)
        elif field == "nome_cliente":
            name = (message.contact_name or raw).strip()
            if len(name) < 2: return "Informe o nome completo do cliente."
            ctx["nome"] = name
            phone = self._phone(message.contact_phone or "")
            if phone: ctx["telefone"] = phone
        elif field == "telefone":
            phone = self._phone(message.contact_phone or raw)
            if not phone: return "O telefone informado não é válido."
            ctx["telefone"] = phone
        elif field == "data_entrega":
            if choice in {"1", "hoje"}: value = now
            elif choice in {"2", "amanha"}: value = now + timedelta(days=1)
            else:
                try: value = datetime.strptime(raw, "%d/%m/%Y").replace(tzinfo=now.tzinfo)
                except ValueError: return "Data inválida. Use Hoje, Amanhã ou DD/MM/AAAA."
            ctx["data"] = value.isoformat()
        elif field == "hora_entrega":
            if not self._horario_valido(raw): return "Horário inválido. Envie no formato HH:MM, por exemplo 14:00 ou 09:30."
            ctx["horario_agendado"] = raw
            for item in ctx.get("itens") or []: item["horario_agendado"] = raw
        elif field == "tipo_residuo":
            residue = self._parse_residue(choice)
            if not residue: return self._residuo_prompt({"residuos": [], "quantidade": 1})
            ctx["residuos"] = [residue for _ in range(ctx.get("quantidade") or 1)]
            for item in ctx.get("itens") or []: item["residuo_contratado"] = residue
        elif field == "mao_de_obra":
            value = self._parse_mao_obra(choice)
            if value is None: return self._mao_obra_prompt()
            ctx["precisa_mao_de_obra"] = value
        elif field == "valor_total":
            try: ctx["valor"] = str(float(raw.replace(",", ".")))
            except ValueError: return "Valor inválido."
        elif field == "status_pagamento":
            if choice in {"1", "sim", "sim, ja esta pago"}:
                ctx.update({"pago": True, "editing_field": "forma_pagamento"})
                return AdvanceTransition("v24_cadastro_edicao_opcao", ctx, "")
            if choice in {"2", "nao", "nao, pendente"}: ctx.update({"pago": False, "forma": None})
            else: return "Selecione uma das opções de pagamento."
        elif field == "forma_pagamento":
            forma = self._parse_forma(choice)
            if not forma: return "Selecione uma forma de pagamento."
            ctx["forma"] = forma
        elif field == "endereco":
            if message.tipo == "location" and not coordinates: return "Não foi possível ler a localização. Reenvie a localização nativa ou digite o endereço."
            if not raw or len(raw) > 300: return "O endereço precisa ter entre 1 e 300 caracteres."
            ctx["endereco"] = raw
            if coordinates: ctx["endereco_latitude"], ctx["endereco_longitude"] = coordinates
        elif field == "ponto_referencia": ctx["referencia"] = None
        elif field == "ponto_referencia_texto":
            if not 1 <= len(raw) <= 50: return "O ponto de referência deve ter no máximo 50 caracteres."
            ctx["referencia"] = raw
        else: return None
        ctx.pop("editing_field", None); ctx.pop("_confirmado", None)
        return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")

    def decide_edicao_referencia_opcao(self, context, message):
        choice = self._normalize(message.texto); ctx = dict(context or {})
        if choice in {"1", "sim"}:
            ctx["editing_field"] = "ponto_referencia_texto"
            return AdvanceTransition("v24_cadastro_edicao_texto", ctx, "Qual é o novo ponto de referência?")
        if choice in {"2", "nao"}:
            ctx["referencia"] = None; ctx.pop("editing_field", None); ctx.pop("_confirmado", None)
            return AdvanceTransition("v24_cadastro_confirmacao", ctx, "")
        return "Selecione Sim ou Não."

    @staticmethod
    def _ajustar_itens_quantidade(ctx):
        itens = list(ctx.get("itens") or []); quantidade = ctx.get("quantidade") or len(itens)
        if not itens: return
        itens = itens[:quantidade]
        while len(itens) < quantidade: itens.append(dict(itens[-1]))
        ctx["itens"] = itens; ctx["residuos"] = [item.get("residuo_contratado") for item in itens]

    @staticmethod
    def _normalize(value):
        value = unicodedata.normalize("NFKD", value or "")
        return "".join(c for c in value if not unicodedata.combining(c)).strip().lower()
    @staticmethod
    def _phone(value):
        digits = re.sub(r"\D", "", value or ""); return digits if len(digits) >= 9 else None
    @staticmethod
    def _horario_valido(value):
        try: return datetime.strptime(value, "%H:%M").strftime("%H:%M") == value
        except ValueError: return False
    @staticmethod
    def _parse_residue(choice):
        return {"1":"Entulho Limpo","option_1":"Entulho Limpo","pedido_residuo_limpo":"Entulho Limpo","entulho limpo":"Entulho Limpo","limpo":"Entulho Limpo","2":"Entulho Misto","option_2":"Entulho Misto","pedido_residuo_misto":"Entulho Misto","entulho misto":"Entulho Misto","misto":"Entulho Misto"}.get(choice)
    @staticmethod
    def _parse_mao_obra(choice):
        if choice in {"1","option_1","pedido_mao_obra_sim","sim","✅ sim","sim, com pessoal","com pessoal"}: return True
        if choice in {"2","option_2","pedido_mao_obra_nao","nao","❌ nao","nao, apenas equipamento","apenas equipamento"}: return False
        return None
    @staticmethod
    def _parse_forma(choice):
        return {"1":"MBWay","mbway":"MBWay","2":"Transferência","transferencia":"Transferência","3":"Dinheiro","dinheiro":"Dinheiro","4":"Outro","outro":"Outro"}.get(choice)
    @staticmethod
    def _quantidade_prompt(): return "🔢 Quantas carrinhas são necessárias para este pedido?"
    @staticmethod
    def _data_prompt(): return "Quando está planejada a chegada?\n\n1. Hoje\n2. Amanhã\n3. Outra data"
    @staticmethod
    def _horario_prompt(): return "Qual o horário agendado da carrinha? Envie no formato HH:MM. Ex: 14:00"
    @staticmethod
    def _residuo_prompt(ctx):
        return f"Resíduo da carrinha {len(ctx.get('residuos') or []) + 1}/{ctx['quantidade']}:\n\n1. 🟢 Entulho Limpo\n2. 🟠 Entulho Misto"
    @staticmethod
    def _mao_obra_prompt(): return "O cliente solicitou pessoal para carregamento do resíduo?\n\n1. Sim, com pessoal\n2. Não, apenas equipamento"
    @staticmethod
    def _endereco_prompt(): return "Informe o endereço aproximado (até 300 caracteres) ou envie um link do Google Maps."
