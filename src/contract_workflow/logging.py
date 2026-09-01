from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventLogger:
    def __init__(self, path: Path):
        self.path = path

    def emit(self, event: str, **data: Any) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
