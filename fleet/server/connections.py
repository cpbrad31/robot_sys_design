"""Tracks which robots currently have an open websocket, and sends them messages."""

from __future__ import annotations

import json
import threading
from typing import Any


class RobotNotConnected(Exception):
    pass


class ConnectionRegistry:
    """Maps robot_id -> open connection. Written by websocket handler threads,
    read by HTTP handler threads, so every access goes through the lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._connections: dict[str, Any] = {}

    def register(self, robot_id: str, connection: Any) -> None:
        """A robot reconnecting replaces (and closes) its stale connection."""
        with self._lock:
            previous = self._connections.get(robot_id)
            self._connections[robot_id] = connection
        if previous is not None and previous is not connection:
            _close_quietly(previous)

    def unregister(self, robot_id: str, connection: Any) -> None:
        """Only removes the entry if it's still this connection, so a late
        cleanup of an old socket can't evict the robot's newer one."""
        with self._lock:
            if self._connections.get(robot_id) is connection:
                del self._connections[robot_id]

    def connected_ids(self) -> set[str]:
        with self._lock:
            return set(self._connections)

    def send(self, robot_id: str, message: dict) -> None:
        """Raises RobotNotConnected if the robot has no live connection."""
        with self._lock:
            connection = self._connections.get(robot_id)
        if connection is None:
            raise RobotNotConnected(robot_id)
        try:
            connection.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001 - any send failure means the link is dead
            self.unregister(robot_id, connection)
            raise RobotNotConnected(robot_id) from exc


def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass
