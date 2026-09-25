"""Replay buffer / sequencing / snapshot / idempotency component for the WS server.

Extracted from ``websocket_server.py`` (ST4 B12) as the self-contained
"replay buffer" third of the transport / dispatch / replay split. This class owns
all per-``session_token`` resilience state behind a small method surface:

  * monotonic seq counters (Phase 3A),
  * the per-token replay ring buffer with soft-cap (turns) / hard-cap (bytes)
    eviction (Phase 3B),
  * latest-value snapshots, overwritten in place and re-sent on resume (Phase 3C),
  * the idempotency dedup cache for non-idempotent client actions (Phase 2D).

It is a pure-stdlib leaf: it imports nothing from ``backend``/``ui``. Behavior is
identical to the original inline ``WebSocketManager`` methods — lock acquisition,
eviction order, and the is_generating turn-boundary semantics are preserved
exactly. ``WebSocketManager`` holds one instance (``self._replay``) and delegates.
"""
import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Phase 2D: non-idempotent client actions deduped via the idempotency cache.
NON_IDEMPOTENT_ACTIONS = frozenset({
    "attach_image",
    "elyth_toggle_character",
    "character_appear",
    "elyth_start_session",
    "command_approve",
    "command_deny",
    # AG Client Addon hotkeys: a reconnect-queue resend must not double-fire
    # a recording toggle (the 500 ms debounce only covers live key repeats).
    "hotkey_start_recording",
    "hotkey_stop_recording",
    "hotkey_toggle_recording",
})

# Phase 3B: actions whose order matters during disconnect — buffered per
# session_token and re-sent on resume. Source: internal WS message classification
# table §5.2 (design doc, not part of the repository; this constant is the source of truth).
REPLAY_ACTIONS = frozenset({
    "auto_prompt_chat_update",        # s18
    "command_pre_response",            # s40
    "command_loop_spinner",            # s41
    "command_loop_block",              # s42
    "talk_theme_block",                # s43
    "image_generating_spinner",        # s44
    "image_generation_block",          # s45
    "camera_capture_block",            # s46
    "deep_search_spinner",             # s47
    "deep_search_block",               # s48
    "map_search_spinner",              # s49
    "map_search_block",                # s50
    "elyth_block",                     # s51
    "command_approval_pending",        # s60
    "command_approval_resolved",       # s61
})

# Phase 3B: replay buffer caps. Soft cap is the user-meaningful one (last N turns
# of the conversation). Hard cap is a runaway safety valve — image_generation_block
# alone can be 1-2MB, so 5MB tolerates a few abnormal turns before evicting.
REPLAY_BUFFER_HARD_CAP_BYTES = 5 * 1024 * 1024
REPLAY_BUFFER_SOFT_CAP_TURNS = 10

# Phase 3C: actions whose latest value alone is meaningful — overwritten in place,
# not buffered. Re-sent in full on resume (after replay flush).
# Source: internal WS message classification table §5.3 (see the REPLAY_ACTIONS note above).
SNAPSHOT_ACTIONS = frozenset({
    "session_status_update",           # s04 (admin-bound; admin has no token so never stored)
    "update_chat",                     # s10
    "extraction_started",              # s14
    "extraction_completed",            # s15
    "auto_prompt_countdown_start",     # s16
    "auto_prompt_countdown_stop",      # s17
    "character_appear_closed",         # s21
    "image_slot_update",               # s22
    "active_character_update",         # s23
    "conversation_state_update",       # s24
    "is_generating_update",            # s25
    "elyth_status",                    # s70
    "talk_theme_updated",              # s90 (Phase 4B)
})


