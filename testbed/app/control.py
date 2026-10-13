"""Loopback-only control channel for the experiment harness (Phase 2).

Served by the standard library on 127.0.0.1 inside the container, so it is
reachable only through `docker exec`, never through the service network or the
host. It is deliberately invisible to telemetry: no OpenTelemetry
instrumentation, no log output, and the database work it does runs under
`suppress_instrumentation()`.

State it can set:
  - an added delay on one (method, path) operation, applied by the request
    middleware inside the server span;
  - N pooled DB connections checked out and held inside an open transaction.
Every response carries the container clock (`time_ns`, the Colima VM clock).
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry.instrumentation.utils import suppress_instrumentation
from sqlalchemy import text

_lock = threading.Lock()
_delays: dict[tuple[str, str], float] = {}  # (METHOD, path) -> seconds
_held: list = []
_engine = None


def attach_engine(engine) -> None:
    global _engine
    _engine = engine


def delay_for(method: str, path: str) -> float:
    with _lock:
        return _delays.get((method.upper(), path), 0.0)


def _state() -> dict:
    with _lock:
        return {"delays": [{"method": m, "path": p, "ms": round(s * 1000)} for (m, p), s in _delays.items()],
                "held_connections": len(_held), "time_ns": time.time_ns()}


def _hold(n: int, deadline_s: float) -> None:
    deadline = time.monotonic() + deadline_s
    with suppress_instrumentation():
        while len(_held) < n and time.monotonic() < deadline:
            try:
                conn = _engine.connect()
                conn.execute(text("SELECT 1"))  # opens a transaction that stays open while held
            except Exception:  # pool momentarily busy (TimeoutError) -> retry until the deadline
                continue
            with _lock:
                _held.append(conn)


def _release() -> None:
    with suppress_instrumentation():
        while True:
            with _lock:
                if not _held:
                    return
                conn = _held.pop()
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silent by design
        pass

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._reply(200, _state()) if self.path == "/state" else self._reply(404, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        before_ns = time.time_ns()
        if self.path == "/delay":
            with _lock:
                _delays[(body["method"].upper(), body["path"])] = float(body["ms"]) / 1000.0
        elif self.path == "/delay/clear":
            with _lock:
                _delays.clear()
        elif self.path == "/hold":
            if _engine is None:
                return self._reply(409, {"error": "no database pool in this service"})
            _hold(int(body["n"]), float(body.get("deadline_s", 30)))
        elif self.path == "/release":
            _release()
        else:
            return self._reply(404, {})
        self._reply(200, {**_state(), "applied_from_ns": before_ns})


def start() -> None:
    port = int(os.environ.get("CONTROL_PORT", "9000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="control").start()
