"""
backend/elyth_tools.py

Function Calling definitions, provider formatting, and dispatch for ELYTH integration.
Provides 12 ELYTH API tools + 3 ELYTH note tools = 15 total (Agent API v2).

Note: `get_notifications` / `get_timeline` / `get_thread` / `get_aituber` are
AG-side aggregates (backend/elyth/elyth_aggregates.py) — one LLM tool call
fans out to several v2 HTTP calls and returns the AG stable format
(ELYTH integration spec v6 §5, internal design doc), keeping the v1 "one call to check
the inbox / one call to see the feed" turn economy.
"""

import logging
from typing import Any, Dict, List

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELYTH_API_TOOL_NAMES = frozenset({
    "create_post", "create_reply",
    "get_notifications", "get_timeline", "mark_notifications_read",
    "get_thread", "get_my_posts", "like_post",
    "follow_aituber", "get_aituber",
    "search_posts", "get_relationships",
})

ELYTH_NOTE_TOOL_NAMES = frozenset({
    "add_elyth_note", "remove_elyth_note", "replace_elyth_note",
})

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

CREATE_POST_TOOL = {
    "name": "create_post",
    "description": "ELYTHに新しい投稿をする。500文字以内。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "投稿内容（500文字以内）"
            }
        },
        "required": ["content"]
    }
}

CREATE_REPLY_TOOL = {
    "name": "create_reply",
    "description": "ELYTHの特定の投稿にリプライする。500文字以内。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "リプライ内容（500文字以内）"
            },
            "reply_to_id": {
                "type": "string",
                "description": "返信先の投稿ID"
            }
        },
        "required": ["content", "reply_to_id"]
    }
}

GET_NOTIFICATIONS_TOOL = {
    "name": "get_notifications",
    "description": (
        "未読の通知（リプライ・メンション・フォロー・運営告知など）と"
        "自分のメトリクス（フォロワー数・投稿数・GLYPH残高）を取得する。"
        "セッションの最初に使って、返信すべきものがないか確認する。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

GET_TIMELINE_TOOL = {
    "name": "get_timeline",
    "description": (
        "ELYTHのタイムライン、トレンド、今日のトピック、新規参加AITuberを"
        "一括取得する。SNSの今の状況を把握するために使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timeline_limit": {
                "type": "integer",
                "description": "タイムラインの件数（1-50、デフォルト10）"
            }
        },
        "required": []
    }
}

MARK_NOTIFICATIONS_READ_TOOL = {
    "name": "mark_notifications_read",
    "description": "対応した通知を既読にする。対応した通知のIDをリストで渡す。",
    "parameters": {
        "type": "object",
        "properties": {
            "notification_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "既読にする通知IDのリスト"
            }
        },
        "required": ["notification_ids"]
    }
}

GET_THREAD_TOOL = {
    "name": "get_thread",
    "description": (
        "特定の投稿のスレッド全体を取得する。"
        "リプライする前に会話の文脈を確認したいときに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "post_id": {
                "type": "string",
                "description": "スレッドを取得する投稿のID"
            }
        },
        "required": ["post_id"]
    }
}

GET_MY_POSTS_TOOL = {
    "name": "get_my_posts",
    "description": (
        "自分の過去の投稿を確認する。"
        "同じような内容を投稿しないためのチェックに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "取得件数（デフォルト: 20）"
            }
        },
        "required": []
    }
}

LIKE_POST_TOOL = {
    "name": "like_post",
    "description": "投稿にいいねする。",
    "parameters": {
        "type": "object",
        "properties": {
            "post_id": {
                "type": "string",
                "description": "いいねする投稿のID"
            }
        },
        "required": ["post_id"]
    }
}

FOLLOW_AITUBER_TOOL = {
    "name": "follow_aituber",
    "description": (
        "AITuberをフォローする。"
        "aituber_idにはAITuberのID（通知のpost_author_id、タイムラインの"
        "author_id等）またはハンドル名を指定できる。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "aituber_id": {
                "type": "string",
                "description": "フォロー先のAITuber IDまたはハンドル名"
            }
        },
        "required": ["aituber_id"]
    }
}

SEARCH_POSTS_TOOL = {
    "name": "search_posts",
    "description": (
        "ハッシュタグでELYTHの投稿を検索する。"
        "話題になっているテーマの投稿を探すときに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hashtag": {
                "type": "string",
                "description": "検索するハッシュタグ（#は付けても付けなくてもよい）"
            }
        },
        "required": ["hashtag"]
    }
}

GET_RELATIONSHIPS_TOOL = {
    "name": "get_relationships",
    "description": (
        "自分のフォロワー・フォロー中・相互フォローのAITuber一覧を取得する。"
        "自分の交友関係を確認するときに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["followers", "following", "mutual"],
                "description": "取得する関係の種類"
            }
        },
        "required": ["kind"]
    }
}

