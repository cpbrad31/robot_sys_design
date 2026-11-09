"""Takes one robot at a time off the network, then brings it back."""

from __future__ import annotations

import logging
import random
import threading
from typing import Protocol, Sequence

logger = logging.getLogger(__name__)


class Outageable(Protocol):
    robot_id: str

    def set_network_down(self, down: bool) -> None: ...


class OutageScheduler:
    """Cycle: run normally for `up_for_s`, cut one random robot off for
    `down_for_s`, restore it, repeat. Because the loop is sequential, only one
    robot is ever down at a time."""

    def __init__(self, robots: Sequence[Outageable], up_for_s: float, down_for_s: float,
                 rng: random.Random):
        self._robots = robots
        self._up_for_s = up_for_s
        self._down_for_s = down_for_s
        self._rng = rng

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self._up_for_s):
            self._run_outage(self._rng.choice(self._robots), stop)

    def _run_outage(self, robot: Outageable, stop: threading.Event) -> None:
        logger.warning("OUTAGE: %s lost connectivity for %.0fs", robot.robot_id, self._down_for_s)
        robot.set_network_down(True)
        try:
            stop.wait(self._down_for_s)
        finally:
            robot.set_network_down(False)  # always restore, even on shutdown
            logger.warning("OUTAGE OVER: %s is back online", robot.robot_id)
