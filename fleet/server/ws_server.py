"""Websocket endpoint robots connect to for receiving commands."""

from __future__ import annotations

import json
import logging
import threading

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, serve

from fleet.protocol import parse_hello
from fleet.server.connections import ConnectionRegistry

logger = logging.getLogger("fleet.server.ws")

HELLO_TIMEOUT_S = 5.0


def make_ws_handler(registry: ConnectionRegistry):
    def handler(connection) -> None:
        robot_id = None
        try:
            robot_id = parse_hello(json.loads(connection.recv(timeout=HELLO_TIMEOUT_S)))
            registry.register(robot_id, connection)
            logger.info("robot connected: %s", robot_id)
            for _ in connection:
                pass  # robot->server messages aren't used yet; just keep the socket open
        except (ValueError, TimeoutError) as exc:
            logger.warning("rejecting websocket client: %s", exc)
        except ConnectionClosed:
            pass
        finally:
            if robot_id is not None:
                registry.unregister(robot_id, connection)
                logger.info("robot disconnected: %s", robot_id)

    return handler


def start_ws_server(registry: ConnectionRegistry, host: str, port: int) -> Server:
    """Starts serving on a background thread; call .shutdown() to stop."""
    server = serve(make_ws_handler(registry), host, port)
    threading.Thread(target=server.serve_forever, name="ws-server", daemon=True).start()
    return server
