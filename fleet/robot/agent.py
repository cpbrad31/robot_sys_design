"""The robot's cloud agent: samples telemetry and delivers it to the server.

Sampling and delivery are deliberately separate. Every interval the agent
*always* records a heartbeat into a local buffer, then tries to deliver
whatever is buffered. If the server is unreachable nothing is lost: the
buffer keeps growing and the backlog is delivered, oldest first, with its
original timestamps, as soon as the connection returns.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

from fleet.protocol import Heartbeat
from fleet.robot.state import RobotState
from fleet.robot.telemetry_buffer import TelemetryBuffer

logger = logging.getLogger(__name__)

# (url, payload) -> None; raises on failure
Sender = Callable[[str, dict], None]

BATCH_SIZE = 50  # max heartbeats per request, so a long backlog doesn't become one huge POST


class Agent:
    def __init__(self, robot_id: str, state: RobotState, heartbeat_url: str,
                 interval_s: float, sender: Sender, rng: random.Random,
                 buffer: TelemetryBuffer | None = None):
        self._robot_id = robot_id
        self._state = state
        self._heartbeat_url = heartbeat_url
        self._interval_s = interval_s
        self._sender = sender
        self._rng = rng
        self._buffer = buffer if buffer is not None else TelemetryBuffer()
        self._seq = 0
        self._delivery_failing = False  # so we log outage start/end once, not every interval

    def build_heartbeat(self) -> Heartbeat:
        snap = self._state.snapshot()
        self._seq += 1
        return Heartbeat(
            robot_id=self._robot_id,
            seq=self._seq,
            firmware_version=snap.firmware_version,
            battery_level=round(snap.battery_level, 1),
            status_code=int(snap.status_code),
            sent_at=time.time(),
            active_slot=snap.active_slot,
            inactive_slot_version=snap.inactive_slot_version,
            last_update_result=snap.last_update_result.value,
        )

    def tick(self) -> bool:
        """One cycle: record a heartbeat, then try to deliver the backlog.
        Returns True if the buffer was fully delivered."""
        self._record()
        return self._flush()

    def run(self, stop: threading.Event) -> None:
        # Random initial offset so the whole fleet doesn't beat in lockstep.
        if stop.wait(self._rng.uniform(0, self._interval_s)):
            return
        while True:
            self.tick()
            if stop.wait(self._interval_s):
                return

    # --- internals --------------------------------------------------------

    def _record(self) -> None:
        if self._buffer.add(self.build_heartbeat()):
            logger.warning("%s: buffer full, dropped oldest heartbeat", self._robot_id)

    def _flush(self) -> bool:
        """Send buffered heartbeats in order. A batch only leaves the buffer once
        the server has accepted it, so a failure never loses data. Failures are
        swallowed: a flaky network must never take the agent down."""
        delivered = 0
        while len(self._buffer):
            batch = self._buffer.peek(BATCH_SIZE)
            try:
                self._sender(self._heartbeat_url, {"heartbeats": [h.to_dict() for h in batch]})
            except Exception as exc:  # noqa: BLE001 - agent must survive anything
                self._note_failure(exc)
                return False
            self._buffer.discard(len(batch))
            delivered += len(batch)
        self._note_success(delivered)
        return True

    def _note_failure(self, exc: Exception) -> None:
        if not self._delivery_failing:
            self._delivery_failing = True
            logger.warning("%s: server unreachable (%s); buffering telemetry",
                           self._robot_id, exc)

    def _note_success(self, delivered: int) -> None:
        if self._delivery_failing:
            self._delivery_failing = False
            logger.info("%s: connection restored; delivered %d buffered heartbeats",
                        self._robot_id, delivered)
