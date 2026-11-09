"""The robot's local control loop.

This stands in for the real-time controller on a physical arm. It must keep
running no matter what happens to the network, which is why it knows nothing
about the agent or the cloud.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from fleet.protocol import UPDATE_STATUSES, StatusCode
from fleet.robot.state import RobotState


@dataclass(frozen=True)
class ControllerConfig:
    tick_s: float = 0.1  # control loop period
    run_drain_per_s: float = 1.0  # battery % lost per second while running
    idle_drain_per_s: float = 0.05
    charge_rate_per_s: float = 4.0
    low_battery_threshold: float = 15.0  # robot docks to charge below this
    start_job_rate_per_s: float = 0.3  # chance/sec an idle robot starts a job
    finish_job_rate_per_s: float = 0.05  # chance/sec a running robot finishes
    fault_rate_per_s: float = 0.01  # chance/sec a running robot faults
    fault_duration_s: float = 8.0


class Controller:
    def __init__(self, state: RobotState, rng: random.Random,
                 config: ControllerConfig = ControllerConfig()):
        self._state = state
        self._rng = rng
        self._config = config
        self._fault_remaining_s = 0.0

    def step(self, dt: float) -> None:
        """Advance the simulation by dt seconds. Pure logic, no sleeping."""
        snap = self._state.snapshot()
        battery, status = snap.battery_level, snap.status_code

        if snap.update_phase is not None:
            # Mid firmware update: the arm is out of service. Takes precedence over pause.
            # (Simplification: battery is frozen, like when paused.)
            self._state.update(battery, snap.update_phase)
            return
        if snap.paused:
            # Frozen: no battery drain/charge, no job transitions, no faults.
            self._state.update(battery, StatusCode.PAUSED)
            return
        if status == StatusCode.PAUSED or status in UPDATE_STATUSES:
            status = StatusCode.IDLE  # resumed / update finished: come back as idle and carry on

        if status == StatusCode.ERROR:
            battery, status = self._step_error(battery, dt)
        elif status == StatusCode.CHARGING:
            battery, status = self._step_charging(battery, dt)
        elif status == StatusCode.RUNNING:
            battery, status = self._step_running(battery, dt)
        else:
            battery, status = self._step_idle(battery, dt)

        self._state.update(min(100.0, max(0.0, battery)), status)

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self._config.tick_s):
            self.step(self._config.tick_s)

    # --- per-status transitions -------------------------------------------

    def _step_idle(self, battery: float, dt: float) -> tuple[float, StatusCode]:
        battery -= self._config.idle_drain_per_s * dt
        if battery <= self._config.low_battery_threshold:
            return battery, StatusCode.CHARGING
        if self._happens(self._config.start_job_rate_per_s, dt):
            return battery, StatusCode.RUNNING
        return battery, StatusCode.IDLE

    def _step_running(self, battery: float, dt: float) -> tuple[float, StatusCode]:
        battery -= self._config.run_drain_per_s * dt
        if self._happens(self._config.fault_rate_per_s, dt):
            self._fault_remaining_s = self._config.fault_duration_s
            return battery, StatusCode.ERROR
        if battery <= self._config.low_battery_threshold:
            return battery, StatusCode.CHARGING
        if self._happens(self._config.finish_job_rate_per_s, dt):
            return battery, StatusCode.IDLE
        return battery, StatusCode.RUNNING

    def _step_charging(self, battery: float, dt: float) -> tuple[float, StatusCode]:
        battery += self._config.charge_rate_per_s * dt
        if battery >= 100.0:
            return 100.0, StatusCode.IDLE
        return battery, StatusCode.CHARGING

    def _step_error(self, battery: float, dt: float) -> tuple[float, StatusCode]:
        self._fault_remaining_s -= dt
        if self._fault_remaining_s <= 0:
            return battery, StatusCode.IDLE
        return battery, StatusCode.ERROR

    def _happens(self, rate_per_s: float, dt: float) -> bool:
        return self._rng.random() < rate_per_s * dt
