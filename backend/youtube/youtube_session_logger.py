"""
backend/youtube/youtube_session_logger.py

Operational logging for YouTube reply sessions (spec §6: 実行ログはlogs/側):
one JSON file at a fixed path holding the latest run plus a short FIFO of
sessions that actually replied, so disk usage stays bounded.

On-disk format (v2, History-page readable):
  {"last_run": {...} | null, "sessions": [... up to MAX_SESSIONS_KEPT ...]}
- last_run … the most recent run, always overwritten (0コメント実行や
  クラッシュ痕跡もここに残る — トラブルシュート用の記録)。
- sessions … コメントを1件以上処理したセッションのみ昇格
  (0コメント=ノーセッション扱い — 稜裁定 2026-07-20)。FIFO 3件で
  History ページが過去の返信セッションを遡れる。途中死したセッションは
  次回 start_session 時に end_reason="crash" で救済昇格する。
- 旧形式 (v1: セッション dict 単体・毎回上書き) は読込時に移行する。

Captures per comment: status (generated / skipped(reason) / dry_run), the
generated text (spec §5: skip時の生成出力も残し稜が事後確認できる — deny や
dry-run の目視ゲートの実体), LLM usage metadata when available. 投稿ワーカー
はここに書かない — 投稿成否は history.json / post_queue.json 側が真実源。
Flushed after every append so a crash still leaves a trail.
"""

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.shared.constants import LOGS_DIR

logger = logging.getLogger(__name__)

_OUTPUT_PATH = LOGS_DIR / "youtube_session_log.json"

# 1セッション最大30リプライ想定 — 5件は多く1件では遡れない (稜裁定 2026-07-20)
MAX_SESSIONS_KEPT = 3


def _session_key(session: Dict[str, Any]) -> str:
    # v1 sessions have no session_id — started_at is unique enough there
    return session.get("session_id") or session.get("started_at") or ""


def _empty_doc() -> Dict[str, Any]:
    return {"last_run": None, "sessions": []}


def load_session_history(output_path=None) -> Dict[str, Any]:
    """Read the session log for display (History page).

    Tolerates a missing, corrupt, or pre-FIFO v1 file; always returns
    {"last_run": dict|None, "sessions": [dict, ...]}.
    """
    path = output_path or _OUTPUT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _empty_doc()
    except Exception as e:
        logger.warning(f"[YouTube] Session log unreadable, treating as empty: {e}")
        return _empty_doc()
    if isinstance(raw, dict) and "sessions" in raw:
        last_run = raw.get("last_run")
        sessions = raw.get("sessions")
        return {
            "last_run": last_run if isinstance(last_run, dict) else None,
            "sessions": ([s for s in sessions if isinstance(s, dict)]
                         if isinstance(sessions, list) else []),
        }
    if isinstance(raw, dict) and "started_at" in raw:
        # v1 single-session file — keep it in the FIFO if it replied
        return {"last_run": raw, "sessions": [raw] if raw.get("comments") else []}
    return _empty_doc()


class YouTubeSessionLogger:
    """One generation session (+ the worker run it spawns) per instance;
    the file keeps last_run + the replied-session FIFO."""

    def __init__(self, output_path=None):
        self.output_path = output_path or _OUTPUT_PATH
        self._data: Dict[str, Any] = {}
        self._sessions: List[Dict[str, Any]] = []

    def start_session(self, character_id: str, character_name: str,
                      dry_run: bool, target_channel_id: str) -> None:
        prev = load_session_history(self.output_path)
        self._sessions = prev["sessions"]
        self._rescue_previous_run(prev["last_run"])
        self._data = {
            "session_id": uuid.uuid4().hex[:12],
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "character_id": character_id,
            "character_name": character_name,
            "target_channel_id": target_channel_id,
            "dry_run": dry_run,
            "events": [],
            "comments": [],
            "ended_at": None,
            "end_reason": None,
        }
        self._flush()

    def log_event(self, kind: str, detail: str = "", **fields: Any) -> None:
        if not self._data:
            return
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "detail": detail,
        }
        event.update(fields)
        self._data["events"].append(event)
        self._flush()

    def log_comment(self, comment: Dict[str, Any], status: str,
                    reply_text: str = "", skip_reason: str = "",
                    usage: Optional[Dict[str, Any]] = None,
                    elapsed_ms: int = 0) -> None:
        if not self._data:
            return
        self._data["comments"].append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "comment_id": comment.get("comment_id", ""),
            "video_id": comment.get("video_id", ""),
            "author_name": comment.get("author_name", ""),
            "published_at": comment.get("published_at", ""),
            "comment_text": comment.get("text", ""),
            "status": status,
            "skip_reason": skip_reason,
            # 生成文はskip時も残す（deny・dry-runの事後確認用 — spec §5）
            "reply_text": reply_text,
            "usage": usage or {},
            "elapsed_ms": elapsed_ms,
        })
        self._flush()

    def end_session(self, end_reason: str) -> None:
        if not self._data:
            return
        self._data["ended_at"] = datetime.now().isoformat(timespec="seconds")
        self._data["end_reason"] = end_reason
        # 0コメント=ノーセッション: FIFOへは昇格しない (last_runには残る)
        if self._data.get("comments"):
            self._promote(self._data)
        self._flush()

    def _rescue_previous_run(self, last_run: Optional[Dict[str, Any]]) -> None:
        """A run that replied but never reached end_session (process death)
        would be silently overwritten by start_session — promote it first."""
        if not last_run or not last_run.get("comments"):
            return
        if not last_run.get("ended_at"):
            last_run["end_reason"] = "crash"
        self._promote(last_run)

    def _promote(self, session: Dict[str, Any]) -> None:
        kept = {_session_key(s) for s in self._sessions}
        if _session_key(session) in kept:
            return
        self._sessions.append(session)
        del self._sessions[:-MAX_SESSIONS_KEPT]

    def _flush(self) -> None:
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.output_path.with_suffix(".tmp")
            doc = {"last_run": self._data or None, "sessions": self._sessions}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            os.replace(str(tmp), str(self.output_path))
        except Exception as e:
            logger.warning(f"[YouTube] Session log flush failed: {e}")
