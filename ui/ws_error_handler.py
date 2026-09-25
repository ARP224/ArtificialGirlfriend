"""
ui/ws_error_handler.py

WebSocket-backed logging handler (SL12 WS-Transport, UI layer).

Extracted verbatim from ui/app.py (ST4-B11b). Forwards AG-internal
ERROR/CRITICAL log records to connected clients as toast notifications.
"""

import logging
import threading
import time
from collections import OrderedDict


# Phase 4D: WebSocket-backed log handler for live error toast notifications.
# Attached only in server mode (see run_ui()) so local mode keeps current
# behavior. The two-tier prefix filter (INCLUDED ∧ ¬EXCLUDED) keeps the
# scope tight to AG-internal app errors and prevents the cycle:
#   broadcast() fails → backend.server.websocket_server.logger.error → emit()
#   → broadcast() fails → ... (would loop indefinitely without the filter).
class WebSocketErrorHandler(logging.Handler):
    """Forward AG-internal ERROR/CRITICAL logs to clients as toast events.

    Filtering rules (must satisfy both):
      1. INCLUDE: logger.name starts with one of INCLUDED_PREFIXES
         → keeps third-party noise (websockets, asyncio, httpx, ...) out
      2. EXCLUDE: logger.name starts with one of EXCLUDED_PREFIXES
         → keeps the WS/session layer's own errors out (loop prevention)

    Throttle: same (logger_name, message[:100]) within 1s is dropped.
    KEY_CHARS=100 chosen to keep variable-arg messages ('processed N items')
    from generating distinct keys per call. Phase 5 may shrink to 50 if
    toasts feel chatty.

    Defensive: every exception inside emit() is swallowed. A handler that
    raises destabilizes the whole logging pipeline.
    """

    INCLUDED_PREFIXES = ('backend.', 'ui.')
    # B13: websocket_server/session_manager moved into the backend.server
    # subpackage, so their `logging.getLogger(__name__)` names gained the
    # `.server` segment. Both must stay excluded or the
    # broadcast-fail → error-log → emit() cycle reopens.
    # (B14 removed the legacy flat-name entries — the compat shims are gone,
    # so no logger can carry the old names anymore.)
    EXCLUDED_PREFIXES = (
        'backend.server.websocket_server', 'backend.server.session_manager',
    )
    THROTTLE_WINDOW_SECONDS = 1.0
    THROTTLE_EVICT_AFTER_SECONDS = 60.0
    KEY_CHARS = 100

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._throttle: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            name = record.name or ""
            if not name.startswith(self.INCLUDED_PREFIXES):
                return
            if name.startswith(self.EXCLUDED_PREFIXES):
                return
            # ポップアップ経路の記録ログ(ui/state.py show_popup)は既に
            # popup_notification でユーザーへ届くため転送しない — 同内容
            # トースト2枚問題の根治(稜裁定 2026-08-02)
            if getattr(record, 'ag_no_ws_toast', False):
                return

            msg = record.getMessage()
            key = f"{name}:{msg[:self.KEY_CHARS]}"
            now = time.time()

            with self._lock:
                last = self._throttle.get(key, 0.0)
                if now - last < self.THROTTLE_WINDOW_SECONDS:
                    return
                self._throttle[key] = now
                self._throttle.move_to_end(key)
                # Evict entries older than THROTTLE_EVICT_AFTER_SECONDS
                while self._throttle:
                    oldest_key = next(iter(self._throttle))
                    if now - self._throttle[oldest_key] > self.THROTTLE_EVICT_AFTER_SECONDS:
                        self._throttle.popitem(last=False)
                    else:
                        break

            # トースト本文は300字で切り詰め(全文はファイル/コンソールログに
            # 残る)。生ログは英語のまま=タイトルのみ翻訳(稜裁定 2026-07-25)。
            if len(msg) > 300:
                msg = msg[:300] + "…"

            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().send_error_notification_sync(
                msg, record.levelname
            )
        except Exception:
            # Never let a logging handler raise — that would destabilize
            # the entire app. Errors here are silently dropped (the file
            # and console handlers still capture the original record).
            pass
