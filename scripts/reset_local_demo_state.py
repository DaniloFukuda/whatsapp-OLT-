import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import SessionLocal
from app.services.demo_reset_service import DemoResetService


def main() -> None:
    with SessionLocal() as db:
        result = DemoResetService(db).reset()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
