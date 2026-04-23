"""Audit log — records each command execution as a JSONL entry"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class AuditLogger:
    def __init__(self, log_path: str):
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        entry = {**entry, "ts": time.time()}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
