"""Local HTTP API server — exposes HC3Menu state and device control to localhost.

Listens on 127.0.0.1 only (never reachable from the network).

Endpoints
---------
GET  /status                         connection state + version
GET  /devices                        all devices (compact summary)
GET  /devices/<id>                   full device struct from cache
GET  /attention                      dead / low-battery devices
GET  /activity                       recent 50 activity events
GET  /scenes                         all scenes
GET  /debug                          recent 100 QA debug messages
POST /devices/<id>/on                turn on
POST /devices/<id>/off               turn off
POST /devices/<id>/level             {"value": 75}  set brightness / value
POST /devices/<id>/action/<name>     {"args": [...]}  any HC3 action
POST /scenes/<id>/run                run a scene

Quick-start (default port 34562):

  curl http://localhost:34562/status
  curl http://localhost:34562/devices | python3 -m json.tool
  curl -X POST http://localhost:34562/devices/42/on
  curl -X POST http://localhost:34562/devices/42/level -d '{"value":75}'
  curl -X POST http://localhost:34562/scenes/7/run
"""
from __future__ import annotations

import json
import logging
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Optional

from .__version__ import __version__
from .state import StateStore

log = logging.getLogger(__name__)

_RE_DEVICE_ACTION = re.compile(r"^/devices/(\d+)/(on|off|level|action/.+)$")
_RE_SCENE_RUN = re.compile(r"^/scenes/(\d+)/run$")


def _json_response(handler: BaseHTTPRequestHandler, code: int, body: Any) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _err(handler: BaseHTTPRequestHandler, code: int, msg: str) -> None:
    _json_response(handler, code, {"error": msg})


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802
        log.debug("local-api: " + fmt, *args)

    # ------------------------------------------------------------------ GET --
    def do_GET(self):  # noqa: N802
        store: StateStore = self.server._store
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/status":
            _json_response(self, 200, {
                "connected": store.is_connected(),
                "error": store.last_error(),
                "version": __version__,
            })
            return

        if path == "/devices":
            _json_response(self, 200, [_device_summary(d) for d in store.all_devices()])
            return

        m = re.match(r"^/devices/(\d+)$", path)
        if m:
            d = store.get_device(int(m.group(1)))
            if d is None:
                _err(self, 404, f"Device {m.group(1)} not found in cache")
            else:
                _json_response(self, 200, d)
            return

        if path == "/attention":
            _json_response(self, 200, store.attention_devices())
            return

        if path == "/activity":
            _json_response(self, 200, store.recent_activity(50))
            return

        if path == "/scenes":
            _json_response(self, 200, store.all_scenes())
            return

        if path == "/debug":
            _json_response(self, 200, store.recent_debug_messages(100))
            return

        _err(self, 404, f"Unknown path: {path}")

    # ----------------------------------------------------------------- POST --
    def do_POST(self):  # noqa: N802
        client = self.server._client_getter()
        if client is None:
            _err(self, 503, "Not connected to HC3")
            return

        path = self.path.split("?")[0].rstrip("/")

        # Read optional JSON body
        body: dict = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                _err(self, 400, "Invalid JSON body")
                return

        m = _RE_DEVICE_ACTION.match(path)
        if m:
            dev_id = int(m.group(1))
            action = m.group(2)
            try:
                if action == "on":
                    result = client.turn_on(dev_id)
                elif action == "off":
                    result = client.turn_off(dev_id)
                elif action == "level":
                    v = body.get("value")
                    if v is None:
                        _err(self, 400, 'Body must contain {"value": <number>}')
                        return
                    result = client.set_value(dev_id, v)
                else:
                    # action/<name>
                    action_name = action.split("/", 1)[1]
                    result = client.call_action(dev_id, action_name, body.get("args", []))
                _json_response(self, 200, {"ok": True, "result": result})
            except Exception as e:
                _err(self, 500, str(e))
            return

        m = _RE_SCENE_RUN.match(path)
        if m:
            try:
                result = client.run_scene(int(m.group(1)))
                _json_response(self, 200, {"ok": True, "result": result})
            except Exception as e:
                _err(self, 500, str(e))
            return

        _err(self, 404, f"Unknown path: {path}")


def _device_summary(d: dict) -> dict:
    props = d.get("properties") or {}
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "type": d.get("type"),
        "roomID": d.get("roomID"),
        "enabled": d.get("enabled"),
        "visible": d.get("visible"),
        "value": props.get("value"),
        "state": props.get("state"),
        "dead": props.get("dead"),
        "batteryLevel": props.get("batteryLevel"),
    }


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int,
                 store: StateStore, client_getter: Callable):
        self._store = store
        self._client_getter = client_getter
        super().__init__((host, port), _Handler)


class LocalAPIServer:
    """Runs a minimal JSON-over-HTTP server on localhost in a daemon thread.

    Bound exclusively to 127.0.0.1 so it is never reachable from the network.
    Start/stop are idempotent.
    """

    HOST = "127.0.0.1"

    def __init__(self, store: StateStore,
                 client_getter: Callable[[], Optional[Any]],
                 port: int = 34562):
        self._store = store
        self._client_getter = client_getter
        self._port = port
        self._server: Optional[_ThreadedServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = _ThreadedServer(
                self.HOST, self._port, self._store, self._client_getter
            )
        except OSError as e:
            log.warning("local-api: could not bind %s:%d — %s", self.HOST, self._port, e)
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="hc3menu-local-api",
            daemon=True,
        )
        self._thread.start()
        log.info("local-api: listening on %s:%d", self.HOST, self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        return self._port
