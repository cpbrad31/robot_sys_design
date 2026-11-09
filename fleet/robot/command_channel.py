"""Robot side of the websocket: receives commands pushed by the server."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from fleet.protocol import Command, CommandType, hello_message, parse_command_message
from fleet.robot.firmware_updater import FirmwareUpdater
from fleet.robot.state import RobotState

logger = logging.getLogger(__name__)

# url -> context manager yielding a connection with send()/recv(timeout=)
Connect = Callable[[str], Any]

RECV_TIMEOUT_S = 0.5  # how often we wake up to check for a stop request


class CommandChannel:
    def __init__(self, robot_id: str, state: RobotState, ws_url: str,
                 connect: Connect, updater: FirmwareUpdater, reconnect_delay_s: float = 2.0):
        self._robot_id = robot_id
        self._state = state
        self._ws_url = ws_url
        self._connect = connect
        self._updater = updater
        self._reconnect_delay_s = reconnect_delay_s
        self._reported_down = False

    def handle_message(self, raw: str | bytes) -> None:
        """Parse one server message and apply it. Bad messages are logged and ignored."""
        try:
            command = parse_command_message(json.loads(raw))
        except ValueError as exc:  # includes JSONDecodeError
            logger.warning("%s: ignoring bad message: %s", self._robot_id, exc)
            return
        logger.info("%s: received command %s", self._robot_id, command.type.value)
        self._apply(command)

    def _apply(self, command: Command) -> None:
        if command.type == CommandType.PAUSE:
            self._state.set_paused(True)
        elif command.type == CommandType.RESUME:
            self._state.set_paused(False)
        elif command.type == CommandType.UPDATE_FIRMWARE:
            # Returns immediately; the update runs on its own thread so this
            # channel keeps receiving messages while the arm is updating.
            self._updater.request_update(command.version, command.sha256)

    def run(self, stop: threading.Event) -> None:
        """Stay connected until stopped, reconnecting whenever the link drops."""
        while not stop.is_set():
            try:
                self._serve_one_connection(stop)
            except Exception as exc:  # noqa: BLE001 - channel must survive anything
                if not self._reported_down:  # log the outage once, not on every retry
                    self._reported_down = True
                    logger.warning("%s: command channel down: %s", self._robot_id, exc)
            stop.wait(self._reconnect_delay_s)

    def _serve_one_connection(self, stop: threading.Event) -> None:
        with self._connect(self._ws_url) as ws:
            ws.send(json.dumps(hello_message(self._robot_id)))
            self._reported_down = False
            logger.info("%s: command channel connected", self._robot_id)
            while not stop.is_set():
                try:
                    message = ws.recv(timeout=RECV_TIMEOUT_S)
                except TimeoutError:
                    continue
                self.handle_message(message)
