"""Bounded, in-order queue of telemetry waiting to be delivered to the server."""

from __future__ import annotations

from collections import deque

from fleet.protocol import Heartbeat


class TelemetryBuffer:
    """FIFO with a capacity. When full, the *oldest* entry is dropped to make
    room: a robot offline for a long time keeps the most recent history rather
    than growing without bound.

    Not thread-safe: it is owned by the agent thread and nothing else touches it.
    (A real robot would persist this to disk so a reboot doesn't lose it.)
    """

    def __init__(self, capacity: int = 1000):
        self._items: deque[Heartbeat] = deque()
        self._capacity = capacity

    def add(self, heartbeat: Heartbeat) -> bool:
        """Append. Returns True if an older entry had to be dropped."""
        dropped = len(self._items) >= self._capacity
        if dropped:
            self._items.popleft()
        self._items.append(heartbeat)
        return dropped

    def peek(self, count: int) -> list[Heartbeat]:
        """The oldest `count` entries, left in place until discard() confirms delivery."""
        return [self._items[i] for i in range(min(count, len(self._items)))]

    def discard(self, count: int) -> None:
        """Remove the oldest `count` entries (call after they were delivered)."""
        for _ in range(min(count, len(self._items))):
            self._items.popleft()

    def __len__(self) -> int:
        return len(self._items)
