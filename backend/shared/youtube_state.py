"""
backend/shared/youtube_state.py

Runtime state container for the YouTube comment auto-reply feature
(spec §3: 「YouTube生成フェーズ中」フラグは _backend_state に直接足さない —
状態切出しの正準形に従う所有オブジェクト).

All flags here are runtime-only and never persisted (spec §2: auth_error は
ランタイム状態 — アプリ再起動後は次セッション冒頭の認証確認で再判定される).

Shared-leaf module: standard library only, so any layer may depend on it
downward (conversation_manager / elyth_session_manager read session_active
for mutual exclusion — spec §3 の越境編集の判定元).
"""

import threading
from typing import Optional


class YouTubeState:
    """Runtime flags owned by the YouTube session manager."""

    def __init__(self):
        # Generation phase running (LLM queue occupied). Set BEFORE the task
        # is enqueued and cleared in the task's try/finally (spec §4: LLM
        # キュー待機中の二重セッション防止＝単一書き手保証の前提).
        self.session_active: bool = False

        # Posting worker thread alive. Guarded by worker_lock when starting
        # (single-worker rule, spec §4); cleared in the worker's try/finally.
        self.worker_running: bool = False
        self.worker_lock = threading.Lock()

        # Stop request for the posting worker (feature toggle OFF /
        # app shutdown). The cooldown wait also waits on this event so a
        # stop interrupts the 1-5 min sleep immediately.
        self.worker_stop_event = threading.Event()

        # Re-authorization required (invalid_grant). While True the
        # scheduler starts no sessions and watches token.json's mtime for
        # an out-of-band re-auth (spec §2-4).
        self.auth_error: bool = False
        self.auth_error_token_mtime: Optional[float] = None


_youtube_state: Optional[YouTubeState] = None
_state_lock = threading.Lock()


def get_youtube_state() -> YouTubeState:
    """Process-wide singleton accessor."""
    global _youtube_state
    with _state_lock:
        if _youtube_state is None:
            _youtube_state = YouTubeState()
    return _youtube_state
