"""
backend/shared/ambient_camera_state.py

Live Camera runtime state (shared/state layer).

Naming (稜裁定 2026-07-19): the feature's display name is Live Camera
(日本語UI: 常時カメラ). Internal identifiers keep the historical
"ambient" name (module/file names, AMBIENT_* constants, WS actions
ambient_frame/ambient_capture_request/ambient_camera_status/
set_ambient_camera/ambient_camera_display, persisted settings key
ambient_camera_enabled, DOM ids ambient-cam-*, locale key
utility.ambient) — renaming them would force a settings migration and
touch the WS protocol for zero user-visible gain.

Owns the client-camera handshake for the Live Camera feature: which
client currently provides frames, the single frame slot (overwritten per
capture, consumed per generation, never persisted to history), and the
request/wait plumbing between generation entry points and the WebSocket
transport.

Design (稜裁定 2026-07-15):
- Server-orchestrated: user-initiated entry points (voice stop, desktop /
  mobile / MotionPNGPlayer text send) call :func:`fire_capture_request`;
  the generation task calls :meth:`consume_frame_if_pending`. Paths that
  never fire (auto prompts, ELYTH, YouTube) pay **zero** wait — capture is
  opt-in per entry point by design.
- request_id matching: frames answering a superseded/expired request are
  rejected so a stale scene can never leak into a later turn.
- Circuit breaker: AMBIENT_TIMEOUT_DEGRADE_THRESHOLD consecutive timeouts
  degrade the provider (no more waiting) until the page re-announces via
  the ambient_camera_status action (WS reconnect / tab visibility).

Threading: begin/deliver run on the transport's asyncio thread, consume on
the generation worker thread — all state transitions are lock-guarded and
the hand-off uses a threading.Event (same shape as the browser-mic bridge).
"""

import logging
import os
import threading
import time
from typing import Optional, Tuple

from backend.shared.constants import (
    AMBIENT_FRAME_MAX_AGE,
    AMBIENT_TIMEOUT_DEGRADE_THRESHOLD,
)
from backend.shared.ui_events import publish_ui_update

logger = logging.getLogger(__name__)


class AmbientCameraState:
    """Provider availability + single-slot frame hand-off for Live Camera."""

    def __init__(self):
        self._lock = threading.Lock()
        self.provider_available = False      # a page announced camera ON
        self.degraded = False                # circuit breaker open
        self.consecutive_timeouts = 0
        self._request_seq = 0
        self._active_request_id: Optional[str] = None
        self._request_started_at: float = 0.0
        self._frame_event = threading.Event()
        self._frame_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Provider lifecycle (transport side)
    # ------------------------------------------------------------------

    def set_provider_available(self, available: bool) -> None:
        """Page announced camera ON/OFF. Re-announce resets the breaker."""
        with self._lock:
            self.provider_available = available
            self.degraded = False
            self.consecutive_timeouts = 0
            if not available:
                self._active_request_id = None
                self._frame_path = None
                self._frame_event.clear()
        logger.info(f"[LiveCamera] Provider available={available}")

    # ------------------------------------------------------------------
    # Capture request lifecycle
    # ------------------------------------------------------------------

    def begin_request(self) -> Optional[str]:
        """Claim a new capture request.

        Returns the request_id, or None when no provider is available /
        the breaker is open. If a fresh request is already in flight its id
        is returned (double-fire from a race is harmless — one frame
        answers). A stranded request older than AMBIENT_FRAME_MAX_AGE (its
        turn was rejected after firing) is superseded and its
        delivered-but-unconsumed frame, if any, is deleted so stale scenery
        cannot leak into a later turn.
        """
        stale_path = None
        with self._lock:
            if not self.provider_available or self.degraded:
                return None
            if self._active_request_id is not None:
                age = time.monotonic() - self._request_started_at
                if age <= AMBIENT_FRAME_MAX_AGE:
                    return self._active_request_id
                stale_path = self._frame_path
                logger.info(f"[LiveCamera] Superseding stranded request "
                            f"{self._active_request_id} (age {age:.1f}s)")
            self._request_seq += 1
            request_id = f"amb-{self._request_seq}"
            self._active_request_id = request_id
            self._request_started_at = time.monotonic()
            self._frame_event.clear()
            self._frame_path = None
        _unlink_quiet(stale_path)
        return request_id

    def cancel_request(self, request_id: str) -> None:
        """Release a claim that could not be sent (transport failure).

        Not counted as a timeout — the breaker only tracks real waits.
        """
        with self._lock:
            if self._active_request_id == request_id:
                self._active_request_id = None
                self._frame_path = None
                self._frame_event.clear()

    def deliver_frame(self, request_id: str, file_path: str) -> Tuple[bool, int]:
        """Transport hands over a saved frame file.

        Returns (accepted, elapsed_ms). A frame for a superseded/expired
        request is rejected — the caller must delete its temp file.
        """
        with self._lock:
            if request_id != self._active_request_id:
                logger.info(f"[LiveCamera] Stale frame rejected "
                            f"(request_id={request_id})")
                return False, 0
            elapsed_ms = int((time.monotonic() - self._request_started_at) * 1000)
            self._frame_path = file_path
            self.consecutive_timeouts = 0
            self._frame_event.set()
        logger.info(f"[LiveCamera] Frame delivered in {elapsed_ms} ms "
                    f"({file_path})")
        return True, elapsed_ms

    # ------------------------------------------------------------------
    # Generation choke point (worker thread)
    # ------------------------------------------------------------------

    def consume_frame_if_pending(self, timeout: float) -> Optional[str]:
        """Wait for and consume the frame of the in-flight request.

        Zero-cost when no entry point fired a request. On timeout the turn
        proceeds without a frame; consecutive timeouts open the breaker
        (degraded) until the page re-announces. The returned file is owned
        by the caller (add it to the prompt-tmp cleanup list).
        """
        with self._lock:
            request_id = self._active_request_id
            if request_id is None:
                return None
        got = self._frame_event.wait(timeout)
        with self._lock:
            if self._active_request_id != request_id:
                # Superseded while waiting (provider went away) — no frame.
                return None
            path = self._frame_path
            self._active_request_id = None
            self._frame_path = None
            self._frame_event.clear()
            if got and path:
                age = time.monotonic() - self._request_started_at
                if age > AMBIENT_FRAME_MAX_AGE:
                    # Stranded frame from a long-rejected turn — the camera
                    # answered fine (no timeout penalty), but the scene is old.
                    self.consecutive_timeouts = 0
                    logger.info(f"[LiveCamera] Discarding stale frame "
                                f"(age {age:.1f}s)")
                    _unlink_quiet(path)
                    return None
                elapsed_ms = int(age * 1000)
                logger.info(f"[LiveCamera] Frame consumed for this turn "
                            f"({elapsed_ms} ms after request)")
                return path
            self.consecutive_timeouts += 1
            degraded_now = self.consecutive_timeouts >= AMBIENT_TIMEOUT_DEGRADE_THRESHOLD
            if degraded_now:
                self.degraded = True
        if degraded_now:
            logger.warning(
                "[LiveCamera] Capture timeout — provider degraded after "
                f"{AMBIENT_TIMEOUT_DEGRADE_THRESHOLD} consecutive timeouts "
                "(waiting suspended until the page re-announces)")
        else:
            logger.warning("[LiveCamera] Capture timeout — proceeding without frame")
        # Indicator truth source: tell clients the wait failed.
        try:
            publish_ui_update("ambient_camera_display", reason="capture_timeout",
                              data={"state": "timeout", "degraded": degraded_now})
        except Exception:
            pass
        return None


