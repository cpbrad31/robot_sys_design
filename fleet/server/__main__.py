"""Run the server.  Usage: python -m fleet.server"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fleet.server.app import ServerApp
from fleet.server.connections import ConnectionRegistry
from fleet.server.firmware import default_catalog
from fleet.server.heartbeat_log import HeartbeatLog
from fleet.server.http_server import create_server
from fleet.server.store import HeartbeatStore
from fleet.server.ws_server import start_ws_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Fleet telemetry server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (dashboard + heartbeats)")
    parser.add_argument("--ws-port", type=int, default=8001, help="websocket port (commands)")
    parser.add_argument("--log-file", type=Path, default=Path("data/heartbeats.jsonl"))
    parser.add_argument("--offline-after", type=float, default=6.0,
                        help="seconds without a heartbeat before a robot shows offline")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = HeartbeatLog(args.log_file)
    registry = ConnectionRegistry()
    app = ServerApp(HeartbeatStore(), log, registry, default_catalog(),
                    offline_after_s=args.offline_after)
    http_server = create_server(app, args.host, args.port)
    ws_server = start_ws_server(registry, args.host, args.ws_port)

    logging.info("dashboard: http://%s:%d  websocket: ws://%s:%d  (heartbeat log: %s)",
                 args.host, args.port, args.host, args.ws_port, args.log_file)
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        http_server.server_close()
        ws_server.shutdown()
        log.close()


if __name__ == "__main__":
    main()
