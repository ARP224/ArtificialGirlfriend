"""
launcher/tray_app.py

AG tray launcher / backend supervisor.

Successor to the LaunchHidden.vbs -> AirtificialGirlfriend.ps1 chain.
Runs as an independent pythonw process. This module never imports
backend/* or ui/* — it talks to the backend process only via:
  - subprocess spawn of run.py (stdout captured to a log file)
  - the localhost control API (backend/server/control_api.py)
  - the .restart_flag file (same contract the PS1 launcher used)

Responsibilities:
  - single-instance guard (tray_port); a second launch forwards
    "open-front" to the running instance instead of starting twice
  - backend supervision: spawn, wait for the web port, restart on
    .restart_flag, cap consecutive startup failures
  - front window management: open Chrome --app, or focus the existing
    window (WS front_status + window-title search, PS1 parity)
  - tray icon + menu

Usage:
    pythonw launcher/tray_app.py               # tray + backend, no front
    pythonw launcher/tray_app.py --open-front  # also open the front
                                               # (ArtificialGirlfriend.pyw)
"""

import json
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from launcher.tray_i18n import t  # noqa: E402

# Platform adapter (Mac port plan Phase 1): one side is imported by OS and
# called under the common name `plat`. All win32/darwin specifics
# (window find/close/focus/open, dialogs, venv layout, spawn flags,
# pystray patches) live behind this seam.
if sys.platform == 'darwin':
    from launcher import platform_mac as plat  # noqa: E402
else:
    from launcher import platform_win as plat  # noqa: E402

RUN_PY = ROOT / 'run.py'
RESTART_FLAG = ROOT / '.restart_flag'
LOG_DIR = ROOT / 'logs' / 'launcher'
BACKEND_STDOUT_LOG = LOG_DIR / 'backend-stdout.log'

DEFAULT_WEB_PORT = 7860  # actual port comes from launch_config launcher.web_port

# darwin: white-only variant (background gradient extracted) — a colored
# icon looks out of place in the macOS menu bar (稜裁定 2026-07-22). It is
# presented as a *template image* (platform_mac pystray patch): AppKit tints
# the alpha black/white to match the bar, so only the shape matters there.
if sys.platform == 'darwin':
    APP_IMAGES_LOGO = ROOT / 'app_images' / 'Artificial_Girlfriend_Logo_Mac.png'
else:
    APP_IMAGES_LOGO = ROOT / 'app_images' / 'Artificial_Girlfriend_Logo.png'
APP_IMAGES_LOGO_ICO = ROOT / 'app_images' / 'Artificial_Girlfriend_Logo.ico'

# How long to wait for Tailscale at startup in server mode (boot ordering:
# the tray may come up before Tailscale finishes connecting)
TAILSCALE_WAIT_SECONDS = 60

# 自ホスト向け生存探査(バックエンド稼働チェック)のタイムアウト。
# このPCではFWが閉じたポートへのSYNをRST拒否でなくdropするため、探査は
# 「閉じている」場合に必ずタイムアウト満了まで待つ(2026-07-18実測: localhost
# 全ポートでフル1.0秒)。spawn前チェックがこの値×回数の固定費になるので短く
# 保つ。稼働中の成功応答はカーネルaccept/ローカルHTTPとも数msなので 0.3秒で
# 誤判定しない(採用判定は web_port_open が先=カーネル応答でGILビジーにも頑健)。
PROBE_TIMEOUT = 0.3

logger = logging.getLogger('tray')


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def launch_config_path() -> Path:
    """Settings dir (correct spelling), with one-time migration from the
    misspelled legacy dir — the tray starts before the backend, so the
    migration must happen here too (idempotent, same logic as settings_store)."""
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA', ''))
    else:
        base = Path.home() / '.config'
    settings_dir = base / 'ArtificialGirlfriend'
    legacy_dir = base / 'AirtificialGirlfriend'
    if legacy_dir.exists() and not settings_dir.exists():
        try:
            legacy_dir.rename(settings_dir)
            logger.info(f"Migrated settings dir: {legacy_dir} -> {settings_dir}")
        except OSError as e:
            logger.warning(f"Settings dir migration failed, using legacy: {e}")
            return legacy_dir / 'launch_config.json'
    return settings_dir / 'launch_config.json'


