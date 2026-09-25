"""
backend/map_search_tools.py

Tool definitions, provider formatting, and dispatch for Map Search Function Calling.
Provides search_places, get_place_details, and get_directions tools for API providers.
Ollama: available on tools-capable models (2026-08-11).

These tools are automatically enabled when the user sends location data from
the mobile UI, and disabled when the location slot expires (20-turn rule).
"""

import logging
from typing import List, Dict, Any

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAP_SEARCH_TOOL_NAMES = frozenset({"search_places", "get_place_details", "get_directions"})

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

SEARCH_PLACES_TOOL = {
    "name": "search_places",
    "description": (
        "ユーザーの現在地付近のお店や場所を検索する。"
        "「近くのラーメン」「カフェ」のような検索ができる。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "検索クエリ（例: ラーメン、カフェ、コンビニ）",
            }
        },
        "required": ["query"],
    },
}

GET_PLACE_DETAILS_TOOL = {
    "name": "get_place_details",
    "description": (
        "search_placesで取得した場所の口コミやAI要約を取得する。"
        "place_idはsearch_placesの結果から取得すること。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "place_id": {
                "type": "string",
                "description": "search_placesの結果から取得したPlace ID",
            }
        },
        "required": ["place_id"],
    },
}

GET_DIRECTIONS_TOOL = {
    "name": "get_directions",
    "description": (
        "ユーザーの現在地から目的地までの距離・所要時間・ルートを案内する。"
        "place_idはsearch_placesの結果から取得すること。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "place_id": {
                "type": "string",
                "description": "目的地のPlace ID",
            },
            "mode": {
                "type": "string",
                "description": (
                    "移動手段: walking（徒歩）, driving（車）, transit（公共交通機関）。"
                    "デフォルトはwalking。"
                ),
            },
        },
        "required": ["place_id"],
    },
}

_MAP_SEARCH_TOOLS = [SEARCH_PLACES_TOOL, GET_PLACE_DETAILS_TOOL, GET_DIRECTIONS_TOOL]


# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_map_search_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return map search tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_MAP_SEARCH_TOOLS, language))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_map_search_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    lat: float,
    lng: float,
    api_key: str,
    language: str,
) -> Dict[str, Any]:
    """Dispatch a map search tool call and return structured result.

    Args:
        tool_name: "search_places", "get_place_details", or "get_directions"
        tool_args: Tool arguments dict from the LLM
        lat: User's current latitude (from location slot)
        lng: User's current longitude (from location slot)
        api_key: Google Maps API key

    Returns:
        {"status": "success"/"error", "result_text": str}
    """
    from backend.tools.map_search import search_places, get_place_details, get_directions

    if tool_name == "search_places":
        query = tool_args.get("query", "")
        logger.info(f"[MapSearch] search_places: query={query!r}, lat={lat}, lng={lng}")
        result = search_places(query, lat, lng, api_key, language)

    elif tool_name == "get_place_details":
        place_id = tool_args.get("place_id", "")
        logger.info(f"[MapSearch] get_place_details: place_id={place_id!r}")
        result = get_place_details(place_id, api_key, language)

    elif tool_name == "get_directions":
        place_id = tool_args.get("place_id", "")
        mode = tool_args.get("mode", "walking")
        logger.info(f"[MapSearch] get_directions: place_id={place_id!r}, mode={mode}")
        result = get_directions(place_id, lat, lng, mode, api_key, language)

    else:
        logger.warning(f"[MapSearch] Unknown tool: {tool_name}")
        from backend.shared.prompt_i18n import prompt_text
        return {"status": "error", "result_text": prompt_text("res.map_search.unknown_tool", language, name=tool_name)}

    if result["success"]:
        return {"status": "success", "result_text": result["result"]}
    else:
        from backend.shared.prompt_i18n import prompt_text
        return {"status": "error", "result_text": prompt_text("res.map_search.error", language, error=result['error'])}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_map_search_prompt(language: str) -> str:
    """Return the Map Search system prompt instruction text."""
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("map_search_instructions", language)
