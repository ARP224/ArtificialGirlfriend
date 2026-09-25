"""
backend/server/remote_switch_listener.py

Remote mode-switch listener (local mode only, opt-in via
launch_config server_mode.remote_switch_enabled).

While the app runs in local mode, a minimal HTTPS server listens on the
Tailscale IP (same port as the web UI, distinct bind address) and serves
a single-purpose page: trigger the switch to server mode from a remote
device without touching the PC. Every GET path returns the switch page —
the visitor's bookmarked server-mode URL (e.g. /mobile) therefore lands
here in local mode and reloads into the real UI after the switch.

Layer note: this module is transport. The actual switch (config write +
restart) is injected as a callable from the composition root (ui/app.py),
so a lower layer never imports an upper one — same pattern as
control_api.py.

Endpoints:
    GET  <any path>  -> switch page (marker header X-AG-Switch-Page: 1).
                        If the injected busy_cb() returns a reason (memory
                        task = extraction / relationship update running,
                        2026-08-16 稜裁定), the button is rendered disabled
                        with that reason + "reopen later" — the page has no
                        WS, so it does not auto-recover; POST /switch keeps
                        its own guard as the second line of defence.
    POST /switch     -> {"accepted": bool, "reason": str}
                        requires header X-AG-Remote-Switch: 1 (CSRF guard;
                        cross-origin requests cannot set custom headers
                        without a preflight, and OPTIONS is not served)
"""

import json
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# How long to wait between Tailscale availability probes / bind retries.
RETRY_INTERVAL_SECONDS = 60

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
body { margin: 0; font-family: system-ui, sans-serif; background: #1a1a2e;
       color: #eaeaea; display: flex; min-height: 100vh;
       align-items: center; justify-content: center; }
