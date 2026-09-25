"""
backend/server/control_api.py

Localhost-only control API for the tray launcher.

A tiny stdlib HTTP server exposing system-control operations
(shutdown / restart / front status) to the tray launcher process.
Bound to 127.0.0.1 only, so it is never reachable from the network —
including Tailscale in server mode — regardless of how the main web
app is served (Gradio internal server in local mode, uvicorn in
server mode).

Layer note: this module is transport. It owns no behavior — all
operations are injected as callables from the composition root
(ui/app.py) — a lower layer never imports an upper one.

Endpoints (all JSON):
    GET  /control/status        -> {"status": "ok"}
    GET  /control/front_status  -> {"clients": {"desktop": 0, ...}}
    POST /control/shutdown      -> {"accepted": true|false, "reason": ...}
    POST /control/restart       -> {"accepted": true|false, "reason": ...}
    POST /control/switch_mode   {"server_mode": bool}
                                -> {"accepted": true|false, "reason": ...}
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()

# Bind outcome for UI display (System page warning when fully failed).
# Phase 4還元 2026-07-22: on the M1, Native Instruments' NTKDaemon squats
# 127.0.0.1:7865 — the bind failed silently every boot and the tray's
# "no response" message pointed nowhere near the cause.
_status: Dict[str, Optional[object]] = {
    'requested': None,  # configured port (None until start attempted)
    'port': None,       # actually bound port (None = not running)
    'error': None,      # last bind error string when fully failed
}


def get_control_api_status() -> Dict[str, Optional[object]]:
    """Bind outcome: {'requested', 'port', 'error'} (see _status)."""
    return dict(_status)


class _ControlRequestHandler(BaseHTTPRequestHandler):
    """Request handler with injected callbacks (set as class attributes)."""

    # Injected by start_control_api()
    shutdown_cb: Callable[[], Dict] = None
    restart_cb: Callable[[], Dict] = None
    front_status_cb: Callable[[], Dict] = None
    switch_mode_cb: Callable[[Dict], Dict] = None

    # Silence default stderr access logging; route to our logger instead
    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        logger.debug("[ControlAPI] %s", format % args)

    def _send_json(self, status_code: int, payload: Dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        try:
            if self.path == '/control/status':
                self._send_json(200, {"status": "ok"})
            elif self.path == '/control/front_status':
                counts = self.front_status_cb() if self.front_status_cb else {}
                self._send_json(200, {"clients": counts})
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:
            logger.error(f"[ControlAPI] GET {self.path} failed: {e}")
            self._send_json(500, {"error": str(e)})

    def _read_body(self) -> Dict:
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, OSError):
            return {}

    def do_POST(self):  # noqa: N802 (stdlib naming)
        try:
            if self.path == '/control/shutdown':
                result = self.shutdown_cb() if self.shutdown_cb else {"accepted": False}
                self._send_json(200, result)
            elif self.path == '/control/restart':
                result = self.restart_cb() if self.restart_cb else {"accepted": False}
                self._send_json(200, result)
            elif self.path == '/control/switch_mode':
                body = self._read_body()
                result = self.switch_mode_cb(body) if self.switch_mode_cb else {"accepted": False}
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:
            logger.error(f"[ControlAPI] POST {self.path} failed: {e}")
            self._send_json(500, {"error": str(e)})


def start_control_api(port: int,
                      shutdown_cb: Callable[[], Dict],
                      restart_cb: Callable[[], Dict],
                      front_status_cb: Callable[[], Dict],
                      switch_mode_cb: Callable[[Dict], Dict] = None,
                      max_port_attempts: int = 10) -> Optional[int]:
    """
    Start the control API server on 127.0.0.1:<port> in a daemon thread.

    If the configured port is taken (third-party daemons do squat fixed
    ports — NTKDaemon on 7865, M1実測 2026-07-22), fall back to the next
    ports up to +max_port_attempts. The caller is expected to persist the
    returned port when it differs, so the tray finds it on its next
    config read.

    Args:
        port: first TCP port to try (loopback only)
        shutdown_cb: returns {"accepted": bool, ...}; must not block
        restart_cb: returns {"accepted": bool, ...}; must not block
        front_status_cb: returns {client_type: count} for connected WS clients
        switch_mode_cb: takes the request body ({"server_mode": bool}),
                        returns {"accepted": bool, ...}; must not block
        max_port_attempts: how many successor ports to try after `port`

    Returns:
        The port actually bound, or None if every candidate failed
        (logged, non-fatal — get_control_api_status() carries the error).
    """
    global _server
    with _server_lock:
        if _server is not None:
            logger.warning("[ControlAPI] Already started")
            return _server.server_address[1]
        _status['requested'] = port
        handler = _ControlRequestHandler
        handler.shutdown_cb = staticmethod(shutdown_cb)
        handler.restart_cb = staticmethod(restart_cb)
        handler.front_status_cb = staticmethod(front_status_cb)
        handler.switch_mode_cb = staticmethod(switch_mode_cb) if switch_mode_cb else None
        bind_error = None
        for candidate in range(port, port + max_port_attempts + 1):
            try:
                _server = ThreadingHTTPServer(('127.0.0.1', candidate), handler)
                _server.daemon_threads = True
                break
            except OSError as e:
                bind_error = e
                logger.warning(f"[ControlAPI] 127.0.0.1:{candidate} unavailable: {e}")
                _server = None
        if _server is None:
            _status['error'] = str(bind_error)
            logger.error(
                f"[ControlAPI] No free port in {port}..{port + max_port_attempts} — "
                "tray control disabled (change launcher.control_port in launch_config.json)")
            return None

    bound = _server.server_address[1]
    _status['port'] = bound
    if bound != port:
        logger.warning(f"[ControlAPI] Configured port {port} taken — fell back to {bound}")
    thread = threading.Thread(
        target=_server.serve_forever,
        name="control-api",
        daemon=True,
    )
    thread.start()
    logger.info(f"[ControlAPI] Listening on 127.0.0.1:{bound}")
    return bound
