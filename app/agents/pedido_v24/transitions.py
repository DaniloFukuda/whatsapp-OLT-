"""Decisões puras de transição conversacional do fluxo Pedido V2.4."""

from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True)
class AdvanceTransition:
    next_state: str
    context: dict[str, Any]
    response: str


@dataclass(frozen=True)
class IdleTransition:
    response: str


OperationalTransition: TypeAlias = AdvanceTransition | IdleTransition
