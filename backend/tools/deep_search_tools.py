"""
backend/deep_search_tools.py

Tool definitions, provider formatting, and dispatch for Deep Search Function Calling.
Provides search_web and read_webpage tools for API providers.
Ollama: available on tools-capable models (2026-08-11).
"""

import logging
from typing import List, Dict, Any

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEEP_SEARCH_TOOL_NAMES = frozenset({"search_web", "read_webpage"})

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

SEARCH_WEB_TOOL = {
    "name": "search_web",
    "description": (
        "インターネットでキーワード検索を行い、検索結果の一覧を取得する。"
        "詳しく調べたいとき、最新情報が必要なとき、ユーザーの質問に正確に答えるために使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "検索クエリ",
            }
        },
        "required": ["query"],
    },
}

READ_WEBPAGE_TOOL = {
    "name": "read_webpage",
    "description": (
        "指定されたURLのWebページにアクセスし、本文テキストを取得する。"
        "検索結果から気になる記事を読むとき、"
        "またはユーザーが共有したURLの内容を確認するときに使う。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "読み取るWebページのURL",
            }
        },
        "required": ["url"],
    },
}

_DEEP_SEARCH_TOOLS = [SEARCH_WEB_TOOL, READ_WEBPAGE_TOOL]


# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_deep_search_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return deep search tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_DEEP_SEARCH_TOOLS, language))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_deep_search_tool(tool_name: str, tool_args: Dict[str, Any], language: str) -> Dict[str, Any]:
    """Dispatch a deep search tool call and return structured result.

    Args:
        tool_name: "search_web" or "read_webpage"
        tool_args: Tool arguments dict

    Returns:
        {"status": "success"/"error", "result_text": str}
    """
    from backend.tools.deep_search import search_web, read_webpage

    if tool_name == "search_web":
        query = tool_args.get("query", "")
        logger.info(f"[DeepSearch] search_web: query={query!r}")
        result = search_web(query, language=language)
    elif tool_name == "read_webpage":
        url = tool_args.get("url", "")
        logger.info(f"[DeepSearch] read_webpage: url={url!r}")
        result = read_webpage(url, language=language)
    else:
        logger.warning(f"[DeepSearch] Unknown tool: {tool_name}")
        from backend.shared.prompt_i18n import prompt_text
        return {"status": "error", "result_text": prompt_text("res.deep_search.unknown_tool", language, name=tool_name)}

    if result["success"]:
        return {"status": "success", "result_text": result["result"]}
    else:
        from backend.shared.prompt_i18n import prompt_text
        return {"status": "error", "result_text": prompt_text("res.deep_search.error", language, error=result['error'])}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_deep_search_prompt(language: str) -> str:
    """Return the Deep Search system prompt instruction text."""
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("deep_search_instructions", language)