.card { max-width: 26em; padding: 2em; text-align: center; }
h1 { font-size: 1.3em; }
p { color: #b8b8c8; line-height: 1.6; }
button { font-size: 1.1em; padding: 0.8em 2em; border: none; border-radius: 8px;
         background: #7c5cbf; color: #fff; cursor: pointer; }
button:disabled { background: #555; cursor: default; }
#status { min-height: 3em; margin-top: 1.5em; color: #eaeaea; }
</style>
</head>
<body>
<div class="card">
<h1>__HEADING__</h1>
<p>__DESC__</p>
<button id="switch-btn"__BUTTON_DISABLED__>__BUTTON__</button>
<div id="status">__INITIAL_STATUS__</div>
</div>
<script>
(function () {
  var MSG = __MESSAGES_JSON__;
  var btn = document.getElementById('switch-btn');
  var statusEl = document.getElementById('status');

  function poll(deadline) {
    if (Date.now() > deadline) { statusEl.textContent = MSG.timeout; return; }
    fetch(window.location.href, { cache: 'no-store' }).then(function (r) {
      if (!r.headers.get('X-AG-Switch-Page')) { window.location.reload(); return; }
      setTimeout(function () { poll(deadline); }, 2000);
    }).catch(function () {
      // The app is restarting; connection errors are expected here.
      setTimeout(function () { poll(deadline); }, 2000);
    });
  }

  btn.addEventListener('click', function () {
    if (!confirm(MSG.confirm)) { return; }
    btn.disabled = true;
    statusEl.textContent = MSG.requesting;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 60000);
    fetch('/switch', {
      method: 'POST',
      headers: { 'X-AG-Remote-Switch': '1' },
      signal: ctrl.signal
    }).then(function (r) { clearTimeout(timer); return r.json(); })
      .then(function (data) {
        if (!data.accepted) {
          statusEl.textContent = data.reason || MSG.request_failed;
          btn.disabled = false;
          return;
        }
        statusEl.textContent = MSG.switching;
        setTimeout(function () { poll(Date.now() + 180000); }, 2000);
      })
      .catch(function () {
        clearTimeout(timer);
        statusEl.textContent = MSG.request_failed;
        btn.disabled = false;
      });
  });
})();
</script>
</body>
</html>
"""


def _render_page(busy_reason: Optional[str] = None) -> bytes:
    """Render the switch page in the active UI language.

    busy_reason: 記憶タスク中の理由文(合成根の busy_cb が返す)。あればボタンを
    disabled で描き、理由をステータス欄に前置する(開いた瞬間に押せないことが
    分かる)。None なら従来どおり。
    """
    import html as _html
    from backend.shared.i18n import current_language, t
    messages = {
        'confirm': t('remote_switch.confirm'),
        'requesting': t('remote_switch.requesting'),
        'switching': t('remote_switch.switching'),
        'request_failed': t('remote_switch.request_failed'),
        'timeout': t('remote_switch.timeout'),
    }
    page = (_PAGE_TEMPLATE
            .replace('__LANG__', current_language())
            .replace('__TITLE__', t('remote_switch.page_title'))
            .replace('__HEADING__', t('remote_switch.heading'))
            .replace('__DESC__', t('remote_switch.desc'))
            .replace('__BUTTON__', t('remote_switch.button'))
            .replace('__BUTTON_DISABLED__', ' disabled' if busy_reason else '')
            .replace('__INITIAL_STATUS__', _html.escape(busy_reason) if busy_reason else '')
            .replace('__MESSAGES_JSON__', json.dumps(messages, ensure_ascii=False)))
    return page.encode('utf-8')


class _SwitchRequestHandler(BaseHTTPRequestHandler):
    """Request handler with the switch callback injected as a class attribute."""

    # Injected by _build_server()
    switch_cb: Callable[[], Dict] = None
    # Injected by _build_server(); returns a reason string while a memory task
    # (extraction / relationship update) is running, else None. Optional.
    busy_cb: Optional[Callable[[], Optional[str]]] = None

    # A dead connection must not pin a worker thread (TLS handshake is
    # deferred to the first read here — see _build_server).
    timeout = 20

    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        logger.debug("[RemoteSwitch] %s", format % args)

    def _send(self, status_code: int, content_type: str, body: bytes,
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status_code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        try:
            # Any path serves the switch page so a bookmarked server-mode URL
            # (/, /mobile, /admin/...) lands on it while in local mode.
            busy_reason = None
            if self.busy_cb is not None:
                try:
                    busy_reason = self.busy_cb() or None
                except Exception as e:  # 判定失敗は「忙しくない」扱い(押下時ガードが残る)
                    logger.debug(f"[RemoteSwitch] busy_cb failed: {e}")
            self._send(200, 'text/html; charset=utf-8', _render_page(busy_reason),
                       {'X-AG-Switch-Page': '1', 'Cache-Control': 'no-store'})
        except Exception as e:
            logger.error(f"[RemoteSwitch] GET {self.path} failed: {e}")

    def do_POST(self):  # noqa: N802 (stdlib naming)
        try:
            if self.path != '/switch':
                self._send(404, 'application/json; charset=utf-8',
                           b'{"error": "not found"}')
                return
            if self.headers.get('X-AG-Remote-Switch') != '1':
                self._send(403, 'application/json; charset=utf-8',
                           b'{"accepted": false, "reason": "forbidden"}')
                return
            result = self.switch_cb() if self.switch_cb else {"accepted": False}
            body = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self._send(200, 'application/json; charset=utf-8', body)
        except Exception as e:
            logger.error(f"[RemoteSwitch] POST {self.path} failed: {e}")


def _build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Server-side TLS context (separate function = test seam: tests patch
    this to None and exercise the lifecycle over plain HTTP)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def _build_server(host: str, port: int, switch_cb: Callable[[], Dict],
                  ssl_context: Optional[ssl.SSLContext] = None,
                  busy_cb: Optional[Callable[[], Optional[str]]] = None) -> ThreadingHTTPServer:
    """Bind the listener socket (plain HTTP when ssl_context is None — tests)."""
    handler = type('_BoundSwitchHandler', (_SwitchRequestHandler,),
                   {'switch_cb': staticmethod(switch_cb),
                    'busy_cb': staticmethod(busy_cb) if busy_cb is not None else None})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    if ssl_context is not None:
        # do_handshake_on_connect=False: a client stalling mid-handshake must
        # not block the accept loop; the handshake runs lazily in the worker
        # thread on first read (bounded by the handler timeout above).
        server.socket = ssl_context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False)
    return server


class RemoteSwitchListener:
    """
    Owns the listener lifecycle: Tailscale wait -> cert -> bind -> serve,
    then a watchdog that keeps probing Tailscale and rebinds after an
    outage (a fresh bind after recovery sidesteps any question of whether
    the old socket survived the interface bounce).

    configure() is called once at boot by the composition root (even when
    the opt-in flag is off, so the System-page checkbox can start it later);
    start()/stop() are idempotent. Every start() creates its own stop Event
    (generation token) that the worker threads capture — a quick OFF->ON
    toggle can therefore never leave a stale thread running with a cleared
    flag, and writes from an outdated generation are discarded.

    on_status_change (injected) fires after every status transition and on
    every watchdog tick; the composition root uses it to push the translated
    status line to the browser (ui_events -> WS -> JS-owned div).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._switch_cb: Optional[Callable[[], Dict]] = None
        self._busy_cb: Optional[Callable[[], Optional[str]]] = None
        self._on_status_change: Optional[Callable[[], None]] = None
        self._port: Optional[int] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._run_event: Optional[threading.Event] = None  # current generation
        self._setup_thread: Optional[threading.Thread] = None
        # stopped | starting | waiting_tailscale | listening | cert_failed
        # ('starting' = probe/cert/bind in progress, so the System page never
        # claims "Tailscale not connected" while Tailscale is actually up)
        self.status = 'stopped'
        self.listen_url: Optional[str] = None  # set while listening

    def configure(self, switch_cb: Callable[[], Dict], port: int,
                  on_status_change: Optional[Callable[[], None]] = None,
                  busy_cb: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._switch_cb = switch_cb
        self._port = port
        self._on_status_change = on_status_change
        self._busy_cb = busy_cb

    def start(self) -> None:
        with self._lock:
            if self._switch_cb is None or self._port is None:
                logger.error("[RemoteSwitch] start() before configure()")
                return
            thread = self._setup_thread
            run_event = self._run_event
            if (thread is not None and thread.is_alive()
                    and run_event is not None and not run_event.is_set()):
                return  # already running
        # A stopped generation may still be winding down — wait it out so
        # the new bind cannot race the old socket.
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("[RemoteSwitch] Previous listener thread still "
                               "busy, not restarting")
                return
        with self._lock:
            run_event = threading.Event()
            self._run_event = run_event
            self.status = 'starting'
            self.listen_url = None
            self._setup_thread = threading.Thread(
                target=self._setup_and_serve, args=(run_event,),
                name='remote-switch-setup', daemon=True)
            self._setup_thread.start()
        self._notify_status()

    def stop(self) -> None:
        with self._lock:
            if self._run_event is not None:
                self._run_event.set()
            server, self._server = self._server, None
            self.status = 'stopped'
            self.listen_url = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as e:
                logger.warning(f"[RemoteSwitch] stop failed: {e}")
        self._notify_status()
        logger.info("[RemoteSwitch] Stopped")

    # -- internal -------------------------------------------------------------

    def _notify_status(self) -> None:
        """Fire the injected observer; a UI failure must never hurt us."""
        callback = self._on_status_change
        if callback is None:
            return
        try:
            callback()
        except Exception as e:
            logger.debug(f"[RemoteSwitch] status notify failed: {e}")

    def _set_run_status(self, run_event: threading.Event, status: str,
                        listen_url: Optional[str] = None) -> None:
        """Status write guarded by generation: a superseded or stopped
        thread must not clobber the current state."""
        with self._lock:
            if self._run_event is not run_event or run_event.is_set():
                return
            self.status = status
            self.listen_url = listen_url
        self._notify_status()

    def _teardown_server(self, server: ThreadingHTTPServer) -> None:
        with self._lock:
            if self._server is server:
                self._server = None
        try:
            server.shutdown()
            server.server_close()
        except Exception as e:
            logger.warning(f"[RemoteSwitch] teardown failed: {e}")

    def _setup_and_serve(self, run_event: threading.Event) -> None:
        from backend.server.tailscale import (
            check_tailscale_available,
            ensure_valid_cert,
            get_tailscale_hostname,
            get_tailscale_ip,
        )
        while not run_event.is_set():
            if not check_tailscale_available():
                if self.status != 'waiting_tailscale':
                    logger.info("[RemoteSwitch] Tailscale not available, "
                                f"retrying every {RETRY_INTERVAL_SECONDS}s")
                self._set_run_status(run_event, 'waiting_tailscale')
                run_event.wait(RETRY_INTERVAL_SECONDS)
                continue
            self._set_run_status(run_event, 'starting')
            try:
                ip = get_tailscale_ip()
            except RuntimeError as e:
                logger.info(f"[RemoteSwitch] Tailscale IP not available ({e}), retrying")
                self._set_run_status(run_event, 'waiting_tailscale')
                run_event.wait(RETRY_INTERVAL_SECONDS)
                continue

            # run_tailscale_cert retries transient ACME failures internally;
            # a failure that survives those retries is likely persistent —
            # give up until the next start().
            try:
                certfile, keyfile = ensure_valid_cert()
            except Exception as e:
                logger.error(f"[RemoteSwitch] Certificate unavailable, giving up: {e}")
                self._set_run_status(run_event, 'cert_failed')
                return

            try:
                ctx = _build_ssl_context(certfile, keyfile)
                server = _build_server(ip, self._port, self._switch_cb, ctx,
                                       busy_cb=self._busy_cb)
            except OSError as e:
                # e.g. lingering TIME_WAIT right after a server->local restart
                logger.warning(f"[RemoteSwitch] Bind {ip}:{self._port} failed ({e}), retrying")
                run_event.wait(RETRY_INTERVAL_SECONDS)
                continue

            # Prefer the tailnet DNS name (matches the certificate) for the
            # URL shown on the System page; fall back to the raw IP.
            try:
                host = get_tailscale_hostname()
            except Exception:
                host = ip

            with self._lock:
                if self._run_event is not run_event or run_event.is_set():
                    server.server_close()
                    return
                self._server = server
                self.listen_url = f"https://{host}:{self._port}"
                self.status = 'listening'
            threading.Thread(target=server.serve_forever,
                             name='remote-switch-serve', daemon=True).start()
            self._notify_status()
            logger.info(f"[RemoteSwitch] Listening on https://{ip}:{self._port}")

            # Watchdog: keep probing Tailscale while serving. On outage,
            # tear down and fall back to the wait loop; the periodic
            # notify also re-syncs a WS client that reconnected and
            # missed a transition.
            while not run_event.wait(RETRY_INTERVAL_SECONDS):
                if not check_tailscale_available():
                    logger.info("[RemoteSwitch] Tailscale lost, closing "
                                "listener until it returns")
                    self._teardown_server(server)
                    self._set_run_status(run_event, 'waiting_tailscale')
                    break
                self._notify_status()
            else:
                # This generation was stopped; stop() already tore the
                # server down.
                return


_listener = RemoteSwitchListener()


def get_remote_switch_listener() -> RemoteSwitchListener:
    return _listener
