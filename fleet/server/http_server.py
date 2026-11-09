"""HTTP layer: routing, JSON parsing, and static file serving."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fleet.server.app import ServerApp, UpdateConflict
from fleet.server.connections import RobotNotConnected

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 256 * 1024  # comfortably fits a full MAX_BATCH_SIZE batch


def make_handler(app: ServerApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]

            if url.path == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            elif parts == ["api", "robots"]:
                self._send_json(HTTPStatus.OK, app.fleet())
            elif parts == ["api", "firmware"]:
                self._send_json(HTTPStatus.OK, app.firmware_info())
            elif len(parts) == 3 and parts[:2] == ["api", "firmware"]:
                self._handle_firmware_download(version=parts[2])
            elif len(parts) == 4 and parts[:2] == ["api", "robots"] and parts[3] == "heartbeats":
                limit = _int_param(parse_qs(url.query), "limit", default=50)
                self._send_json(HTTPStatus.OK, app.history(parts[2], limit))
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]

            if parts == ["api", "heartbeat"]:
                self._handle_heartbeat()
            elif parts == ["api", "heartbeats"]:
                self._handle_heartbeat_batch()
            elif len(parts) == 4 and parts[:2] == ["api", "robots"] and parts[3] == "command":
                self._handle_command(robot_id=parts[2])
            elif len(parts) == 4 and parts[:2] == ["api", "robots"] and parts[3] == "firmware":
                self._handle_firmware_update(robot_id=parts[2])
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _handle_firmware_download(self, version: str) -> None:
            """Serves the artifact bytes robots fetch during an update."""
            artifact = app.firmware_artifact(version)
            if artifact is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown version: {version}"})
                return
            self._send(HTTPStatus.OK, artifact.payload, "application/octet-stream")

        def _handle_firmware_update(self, robot_id: str) -> None:
            """Body: {"version": "2.0.0"}. 409 if the robot is unreachable, already
            updating, or already on that version."""
            try:
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                app.start_firmware_update(robot_id, body.get("version"))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except RobotNotConnected:
                self._send_json(HTTPStatus.CONFLICT, {"error": f"{robot_id} is not connected"})
                return
            except UpdateConflict as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, {"ok": True})

        def _handle_heartbeat(self) -> None:
            try:
                app.ingest(self._read_json_body())
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, {"ok": True})

        def _handle_heartbeat_batch(self) -> None:
            """Body: {"heartbeats": [...]}. Replies with per-item counts so the robot
            can safely drop the batch once it gets any 2xx."""
            try:
                result = app.ingest_batch(self._read_json_body())
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, result.to_dict())

        def _handle_command(self, robot_id: str) -> None:
            """Body: {"command": "pause" | "resume"}. 409 if the robot has no open websocket."""
            try:
                body = self._read_json_body()
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                app.send_command(robot_id, body.get("command"))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except RobotNotConnected:
                self._send_json(HTTPStatus.CONFLICT, {"error": f"{robot_id} is not connected"})
                return
            self._send_json(HTTPStatus.ACCEPTED, {"ok": True})

        # --- helpers ------------------------------------------------------

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("missing or oversized body")
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ValueError("body is not valid JSON") from None

        def _send_json(self, status: HTTPStatus, body) -> None:
            self._send(status, json.dumps(body).encode("utf-8"), "application/json")

        def _send_file(self, path: Path, content_type: str) -> None:
            self._send(HTTPStatus.OK, path.read_bytes(), content_type)

        def _send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:  # silence per-request access log
            pass

    return Handler


def _int_param(query: dict, name: str, default: int) -> int:
    try:
        return max(1, min(1000, int(query[name][0])))
    except (KeyError, ValueError, IndexError):
        return default


def create_server(app: ServerApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