GET_AITUBER_TOOL = {
    "name": "get_aituber",
    "description": (
        "特定のAITuberのプロフィールと最近の投稿を取得する。"
        "フォロー前の確認や、相手をよく知りたいときに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "AITuberのハンドル名"
            }
        },
        "required": ["handle"]
    }
}

ADD_ELYTH_NOTE_TOOL = {
    "name": "add_elyth_note",
    "description": "ELYTHノートに新しいエントリを追加する。SNSで出会ったAITuberの情報や覚えておきたいことを記録する。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "追加する内容（1行、簡潔に）"
            }
        },
        "required": ["content"]
    }
}

REMOVE_ELYTH_NOTE_TOOL = {
    "name": "remove_elyth_note",
    "description": "ELYTHノートから不要になったエントリを削除する。",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "削除するエントリの番号"
            }
        },
        "required": ["index"]
    }
}

REPLACE_ELYTH_NOTE_TOOL = {
    "name": "replace_elyth_note",
    "description": "ELYTHノートの既存エントリを更新する。関係が進展したり情報が変わったときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "更新するエントリの番号"
            },
            "content": {
                "type": "string",
                "description": "新しい内容（1行、簡潔に）"
            }
        },
        "required": ["index", "content"]
    }
}

# All ELYTH API tools (session mode: all 12)
_ELYTH_API_TOOLS = [
    CREATE_POST_TOOL, CREATE_REPLY_TOOL,
    GET_NOTIFICATIONS_TOOL, GET_TIMELINE_TOOL, MARK_NOTIFICATIONS_READ_TOOL,
    GET_THREAD_TOOL, GET_MY_POSTS_TOOL, LIKE_POST_TOOL,
    FOLLOW_AITUBER_TOOL, GET_AITUBER_TOOL,
    SEARCH_POSTS_TOOL, GET_RELATIONSHIPS_TOOL,
]

# ELYTH note tools (session mode only)
_ELYTH_NOTE_TOOLS = [
    ADD_ELYTH_NOTE_TOOL, REMOVE_ELYTH_NOTE_TOOL, REPLACE_ELYTH_NOTE_TOOL,
]

# Conversation mode: only create_post
_ELYTH_CONVERSATION_TOOLS = [CREATE_POST_TOOL]

# Wind-down mode (latter half of a session): no new replies and no feed fetches
# (create_reply / get_notifications / get_timeline / get_thread removed), so the
# session winds down with posting + housekeeping. mark_notifications_read is kept
# so the inbox can still be cleared; get_aituber is kept so profiles can still be
# looked up while wrapping up. search_posts / get_relationships stay available —
# they feed topical posting and relationship-aware notes, which is exactly the
# wind-down job (post + housekeep), not reply-chasing.
ELYTH_WINDDOWN_TOOL_NAMES = frozenset({
    "create_post", "mark_notifications_read", "like_post", "get_aituber",
    "search_posts", "get_relationships",
    "add_elyth_note", "remove_elyth_note", "replace_elyth_note",
})
_ELYTH_WINDDOWN_TOOLS = [
    t for t in (_ELYTH_API_TOOLS + _ELYTH_NOTE_TOOLS)
    if t["name"] in ELYTH_WINDDOWN_TOOL_NAMES
]


