import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import SessionLocal
from app.models.aluguer import AluguerContentor


def serialize(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    with SessionLocal() as db:
        aluguer = db.query(AluguerContentor).order_by(AluguerContentor.id.desc()).first()
        if not aluguer:
            print(json.dumps({"found": False}, ensure_ascii=False))
            return

        summary = {
            "found": True,
            "aluguer_id": aluguer.id,
            "contentor": aluguer.contentor.codigo,
            "status_contentor": aluguer.contentor.status.value,
            "nome_cliente": aluguer.nome_cliente,
            "telefone_cliente": aluguer.telefone_cliente,
            "valor": serialize(aluguer.valor),
            "pago": aluguer.pago,
            "forma_pagamento": aluguer.forma_pagamento,
            "data_entrega": serialize(aluguer.data_entrega),
            "data_vencimento": serialize(aluguer.data_vencimento),
            "status_aluguer": aluguer.status.value,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
