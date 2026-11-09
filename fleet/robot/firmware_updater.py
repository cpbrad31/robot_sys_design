"""Simulated over-the-air firmware update with A/B slots and trial boot.

Sequence (each step is visible as a status code in the robot's telemetry):

    DOWNLOADING   fetch the artifact, verify its checksum, write it to the inactive slot
    REBOOTING     restart into the new slot
    TRIAL_BOOT    run the new firmware on probation, then decide:
        pass  -> commit: the new slot stays active                      (result: committed)
        fail  -> ROLLING_BACK: reboot into the previous slot            (result: rolled_back)

A bad download never reaches the slots at all (result: download_failed).
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
from dataclasses import dataclass
from typing import Callable

from fleet.protocol import StatusCode, UpdateResult
from fleet.robot.firmware_slots import FirmwareSlots
from fleet.robot.state import RobotState

logger = logging.getLogger(__name__)

# url -> artifact bytes; raises on failure
Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class UpdateConfig:
    download_s: float = 6.0
    reboot_s: float = 4.0
    trial_s: float = 8.0
    rollback_s: float = 4.0
    success_probability: float = 0.5  # chance the trial boot passes


class _Shutdown(Exception):
    """The robot is stopping; abandon the update."""


class FirmwareUpdater:
    def __init__(self, robot_id: str, state: RobotState, slots: FirmwareSlots, fetch: Fetch,
                 artifact_base_url: str, rng: random.Random, stop: threading.Event,
                 config: UpdateConfig = UpdateConfig()):
        self._robot_id = robot_id
        self._state = state
        self._slots = slots
        self._fetch = fetch
        self._artifact_base_url = artifact_base_url
        self._rng = rng
        self._stop = stop
        self._config = config
        self._busy = threading.Lock()  # held for the whole duration of an update

    def request_update(self, version: str, sha256: str) -> bool:
        """Start an update in the background. Returns False (and does nothing) if one
        is already running or the robot is already on `version`. Called from the
        command channel thread, so it must return quickly."""
        if not self._busy.acquire(blocking=False):
            logger.warning("%s: ignoring update to %s: an update is already in progress",
                           self._robot_id, version)
            return False
        if version == self._slots.active_version:
            self._busy.release()
            logger.warning("%s: ignoring update: already running %s", self._robot_id, version)
            return False
        threading.Thread(target=self._run_in_background, args=(version, sha256),
                         name=f"{self._robot_id}-firmware", daemon=True).start()
        return True

    def run_update(self, version: str, sha256: str) -> UpdateResult | None:
        """Run the whole update synchronously. Returns the outcome, or None if the
        robot shut down partway through."""
        logger.info("%s: firmware update to %s starting (active slot %s, running %s)",
                    self._robot_id, version, self._slots.active_slot, self._slots.active_version)
        try:
            return self._run(version, sha256)
        except _Shutdown:
            return None

    # --- the sequence -----------------------------------------------------

    def _run(self, version: str, sha256: str) -> UpdateResult:
        self._state.set_update_phase(StatusCode.DOWNLOADING)
        if not self._fetch_and_verify(version, sha256):
            return self._finish(UpdateResult.DOWNLOAD_FAILED)
        self._wait(self._config.download_s)  # time to write the image to flash
        self._slots.stage(version)
        self._publish()

        self._enter(StatusCode.REBOOTING, self._config.reboot_s)
        self._slots.boot_into_staged()
        self._publish()

        self._enter(StatusCode.TRIAL_BOOT, self._config.trial_s)
        if self._trial_passes():
            self._slots.commit()
            self._publish()
            return self._finish(UpdateResult.COMMITTED)

        self._enter(StatusCode.ROLLING_BACK, self._config.rollback_s)
        self._slots.rollback()
        self._publish()
        return self._finish(UpdateResult.ROLLED_BACK)

    # --- steps ------------------------------------------------------------

    def _fetch_and_verify(self, version: str, sha256: str) -> bool:
        url = f"{self._artifact_base_url}/{version}"
        try:
            payload = self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - any fetch failure just fails the update
            logger.warning("%s: could not fetch %s: %s", self._robot_id, url, exc)
            return False
        if hashlib.sha256(payload).hexdigest() != sha256:
            logger.warning("%s: checksum mismatch for firmware %s; discarding",
                           self._robot_id, version)
            return False
        return True

    def _trial_passes(self) -> bool:
        return self._rng.random() < self._config.success_probability

    def _enter(self, phase: StatusCode, duration_s: float) -> None:
        """Report `phase`, then stay in it for `duration_s`."""
        self._state.set_update_phase(phase)
        self._wait(duration_s)

    def _wait(self, duration_s: float) -> None:
        if self._stop.wait(duration_s):
            raise _Shutdown

    def _publish(self) -> None:
        self._state.set_firmware(self._slots.active_version, self._slots.active_slot,
                                 self._slots.inactive_version)

    def _finish(self, result: UpdateResult) -> UpdateResult:
        self._state.set_last_update_result(result)
        self._state.set_update_phase(None)  # controller resumes normal operation
        logger.info("%s: firmware update finished: %s (now running %s on slot %s)",
                    self._robot_id, result.value, self._slots.active_version,
                    self._slots.active_slot)
        return result

    def _run_in_background(self, version: str, sha256: str) -> None:
        try:
            self.run_update(version, sha256)
        except Exception:  # noqa: BLE001 - never leave the robot stuck "updating"
            logger.exception("%s: firmware update crashed", self._robot_id)
        finally:
            self._state.set_update_phase(None)
            self._busy.release()
