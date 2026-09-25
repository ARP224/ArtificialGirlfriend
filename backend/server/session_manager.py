"""
backend/server/session_manager.py

Manages remote session state for server mode.
Enforces primary + companion session model for desktop/mobile clients.
"""

import asyncio
import json
import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from backend.shared.awake_clock import awake_seconds

logger = logging.getLogger(__name__)


# WebSocket close codes (Phase 2A)
# 4xxx is the application-defined range; 1000 is normal closure.
CLOSE_CODE_NORMAL = 1000
CLOSE_CODE_PRIMARY_OCCUPIED = 4001
CLOSE_CODE_FORCE_DISCONNECT = 4002
CLOSE_CODE_TIMEOUT = 4003
# ST-F: local-mode "last one wins" — the old front is superseded by a new one
CLOSE_CODE_SUPERSEDED = 4004
# Liveness monitor: session silent (no pong) beyond threshold. Deliberately NOT
# in the clients' no-reconnect maps — a client that receives this must
# auto-reconnect (it was alive after all; token resume restores the session).
CLOSE_CODE_LIVENESS_LOST = 4005
# Companion closed because the primary session ended. 4002 is admin-kick ONLY:
# clients render it as 「管理者により切断されました」, which is a lie for the
# primary-left and stale-replace cases (稜実機 2026-07-30) — those use this
# code and CLOSE_CODE_SUPERSEDED respectively.
CLOSE_CODE_COMPANION_PRIMARY_LEFT = 4006
# The client pressed its own 接続解除 button (desktop/mobile System page).
# Same release flow as an admin kick, but the client must NOT be told
# 「管理者により切断されました」 about its own action (稜実機 2026-07-30).
CLOSE_CODE_SELF_DISCONNECT = 4007

# Canonical short reason strings (WS close `reason` is limited to 123 bytes).
CLOSE_REASON_MAP = {
    CLOSE_CODE_NORMAL: "",
    CLOSE_CODE_PRIMARY_OCCUPIED: "primary_occupied",
    CLOSE_CODE_FORCE_DISCONNECT: "force_disconnect",
    CLOSE_CODE_TIMEOUT: "timeout",
    CLOSE_CODE_SUPERSEDED: "superseded",
    CLOSE_CODE_LIVENESS_LOST: "liveness_lost",
    CLOSE_CODE_COMPANION_PRIMARY_LEFT: "primary_left",
    CLOSE_CODE_SELF_DISCONNECT: "self_disconnect",
}

# Phase 2B: grace period for primary reconnection (seconds)
GRACE_PERIOD_SECONDS = 60

# Liveness monitor: the server-mode WS path (FastAPI /ws) sends no protocol
# pings, so a page killed without a close frame (mobile tab kill, suspend,
# NAT death) is undetectable by recv alone. The monitor thread sends an
# app-level ping every LIVENESS_PING_INTERVAL_SECONDS; a session that stays
# silent for LIVENESS_TIMEOUT_SECONDS (= 3 missed pongs) is treated as a
# network disconnect (grace path, NOT immediate release — pending sessions
# yield to new clients via preempt, so nothing is ever blocked by grace).
LIVENESS_PING_INTERVAL_SECONDS = 15
LIVENESS_TIMEOUT_SECONDS = 45


