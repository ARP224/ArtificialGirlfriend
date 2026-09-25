"""
backend/elyth_memory.py

Short-term memory for ELYTH sessions — FIFO session log management.
Stores session logs in a provider-independent rich format.
Converts to provider-specific messages on load.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from backend.shared.constants import ELYTH_SESSION_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SESSIONS_KEPT = 5     # FIFO: keep last 5 sessions
DEFAULT_LOAD_LIMIT = 3    # Load last 3 for prompt

# 削除済みキャラの id(プロセスローカルの墓標)。save_session_log が書き戻しを
# 拒否するために見る。詳細な理由は remove_session_log の docstring。
_REMOVED_CHARACTER_IDS = set()

# Tool results are replaced with the res.elyth.get_info_placeholder catalog
# text when replaying past sessions.
# Rationale: these tools return time-dependent or bulky data that is no longer
# relevant to the current session — only the fact that they were called matters.
# `create_post` / `create_reply` / note ops are kept since self-statements and
# notebook actions carry continuity value.
_OMIT_TOOL_NAMES = frozenset({
    "get_notifications",  # Notifications age out — old ones have no value
    "get_timeline",        # Timeline is ephemeral by nature
    "get_thread",          # Thread context is session-local
    "get_my_posts",        # Create_post records already capture this
    "get_aituber",         # Profiles can be re-fetched when needed; notes/relationships persist the essentials
})

# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------

def save_session_log(character_id: str, session_data: Dict[str, Any]) -> None:
    """Upsert a session into the log file with FIFO trimming.

    セッション途中の逐次保存(ハードkill耐性)に対応するため upsert 意味論:
    同じ session_id を持つ既存エントリがあれば置換、なければ append。
    session_id を持たない旧形式エントリは置換対象にならない。

    Args:
        character_id: Character ID
        session_data: Session dict with keys: session_id, timestamp,
                      character_id, end_reason, turns[]
    """
    if character_id in _REMOVED_CHARACTER_IDS:
        # 削除済みキャラへの書き戻し=ファイルが復活して孤児になる。生き残った
        # セッションスレッドからの遅延保存を最後に止める線(remove_session_log 参照)
        logger.warning(
            f"[ELYTH Memory] Session save ignored for removed character {character_id}")
        return

    ELYTH_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = ELYTH_SESSION_DIR / f"{character_id}.json"

    # Load existing
    data = {"sessions": []}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[ELYTH Memory] Failed to load existing log for {character_id}: {e}")
            data = {"sessions": []}

    # Upsert: replace the entry for this session if already saved, else append
    session_id = session_data.get("session_id")
    for i, existing in enumerate(data["sessions"]):
        if session_id and existing.get("session_id") == session_id:
            data["sessions"][i] = session_data
            break
    else:
        data["sessions"].append(session_data)

    # FIFO trim
    if len(data["sessions"]) > MAX_SESSIONS_KEPT:
        data["sessions"] = data["sessions"][-MAX_SESSIONS_KEPT:]

    # Atomic write
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp_path), str(path))
        logger.info(
            f"[ELYTH Memory] Saved session for {character_id} "
            f"({len(session_data.get('turns', []))} turns, "
            f"end_reason={session_data.get('end_reason', 'unknown')})"
        )
    except Exception as e:
        logger.error(f"[ELYTH Memory] Failed to save session for {character_id}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def load_session_logs(character_id: str, limit: int = DEFAULT_LOAD_LIMIT) -> List[Dict[str, Any]]:
    """Load the most recent session logs.

    Args:
        character_id: Character ID
        limit: Number of recent sessions to load

    Returns:
        List of session dicts (most recent last), may be empty.
    """
    path = ELYTH_SESSION_DIR / f"{character_id}.json"
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sessions = data.get("sessions", [])
        return sessions[-limit:] if len(sessions) > limit else sessions
    except Exception as e:
        logger.error(f"[ELYTH Memory] Failed to load logs for {character_id}: {e}")
        return []


# ---------------------------------------------------------------------------
# Removal (character deletion)
# ---------------------------------------------------------------------------

def remove_session_log(character_id: str) -> None:
    """Forget a character (character deletion): its session-log file and the
    atomic-write temp sidecar. No error if absent.

    <uuid>.tmp は save_session_log の os.replace が途中で落ちたときの残骸
    (Path("<uuid>.json").with_suffix(".tmp") == "<uuid>.tmp")。.db に対する
    -wal/-shm と同じで、本体だけ消すと付随ファイルが孤児として残る。

    墓標(_REMOVED_CHARACTER_IDS)を先に立てる理由: ELYTH セッションは
    _session_task が queue_manager のデーモンスレッドで走り、
    future.result(timeout=600) が切れても Python はスレッドを殺せないので、
    elyth_session_active が False に落ちた後も生き続けて save_session_log を
    呼びうる。remove_character 側のガードは「拒否して理由をユーザーに伝える」
    ためのもので、この競合そのものは塞げない。
    config の存在確認ではなくプロセスローカルの墓標なのは、remove_character が
    remover 群(ここ)を config 削除より先に走らせるため、config を見ると
    「まだ存在する」窓が残るから。
    """
    _REMOVED_CHARACTER_IDS.add(character_id)
    for path in (ELYTH_SESSION_DIR / f"{character_id}.json",
                 ELYTH_SESSION_DIR / f"{character_id}.tmp"):
        if not path.exists():
            continue
        try:
            os.remove(path)
            logger.info(f"[ELYTH Memory] Removed {path.name} for {character_id}")
        except Exception as e:
            logger.error(
                f"[ELYTH Memory] Failed to remove {path.name} for {character_id}: {e}")


# ---------------------------------------------------------------------------
# Provider-specific conversion
# ---------------------------------------------------------------------------

def convert_session_to_messages(
    session: Dict[str, Any],
    provider: str,
    omit_get_info: bool = True,
    *,
    language: str,
) -> List[Dict[str, Any]]:
    """Convert a stored session log to provider-specific message array.

    Each turn becomes:
      1. An assistant message (text + tool_calls)
      2. Tool result messages (one per tool result)

    Args:
        session: A single session dict from the log
        provider: 'openai', 'xai', 'anthropic', or 'google'
        omit_get_info: If True, replace get_information results with placeholder
        language: Prompt language for the placeholder text (reaches the LLM)

    Returns:
        List of provider-formatted messages.
    """
    from backend.llm.api_integration import format_tool_result_message
    from backend.shared.prompt_i18n import prompt_text

    messages = []
    for turn in session.get("turns", []):
        content = turn.get("content", "")
        metadata = turn.get("metadata", {})
        tool_calls = metadata.get("elyth_tool_calls", [])
        tool_results = metadata.get("elyth_tool_results", [])

        # Build assistant message with tool calls
        assistant_msg = _build_assistant_msg_from_log(content, tool_calls, provider)
        if assistant_msg:
            messages.append(assistant_msg)

        # Build tool result messages
        for tr in tool_results:
            tr_id = tr.get("id", "")
            tr_name = tr.get("name", "")
            tr_content = tr.get("content", "")

            # Omit bulk-information tool results from past sessions (see _OMIT_TOOL_NAMES).
            if omit_get_info and tr_name in _OMIT_TOOL_NAMES:
                tr_content = prompt_text("res.elyth.get_info_placeholder", language)

            messages.append(
                format_tool_result_message(provider, tr_id, tr_name, tr_content)
            )

    return messages


def _build_assistant_msg_from_log(
    content: str,
    tool_calls: List[Dict[str, Any]],
    provider: str,
) -> Optional[Dict[str, Any]]:
    """Build a provider-specific assistant message from stored log data.

    This is the log-restoration equivalent of build_assistant_msg_with_tool_calls,
    but works from dict data instead of APIResponse.
    """
    if not content and not tool_calls:
        return None

    if provider in ("openai", "xai"):
        # Responses API format
        items = []
        if content:
            items.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            })
        for tc in tool_calls:
            items.append({
                "type": "function_call",
                "id": tc.get("id", ""),
                "call_id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
            })
        return {"type": "response_output", "items": items}

    elif provider == "anthropic":
        # Anthropic Messages API format
        content_blocks = []
        if content:
            content_blocks.append({"type": "text", "text": content})
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "input": tc.get("arguments", {}),
            })
        return {"role": "assistant", "content": content_blocks}

    elif provider == "google":
        # Gemini format
        parts = []
        if content:
            parts.append({"text": content})
        for tc in tool_calls:
            parts.append({
                "functionCall": {
                    "name": tc.get("name", ""),
                    "args": tc.get("arguments", {}),
                }
            })
        return {"role": "model", "parts": parts}

    else:
        # Fallback
        return {"role": "assistant", "content": content or ""}


# ---------------------------------------------------------------------------
# Session data builder (helper for session manager)
# ---------------------------------------------------------------------------

def create_session_data(character_id: str) -> Dict[str, Any]:
    """Create a new empty session data structure.

    end_reason の初期値は "interrupted": 正常経路では必ず実際の終了理由で
    上書き保存されるため、この値がログに残る＝途中で強制終了された回。
    """
    return {
        "session_id": uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "character_id": character_id,
        "end_reason": "interrupted",
        "turns": [],
    }


def add_turn_to_session(
    session_data: Dict[str, Any],
    content: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Add a completed turn to session data.

    Args:
        session_data: The session data dict to append to
        content: Assistant's thought text
        tool_calls: List of {"id", "name", "arguments"} dicts
        tool_results: List of {"id", "name", "content"} dicts
    """
    turn = {
        "role": "assistant",
        "content": content or "",
        "timestamp": datetime.now().isoformat(),
        "metadata": {},
    }
    if tool_calls:
        turn["metadata"]["elyth_tool_calls"] = tool_calls
    if tool_results:
        turn["metadata"]["elyth_tool_results"] = tool_results
    session_data["turns"].append(turn)
