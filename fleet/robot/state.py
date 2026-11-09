"""Thread-safe state shared between a robot's controller, cloud agent, command channel
and firmware updater."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from fleet.protocol import StatusCode, UpdateResult


@dataclass(frozen=True)
class RobotSnapshot:
    firmware_version: str  # the version currently running
    battery_level: float
    status_code: StatusCode
    paused: bool
    active_slot: str
    inactive_slot_version: str | None
    update_phase: StatusCode | None  # set while a firmware update is in progress
    last_update_result: UpdateResult


class RobotState:
    """Who writes what:
      - controller: battery_level and status_code (via update)
      - command channel: paused (via set_paused), a *request* the controller honours
      - firmware updater: update_phase (a request the controller honours), plus the
        firmware fields and last_update_result
      - agent: read-only snapshots

    Keeping the controller as the only writer of status_code avoids races
    between a command arriving and the control loop's own transitions.

    The lock guarantees readers never see a half-updated state (e.g. new
    status paired with old battery level, or a new version with the old slot).
    """

    def __init__(self, firmware_version: str, battery_level: float = 100.0,
                 status_code: StatusCode = StatusCode.IDLE):
        self._lock = threading.Lock()
        self._firmware_version = firmware_version
        self._battery_level = battery_level
        self._status_code = status_code
        self._paused = False
        self._active_slot = "A"
        self._inactive_slot_version: str | None = None
        self._update_phase: StatusCode | None = None
        self._last_update_result = UpdateResult.NONE

    def snapshot(self) -> RobotSnapshot:
        with self._lock:
            return RobotSnapshot(
                self._firmware_version, self._battery_level, self._status_code, self._paused,
                self._active_slot, self._inactive_slot_version, self._update_phase,
                self._last_update_result,
            )

    def update(self, battery_level: float, status_code: StatusCode) -> None:
        with self._lock:
            self._battery_level = battery_level
            self._status_code = status_code

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused

    def set_update_phase(self, phase: StatusCode | None) -> None:
        with self._lock:
            self._update_phase = phase

    def set_firmware(self, running_version: str, active_slot: str,
                     inactive_slot_version: str | None) -> None:
        """Publish the slot layout. One call so readers can't see a half-switched state."""
        with self._lock:
            self._firmware_version = running_version
            self._active_slot = active_slot
            self._inactive_slot_version = inactive_slot_version

    def set_last_update_result(self, result: UpdateResult) -> None:
        with self._lock:
            self._last_update_result = result