@dataclass
class RemoteSession:
    """Active remote session state."""
    active: bool = False
    device_name: str = ""       # Tailscale HostName
    client_ip: str = ""
    client_type: str = ""       # "desktop" or "mobile" (Phase 2B: needed for reactivation matching)
    connected_at: float = 0.0
    last_activity: float = 0.0
    # スリープ耐性: last_activity の awake時計版（_timeout_loop のアイドル判定専用）。
    # 表示(to_dict/idle_seconds)はwallのまま、判定だけスリープ除外で行う。
    last_activity_awake: float = 0.0
    websocket: Any = None
    # Phase 2B: grace period state for primary reconnection
    pending_reconnect: bool = False
    pending_reconnect_started_at: float = 0.0
    # スリープ耐性: pending_reconnect_started_at の awake時計版（グレース判定専用）。
    pending_reconnect_started_awake: float = 0.0
    # Phase 3A: server-issued session token. Stable across reactivation/stale-replace.
    # Phase 3D switches Case 1/3 matching from client_ip+client_type to this token.
    session_token: str = ""
    # Liveness: awake-clock time of the last WS sign of life (pong/ping received).
    # Separate from last_activity — liveness must NOT extend the idle timeout.
    ws_liveness_awake: float = 0.0

    def touch(self) -> None:
        """Update last_activity timestamp."""
        self.last_activity = time.time()
        self.last_activity_awake = awake_seconds()

    def duration_seconds(self) -> float:
        """Seconds since connection started."""
        if not self.active or self.connected_at == 0:
            return 0.0
        return time.time() - self.connected_at

    def idle_seconds(self) -> float:
        """Seconds since last activity."""
        if not self.active or self.last_activity == 0:
            return 0.0
        return time.time() - self.last_activity

    def to_dict(self) -> dict:
        """Serialize to dict (excluding websocket)."""
        return {
            "active": self.active,
            "device_name": self.device_name,
            "client_ip": self.client_ip,
            "client_type": self.client_type,
            "connected_at": self.connected_at,
            "last_activity": self.last_activity,
            "duration_seconds": self.duration_seconds(),
            "idle_seconds": self.idle_seconds(),
            # Phase 2B: pending_reconnect state for admin UI
            "pending_reconnect": self.pending_reconnect,
            "pending_reconnect_until": (
                self.pending_reconnect_started_at + GRACE_PERIOD_SECONDS
                if self.pending_reconnect else None
            ),
        }