def _unlink_quiet(path: Optional[str]) -> None:
    """Delete a discarded frame temp file (no error if already gone)."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Module singleton + fire helper
# ---------------------------------------------------------------------------

_ambient_camera_state: Optional[AmbientCameraState] = None
_state_lock = threading.Lock()


def get_ambient_camera_state() -> AmbientCameraState:
    """Return the process-wide Live Camera state (lazy singleton)."""
    global _ambient_camera_state
    with _state_lock:
        if _ambient_camera_state is None:
            _ambient_camera_state = AmbientCameraState()
        return _ambient_camera_state


def _send_capture_request(state: AmbientCameraState, request_id: str) -> bool:
    """Send a claimed capture request via the transport; release on failure.

    Call-time import of the transport (sanctioned seam ①) so this shared
    leaf holds no upward module-load dependency.
    """
    try:
        from backend.server.websocket_server import get_websocket_manager
        sent = get_websocket_manager().send_ambient_capture_request_sync(request_id)
    except Exception as e:
        logger.error(f"[LiveCamera] Failed to send capture request: {e}")
        sent = False
    if not sent:
        state.cancel_request(request_id)
    return sent


def fire_capture_request() -> Optional[str]:
    """Fire-and-forget capture request from a user-initiated *send* path.

    Gated on the Live Camera auto-attach feature toggle — Camera ON with
    Live Camera OFF means "camera connected for the AI tool only", so send
    paths must not fire (稜裁定 2026-07-16). Safe no-op (returns None) when
    the feature is off, no provider is available, the breaker is open, or
    the transport is down. The AI tool path uses :func:`request_tool_frame`
    instead, which ignores the auto-attach toggle.
    """
    try:
        from backend.shared.runtime_state import get_feature_status
        if not get_feature_status().get("ambient_camera_enabled", False):
            return None
    except Exception:
        return None
    state = get_ambient_camera_state()
    request_id = state.begin_request()
    if request_id is None:
        return None
    if not _send_capture_request(state, request_id):
        return None
    return request_id


def is_provider_available() -> bool:
    """Whether a camera provider page is connected and not degraded."""
    state = get_ambient_camera_state()
    return state.provider_available and not state.degraded


def request_tool_frame(timeout: float) -> Optional[str]:
    """Synchronous capture for the AI's capture_camera tool.

    Claims a request, sends it, and waits up to ``timeout`` for the frame.
    Ignores the Live Camera auto-attach toggle (the tool only needs a connected
    camera). Returns the frame temp-file path (caller owns the file) or
    None on no-provider / send failure / timeout.
    """
    state = get_ambient_camera_state()
    request_id = state.begin_request()
    if request_id is None:
        return None
    if not _send_capture_request(state, request_id):
        return None
    return state.consume_frame_if_pending(timeout)