class IdempotencyCache:
    """OrderedDict-backed TTL + LRU cache for idempotency keys.

    Duplicate detection is a silent drop: callers check has() before dispatch
    and skip processing if True. Cache entries are evicted by TTL or by LRU
    when capacity is exceeded. Both checks are O(1) amortized.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 10000):
        self._cache: "OrderedDict[str, float]" = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_size
        self._lock = threading.Lock()

    def has(self, key: str) -> bool:
        with self._lock:
            self._evict_expired()
            return key in self._cache

    def set(self, key: str):
        with self._lock:
            self._cache[key] = time.time()
            self._cache.move_to_end(key)
            self._evict_expired()
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def _evict_expired(self):
        now = time.time()
        # Entries are inserted/refreshed in time order, but expiration is
        # absolute (not LRU). Walk from the front and pop expired entries.
        while self._cache:
            oldest_key = next(iter(self._cache))
            if now - self._cache[oldest_key] > self._ttl:
                self._cache.popitem(last=False)
            else:
                break


class ReplayBuffer:
    """Owns per-session_token seq / replay / snapshot / idempotency state.

    All public methods are thread-safe; the locking discipline mirrors the
    original inline implementation exactly:

      * ``_seq_lock`` guards the seq counters (briefly held in ``next_seq``).
      * ``_buffer_lock`` guards the replay ring buffer, the turn counters, the
        byte accounting and the snapshots (they share one lock because their
        lifetime/scope is the same). It is a non-reentrant ``threading.Lock``,
        so ``note_turn_transition`` reads the token list under the lock and then
        invokes ``_on_turn_end`` *outside* it.

    Lock order where both are taken (``drop_session``): ``_buffer_lock`` then
    ``_seq_lock`` — never the reverse.
    """

    def __init__(self):
        # Phase 2D: idempotency cache for non-idempotent client actions
        self._idempotency_cache = IdempotencyCache(ttl_seconds=300, max_size=10000)
        # Phase 3A: per-session_token seq counter (admin / motion_pngtuber /
        # local mode use empty token and are not stamped).
        self._seq_counters: Dict[str, int] = {}
        self._seq_lock = threading.Lock()
        # Phase 3B: replay buffer state — keyed by session_token so it survives
        # WS reconnects. Turn boundary detected from is_generating true→false.
        self._replay_buffers: Dict[str, deque] = {}
        self._turn_counter: Dict[str, int] = {}
        self._buffer_size: Dict[str, int] = {}
        self._last_is_generating: bool = False
        # Phase 3C: latest snapshot per session — overwritten on every emit.
        # Shares _buffer_lock since lifetime/scope is the same.
        self._snapshot_latest: Dict[str, Dict[str, dict]] = {}
        self._buffer_lock = threading.Lock()

    # --- Phase 2D: idempotency ---

    def is_duplicate(self, key: str) -> bool:
        """Return True if this idempotency key has already been seen (silent-drop)."""
        return self._idempotency_cache.has(key)

    def mark_seen(self, key: str) -> None:
        """Record an idempotency key as processed."""
        self._idempotency_cache.set(key)

    # --- Phase 3A: sequencing ---

    def next_seq(self, session_token: str) -> int:
        """Return the next seq number for a session_token. Monotonically increasing."""
        with self._seq_lock:
            seq = self._seq_counters.get(session_token, 0) + 1
            self._seq_counters[session_token] = seq
            return seq

    # --- Phase 3B/3C: outgoing tracking ---

    def on_outgoing(self, session_token: str, message: dict, msg_bytes: int):
        """Track outgoing messages for replay/snapshot.

        Called per-session_token from broadcast() with the already-seq-stamped
        message. REPLAY_ACTIONS append to the ring buffer; SNAPSHOT_ACTIONS
        overwrite the latest-value entry (resume sends them in full after replay).
        """
        if not session_token:
            return
        action = message.get("action")
        if action in REPLAY_ACTIONS:
            self._add_to_replay_buffer(session_token, message, msg_bytes)
        elif action in SNAPSHOT_ACTIONS:
            with self._buffer_lock:
                snap = self._snapshot_latest.setdefault(session_token, {})
                snap[action] = message

    def _add_to_replay_buffer(self, session_token: str, message: dict, msg_bytes: int):
        """Append a replay-target message to the per-token ring buffer."""
        if message.get("action") not in REPLAY_ACTIONS:
            return
        with self._buffer_lock:
            buf = self._replay_buffers.setdefault(session_token, deque())
            entry = {
                "turn": self._turn_counter.get(session_token, 0),
                "seq": message.get("seq"),
                "message": message,
                "size": msg_bytes,
            }
            buf.append(entry)
            self._buffer_size[session_token] = (
                self._buffer_size.get(session_token, 0) + msg_bytes
            )
            self._evict_buffer_locked(session_token)

    def note_turn_transition(self, is_generating: bool) -> None:
        """Detect a turn boundary (is_generating true→false) and bump turn counters.

        The transition check + _last_is_generating write are done atomically inside
        _buffer_lock to prevent double-increment if multiple threads call this
        concurrently. _on_turn_end is invoked outside the lock because it
        re-acquires _buffer_lock (threading.Lock is non-reentrant).
        """
        with self._buffer_lock:
            transitioning = self._last_is_generating and not is_generating
            self._last_is_generating = is_generating
            tokens = list(self._replay_buffers.keys()) if transitioning else []
        for token in tokens:
            self._on_turn_end(token)

    def _on_turn_end(self, session_token: str):
        """Bump the turn counter and re-evaluate eviction. Called once per
        is_generating true→false transition."""
        with self._buffer_lock:
            self._turn_counter[session_token] = (
                self._turn_counter.get(session_token, 0) + 1
            )
            self._evict_buffer_locked(session_token)

    def _evict_buffer_locked(self, session_token: str):
        """Evict by soft-cap (turns) then hard-cap (bytes). Caller MUST hold _buffer_lock."""
        buf = self._replay_buffers.get(session_token)
        if not buf:
            return
        current_turn = self._turn_counter.get(session_token, 0)
        # Soft cap: keep last N turns
        min_turn_to_keep = max(0, current_turn - REPLAY_BUFFER_SOFT_CAP_TURNS)
        while buf and buf[0]["turn"] < min_turn_to_keep:
            popped = buf.popleft()
            self._buffer_size[session_token] -= popped["size"]
        # Hard cap: total bytes
        while (self._buffer_size.get(session_token, 0) > REPLAY_BUFFER_HARD_CAP_BYTES
               and buf):
            popped = buf.popleft()
            self._buffer_size[session_token] -= popped["size"]
            logger.warning(
                f"[Replay] Hard cap evicted (session={session_token[:8]}, "
                f"turn={popped['turn']}, action={popped['message'].get('action')})"
            )

    # --- resume reads (Phase 3D): each acquires _buffer_lock briefly so the
    # caller never holds it across an ``await`` send. ---

    def get_replay_entries(self, session_token: str) -> List[dict]:
        """Snapshot of the per-token replay ring buffer (in seq order)."""
        with self._buffer_lock:
            return list(self._replay_buffers.get(session_token, []))

    def get_snapshot_actions(self, session_token: str) -> List[str]:
        """Action keys currently held in the latest-value snapshot for a token."""
        with self._buffer_lock:
            return list(self._snapshot_latest.get(session_token, {}).keys())

    def get_snapshot_message(self, session_token: str, action: str) -> Optional[dict]:
        """Latest snapshot message for (token, action), or None.

        Fetched fresh per send so a concurrent broadcast that updated the snapshot
        is not clobbered (the resume path never writes the new seq back).
        """
        with self._buffer_lock:
            return self._snapshot_latest.get(session_token, {}).get(action)

    # --- Phase 3D: session lifecycle ---

    def drop_session(self, session_token: str):
        """Drop all per-session state when a session ends completely.

        Called from SessionManager._execute_cleanup (Phase 3D Step 7) — must NOT
        hold session_manager._lock when invoking this, since it acquires
        _buffer_lock and _seq_lock here.
        """
        if not session_token:
            return
        with self._buffer_lock:
            self._replay_buffers.pop(session_token, None)
            self._turn_counter.pop(session_token, None)
            self._buffer_size.pop(session_token, None)
            self._snapshot_latest.pop(session_token, None)
        with self._seq_lock:
            self._seq_counters.pop(session_token, None)
