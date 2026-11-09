"""HTTP transport used by the agent. Kept separate so tests can swap it out."""

from __future__ import annotations

import json
import urllib.request

from websockets.sync.client import connect as _ws_connect


def open_command_connection(url: str):
    """Open a websocket to the server. Use as a context manager."""
    return _ws_connect(url, open_timeout=3)


def post_json(url: str, payload: dict, timeout_s: float = 2.0) -> None:
    """POST a JSON payload. Raises on network error or non-2xx response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s):
        pass


def get_bytes(url: str, timeout_s: float = 5.0) -> bytes:
    """GET a binary resource (e.g. a firmware artifact). Raises on error or non-2xx."""
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return response.read()
