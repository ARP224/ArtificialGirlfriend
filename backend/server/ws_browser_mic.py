"""
backend/server/ws_browser_mic.py

Browser-mic transcription bridge (B12 split from websocket_server).

A thread-safe, latest-only relay between the WebSocket dispatch loop (which
submits transcription results/errors arriving from the browser) and the
recording flow in ``ui.conversation`` (which blocks on ``wait_for_result``).
Pure stdlib leaf — imports nothing from backend/ui, so it can be reused by the
transport without creating a cycle.
"""

import threading
from typing import Optional, Tuple


class BrowserMicBridge:
    """Thread-safe latest-only bridge for browser mic transcription results.

    Phase 5 Day 1: changed from queue.Queue (FIFO) to latest-only design.
    Reason: under flaky mobile networks, JS-side burst clicks produced
    multiple silent blobs whose 'No speech' submit_error entries piled up
    in the FIFO queue and shadowed real results in subsequent turns
    (logs/app.log around 2026-04-26 12:49-12:53).

    Latest-only semantics:
      - submit_result/submit_error overwrite any previous unread entry
      - wait_for_result always observes the most recent submission
      - reset() clears the slot (called at recording start to discard
        any stale entry from the previous turn)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: Optional[Tuple[str, str]] = None  # ("ok"|"error", payload)
        self._event = threading.Event()
        self.mic_available: bool = False  # Set by JS client via WebSocket

    def reset(self):
        """Clear the latest slot. Called at recording start to discard stale results."""
        with self._lock:
            self._latest = None
            self._event.clear()

    def submit_result(self, text: str):
        """Submit a successful transcription result, overwriting any previous."""
        with self._lock:
            self._latest = ("ok", text)
            self._event.set()

    def submit_error(self, error: str, code: Optional[str] = None):
        """Submit a transcription error, overwriting any previous.

        Args:
            error: English message (log-facing; never translated).
            code: Optional stable error code (``ag_code`` 規約 — 稜裁定
                2026-07-25 の文字列一致全廃)。wait_for_result が再送出する
                例外に属性として乗せ、UI層が i18n 照合に使う。
        """
        with self._lock:
            self._latest = ("error", (error, code))
            self._event.set()

    def wait_for_result(self, timeout: float = 15.0) -> str:
        """Block until a result is available or timeout.

        Phase 5 Day 1: timeout shortened from 30s to 15s. Combined with
        the latest-only semantics and stop_and_transcribe_phase1's
        TimeoutError handler (which broadcasts mic_state_reset), 15s
        gives reasonable feedback for explicit-stop-with-silence while
        still leaving headroom for slow mobile uploads.

        Raises:
            TimeoutError: No result within *timeout* seconds, or event was
                          set but slot was empty (concurrent wait / stale
                          event from reset).
            RuntimeError: WS handler submitted an error.
        """
        if not self._event.wait(timeout=timeout):
            raise TimeoutError("Browser mic transcription timed out")
        with self._lock:
            result = self._latest
            self._latest = None
            self._event.clear()
        if result is None:
            raise TimeoutError("Browser mic: no result available")
        status, value = result
        if status == "error":
            msg, code = value if isinstance(value, tuple) else (value, None)
            exc = RuntimeError(msg)
            if code:
                # duck-typed ag_code 規約(stdlib純度維持のため動的属性で運ぶ)
                exc.ag_code = code
                exc.ag_params = {}
            raise exc
        return value


# BrowserMicBridge singleton
_browser_mic_bridge = BrowserMicBridge()


def get_browser_mic_bridge() -> BrowserMicBridge:
    """Return the global BrowserMicBridge instance."""
    return _browser_mic_bridge
