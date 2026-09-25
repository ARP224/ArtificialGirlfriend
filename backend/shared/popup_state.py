"""
backend/shared/popup_state.py

Pending popup-notification queue (shared leaf, stdlib only).

Holds popup toasts (title/message/level) raised while no desktop client is
connected — startup warnings fired before the browser opens, or errors raised
while the app sits tray-resident. ``ui.state.show_popup`` enqueues here when
live WS delivery is not possible; the WS transport
(``backend/server/websocket_server``) drains the queue to a desktop client
right after it identifies. Shared-leaf placement keeps both accesses downward
(ui -> shared, server -> shared).
"""

import threading
from typing import Dict, List

# Mirrors the old AppState.add_error_popup queue cap.
MAX_PENDING_POPUPS = 10

_lock = threading.Lock()
_pending: List[Dict[str, str]] = []
# 起動フェーズ境目(稜裁定 2026-08-02): キューは「誰も接続する前」の
# 起動時エラーを最初の画面オープンで見せるためのもの。一度でも何かの
# クライアント(モバイル含む)が identify した後は、不達ポップアップを
# 溜めず捨てる(記録は show_popup 側のログに常に残る)。これが無いと
# モバイルだけで使った数時間分のエラーが、後からデスクトップUIを開いた
# 瞬間にまとめて連射されていた。
_client_seen = False


def mark_client_seen() -> None:
    """Called by the WS transport on the first client identify (any type)."""
    global _client_seen
    with _lock:
        _client_seen = True


def enqueue_popup(title: str, message: str, level: str) -> None:
    """Queue a popup for delivery when a desktop client next connects.

    After the first client has ever identified, undeliverable popups are
    dropped instead (startup-phase-only queue; see _client_seen above).
    """
    with _lock:
        if _client_seen:
            return
        if len(_pending) >= MAX_PENDING_POPUPS:
            _pending.pop(0)
        _pending.append({"title": title, "message": message, "level": level})


def drain_popups() -> List[Dict[str, str]]:
    """Return all pending popups (oldest first) and clear the queue."""
    with _lock:
        items = _pending[:]
        _pending.clear()
        return items