def load_launch_config() -> dict:
    """Read launch_config.json directly (stdlib only, BOM tolerant)."""
    try:
        with open(launch_config_path(), 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def launcher_setting(config: dict, key: str, default):
    return config.get('launcher', {}).get(key, default)


class Target:
    """Where the backend front lives for the current mode."""

    def __init__(self, mode: str, url: str, host: str, front_client_type: str,
                 port: int = DEFAULT_WEB_PORT):
        self.mode = mode                          # 'local' | 'server'
        self.url = url                            # Chrome --app URL
        self.host = host                          # host for web-port checks
        self.front_client_type = front_client_type  # WS client_type of the front
        self.port = port                          # web UI port (launcher.web_port)


# Minimal duplicate of backend/server/tailscale.py resolve_tailscale_cli()
# (tray_i18n precedent: the launcher never imports backend/*). Covers the
# macOS App Store CLI (inside the app bundle, never on PATH) and installs
# where PATH wasn't updated.
TAILSCALE_FALLBACK_PATHS = (
    '/Applications/Tailscale.app/Contents/MacOS/Tailscale',
    '/opt/homebrew/bin/tailscale',
    r'C:\Program Files\Tailscale\tailscale.exe',
)


def resolve_tailscale_cli() -> str:
    """Absolute path to the tailscale CLI, or the bare name as last resort."""
    found = shutil.which('tailscale')
    if found:
        return found
    for candidate in TAILSCALE_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return 'tailscale'


def resolve_tailscale_dns() -> str:
    """Return the Tailscale MagicDNS name of this machine, or ''."""
    try:
        result = subprocess.run(
            [resolve_tailscale_cli(), 'status', '--json'],
            capture_output=True, timeout=10, **plat.spawn_kwargs(),
        )
        data = json.loads(result.stdout.decode('utf-8', errors='replace'))
        return data.get('Self', {}).get('DNSName', '').rstrip('.')
    except Exception as e:
        logger.warning(f"Tailscale DNS resolution failed: {e}")
        return ''


def compute_target(config: dict, resolve=resolve_tailscale_dns) -> Target:
    """
    PS1 parity: server mode requires both the config flag and a resolvable
    Tailscale hostname; otherwise fall back to local mode with a warning.
    The resolver is injectable so the supervisor can use a retrying one.
    """
    web_port = launcher_setting(config, 'web_port', DEFAULT_WEB_PORT)
    if config.get('server_mode', {}).get('enabled'):
        dns_name = resolve()
        if dns_name:
            return Target('server', f'https://{dns_name}:{web_port}/admin/', dns_name,
                          'admin', web_port)
        logger.warning("Server mode enabled but Tailscale hostname unavailable — using local mode")
    return Target('local', f'http://127.0.0.1:{web_port}', '127.0.0.1', 'desktop', web_port)


# ---------------------------------------------------------------------------
# Control API client (backend side binds 127.0.0.1 only)
# ---------------------------------------------------------------------------

def control_request(port: int, path: str, method: str = 'GET', timeout: float = 2.0,
                    body: dict = None):
    """Return parsed JSON from the backend control API, or None if unreachable."""
    url = f'http://127.0.0.1:{port}{path}'
    try:
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(url, method=method, data=data)
        if data is not None:
            req.add_header('Content-Type', 'application/json; charset=utf-8')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def backend_alive(control_port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    result = control_request(control_port, '/control/status', timeout=timeout)
    return bool(result and result.get('status') == 'ok')


def web_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def front_client_count(control_port: int, client_type: str) -> int:
    result = control_request(control_port, '/control/front_status')
    if not result:
        return 0
    return int(result.get('clients', {}).get(client_type, 0))


# ---------------------------------------------------------------------------
# Backend supervisor
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_FAILURES = 5  # PS1 parity ($maxRestarts)


class BackendSupervisor(threading.Thread):
    """
    Owns the backend process lifecycle.

    States: 'stopped' | 'starting' | 'running'
    The .restart_flag contract is unchanged from the PS1 launcher: the
    backend exits after creating the flag, and the supervisor restarts it.
    A clean exit without the flag (UI Exit button / tray stop) leaves the
    backend stopped but keeps the tray alive.
    """

    def __init__(self, on_state_change, on_event=None):
        super().__init__(name='backend-supervisor', daemon=True)
        self.want_running = threading.Event()
        self._quit = threading.Event()
        self._wake = threading.Event()
        self._on_state_change = on_state_change
        self._on_event = on_event  # 'restart_pending' | 'front_reopen' | 'stopped_clean' | 'stopped_crash'
        self._process = None
        self._failures = 0
        self._pending_reopen = False   # set by a .restart_flag cycle
        self._last_exit_code = None    # None = adopted backend (code unknown)
        self.state = 'stopped'
        self.status_note = ''  # extra tooltip text (e.g. "Tailscale 待機中")
        self.target = compute_target(load_launch_config())

    # -- public -------------------------------------------------------------

    def start_backend(self) -> None:
        self.want_running.set()
        self._wake.set()

    def quit(self) -> None:
        self._quit.set()
        self.want_running.clear()
        self._wake.set()

    def force_kill(self) -> bool:
        """Terminate the backend process if we own it (spawned, not adopted).

        Used when the control API is unreachable (e.g. backend still 'starting'
        before the control server is up): quit() alone only sets flags and the
        supervisor is blocked in _monitor_until_exit's process.wait(), so the
        backend would survive as an orphan. Returns True if a process was killed.
        """
        proc = self._process
        if proc is None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return True
        except OSError:
            return False

    # -- internals ----------------------------------------------------------

    def _emit(self, event: str) -> None:
        logger.info(f"Supervisor event: {event}")
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass

    def _set_state(self, state: str, note: str = '') -> None:
        if state != self.state or note != self.status_note:
            self.state = state
            self.status_note = note
            logger.info(f"Backend state: {state}{f' ({note})' if note else ''}")
            try:
                self._on_state_change(state)
            except Exception:
                pass

    def _resolve_tailscale_with_retry(self) -> str:
        """
        Retry Tailscale DNS resolution for up to TAILSCALE_WAIT_SECONDS.
        Replaces the PS1 single-shot resolve: at boot the tray often comes
        up before Tailscale has connected.
        """
        deadline = time.monotonic() + TAILSCALE_WAIT_SECONDS
        while not self._quit.is_set():
            dns_name = resolve_tailscale_dns()
            if dns_name:
                return dns_name
            if time.monotonic() >= deadline:
                return ''
            self._set_state('starting', note=t('tray.tailscale_waiting'))
            self._quit.wait(5)
        return ''

    def _config(self) -> dict:
        return load_launch_config()

    def _control_port(self) -> int:
        return launcher_setting(self._config(), 'control_port', 7865)

    def _spawn(self, config: dict) -> bool:
        """Spawn run.py without a console, stdout captured to a log file."""
        venv_python = plat.venv_python()
        if not venv_python.exists():
            logger.error(f"venv python not found: {venv_python}")
            return False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Keep one previous generation of backend stdout
        try:
            if BACKEND_STDOUT_LOG.exists():
                prev = BACKEND_STDOUT_LOG.with_suffix('.log.prev')
                prev.unlink(missing_ok=True)
                BACKEND_STDOUT_LOG.rename(prev)
        except OSError as e:
            logger.warning(f"Backend stdout log rotation failed: {e}")

        env = os.environ.copy()
        for key, value in launcher_setting(config, 'env', {}).items():
            env[key] = str(value)
        env['PYTHONIOENCODING'] = 'utf-8'

        try:
            log_file = open(BACKEND_STDOUT_LOG, 'wb')
            self._process = subprocess.Popen(
                [str(venv_python), str(RUN_PY)],
                cwd=str(ROOT),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **plat.spawn_kwargs(),
            )
            logger.info(f"Backend spawned (pid={self._process.pid}, mode={self.target.mode})")
            return True
        except Exception as e:
            logger.error(f"Backend spawn failed: {e}")
            return False

    def _wait_web_port(self, config: dict) -> bool:
        """Wait for the web port to come up (PS1 startup wait parity).

        デッドラインベース: startup_timeout_seconds は壁時計の秒。旧実装の
        `for _ in range(timeout)` は反復回数で、1周のコスト(このPCでは閉ポート
        探査がタイムアウト満了する — PROBE_TIMEOUT 参照 — ため約2秒)に実効
        タイムアウトが依存していた(設定90 ≒ 実効180秒)。
        """
        timeout = launcher_setting(config, 'startup_timeout_seconds', 90)
        timeout = max(30, min(300, int(timeout)))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._quit.is_set():
                return False
            if self._process is not None and self._process.poll() is not None:
                logger.error("Backend process exited during startup")
                return False
            if web_port_open(self.target.host, self.target.port, timeout=PROBE_TIMEOUT):
                return True
            time.sleep(0.5)
        logger.error(f"Backend did not open port {self.target.port} within {timeout}s")
        return False

    def _wait_port_release(self) -> None:
        """Wait up to 30s for the web port to be released before restarting."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not web_port_open(self.target.host, self.target.port, timeout=PROBE_TIMEOUT):
                return
            time.sleep(1)
        logger.warning("Web port still open after 30s — attempting restart anyway")

    def _monitor_until_exit(self) -> None:
        """Block until the backend (own process or adopted) is gone."""
        if self._process is not None:
            self._process.wait()
            code = self._process.returncode
            self._last_exit_code = code
            logger.info(f"Backend process exited (code={code})")
            self._process = None
        else:
            # Adopted backend: poll aliveness
            control_port = self._control_port()
            while not self._quit.is_set():
                if not backend_alive(control_port) and not web_port_open(self.target.host, self.target.port, PROBE_TIMEOUT):
                    logger.info("Adopted backend is gone")
                    return
                time.sleep(3)

    def run(self):
        while not self._quit.is_set():
            if not self.want_running.is_set():
                self._set_state('stopped')
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue

            config = self._config()
            self.target = compute_target(config, resolve=self._resolve_tailscale_with_retry)
            control_port = launcher_setting(config, 'control_port', 7865)

            self._last_exit_code = None
            # web_port_open を先に評価: listen ソケットはカーネルが応えるため、
            # バックエンドが推論等でGILビジーでも稼働中を見逃さない(HTTPの
            # backend_alive は応答スレッドが詰まると短タイムアウトで空振りしうる)
            if web_port_open(self.target.host, self.target.port, PROBE_TIMEOUT) or backend_alive(control_port):
                # Adopt an already-running backend (parallel-launch / tray restart)
                logger.info("Adopting already-running backend")
                self._failures = 0
                self._set_state('running')
                if self._pending_reopen:
                    self._pending_reopen = False
                    self._emit('front_reopen')
                self._monitor_until_exit()
            else:
                self._set_state('starting')
                if not self._spawn(config) or not self._wait_web_port(config):
                    if self._process is not None:
                        try:
                            self._process.kill()
                        except OSError:
                            pass
                        self._process = None
                    self._failures += 1
                    logger.error(f"Backend startup failed ({self._failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if self._failures >= MAX_CONSECUTIVE_FAILURES:
                        self.want_running.clear()
                        self._set_state('stopped')
                        plat.alert(
                            t('tray.start_failed_repeat', log=BACKEND_STDOUT_LOG),
                        )
                    continue
                self._failures = 0
                self._set_state('running')
                if self._pending_reopen:
                    self._pending_reopen = False
                    self._emit('front_reopen')
                self._monitor_until_exit()

            # Backend ended — restart flag decides what happens next
            if RESTART_FLAG.exists():
                try:
                    RESTART_FLAG.unlink()
                except OSError as e:
                    logger.warning(f"Failed to remove restart flag: {e}")
                logger.info("Restart flag detected — restarting backend")
                # UI/トレイ発の再起動(モード切替含む): 画面は今すぐ閉じ、
                # 再起動完了後に front_reopen で開き直す
                self._pending_reopen = True
                self._emit('restart_pending')
                self._wait_port_release()
                continue  # want_running stays set
            logger.info("Backend stopped without restart flag")
            self.want_running.clear()
            self._set_state('stopped')
            if self._last_exit_code not in (0, None):
                self._emit('stopped_crash')
            else:
                self._emit('stopped_clean')


# ---------------------------------------------------------------------------
# Front window management
# ---------------------------------------------------------------------------

class FrontOpener:
    """Opens or focuses the AG front window. Serialized by a lock."""

    def __init__(self, supervisor: BackendSupervisor):
        self._supervisor = supervisor
        self._lock = threading.Lock()

    def open_async(self, notify) -> None:
        threading.Thread(target=self._open, args=(notify,), daemon=True,
                         name='front-opener').start()

    def _open(self, notify) -> None:
        if not self._lock.acquire(blocking=False):
            return  # an open is already in progress
        try:
            supervisor = self._supervisor
            config = load_launch_config()
            control_port = launcher_setting(config, 'control_port', 7865)

            # 1. Ensure backend is up
            if supervisor.state != 'running':
                notify(t('tray.starting_notify'))
                supervisor.start_backend()
                timeout = launcher_setting(config, 'startup_timeout_seconds', 90)
                deadline = time.monotonic() + max(30, min(300, int(timeout))) + 5
                while time.monotonic() < deadline and supervisor.state != 'running':
                    time.sleep(0.5)
                if supervisor.state != 'running':
                    notify(t('tray.start_failed'))
                    return
            target = supervisor.target

            # 2. Front already connected? -> focus, don't open another
            if front_client_count(control_port, target.front_client_type) > 0:
                if not plat.focus_front():
                    notify(t('tray.front_already_connected'))
                return
            if plat.focus_front():
                return

            # 3. Open a new front window (Chrome --app; specifics per OS).
            # chrome_profile is written by the System page dropdown
            # (ui/handlers/chrome_profile.py); null = no flag (ruling F).
            # False = Chrome missing on macOS (ruling E: no webbrowser
            # fallback there — present the URL and let the user open it;
            # Windows always returns True).
            opened = plat.open_front(
                target.url, launcher_setting(config, 'chrome_profile', None))
            if not opened:
                notify(t('tray.chrome_missing_url', url=target.url))
        finally:
            self._lock.release()


# ---------------------------------------------------------------------------
# Single instance / command channel
# ---------------------------------------------------------------------------

class CommandServer:
    """
    Owns the tray_port. Owning the bind = being THE tray instance.
    Accepts newline-terminated commands ('open-front') from later launches.
    """

    def __init__(self, port: int, on_command):
        self._on_command = on_command
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        self._sock.bind(('127.0.0.1', port))  # raises OSError if occupied
        self._sock.listen(2)
        self._thread = threading.Thread(target=self._serve, name='command-server',
                                        daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return  # socket closed — shutting down
            try:
                with conn:
                    conn.settimeout(2.0)
                    command = conn.makefile('r', encoding='utf-8').readline().strip()
                    if command == 'ping':
                        # Identity handshake: lets a second launch distinguish
                        # a real tray instance from a foreign app on this port.
                        conn.sendall(b'ag-tray\n')
                if command and command != 'ping':
                    logger.info(f"Command received: {command}")
                    self._on_command(command)
            except Exception as e:
                logger.warning(f"Command handling failed: {e}")

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def forward_to_running_instance(port: int, command: str) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3.0) as conn:
            conn.sendall((command + '\n').encode('utf-8'))
        return True
    except OSError:
        return False


def is_ag_tray(port: int) -> bool:
    """True if the port owner answers the AG tray ping handshake.

    A bind failure alone does not mean another tray is running — any
    application could own the port. Without this check a foreign occupant
    made the tray silently exit as if an instance already existed.
    """
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3.0) as conn:
            conn.sendall(b'ping\n')
            conn.settimeout(3.0)
            return conn.makefile('r', encoding='utf-8').readline().strip() == 'ag-tray'
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Tray application
# ---------------------------------------------------------------------------

STATE_LABELS = {
    'running': t('tray.state.running'),
    'starting': t('tray.state.starting'),
    'stopped': t('tray.state.stopped'),
}
STATE_COLORS = {
    'running': (76, 175, 80),    # green
    'starting': (255, 193, 7),   # amber
    'stopped': (120, 120, 120),  # gray
}


def make_icon_image(state: str):
    """App logo (app_images/) with a state marker; fallback = drawn icon.

    win: coloured status dot (green/amber/gray) bottom-right.
    darwin: the menu bar icon is a template image, so colour cannot carry
    the state — the marker is a shape instead: running=● / starting=○ /
    stopped=none (稜裁定 2026-08-16).
    """
    from PIL import Image, ImageDraw
    size = 64
    try:
        image = Image.open(APP_IMAGES_LOGO).convert('RGBA').resize(
            (size, size), Image.LANCZOS)
    except Exception as e:
        logger.warning(f"Logo image unavailable ({e}) — using drawn fallback icon")
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([4, 4, size - 4, size - 4], fill=(233, 30, 99, 255))
    if sys.platform == 'darwin':
        _draw_state_mark_mono(image, state)
    else:
        _draw_state_dot_color(image, state)
    return image


# Marker geometry shared by both variants (bottom-right corner of the 64px logo)
_MARK_DOT = 22
_MARK_MARGIN = 2


def _draw_state_dot_color(image, state: str) -> None:
    """win: coloured status dot with a white rim so the state stays visible on the logo."""
    from PIL import ImageDraw
    size = image.size[0]
    color = STATE_COLORS.get(state, STATE_COLORS['stopped'])
    draw = ImageDraw.Draw(image)
    dot = _MARK_DOT
    x1, y1 = size - dot - _MARK_MARGIN, size - dot - _MARK_MARGIN
    draw.ellipse([x1, y1, x1 + dot, y1 + dot],
                 fill=color + (255,), outline=(255, 255, 255, 255), width=2)


def _draw_state_mark_mono(image, state: str) -> None:
    """darwin: alpha-only state marker for a template image.

    A transparent gap is punched around the marker first (ImageDraw writes
    pixels, no compositing) so it reads as a separate shape on the
    silhouette: running=filled disc, starting=ring, stopped=nothing.
    """
    if state not in ('running', 'starting'):
        return
    from PIL import ImageDraw
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    dot, gap = _MARK_DOT, 4
    x1, y1 = size - dot - _MARK_MARGIN, size - dot - _MARK_MARGIN
    draw.ellipse([x1 - gap, y1 - gap, x1 + dot + gap, y1 + dot + gap], fill=(0, 0, 0, 0))
    if state == 'running':
        draw.ellipse([x1, y1, x1 + dot, y1 + dot], fill=(255, 255, 255, 255))
    else:
        draw.ellipse([x1, y1, x1 + dot, y1 + dot], outline=(255, 255, 255, 255), width=5)


class TrayApp:
    def __init__(self):
        self.supervisor = BackendSupervisor(
            on_state_change=self._on_state_change,
            on_event=self._on_supervisor_event,
        )
        self.front_opener = FrontOpener(self.supervisor)
        self.icon = None

    def _on_supervisor_event(self, event: str) -> None:
        """
        Auto open/close of the front window (旧PS1/UIの挙動の踏襲):
          - front_reopen: .restart_flag 再起動(モード切替/再起動ボタン)後は
            旧画面を閉じて新モードの画面を開き直す
          - stopped_clean: シャットダウン後は画面も閉じる
          - stopped_crash: 画面は残す(エラーオーバーレイの証拠保全)
        """
        def work():
            if event == 'restart_pending':
                # 保険の再クローズ(冪等)。一次クローズは各メニューの受理直後
                # (on_restart/on_switch_mode等)。UIボタン発の再起動もここで拾う。
                plat.close_front()
            elif event == 'front_reopen':
                plat.close_front()
                # 旧ウィンドウ消滅とWS切断反映を待つ(前面化/接続数の誤爆防止)
                config = load_launch_config()
                control_port = launcher_setting(config, 'control_port', 7865)
                front_type = self.supervisor.target.front_client_type
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if not plat.find_front() and \
                            front_client_count(control_port, front_type) == 0:
                        break
                    time.sleep(0.5)
                self.front_opener.open_async(self.notify)
            elif event == 'stopped_clean':
                plat.close_front()
            elif event == 'stopped_crash':
                self.notify(t('tray.crashed'))
        threading.Thread(target=work, daemon=True, name='front-auto').start()

    # -- state / notifications ----------------------------------------------

    def _on_state_change(self, state: str) -> None:
        if self.icon is not None:
            self.icon.icon = make_icon_image(state)
            mode_label = t('tray.mode.server') if self.supervisor.target.mode == 'server' else t('tray.mode.local')
            title = f"Artificial Girlfriend — {STATE_LABELS.get(state, state)} ({mode_label})"
            if self.supervisor.status_note:
                title += f" / {self.supervisor.status_note}"
            self.icon.title = title
            self.icon.update_menu()

    def notify(self, message: str) -> None:
        logger.info(f"Notify: {message}")
        # pystray toast on both OSes. win: Shell_NotifyIcon balloon -> toast
        # under the AUMID identity (platform_win.platform_setup). darwin:
        # osascript `display notification` = Script Editor icon, title
        # 'Artificial Girlfriend' (稜裁定 2026-08-16: 固定). The applet relay
        # (cb34ab3, AG-icon identity) was retired: macOS silently dropped the
        # applet's banners — no permission prompt, no Settings entry — while
        # `open` still exited 0, so the tray could not detect it (Mac 実機
        # 2026-08-16). All 14 tray notifications funnel through here.
        if self.icon is None:
            return
        try:
            self.icon.notify(message, 'Artificial Girlfriend')
        except Exception:
            # 握り潰すと「通知が出ない」が無音になる(08-16 の教訓)
            logger.warning("Notification failed", exc_info=True)

    # -- menu actions ---------------------------------------------------------

    def on_open_front(self, _icon=None, _item=None):
        self.front_opener.open_async(self.notify)

    def _front_label(self, _item=None) -> str:
        return t('tray.menu.close') if plat.find_front() else t('tray.menu.open')

    def on_toggle_front(self, _icon=None, _item=None):
        # 左クリック(default項目)はグレーアウト中でも発火する(pystrayの
        # Menu.__call__はenabledを見ない)ため、ここでも状態で分岐する
        if plat.find_front():
            plat.close_front()
        elif self.supervisor.state == 'running':
            self.front_opener.open_async(self.notify)
        elif self.supervisor.state == 'starting':
            self.notify(t('tray.starting_notify'))
        else:
            self.notify(t('tray.open_needs_start'))

    def on_restart(self, _icon=None, _item=None):
        def work():
            control_port = launcher_setting(load_launch_config(), 'control_port', 7865)
            if self.supervisor.state == 'running':
                result = control_request(control_port, '/control/restart', method='POST', timeout=5.0)
                if result and not result.get('accepted', False):
                    self.notify(t('tray.restart_blocked_extracting'))
                elif result is None:
                    self.notify(t('tray.no_response'))
                else:
                    # 受理された瞬間に画面を閉じる(restart_pending の再クローズは冪等)
                    plat.close_front()
            else:
                self.supervisor.start_backend()
        threading.Thread(target=work, daemon=True).start()

    def on_stop(self, _icon=None, _item=None):
        def work():
            control_port = launcher_setting(load_launch_config(), 'control_port', 7865)
            result = control_request(control_port, '/control/shutdown', method='POST', timeout=5.0)
            if result and not result.get('accepted', False):
                self.notify(t('tray.stop_blocked_extracting'))
            elif result is None:
                self.notify(t('tray.no_response'))
            else:
                # 受理された瞬間に画面を閉じる(stopped_clean の再クローズは冪等)
                plat.close_front()
        threading.Thread(target=work, daemon=True).start()

    def on_start(self, _icon=None, _item=None):
        # 起動と同時に画面も開く（open_async が backend 起動→画面表示まで面倒を見る）
        self.front_opener.open_async(self.notify)

    # backend/server/tailscale.py の探索先と同一(フールプルーフのグレー条件)。
    # launcher は backend パッケージを import しない軽量プロセスのため複製する。
    _TAILSCALE_FALLBACK_PATHS = (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
    )

    def _tailscale_installed(self) -> bool:
        """Tailscale CLI の存在チェック(subprocess不使用・メニュー表示毎に評価)。"""
        if shutil.which('tailscale'):
            return True
        return any(Path(p).exists() for p in self._TAILSCALE_FALLBACK_PATHS)

    def _switch_mode_enabled(self, _item=None) -> bool:
        """モード切替メニューの有効条件。

        サーバー→ローカル方向は Tailscale 不要なので絶対にグレーにしない。
        ローカル→サーバー方向のみ「未インストール=グレー」(インストール済みで
        未起動は従来どおり押下時の制御API拒否が案内する=稜裁定 2026-07-25)。
        """
        if self.supervisor.state != 'running':
            return False
        return self.supervisor.target.mode == 'server' or self._tailscale_installed()

    def _switch_mode_label(self, _item=None) -> str:
        if self.supervisor.target.mode == 'server':
            return t('tray.switch_to_local')
        return t('tray.switch_to_server')

    def on_switch_mode(self, _icon=None, _item=None):
        def work():
            to_server = self.supervisor.target.mode != 'server'
            label = t('tray.mode.server') if to_server else t('tray.mode.local')
            if not plat.confirm(t('tray.switch_confirm', mode=label)):
                return
            control_port = launcher_setting(load_launch_config(), 'control_port', 7865)
            result = control_request(
                control_port, '/control/switch_mode', method='POST',
                timeout=30.0,  # cert acquisition can take a while
                body={'server_mode': to_server},
            )
            if result is None:
                self.notify(t('tray.no_response'))
            elif not result.get('accepted', False):
                self.notify(t('tray.switch_rejected', reason=result.get('reason', t('tray.unknown_error'))))
            else:
                # 受理された瞬間に画面を閉じる=ユーザーに「動いている」が見える。
                # 再起動完了後の開き直しは front_reopen イベントが行う。
                plat.close_front()
        threading.Thread(target=work, daemon=True).start()

    def on_quit(self, icon=None, _item=None):
        def work():
            if self.supervisor.state != 'stopped':
                if not plat.confirm(t('tray.quit_confirm')):
                    return
                control_port = launcher_setting(load_launch_config(), 'control_port', 7865)
                result = control_request(control_port, '/control/shutdown', method='POST', timeout=5.0)
                if result is None:
                    # Control API unreachable — backend is likely still 'starting'
                    # (control server not up yet). Graceful shutdown can't be
                    # delivered, so kill the process directly if we own it,
                    # otherwise it survives with no supervisor after the tray exits.
                    if self.supervisor.force_kill():
                        logger.warning("Control API unreachable; force-killed starting backend")
                    plat.close_front()
                elif not result.get('accepted', False):
                    self.notify(t('tray.quit_blocked_extracting'))
                    return
                else:
                    plat.close_front()  # 受理された瞬間に画面を閉じる
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline and self.supervisor.state != 'stopped':
                        time.sleep(0.5)
            self.supervisor.quit()
            if self.icon is not None:
                self.icon.stop()
        threading.Thread(target=work, daemon=True).start()

    # -- assembly -------------------------------------------------------------

    def build_menu(self):
        import pystray
        return pystray.Menu(
            # 画面が開いていれば「閉じる」・停止中(画面なし)はグレーアウトして
            # 「AG を起動」へ誘導。クラッシュ時は停止中でも画面が残る(証拠保全)
            # ため、画面がある限り「閉じる」は有効にする。
            pystray.MenuItem(self._front_label, self.on_toggle_front, default=True,
                             enabled=lambda item: self.supervisor.state == 'running'
                             or bool(plat.find_front())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t('tray.menu.restart'), self.on_restart,
                             enabled=lambda item: self.supervisor.state == 'running'),
            pystray.MenuItem(t('tray.menu.stop'), self.on_stop,
                             visible=lambda item: self.supervisor.state != 'stopped'),
            pystray.MenuItem(t('tray.menu.start'), self.on_start,
                             visible=lambda item: self.supervisor.state == 'stopped'),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._switch_mode_label, self.on_switch_mode,
                             enabled=self._switch_mode_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t('tray.menu.quit'), self.on_quit),
        )

    def run(self, open_front: bool) -> None:
        import pystray
        self.icon = pystray.Icon(
            'ArtificialGirlfriend',
            make_icon_image('stopped'),
            'Artificial Girlfriend',
            menu=self.build_menu(),
        )

        def setup(icon):
            icon.visible = True
            # darwin: hide Dock icon + start the menu-refresh republisher
            # (win: no-op). Must run after the icon exists.
            plat.post_icon_setup(icon)
            self.supervisor.start()
            self.supervisor.start_backend()
            if open_front:
                self.on_open_front()

        self.icon.run(setup=setup)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / 'tray.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8',
    )
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main(argv) -> int:
    setup_logging()
    # OS-specific startup fixes (Windows: toast identity + pystray patches).
    # Must precede any window creation (tray message window).
    plat.platform_setup('Artificial Girlfriend', APP_IMAGES_LOGO_ICO)
    open_front = '--open-front' in argv
    config = load_launch_config()
    tray_port = launcher_setting(config, 'tray_port', 7866)

    app = TrayApp()

    def on_command(command: str):
        if command == 'open-front':
            app.on_open_front()

    try:
        command_server = CommandServer(tray_port, on_command)
    except OSError:
        if is_ag_tray(tray_port):
            # Another tray instance owns the port — forward and exit
            logger.info("Tray already running — forwarding command")
            if open_front:
                forward_to_running_instance(tray_port, 'open-front')
            return 0
        logger.error(f"tray_port {tray_port} is occupied by another application")
        plat.alert(
            t('tray.port_conflict', port=tray_port)
        )
        return 1

    try:
        app.run(open_front)
    finally:
        command_server.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        logging.getLogger('tray').critical(f"Tray launcher crashed: {e}", exc_info=True)
        plat.alert(t('tray.crash_box', error=e, log=LOG_DIR / 'tray.log'))
        sys.exit(1)
