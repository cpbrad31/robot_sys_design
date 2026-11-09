"""Simulated network link for one robot, used to inject outages.

The robot's real transports are wrapped so that while the link is "down" every
send fails and the websocket can't be (re)established, exactly as if the
robot's uplink had been cut. The robot itself keeps running.
"""

from __future__ import annotations

import threading
from typing import Any

from fleet.robot.agent import Sender
from fleet.robot.command_channel import Connect
from fleet.robot.firmware_updater import Fetch


class SimulatedLink:
    def __init__(self):
        self._down = threading.Event()

    def set_down(self, down: bool) -> None:
        if down:
            self._down.set()
        else:
            self._down.clear()

    def is_down(self) -> bool:
        return self._down.is_set()

    def check(self) -> None:
        if self.is_down():
            raise ConnectionError("network link is down")

    def wrap_sender(self, sender: Sender) -> Sender:
        def guarded(url: str, payload: dict) -> None:
            self.check()
            sender(url, payload)
        return guarded

    def wrap_fetch(self, fetch: Fetch) -> Fetch:
        def guarded(url: str) -> bytes:
            self.check()
            return fetch(url)
        return guarded

    def wrap_connect(self, connect: Connect) -> Connect:
        def guarded(url: str) -> "_GuardedConnection":
            self.check()
            return _GuardedConnection(self, connect(url))
        return guarded


class _GuardedConnection:
    """Wraps a websocket connection so an outage severs it mid-session."""

    def __init__(self, link: SimulatedLink, inner: Any):
        self._link = link
        self._inner = inner

    def __enter__(self) -> "_GuardedConnection":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info) -> Any:
        return self._inner.__exit__(*exc_info)  # closes the real socket

    def send(self, message: str) -> None:
        self._link.check()
        self._inner.send(message)

    def recv(self, timeout: float | None = None) -> Any:
        self._link.check()
        return self._inner.recv(timeout=timeout)
