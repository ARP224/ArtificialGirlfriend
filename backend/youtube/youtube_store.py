"""
backend/youtube/youtube_store.py

Persistence layer for the YouTube comment auto-reply feature
(spec §6: character_data/youtube/ 配下・すべてJSON・atomic write).

Files owned here:
- history.json        … global reply history (posted のみ転記・1000件FIFO)
- video_context.json  … video_id → {title, description} (初回取得の永久キャッシュ)
- session_state.json  … 基準点(baseline)・処理済みID台帳(500件FIFO)・
                         当日投稿数 {date, count}・記憶抽出カウンタ・セッション回数
- post_queue.json     … 投稿キュー（状態機械 generated→posting→posted/skipped）

Concurrency: the single-writer guarantee comes from the scheduler design
(spec §4: generation never runs while the worker runs), but every
read-modify-write here still goes through one process-wide lock as cheap
insurance; atomic write (tmp + os.replace) protects against file corruption,
which is a separate concern from thread exclusion.

Daily-count reset is READ-TIME NORMALIZATION only (spec §4 v5): both the
limit check and the increment treat a stored date different from today
(local date) as count=0. There is deliberately no write-time reset — that
variant deadlocks when the limit is reached (nobody runs to flip the date).
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.shared.constants import YOUTUBE_DIR

logger = logging.getLogger(__name__)

HISTORY_FILE = YOUTUBE_DIR / "history.json"
VIDEO_CONTEXT_FILE = YOUTUBE_DIR / "video_context.json"
SESSION_STATE_FILE = YOUTUBE_DIR / "session_state.json"
POST_QUEUE_FILE = YOUTUBE_DIR / "post_queue.json"

HISTORY_MAX = 1000        # global history FIFO cap
PROCESSED_IDS_MAX = 500   # processed-comment-id ledger FIFO cap
SELECTION_CAP = 10        # 11件以上はランダム10件（spec §4-4）

_store_lock = threading.RLock()


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"[YouTube] Failed to read {path.name}: {e}")
        return default


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def timestamp_key(published_at: str) -> datetime:
    """RFC3339 → aware datetime for ordering. Unparseable values sort oldest
    (epoch) so a malformed timestamp can never masquerade as 'new'."""
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# session_state.json
# ---------------------------------------------------------------------------

_STATE_DEFAULTS: Dict[str, Any] = {
    "baseline": None,                 # publishedAt of the newness boundary
    "processed_ids": [],              # FIFO ledger (newest last)
    "daily": {"date": "", "count": 0},
    "posts_since_extraction": 0,
    "session_count": 0,
}


def load_session_state() -> Dict[str, Any]:
    import copy
    state = _read_json(SESSION_STATE_FILE, {})
    # deepcopy: the defaults hold mutable containers (processed_ids list) —
    # a shallow merge would hand out the module-level list itself, and the
    # first register_processed_ids would silently mutate the defaults.
    defaults = copy.deepcopy(_STATE_DEFAULTS)
    merged = {**defaults, **state} if isinstance(state, dict) else defaults
    # deep-default the daily sub-dict
    if not isinstance(merged.get("daily"), dict):
        merged["daily"] = {"date": "", "count": 0}
    return merged


def save_session_state(state: Dict[str, Any]) -> None:
    with _store_lock:
        _atomic_write_json(SESSION_STATE_FILE, state)


def is_processed(state: Dict[str, Any], comment_id: str) -> bool:
    return comment_id in state.get("processed_ids", [])


def register_processed_ids(state: Dict[str, Any], comment_ids: List[str]) -> None:
    """Idempotently append ids to the ledger, FIFO-capped. Mutates state;
    the caller decides when to save (spec §4-5d の書込順序は呼び出し側)."""
    ledger: List[str] = state.setdefault("processed_ids", [])
    existing = set(ledger)
    for cid in comment_ids:
        if cid and cid not in existing:
            ledger.append(cid)
            existing.add(cid)
    if len(ledger) > PROCESSED_IDS_MAX:
        del ledger[: len(ledger) - PROCESSED_IDS_MAX]


def _local_today() -> str:
    return time.strftime("%Y-%m-%d")


def get_daily_count(state: Dict[str, Any]) -> int:
    """Read-time normalized posted-today count (0 if the stored date is not
    today's LOCAL date)."""
    daily = state.get("daily") or {}
    if daily.get("date") != _local_today():
        return 0
    return int(daily.get("count", 0))


def increment_daily_count(state: Dict[str, Any]) -> None:
    """Normalize-then-increment (mutates state; caller saves)."""
    today = _local_today()
    daily = state.get("daily") or {}
    count = int(daily.get("count", 0)) if daily.get("date") == today else 0
    state["daily"] = {"date": today, "count": count + 1}


# ---------------------------------------------------------------------------
# Newness judgement + selection (spec §4-3/4)
# ---------------------------------------------------------------------------

def filter_candidates(comments: List[Dict[str, str]], state: Dict[str, Any],
                      own_channel_id: str) -> List[Dict[str, str]]:
    """除外フィルタ + 新規判定.

    - Drops the authorized channel's own comments and ledgered ids.
    - Baseline judgement uses publishedAt >= baseline (same-second inclusive;
      duplicates are what the ID ledger is for). updatedAt is deliberately
      NOT used — order=time can reshuffle on edits (spec §4-4).
    - With no baseline yet (initial run) every filtered comment is a
      candidate; the caller must then take the initial-baseline path
      (record_initial_baseline) instead of replying.
    """
    ledger = set(state.get("processed_ids", []))
    filtered = [
        c for c in comments
        if c.get("comment_id")
        and c["comment_id"] not in ledger
        and c.get("author_channel_id") != own_channel_id
    ]
    baseline = state.get("baseline")
    if baseline is None:
        return filtered
    boundary = timestamp_key(baseline)
    return [c for c in filtered if timestamp_key(c.get("published_at", "")) >= boundary]


def select_targets(candidates: List[Dict[str, str]],
                   cap: int = SELECTION_CAP) -> List[Dict[str, str]]:
    """1〜cap件は全件、超過はランダムcap件（非選定分は永久スキップ＝J10）。"""
    if len(candidates) <= cap:
        return list(candidates)
    return random.sample(candidates, cap)


def record_initial_baseline(state: Dict[str, Any],
                            comments: List[Dict[str, str]]) -> None:
    """First-ever session with >=1 fetched comment (spec §4-4 初回):
    set the baseline to the newest publishedAt and ledger every comment id
    sharing that same second, so the >= judgement can't re-capture the
    baseline comment next session. No replies are generated for these.
    Mutates state; caller saves."""
    if not comments:
        return
    newest = max(comments, key=lambda c: timestamp_key(c.get("published_at", "")))
    newest_key = timestamp_key(newest.get("published_at", ""))
    state["baseline"] = newest.get("published_at", "")
    same_second = [
        c["comment_id"] for c in comments
        if timestamp_key(c.get("published_at", "")) == newest_key
    ]
    register_processed_ids(state, same_second)


def finalize_baseline(state: Dict[str, Any],
                      candidates: List[Dict[str, str]]) -> None:
    """Session-complete bookkeeping (spec §4-6): ledger EVERY newness-judged
    candidate (selected ones are already there via the per-item writes; this
    also covers the non-selected → 永久スキップ) and advance the baseline to
    the newest candidate publishedAt. Mutates state; caller saves."""
    if not candidates:
        return
    register_processed_ids(state, [c["comment_id"] for c in candidates])
    newest = max(candidates, key=lambda c: timestamp_key(c.get("published_at", "")))
    newest_ts = newest.get("published_at", "")
    if state.get("baseline") is None or (
        timestamp_key(newest_ts) >= timestamp_key(state["baseline"])
    ):
        state["baseline"] = newest_ts


# ---------------------------------------------------------------------------
# post_queue.json (state machine: generated → posting → posted / skipped)
# ---------------------------------------------------------------------------

def load_queue() -> List[Dict[str, Any]]:
    queue = _read_json(POST_QUEUE_FILE, [])
    return queue if isinstance(queue, list) else []


def save_queue(items: List[Dict[str, Any]]) -> None:
    with _store_lock:
        _atomic_write_json(POST_QUEUE_FILE, items)


def make_queue_item(comment: Dict[str, str], reply_text: str) -> Dict[str, Any]:
    return {
        "comment_id": comment["comment_id"],
        "video_id": comment.get("video_id", ""),
        "author_channel_id": comment.get("author_channel_id", ""),
        "author_name": comment.get("author_name", ""),
        "comment_text": comment.get("text", ""),
        "reply_text": reply_text,
        "status": "generated",
        "skip_reason": "",
        "retry_count": 0,
        "updated_at": _now_iso(),
    }


def append_queue_item(item: Dict[str, Any]) -> None:
    """Persist one new queue item (spec §4-5d: キュー永続化→台帳追加 の前半)."""
    with _store_lock:
        queue = load_queue()
        queue = [q for q in queue if q.get("comment_id") != item.get("comment_id")]
        queue.append(item)
        save_queue(queue)


def update_queue_item(comment_id: str, **fields: Any) -> None:
    """Set fields on one item (always stamps updated_at) and persist."""
    with _store_lock:
        queue = load_queue()
        for item in queue:
            if item.get("comment_id") == comment_id:
                item.update(fields)
                item["updated_at"] = _now_iso()
                break
        save_queue(queue)


def remove_queue_item(comment_id: str) -> None:
    """Cleanup after posted (history転記後) / skipped (ログ記録後) — spec §6."""
    with _store_lock:
        queue = [q for q in load_queue() if q.get("comment_id") != comment_id]
        save_queue(queue)


def pending_queue_items(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Queue items with the given statuses (default: generated + posting)."""
    wanted = set(statuses or ("generated", "posting"))
    return [q for q in load_queue() if q.get("status") in wanted]


# ---------------------------------------------------------------------------
# history.json (posted のみ・1000件FIFO)
# ---------------------------------------------------------------------------

def load_history() -> List[Dict[str, Any]]:
    history = _read_json(HISTORY_FILE, [])
    return history if isinstance(history, list) else []


def append_history(item: Dict[str, Any]) -> None:
    """Append one posted exchange. Only posted replies are ever recorded
    here (spec §6: denied/skipped の本文をプロンプトへ再注入しないため)."""
    entry = {
        "video_id": item.get("video_id", ""),
        "author_channel_id": item.get("author_channel_id", ""),
        "author_name": item.get("author_name", ""),
        "comment_id": item.get("comment_id", ""),
        "comment_text": item.get("comment_text", ""),
        "reply_text": item.get("reply_text", ""),
        "timestamp": _now_iso(),
    }
    with _store_lock:
        history = load_history()
        history.append(entry)
        if len(history) > HISTORY_MAX:
            del history[: len(history) - HISTORY_MAX]
        _atomic_write_json(HISTORY_FILE, history)


def video_history(video_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Latest exchanges on one video (oldest→newest within the window)."""
    matches = [h for h in load_history() if h.get("video_id") == video_id]
    return matches[-limit:]


def user_history(author_channel_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Latest exchanges with one commenter, matched by channel id — display
    names are mutable and never used as the identifier (spec §5)."""
    if not author_channel_id:
        return []
    matches = [
        h for h in load_history()
        if h.get("author_channel_id") == author_channel_id
    ]
    return matches[-limit:]


def recent_history(count: int) -> List[Dict[str, Any]]:
    """Newest `count` posted exchanges (for the memory-extraction summary)."""
    if count <= 0:
        return []
    return load_history()[-count:]


# ---------------------------------------------------------------------------
# video_context.json (初回取得の永久キャッシュ — spec §10)
# ---------------------------------------------------------------------------

def get_video_context(video_id: str) -> Optional[Dict[str, str]]:
    contexts = _read_json(VIDEO_CONTEXT_FILE, {})
    ctx = contexts.get(video_id) if isinstance(contexts, dict) else None
    return ctx if isinstance(ctx, dict) else None


def set_video_context(video_id: str, title: str, description: str) -> None:
    with _store_lock:
        contexts = _read_json(VIDEO_CONTEXT_FILE, {})
        if not isinstance(contexts, dict):
            contexts = {}
        contexts[video_id] = {"title": title, "description": description}
        _atomic_write_json(VIDEO_CONTEXT_FILE, contexts)
