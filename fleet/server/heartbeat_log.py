"""Durable, append-only record of every heartbeat received (one JSON per line)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fleet.server.store import StoredHeartbeat


class HeartbeatLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def append(self, stored: StoredHeartbeat) -> None:
        line = json.dumps({**stored.heartbeat.to_dict(), "received_at": stored.received_at})
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()
