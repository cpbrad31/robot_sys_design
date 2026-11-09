"""In-memory heartbeat storage."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from fleet.protocol import Heartbeat


@dataclass(frozen=True)
class StoredHeartbeat:
    heartbeat: Heartbeat
    received_at: float  # server-side wall clock; the authority for "last seen"


def _identity(heartbeat: Heartbeat) -> tuple[int, float]:
    """(seq, sent_at) identifies a heartbeat within one robot. A retry resends
    both unchanged; a robot restart resets seq but not sent_at."""
    return heartbeat.seq, heartbeat.sent_at


class HeartbeatStore:
    """Latest heartbeat per robot plus a bounded history. Thread-safe, since
    the HTTP server handles each request on its own thread.

    record() is idempotent: a robot retrying a batch whose response was lost
    must not create duplicates. (Duplicates are only detected while the original
    is still within the bounded history.)"""

    def __init__(self, history_per_robot: int = 200):
        self._lock = threading.Lock()
        self._history_per_robot = history_per_robot
        self._history: dict[str, deque[StoredHeartbeat]] = {}
        self._seen: dict[str, set[tuple[int, float]]] = {}

    def record(self, heartbeat: Heartbeat, received_at: float) -> bool:
        """Store a heartbeat. Returns False (and stores nothing) if it's a duplicate."""
        with self._lock:
            history = self._history.setdefault(
                heartbeat.robot_id, deque(maxlen=self._history_per_robot))
            seen = self._seen.setdefault(heartbeat.robot_id, set())

            identity = _identity(heartbeat)
            if identity in seen:
                return False
            if len(history) == history.maxlen:
                seen.discard(_identity(history[0].heartbeat))  # about to be evicted
            history.append(StoredHeartbeat(heartbeat, received_at))
            seen.add(identity)
            return True

    def latest(self, robot_id: str) -> StoredHeartbeat | None:
        with self._lock:
            history = self._history.get(robot_id)
            return history[-1] if history else None

    def latest_per_robot(self) -> list[StoredHeartbeat]:
        with self._lock:
            return [history[-1] for _, history in sorted(self._history.items())]

    def history(self, robot_id: str, limit: int = 50) -> list[StoredHeartbeat]:
        with self._lock:
            return list(self._history.get(robot_id, []))[-limit:]