class SessionManager:
    """
    Manages remote connection sessions for server mode.

    Supports two session slots:
      - primary: the main desktop/mobile client that drives the conversation
      - companion: a secondary mobile client for camera-only mode

    Thread safety: All state is protected by threading.RLock.
    Three access paths:
      (1) WS async loop — handler identify/disconnect
      (2) Gradio HTTP handlers — touch_activity()
      (3) Timeout monitor thread — periodic idle check
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._primary_session = RemoteSession()
        self._companion_session = RemoteSession()
        self._server_mode = False
        self._timeout_minutes = 60
        self._on_session_end_callbacks: list[Callable] = []
        self._timeout_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Liveness: awake-clock time of the last server→client ping round
        self._last_liveness_ping_awake = 0.0

    def configure(self, server_mode: bool, timeout_minutes: int) -> None:
        """Configure and start timeout monitoring thread."""
        self._server_mode = server_mode
        self._timeout_minutes = timeout_minutes
        if server_mode:
            self._stop_event.clear()
            self._timeout_thread = threading.Thread(
                target=self._timeout_loop,
                name="session-timeout",
                daemon=True,
            )
            self._timeout_thread.start()
            logger.info(
                f"[Session] Configured: timeout={timeout_minutes}min"
            )

    def try_claim_session(self, ws, client_ip: str, client_type: str,
                          session_token: Optional[str] = None) -> Tuple[bool, str, str]:
        """
        Attempt to claim a session slot.

        Phase 2B/2C: handles four cases:
          1. Reactivation:  pending_reconnect primary with matching ip/type → resume
          2. Preempt:       pending_reconnect primary with mismatched ip/type → discard pending, claim new
          3. Stale-replace: ACTIVE primary with matching ip/type → assume old WS is stale,
                            replace it with the new WS in-place (preserves session, history, callbacks).
                            Triggered when client reconnects before TCP keepalive surfaces the dead old WS
                            (typical Wi-Fi toggle scenario).
          4. Existing:      primary available / occupied (different device) → grant primary / companion / block

        Phase 3A/3D: returns the session_token (issued on new grant, preserved
        on reactivation/stale-replace). Case 1/3 match on the ``session_token``
        parameter (Phase 3D), which prevents NAT-mate hijack and treats a
        browser refresh as a fresh session.

        Returns:
            (True, "primary",  token) — granted as primary
            (True, "companion", token) — granted as companion (auto-promoted mobile)
            (False, "blocked", "")    — rejected
        """
        if not self._server_mode:
            return (True, "primary", "")

        # admin and motion_pngtuber bypass session limits — no token issued
        # (admin/motion_pngtuber are not subject to seq stamping or replay)
        if client_type in ("admin", "motion_pngtuber"):
            return (True, "primary", "")

        cleanup_to_process = None  # set when preempting a pending session
        stale_ws_to_close = None   # set when replacing a stale active WS (Case 3)
        result: Tuple[bool, str, str] = (False, "blocked", "")
        notify_admin = False
        resolve_name_token = ""    # set when a new grant needs async device-name resolution

        with self._lock:
            primary = self._primary_session

            # Case 1 / 2: pending_reconnect handling (Phase 3D: session_token based)
            if primary.active and primary.pending_reconnect:
                if session_token and primary.session_token == session_token:
                    # Case 1: reactivate. Token match means same browser session
                    # (not just same NAT IP / device type), so reactivation is
                    # safe across NAT-mate scenarios and identifies refreshes
                    # correctly as new sessions.
                    primary.pending_reconnect = False
                    primary.pending_reconnect_started_at = 0.0
                    primary.pending_reconnect_started_awake = 0.0
                    primary.websocket = ws
                    primary.last_activity = time.time()
                    primary.last_activity_awake = awake_seconds()
                    primary.ws_liveness_awake = awake_seconds()
                    logger.info(
                        f"[Session] Resumed pending_reconnect: "
                        f"{primary.device_name} (token={session_token[:8]})"
                    )
                    notify_admin = True
                    result = (True, "primary", primary.session_token)
                else:
                    # Case 2: preempt — token mismatch, missing token, or new
                    # client. Discard pending and fall through to new claim.
                    logger.info(
                        f"[Session] Pending primary preempted by new client: "
                        f"{primary.device_name} → {client_ip} ({client_type})"
                    )
                    primary.pending_reconnect = False
                    primary.pending_reconnect_started_at = 0.0
                    primary.pending_reconnect_started_awake = 0.0
                    cleanup_to_process = self._do_actual_release(primary)
                    # Fall through to Case 4 (primary slot is now free)

            # Case 3: stale-replace — same session_token reconnecting before TCP
            # timeout surfaced the old WS as dead. Token-based match prevents
            # NAT-mate hijack and treats browser refresh as a fresh session.
            elif (primary.active and not primary.pending_reconnect and
                    session_token and primary.session_token == session_token and
                    primary.websocket is not ws):
                stale_ws_to_close = primary.websocket
                primary.websocket = ws
                primary.last_activity = time.time()
                primary.last_activity_awake = awake_seconds()
                primary.ws_liveness_awake = awake_seconds()
                logger.info(
                    f"[Session] Stale-replace: same session_token "
                    f"(token={session_token[:8]}) reconnected, swapping WS in-place"
                )
                notify_admin = True
                result = (True, "primary", primary.session_token)

            # Case 4: standard claim flow (primary slot may be free now)
            if result == (False, "blocked", ""):
                if not self._primary_session.active:
                    now = time.time()
                    now_awake = awake_seconds()
                    # tailscale whois(最大10s)を _lock 保持中+イベントループスレッド上で
                    # 実行すると全WSと touch_activity/_timeout_loop が連鎖ブロックする。
                    # まず client_ip を仮名で即時グラントし、名前解決は別スレッドで行う。
                    device_name = client_ip
                    new_token = uuid.uuid4().hex
                    resolve_name_token = new_token
                    self._primary_session = RemoteSession(
                        active=True,
                        device_name=device_name,
                        client_ip=client_ip,
                        client_type=client_type,
                        connected_at=now,
                        last_activity=now,
                        last_activity_awake=now_awake,
                        websocket=ws,
                        session_token=new_token,
                        ws_liveness_awake=now_awake,
                    )
                    logger.info(
                        f"[Session] Primary granted to {device_name} ({client_ip}) "
                        f"as {client_type} (token={new_token[:8]})"
                    )
                    notify_admin = True
                    result = (True, "primary", new_token)
                elif client_type == "mobile" and not self._companion_session.active:
                    # Primary occupied — check companion eligibility
                    now = time.time()
                    now_awake = awake_seconds()
                    # 名前解決はロック外・別スレッド(primary グラント側と同じ理由)
                    device_name = client_ip
                    new_token = uuid.uuid4().hex
                    resolve_name_token = new_token
                    self._companion_session = RemoteSession(
                        active=True,
                        device_name=device_name,
                        client_ip=client_ip,
                        client_type=client_type,
                        connected_at=now,
                        last_activity=now,
                        last_activity_awake=now_awake,
                        websocket=ws,
                        session_token=new_token,
                        ws_liveness_awake=now_awake,
                    )
                    logger.info(
                        f"[Session] Companion granted to {device_name} ({client_ip}) "
                        f"(token={new_token[:8]})"
                    )
                    notify_admin = True
                    result = (True, "companion", new_token)
                else:
                    logger.info(
                        f"[Session] Rejected {client_type} from {client_ip} "
                        f"— active session by {self._primary_session.device_name}"
                    )
                    notify_admin = True
                    result = (False, "blocked", "")

        # Lock released — execute side effects
        if stale_ws_to_close is not None:
            # Old WS is presumed dead (TCP not yet drained). Best-effort close;
            # if the old WS happens to still be alive (e.g. user opened a 2nd
            # tab), the old tab shows the "opened in another window" overlay
            # (SUPERSEDED — not FORCE_DISCONNECT, which reads as an admin kick).
            self._force_close_ws(
                stale_ws_to_close,
                "新しい接続に切り替わりました",
                close_code=CLOSE_CODE_SUPERSEDED,
            )
        if cleanup_to_process:
            # _execute_cleanup に一本化。旧インライン複製は session_end コールバック
            # (stop_conversation 等の重い処理)だけを preempt 時のみイベントループ上で
            # 同期実行しており、新クライアントの identify 応答とループ全体を固めていた。
            self._execute_cleanup(cleanup_to_process)
        if notify_admin:
            self._notify_admin_session_update()
        if resolve_name_token and result[0]:
            threading.Thread(
                target=self._resolve_device_name_async,
                args=(resolve_name_token, client_ip),
                name="session-name-resolve",
                daemon=True,
            ).start()
        return result

    def release_session(self, websocket: Any = None, reason: str = "unknown") -> None:
        """
        Release a session slot.

        Phase 2B grace period behavior:
        - reason="disconnect" + primary not yet pending → enter grace period (60s)
        - any other case (companion disconnect, timeout, force, grace-expired,
          or duplicate disconnect on already-pending primary) → immediate release.

        If websocket=None, only timeout / grace-expiry / internal callers can
        invoke this; the call operates on the active primary if any.
        """
        cleanup = None
        grace_entered = False
        device = ""

        with self._lock:
            # Identify target session
            if websocket is not None:
                session = self._find_session_by_websocket(websocket)
                if session is None:
                    return  # already released or never held
            else:
                if not self._primary_session.active:
                    return
                session = self._primary_session

            device = session.device_name
            is_primary = session is self._primary_session

            if (is_primary and reason == "disconnect"
                    and not session.pending_reconnect):
                # Phase 2B: enter grace period instead of immediate release
                session.pending_reconnect = True
                session.pending_reconnect_started_at = time.time()
                session.pending_reconnect_started_awake = awake_seconds()
                session.websocket = None  # avoid dangling ref in broadcasts
                grace_entered = True
            else:
                cleanup = self._do_actual_release(session)

        # Lock released — execute side effects
        if grace_entered:
            logger.info(
                f"[Session] Primary entered pending_reconnect: "
                f"{device} (grace {GRACE_PERIOD_SECONDS}s)"
            )
        if cleanup:
            mode = cleanup["released_mode"]
            logger.info(
                f"[Session] {mode.capitalize()} released ({reason}): {device}"
            )
            self._execute_cleanup(cleanup)
        self._notify_admin_session_update()

    def note_ws_alive(self, websocket: Any) -> None:
        """Record a WS sign of life (pong for our liveness ping, or a
        client-initiated ping). Deliberately separate from touch_activity():
        liveness must not extend the 60-min idle timeout or the admin idle
        display — it only proves the page is still running.
        """
        if not self._server_mode:
            return
        with self._lock:
            session = self._find_session_by_websocket(websocket)
            if session is not None:
                session.ws_liveness_awake = awake_seconds()

    def touch_activity(self) -> None:
        """Update last_activity on the primary session. Called from Gradio handlers."""
        if not self._server_mode:
            return
        with self._lock:
            if self._primary_session.active:
                self._primary_session.touch()
        # Fire-and-forget admin notification
        self._notify_admin_session_update()

    def has_primary_session(self) -> bool:
        """Check if a primary session is currently active."""
        with self._lock:
            return self._primary_session.active

    def get_active_session_tokens(self) -> set:
        """Return set of active session_tokens (including pending_reconnect sessions).

        Phase 3A: used by WebSocketManager.broadcast() so replay/snapshot tracking
        keeps running for sessions whose ws is currently disconnected — without
        this, messages sent during the grace window would never reach the buffer.
        """
        with self._lock:
            tokens = set()
            if self._primary_session.active and self._primary_session.session_token:
                tokens.add(self._primary_session.session_token)
            if self._companion_session.active and self._companion_session.session_token:
                tokens.add(self._companion_session.session_token)
            return tokens

    def force_disconnect(
        self,
        close_code: int = CLOSE_CODE_FORCE_DISCONNECT,
        message: str = "管理者によって接続が切断されました",
    ) -> bool:
        """
        Disconnect the active remote session.

        Default = admin kick (4002). The client-side 接続解除 buttons pass
        CLOSE_CODE_SELF_DISCONNECT so their own window shows 「接続を解除
        しました」 instead of an admin-kick overlay (稜実機 2026-07-30).

        Phase 2B: handles two paths
        - pending_reconnect primary: ws is None, release directly
        - active primary: close ws (close_code), then release directly so the
          handler's finally cannot enter grace period.

        Returns True if a session was disconnected.
        """
        cleanup = None
        ws_to_close = None
        device = ""

        with self._lock:
            primary = self._primary_session
            if not primary.active:
                return False
            device = primary.device_name
            if primary.pending_reconnect:
                primary.pending_reconnect = False
            else:
                ws_to_close = primary.websocket
            cleanup = self._do_actual_release(primary)

        # Lock released — side effects
        if ws_to_close is not None:
            self._force_close_ws(ws_to_close, message, close_code=close_code)
        if cleanup:
            logger.info(f"[Session] Primary released (force_disconnect): {device}")
            self._execute_cleanup(cleanup)
        self._notify_admin_session_update()
        logger.info(f"[Session] Force disconnected: {device}")
        return True

    def get_status(self) -> dict:
        """Get current session status."""
        with self._lock:
            return {
                "primary_session": self._primary_session.to_dict(),
                "companion_session": self._companion_session.to_dict(),
                # Backward compat: keep "session" key for admin UI
                "session": self._primary_session.to_dict(),
                "timeout_minutes": self._timeout_minutes,
            }

    def register_session_end_callback(self, callback: Callable) -> None:
        """Register a callback to be called when a session ends."""
        self._on_session_end_callbacks.append(callback)

    def stop(self) -> None:
        """Stop the timeout monitoring thread."""
        self._stop_event.set()
        if self._timeout_thread and self._timeout_thread.is_alive():
            self._timeout_thread.join(timeout=5.0)
        logger.info("[Session] Stopped")

    # --- Internal methods ---

    def _find_session_by_websocket(self, ws):
        """Locked helper: find the session holding the given websocket.

        Caller must hold self._lock. Returns RemoteSession or None.
        Pending sessions have websocket=None and won't match here.
        """
        if (self._primary_session.active and
                self._primary_session.websocket is ws):
            return self._primary_session
        if (self._companion_session.active and
                self._companion_session.websocket is ws):
            return self._companion_session
        return None

    def _all_active_sessions(self) -> list:
        """Locked helper: return list of active sessions (primary + companion).

        Caller must hold self._lock.
        """
        result = []
        if self._primary_session.active:
            result.append(self._primary_session)
        if self._companion_session.active:
            result.append(self._companion_session)
        return result

    def _do_actual_release(self, session) -> dict:
        """Locked helper: clear session state, return cleanup info for post-lock execution.

        Caller must hold self._lock. State changes only — companion close,
        callbacks, admin broadcast, and replay-buffer drop are performed after
        lock release via _execute_cleanup() and _notify_admin_session_update().

        Phase 3D: returns ``released_tokens`` so _execute_cleanup can drop the
        WebSocketManager replay/snapshot/seq state for ended sessions. When
        primary is released alongside a companion, both tokens are reported.
        """
        cleanup = {
            "companion_ws_to_close": None,
            "callbacks_to_fire": [],
            "released_mode": "",
            "device": session.device_name,
            "released_tokens": [],  # Phase 3D
        }
        if session is self._primary_session:
            cleanup["released_mode"] = "primary"
            if session.session_token:
                cleanup["released_tokens"].append(session.session_token)
            # Companion has no grace period — release alongside primary
            if (self._companion_session.active and
                    not self._companion_session.pending_reconnect):
                cleanup["companion_ws_to_close"] = self._companion_session.websocket
                if self._companion_session.session_token:
                    cleanup["released_tokens"].append(
                        self._companion_session.session_token
                    )
                self._companion_session = RemoteSession()
            cleanup["callbacks_to_fire"] = list(self._on_session_end_callbacks)
            self._primary_session = RemoteSession()
        elif session is self._companion_session:
            cleanup["released_mode"] = "companion"
            if session.session_token:
                cleanup["released_tokens"].append(session.session_token)
            self._companion_session = RemoteSession()
        return cleanup

    def _execute_cleanup(self, cleanup: dict):
        """Execute side effects of a session release. Caller must NOT hold lock."""
        if cleanup["companion_ws_to_close"]:
            self._force_close_ws(
                cleanup["companion_ws_to_close"],
                "プライマリが切断されました",
                close_code=CLOSE_CODE_COMPANION_PRIMARY_LEFT,
            )
            logger.info("[Session] Companion force-disconnected (primary released)")
        # Phase 3D: drop replay/snapshot/seq state for ended sessions
        for tok in cleanup.get("released_tokens", []):
            try:
                from backend.server.websocket_server import get_websocket_manager
                get_websocket_manager()._drop_session_buffer(tok)
            except Exception as e:
                logger.warning(f"[Session] Failed to drop replay buffer: {e}")
        if cleanup["callbacks_to_fire"]:
            callbacks = cleanup["callbacks_to_fire"]

            def _run():
                for cb in callbacks:
                    try:
                        cb()
                    except Exception as e:
                        logger.error(f"[Session] Callback error: {e}")

            threading.Thread(
                target=_run, name="session-end-callbacks", daemon=True
            ).start()

    def _force_close_ws(self, ws, message: str, close_code: int = CLOSE_CODE_NORMAL):
        """Send force_disconnected message and close a WebSocket with a close code.

        The JSON force_disconnected message is sent for backward compatibility
        with clients that don't yet handle the close code (may be phased out
        once no such clients remain).

        In-loop callers (e.g. stale-replace from try_claim_session, which runs
        on the asyncio handler thread) cannot block on future.result(): the
        scheduled coroutine can't run until the loop thread is released, so
        the wait would deadlock for the full timeout (5s) every time. Detect
        the loop-thread case and schedule fire-and-forget instead.
        """
        from backend.server.websocket_server import get_websocket_manager
        mgr = get_websocket_manager()
        if not mgr.loop or mgr.loop.is_closed():
            return

        async def _send_and_close():
            try:
                await ws.send(json.dumps({
                    "type": "force_disconnected",
                    "message": message
                }))
            except Exception:
                pass
            try:
                reason = CLOSE_REASON_MAP.get(close_code, "")
                await ws.close(code=close_code, reason=reason)
            except Exception:
                pass

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is mgr.loop:
            # Same thread as the loop — fire-and-forget so we don't deadlock.
            # The scheduled coroutine runs after the current callback yields.
            try:
                asyncio.ensure_future(_send_and_close(), loop=mgr.loop)
            except Exception as e:
                logger.error(f"[Session] force_close_ws schedule error: {e}")
        else:
            # Cross-thread (timeout monitor, admin force-disconnect, Gradio
            # HTTP handler) — safe to block on the future.
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _send_and_close(), mgr.loop
                )
                future.result(timeout=5.0)
            except Exception as e:
                logger.error(f"[Session] force_close_ws error: {e}")

    def _lookup_device_name(self, client_ip: str) -> str:
        """
        Look up Tailscale device name by IP using ``tailscale whois``.
        """
        try:
            from backend.server.tailscale import resolve_tailscale_cli
            tailscale_exe = resolve_tailscale_cli()
            result = subprocess.run(
                [tailscale_exe, "whois", "--json", client_ip],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10
            )
            if result.returncode != 0:
                logger.warning(
                    f"[Session] tailscale whois failed for {client_ip} "
                    f"(exit {result.returncode}): {result.stderr}"
                )
                return client_ip

            data = json.loads(result.stdout)
            node = data.get("Node", {})
            name = node.get("ComputedName", "") or node.get("Name", "")
            if name:
                # Remove trailing dot from FQDN if present
                name = name.rstrip(".")
                logger.info(
                    f"[Session] Device name resolved: "
                    f"{client_ip} -> {name}"
                )
                return name
        except Exception as e:
            logger.warning(f"[Session] Device name lookup failed: {e}")

        return client_ip

    def _resolve_device_name_async(self, session_token: str, client_ip: str) -> None:
        """Resolve the device name in a worker thread and update the session.

        グラント時は client_ip を仮名として即時応答し、tailscale whois の結果が
        得られたらセッションがまだ同一トークンで生きている場合のみ差し替えて
        admin へ再通知する(解決前に切断/差し替えされていたら何もしない)。
        """
        name = self._lookup_device_name(client_ip)
        if not name or name == client_ip:
            return
        updated = False
        with self._lock:
            for session in (self._primary_session, self._companion_session):
                if session.active and session.session_token == session_token:
                    session.device_name = name
                    updated = True
                    break
        if updated:
            logger.info(f"[Session] Device name updated: {client_ip} -> {name}")
            self._notify_admin_session_update()

    def _timeout_loop(self):
        """Periodic session monitor (Phase 2B: 5s tick).

        Tick interval shortened from 60s to 5s so grace expiry detection
        is precise (max 5s lag instead of 60s, which would render grace
        design effectively useless). Lock-internal work is minimal
        (read dataclass fields, iterate <=2 sessions). Idle path is
        a no-op when nothing crosses the threshold.
        """
        while not self._stop_event.wait(timeout=5.0):
            self._tick()

    def _tick(self):
        """One monitor pass. Split from _timeout_loop so tests can drive it
        directly with a patched awake clock (no thread / no sleep).

        Three responsibilities:
        1. grace expiry (pending_reconnect primary): release if pending > GRACE_PERIOD_SECONDS
        2. liveness (active sessions with a WS, primary + companion):
           server→client app-level ping every LIVENESS_PING_INTERVAL_SECONDS;
           a session silent for LIVENESS_TIMEOUT_SECONDS is treated as a
           network disconnect (grace path) and its WS closed with 4005.
           Catches pages killed without a close frame (mobile tab kill,
           suspend, NAT death) — the FastAPI /ws path sends no protocol pings.
        3. idle timeout (primary only): release if idle > timeout_minutes
        """
        cleanups_to_process = []  # list of (cleanup, ws_to_close, reason, value)
        notify_admin = False
        liveness_dead = []        # list of (ws, device, silent_seconds)
        pings_to_send = []

        with self._lock:
            # スリープ耐性: グレース/アイドル/死活判定は awake時計（スリープ除外）で行う。
            now_awake = awake_seconds()
            idle_limit = self._timeout_minutes * 60
            send_pings = (
                now_awake - self._last_liveness_ping_awake
                >= LIVENESS_PING_INTERVAL_SECONDS
            )
            if send_pings:
                self._last_liveness_ping_awake = now_awake

            # Iterate over a snapshot since _do_actual_release mutates state
            for session in list(self._all_active_sessions()):
                # Grace period expiry (primary in pending_reconnect)
                if session.pending_reconnect:
                    elapsed = now_awake - session.pending_reconnect_started_awake
                    if elapsed >= GRACE_PERIOD_SECONDS:
                        session.pending_reconnect = False
                        cleanup = self._do_actual_release(session)
                        cleanups_to_process.append(
                            (cleanup, None, "grace_expired", elapsed)
                        )
                        notify_admin = True
                    continue
                # Liveness (primary + companion)
                ws = session.websocket
                if ws is not None and session.ws_liveness_awake > 0:
                    silent = now_awake - session.ws_liveness_awake
                    if silent >= LIVENESS_TIMEOUT_SECONDS:
                        # Released below via release_session (grace path) —
                        # NOT _do_actual_release, so a token reconnect within
                        # the grace window still resumes the session.
                        liveness_dead.append((ws, session.device_name, silent))
                        continue
                    if send_pings:
                        pings_to_send.append(ws)
                # Idle timeout (active sessions, primary only)
                if session is not self._primary_session:
                    continue
                idle = now_awake - session.last_activity_awake
                if idle >= idle_limit:
                    ws_to_close = session.websocket
                    cleanup = self._do_actual_release(session)
                    cleanups_to_process.append(
                        (cleanup, ws_to_close, "timeout", idle)
                    )
                    notify_admin = True

        # Lock released — side effects
        if pings_to_send:
            self._send_liveness_pings(pings_to_send)
        for ws, device, silent in liveness_dead:
            logger.info(
                f"[Session] Liveness lost ({silent:.0f}s silent): {device} "
                f"— treating as disconnect (grace)"
            )
            # Same path as a detected network disconnect. If the client was
            # actually alive (throttling edge case), the 4005 close makes it
            # auto-reconnect and resume via its session_token.
            self.release_session(websocket=ws, reason="disconnect")
            self._force_close_ws(
                ws, "応答がないため接続を解除しました",
                close_code=CLOSE_CODE_LIVENESS_LOST,
            )
        for cleanup, ws_to_close, reason, value in cleanups_to_process:
            device = cleanup["device"]
            mode = cleanup["released_mode"]
            if reason == "timeout":
                logger.info(
                    f"[Session] {mode.capitalize()} released (timeout, "
                    f"{value:.0f}s idle): {device}"
                )
                if ws_to_close is not None:
                    self._force_close_ws(
                        ws_to_close, "セッションタイムアウト",
                        close_code=CLOSE_CODE_TIMEOUT,
                    )
            elif reason == "grace_expired":
                logger.info(
                    f"[Session] {mode.capitalize()} released "
                    f"(pending_reconnect expired after {value:.1f}s): {device}"
                )
            self._execute_cleanup(cleanup)
        if notify_admin:
            self._notify_admin_session_update()

    def _send_liveness_pings(self, ws_list: list) -> None:
        """Send an app-level ping to each WS (fire-and-forget, cross-thread).

        Clients answer with {"action": "pong"} → note_ws_alive(). Send errors
        are ignored — a dead WS simply keeps missing pongs and the liveness
        scan reaps it.
        """
        try:
            from backend.server.websocket_server import get_websocket_manager
            mgr = get_websocket_manager()
        except Exception:
            return
        if not mgr.loop or mgr.loop.is_closed():
            return
        payload = json.dumps({"action": "ping", "timestamp": time.time()})

        async def _send(target_ws):
            try:
                await target_ws.send(payload)
            except Exception:
                pass

        for ws in ws_list:
            try:
                asyncio.run_coroutine_threadsafe(_send(ws), mgr.loop)
            except Exception:
                pass

    def _notify_admin_session_update(self):
        """Send session status update to admin clients (fire-and-forget)."""
        try:
            from backend.server.websocket_server import get_websocket_manager
            mgr = get_websocket_manager()
            status = self.get_status()
            message = {
                "type": "session_status_update",
                "session": status["session"],
                "companion_session": status["companion_session"],
                "timeout_minutes": status["timeout_minutes"],
                "timestamp": time.time(),
            }
            mgr.send_to_admin_sync(message)
        except Exception:
            pass

# Singleton
_session_manager: Optional[SessionManager] = None
_sm_lock = threading.Lock()


def get_session_manager() -> SessionManager:
    """Get the global SessionManager instance."""
    global _session_manager
    with _sm_lock:
        if _session_manager is None:
            _session_manager = SessionManager()
    return _session_manager