# ---------------------------------------------------------------------------
# Gemini schema conversion
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_elyth_tool_definitions_for_provider(
    provider: str,
    mode: str = "session",
    *,
    language: str
) -> List[Dict]:
    """Return ELYTH tool definitions formatted for the given provider.

    Args:
        provider: 'openai', 'xai', 'anthropic', 'google', or 'ollama'
        mode: 'session' for all 13 tools, 'conversation' for create_post only,
              'winddown' for the latter-half subset (no replies / feed fetches)
        language: Prompt language for tool descriptions (see prompt_i18n)

    Returns:
        List of provider-formatted tool definitions.
    """
    if mode == "conversation":
        source_tools = _ELYTH_CONVERSATION_TOOLS
    elif mode == "winddown":
        source_tools = _ELYTH_WINDDOWN_TOOLS
    else:
        source_tools = _ELYTH_API_TOOLS + _ELYTH_NOTE_TOOLS

    return format_tools_for_provider(provider, localize_tools(source_tools, language))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_elyth_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    character_id: str,
    api_key: str,
    language: str,
) -> Dict[str, Any]:
    """Dispatch an ELYTH tool call and return a structured result.

    Always returns a result dict — never raises exceptions.
    Callers inspect 'status' to decide retry/abort strategy.

    Args:
        tool_name: The FC tool name
        tool_args: Tool arguments dict
        character_id: Character ID (for note tools)
        api_key: ELYTH API key (for API tools)
        language: Prompt language for LLM-facing result strings

    Returns:
        {"status": str, "result_text": str}
        status: "success", "rate_limit", "auth_error", "server_error", "error"
    """
    from backend.elyth.elyth_api import (
        ElythAPIClient, ElythAPIError,
        ElythRateLimitError, ElythAuthError, ElythServerError,
    )
    from backend.shared.prompt_i18n import prompt_text

    # --- ELYTH note tools ---
    if tool_name in ELYTH_NOTE_TOOL_NAMES:
        try:
            from backend.elyth.elyth_note_manager import dispatch_elyth_note_tool
            result_text = dispatch_elyth_note_tool(character_id, {
                "name": tool_name,
                "arguments": tool_args,
            }, language)
            return {"status": "success", "result_text": result_text}
        except Exception as e:
            logger.error(f"[ELYTH] Note tool error: {tool_name}: {e}")
            return {"status": "error", "result_text": prompt_text("res.elyth.error", language, error=e)}

    # --- ELYTH API tools ---
    if tool_name not in ELYTH_API_TOOL_NAMES:
        return {"status": "error", "result_text": prompt_text("res.elyth.unknown_tool", language, name=tool_name)}

    try:
        client = ElythAPIClient(api_key)
        result = _call_api_tool(client, tool_name, tool_args, character_id)

        # Format result as readable text
        if isinstance(result, dict):
            import json
            result_text = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            result_text = str(result)

        logger.info(f"[ELYTH] {tool_name} success ({len(result_text)} chars)")
        return {"status": "success", "result_text": result_text}

    except ElythRateLimitError as e:
        logger.warning(f"[ELYTH] Rate limit: {tool_name}: {e}")
        return {"status": "rate_limit", "result_text": prompt_text("res.elyth.rate_limit", language, error=e)}
    except ElythAuthError as e:
        logger.error(f"[ELYTH] Auth error: {tool_name}: {e}")
        return {"status": "auth_error", "result_text": prompt_text("res.elyth.auth_error", language, error=e)}
    except ElythServerError as e:
        logger.warning(f"[ELYTH] Server error: {tool_name}: {e}")
        return {"status": "server_error", "result_text": prompt_text("res.elyth.server_error", language, error=e)}
    except ElythAPIError as e:
        logger.error(f"[ELYTH] API error: {tool_name}: {e}")
        return {"status": "error", "result_text": prompt_text("res.elyth.api_error", language, error=e)}
    except Exception as e:
        logger.error(f"[ELYTH] Unexpected error: {tool_name}: {e}", exc_info=True)
        return {"status": "error", "result_text": prompt_text("res.elyth.error", language, error=e)}


def _call_api_tool(
    client: "ElythAPIClient",
    tool_name: str,
    tool_args: Dict[str, Any],
    character_id: str,
) -> Dict[str, Any]:
    """Route a tool name to the v2 client / aggregation layer.

    Aggregated reads return the AG stable format (spec v6 §5); simple writes
    return the (normalized) v2 result.
    """
    from backend.elyth import elyth_aggregates as agg
    from backend.elyth.elyth_api import ElythAPIError

    if tool_name == "create_post":
        return agg.shape_write_result(client.create_post(
            content=tool_args.get("content", ""),
        ))
    elif tool_name == "create_reply":
        return agg.shape_write_result(client.create_reply(
            post_id=tool_args.get("reply_to_id", ""),
            content=tool_args.get("content", ""),
        ))
    elif tool_name == "get_notifications":
        from backend.shared.constants import ELYTH_THREAD_REPLY_CAP
        result, _suppressed = agg.build_notifications_result(
            client, character_id, ELYTH_THREAD_REPLY_CAP)
        return result
    elif tool_name == "get_timeline":
        return agg.build_timeline_result(
            client, tool_args.get("timeline_limit"))
    elif tool_name == "mark_notifications_read":
        return agg.mark_read_chunked(
            client, tool_args.get("notification_ids", []))
    elif tool_name == "get_thread":
        return agg.build_thread_result(
            client, tool_args.get("post_id", ""))
    elif tool_name == "get_my_posts":
        return agg.build_my_posts_result(
            client, tool_args.get("limit", 20))
    elif tool_name == "like_post":
        return agg.shape_write_result(client.like_post(
            post_id=tool_args.get("post_id", ""),
        ))
    elif tool_name == "follow_aituber":
        return client.follow_aituber(
            profile_ref=tool_args.get("aituber_id", ""),
        )
    elif tool_name == "get_aituber":
        return agg.build_aituber_result(
            client, tool_args.get("handle", ""))
    elif tool_name == "search_posts":
        return agg.build_search_result(
            client, tool_args.get("hashtag", ""))
    elif tool_name == "get_relationships":
        kind = str(tool_args.get("kind", "")).strip().lower()
        if kind not in agg.VALID_RELATIONSHIP_KINDS:
            raise ElythAPIError(
                f"Invalid kind '{kind}' — use one of "
                f"{', '.join(agg.VALID_RELATIONSHIP_KINDS)}")
        return agg.build_relationships_result(client, kind)
    else:
        raise ElythAPIError(f"Unknown API tool: {tool_name}")
