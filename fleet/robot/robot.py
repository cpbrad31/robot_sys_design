"""A simulated robot: controller, cloud agent (buffered HTTP telemetry), command channel
(websocket) and firmware updater (A/B slots)."""

from __future__ import annotations

import random
import threading

from fleet.robot.agent import Agent, Sender
from fleet.robot.command_channel import CommandChannel, Connect
from fleet.robot.controller import Controller, ControllerConfig
from fleet.robot.firmware_slots import FirmwareSlots
from fleet.robot.firmware_updater import Fetch, FirmwareUpdater, UpdateConfig
from fleet.robot.network import SimulatedLink
from fleet.robot.state import RobotState


class Robot:
    def __init__(self, robot_id: str, firmware_version: str, heartbeat_url: str,
                 heartbeat_interval_s: float, sender: Sender, ws_url: str, connect: Connect,
                 firmware_url: str, fetch: Fetch, seed: int | None = None,
                 controller_config: ControllerConfig = ControllerConfig(),
                 update_config: UpdateConfig = UpdateConfig()):
        self.robot_id = robot_id
        rng = random.Random(seed)
        # Separate RNGs so the threads never share a (non-thread-safe) generator.
        controller_rng = random.Random(rng.random())
        agent_rng = random.Random(rng.random())
        updater_rng = random.Random(rng.random())

        # All network traffic goes through the link so outages can be injected.
        self._link = SimulatedLink()
        self._stop = threading.Event()

        self._state = RobotState(firmware_version, battery_level=rng.uniform(40, 100))
        self._controller = Controller(self._state, controller_rng, controller_config)
        self._agent = Agent(robot_id, self._state, heartbeat_url, heartbeat_interval_s,
                            self._link.wrap_sender(sender), agent_rng)
        self._updater = FirmwareUpdater(
            robot_id, self._state, FirmwareSlots(firmware_version),
            self._link.wrap_fetch(fetch), firmware_url, updater_rng, self._stop, update_config)
        self._commands = CommandChannel(robot_id, self._state, ws_url,
                                        self._link.wrap_connect(connect), self._updater)
        self._threads = [
            threading.Thread(target=self._controller.run, args=(self._stop,),
                             name=f"{robot_id}-controller", daemon=True),
            threading.Thread(target=self._agent.run, args=(self._stop,),
                             name=f"{robot_id}-agent", daemon=True),
            threading.Thread(target=self._commands.run, args=(self._stop,),
                             name=f"{robot_id}-commands", daemon=True),
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def set_network_down(self, down: bool) -> None:
        """Simulate losing (or regaining) connectivity. The robot keeps running."""
        self._link.set_down(down)

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout_s)
