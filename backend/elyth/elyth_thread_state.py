"""
backend/elyth_thread_state.py

Per-thread reply accounting and own-handle registry for ELYTH sessions.

Two pieces of persistent state, used to curb reply-overweight behaviour and the
self-sustaining reply ping-pong observed in autonomous ELYTH sessions:

  1. Per-character thread reply counts ({thread_id: count}) — how many distinct
     sessions we have replied into a given thread. Used to suppress notifications
     from threads we have already engaged with `ELYTH_THREAD_REPLY_CAP` times.
     Alongside the counts lives a `pending` list: threads replied to in the
     session currently running, persisted at reply time (hard-kill resilience)
     but NOT counted by the cap filter until folded into the counts at session
     end — or, for a killed session, at the start of the next session.

  2. A shared own-handle registry ({character_id: elyth_handle}) — populated
     from GET /me/profile at session start (v2; the v1 create_* response
     sniffing is gone). Used to identify "sibling" threads (where the
     counterpart is another of our own characters), which are exempt from
     dedupe / capping so their banter is preserved.

All writes are atomic (tmp + os.replace). All reads fall back to empty on any
error, so corruption degrades gracefully to "no filtering / no exemption".
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.shared.constants import ELYTH_THREAD_STATE_DIR

logger = logging.getLogger(__name__)

_OWN_HANDLES_FILE = ELYTH_THREAD_STATE_DIR / "_own_handles.json"


# ---------------------------------------------------------------------------
# Low-level IO
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[ELYTH ThreadState] Failed to read {path.name}: {e}")
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        ELYTH_THREAD_STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(path))
    except Exception as e:
        logger.error(f"[ELYTH ThreadState] Failed to write {path.name}: {e}")


def _thread_file(character_id: str) -> Path:
    return ELYTH_THREAD_STATE_DIR / f"{character_id}.json"


# ---------------------------------------------------------------------------
# Own-handle registry
# ---------------------------------------------------------------------------

def record_own_handle(character_id: str, handle: Optional[str]) -> None:
    """Record this character's ELYTH handle (from /me/profile at session start)."""
    if not character_id or not handle:
        return
    registry = _read_json(_OWN_HANDLES_FILE)
    if registry.get(character_id) == handle:
        return  # no change — avoid needless writes
    registry[character_id] = handle
    _write_json(_OWN_HANDLES_FILE, registry)


def get_sibling_handles(character_id: str) -> Set[str]:
    """Return ELYTH handles of our other characters (everyone but `character_id`)."""
    registry = _read_json(_OWN_HANDLES_FILE)
    return {h for cid, h in registry.items() if cid != character_id and h}


def remove_thread_state(character_id: str) -> None:
    """Forget a character (character deletion): its per-thread reply-count file
    and its entry in the own-handle registry. No error if absent."""
    path = _thread_file(character_id)
    if path.exists():
        try:
            os.remove(path)
            logger.info(f"[ELYTH ThreadState] Removed for {character_id}")
        except Exception as e:
            logger.error(f"[ELYTH ThreadState] Failed to remove for {character_id}: {e}")
    registry = _read_json(_OWN_HANDLES_FILE)
    if character_id in registry:
        registry.pop(character_id, None)
        _write_json(_OWN_HANDLES_FILE, registry)


# ---------------------------------------------------------------------------
# Per-thread reply counts
# ---------------------------------------------------------------------------

def _load_thread_counts(character_id: str) -> Dict[str, int]:
    data = _read_json(_thread_file(character_id))
    threads = data.get("threads", {})
    if not isinstance(threads, dict):
        return {}
    # Coerce to ints defensively
    out: Dict[str, int] = {}
    for k, v in threads.items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def get_thread_counts(character_id: str) -> Dict[str, int]:
    """Public view of the per-thread reply counts (used by the v2 aggregation
    layer to decide cap suppression while resolving notifications)."""
    return _load_thread_counts(character_id)


def _load_pending(character_id: str) -> List[str]:
    pending = _read_json(_thread_file(character_id)).get("pending", [])
    if not isinstance(pending, list):
        return []
    return [t for t in pending if isinstance(t, str) and t]


def add_pending_thread_reply(character_id: str, thread_id: str) -> None:
    """Record (immediately, atomically) that this session replied into a thread.

    pending はカウント(threads)に含まれない＝セッション内の cap 判定は不変。
    加算はセッション末(またはkill後の次セッション開始時)の fold で行う。
    """
    if not thread_id:
        return
    pending = _load_pending(character_id)
    if thread_id in pending:
        return  # no change — avoid needless writes
    pending.append(thread_id)
    _write_json(_thread_file(character_id),
                {"threads": _load_thread_counts(character_id), "pending": pending})


def fold_pending_thread_replies(character_id: str) -> None:
    """Fold pending threads into the reply counts (+1 each) and clear pending.

    加算とクリアを1回のアトミック書き込みで行う＝killを挟んでも二重加算しない。
    Called at session end (normal path) and at session start (crash leftovers).
    """
    pending = _load_pending(character_id)
    if not pending:
        return
    counts = _load_thread_counts(character_id)
    for tid in set(pending):
        counts[tid] = counts.get(tid, 0) + 1
    _write_json(_thread_file(character_id), {"threads": counts, "pending": []})


# ---------------------------------------------------------------------------
# Thread-map extraction from AG-format tool results
# ---------------------------------------------------------------------------
# Dedupe / cap suppression moved into the aggregation layer with the v2
# migration (elyth_aggregates.build_notifications_result, spec v6 §5.2) —
# thread_id is only known after per-notification post resolution there, so
# filtering can no longer be a post-hoc pass over the tool result.

def extract_thread_map_from_notifications(raw_json: str) -> Dict[str, str]:
    """Best-effort post_id -> thread_id map from an AG-format
    get_notifications result (spec v6 §5.2: resolved reply/mention entries
    carry `post_id` and `post_thread_id`)."""
    try:
        data = json.loads(raw_json)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    if isinstance(data, dict) and isinstance(data.get("notifications"), list):
        for n in data["notifications"]:
            if isinstance(n, dict):
                pid = n.get("post_id")
                tid = n.get("post_thread_id")
                if pid and tid:
                    out[pid] = tid
    return out


def extract_thread_map_from_posts(raw_json: str) -> Dict[str, str]:
    """Build a post_id -> thread_id map from a get_thread / get_timeline /
    get_my_posts result (each post has `id` and `thread_id`)."""
    try:
        data = json.loads(raw_json)
    except Exception:
        return {}
    out: Dict[str, str] = {}

    def _scan(posts: Any) -> None:
        if isinstance(posts, list):
            for p in posts:
                if isinstance(p, dict):
                    pid = p.get("id")
                    tid = p.get("thread_id")
                    if pid and tid:
                        out[pid] = tid

    if isinstance(data, dict):
        _scan(data.get("posts"))
        _scan(data.get("timeline"))
        trends = data.get("trends")
        if isinstance(trends, dict):
            _scan(trends.get("posts"))
    return out
