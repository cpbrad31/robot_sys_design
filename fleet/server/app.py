"""Application logic for the server, independent of HTTP."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from fleet.protocol import (UPDATE_STATUSES, Command, CommandType, Heartbeat, StatusCode,
                            command_message)
from fleet.server.connections import ConnectionRegistry, RobotNotConnected
from fleet.server.firmware import Artifact, FirmwareCatalog
from fleet.server.heartbeat_log import HeartbeatLog
from fleet.server.store import HeartbeatStore, StoredHeartbeat
from fleet.server.views import fleet_view, log_entry_view

logger = logging.getLogger("fleet.server.heartbeats")

MAX_BATCH_SIZE = 500


@dataclass(frozen=True)
class BatchResult:
    accepted: int = 0    # newly stored
    duplicates: int = 0  # already had it (e.g. a retry after a lost response)
    rejected: int = 0    # failed validation

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class UpdateConflict(Exception):
    """The robot is in a state where a firmware update makes no sense right now."""


class ServerApp:
    def __init__(self, store: HeartbeatStore, log: HeartbeatLog, registry: ConnectionRegistry,
                 catalog: FirmwareCatalog, offline_after_s: float = 6.0,
                 clock: Callable[[], float] = time.time):
        self._store = store
        self._log = log
        self._registry = registry
        self._catalog = catalog
        self._offline_after_s = offline_after_s
        self._clock = clock

    def ingest(self, payload: Any) -> bool:
        """Validate, store and log one heartbeat. Raises ValueError if invalid.
        Returns False if it was a duplicate."""
        heartbeat = Heartbeat.from_dict(payload)
        return self._accept(heartbeat, received_at=self._clock())

    def ingest_batch(self, payload: Any) -> BatchResult:
        """Accept a burst of heartbeats, e.g. a robot flushing telemetry it
        buffered while offline. Each item is handled independently so one bad
        item can't block the rest (and a robot never gets stuck retrying a
        batch that can never succeed). Raises ValueError only if the envelope
        itself is malformed."""
        if not isinstance(payload, dict) or not isinstance(payload.get("heartbeats"), list):
            raise ValueError('body must be {"heartbeats": [...]}')
        items = payload["heartbeats"]
        if len(items) > MAX_BATCH_SIZE:
            raise ValueError(f"batch too large (max {MAX_BATCH_SIZE})")

        received_at = self._clock()  # the whole burst arrived together
        accepted = duplicates = rejected = 0
        for item in items:
            try:
                heartbeat = Heartbeat.from_dict(item)
            except ValueError as exc:
                rejected += 1
                logger.warning("rejected heartbeat in batch: %s", exc)
                continue
            if self._accept(heartbeat, received_at):
                accepted += 1
            else:
                duplicates += 1

        if len(items) > 1:
            logger.info("batch received: accepted=%d duplicates=%d rejected=%d",
                        accepted, duplicates, rejected)
        return BatchResult(accepted, duplicates, rejected)

    def send_command(self, robot_id: str, command: Any) -> None:
        """Push a command to a robot. Raises ValueError for an unknown command,
        RobotNotConnected if the robot has no open websocket."""
        try:
            command_type = CommandType(command)
        except ValueError:
            raise ValueError(f"unknown command: {command!r}") from None
        if command_type == CommandType.UPDATE_FIRMWARE:
            raise ValueError("firmware updates go through POST /api/robots/<id>/firmware")
        self._registry.send(robot_id, command_message(Command(command_type)))
        logger.info("command sent robot=%s command=%s", robot_id, command_type.value)

    def start_firmware_update(self, robot_id: str, version: Any) -> None:
        """Tell a robot to update itself to `version`.

        Raises ValueError for an unknown version, RobotNotConnected if we can't
        reach the robot, UpdateConflict if it's already updating or already on
        that version. (The robot enforces the same rules itself; checking here
        gives the operator a clear error instead of a silently ignored command.)
        """
        artifact = self._catalog.get(version) if isinstance(version, str) else None
        if artifact is None:
            raise ValueError(f"unknown firmware version: {version!r}")
        if robot_id not in self._registry.connected_ids():
            raise RobotNotConnected(robot_id)
        self._check_can_update(robot_id, artifact.version)

        command = Command(CommandType.UPDATE_FIRMWARE, artifact.version, artifact.sha256)
        self._registry.send(robot_id, command_message(command))
        logger.info("firmware update sent robot=%s version=%s", robot_id, artifact.version)

    def firmware_info(self) -> dict:
        return {"latest": self._catalog.latest(), "versions": self._catalog.versions()}

    def firmware_artifact(self, version: str) -> Artifact | None:
        return self._catalog.get(version)

    def _check_can_update(self, robot_id: str, version: str) -> None:
        stored = self._store.latest(robot_id)
        if stored is None:
            return  # connected but no telemetry yet; let the robot decide
        heartbeat = stored.heartbeat
        if StatusCode(heartbeat.status_code) in UPDATE_STATUSES:
            raise UpdateConflict(f"{robot_id} is already updating")
        if heartbeat.firmware_version == version:
            raise UpdateConflict(f"{robot_id} is already running {version}")

    def fleet(self) -> list[dict]:
        return fleet_view(self._store.latest_per_robot(), self._clock(),
                          self._offline_after_s, self._registry.connected_ids())

    def history(self, robot_id: str, limit: int) -> list[dict]:
        return [log_entry_view(s, self._offline_after_s)
                for s in self._store.history(robot_id, limit)]

    def _accept(self, heartbeat: Heartbeat, received_at: float) -> bool:
        """Store + log a validated heartbeat. Returns False if it was a duplicate."""
        if not self._store.record(heartbeat, received_at):
            return False
        self._log.append(StoredHeartbeat(heartbeat, received_at))
        logger.info(
            "heartbeat robot=%s seq=%d fw=%s battery=%.1f%% status=%s delay=%.1fs",
            heartbeat.robot_id, heartbeat.seq, heartbeat.firmware_version,
            heartbeat.battery_level, StatusCode(heartbeat.status_code).name,
            max(0.0, received_at - heartbeat.sent_at),
        )
        return True
