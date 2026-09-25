"""
backend/talk_theme_tools.py

Talk theme management via Function Calling for API providers.
Allows AI characters to set, update, and clear talk themes during conversation.
Ollama: full support on tools-capable models; user-side theme only otherwise (2026-08-11).
"""

import logging
from pathlib import Path
from typing import List, Dict

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TALK_THEME_TOOL_NAMES = frozenset({"set_talk_theme", "clear_talk_theme"})

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

SET_TALK_THEME_TOOL = {
    "name": "set_talk_theme",
    "description": "トークテーマを設定または更新する。会話の流れに応じて、新しい議題を設定したいときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "theme": {
                "type": "string",
                "description": "設定するトークテーマ"
            }
        },
        "required": ["theme"]
    }
}

CLEAR_TALK_THEME_TOOL = {
    "name": "clear_talk_theme",
    "description": "現在のトークテーマをクリアする。話題を自由にしたいときに使う。",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

_TALK_THEME_TOOLS = [SET_TALK_THEME_TOOL, CLEAR_TALK_THEME_TOOL]


# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_talk_theme_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return talk theme tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_TALK_THEME_TOOLS, language))


# ---------------------------------------------------------------------------
# Theme save logic
# ---------------------------------------------------------------------------

def _save_theme(character_id: str, theme: str, language: str) -> str:
    """
    Save talk theme to character config file.

    Args:
        character_id: Character ID
        theme: New theme text (empty string to clear)
        language: Prompt language for LLM-facing result strings

    Returns:
        Empty string on success, error message on failure
    """
    from backend.conversation.character_manager import (
        load_character_config,
        _save_config_file,
        _get_config_filepath,
        _character_lock,
        _character_config_cache
    )
    from backend.shared.prompt_i18n import prompt_text

    try:
        with _character_lock:
            config = load_character_config(character_id)
            if not config:
                return prompt_text("res.talk_theme.no_config", language)

            config['talk_theme'] = theme

            config_path = _get_config_filepath(character_id)
            _save_config_file(Path(config_path), config)
            _character_config_cache[character_id] = config.copy()

            character_name = config.get('name') or 'Unknown'
            if theme:
                logger.info(f"[TalkTheme] Character '{character_name}' ({character_id}) set theme: '{theme}'")
            else:
                logger.info(f"[TalkTheme] Character '{character_name}' ({character_id}) cleared theme")

        # Phase 4B: broadcast talk_theme_updated (snapshot s90).
        # AI tool path → mirrors the user-button path in backend.update_talk_theme().
        try:
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_talk_theme_updated_sync(theme, character_id)
        except Exception as broadcast_err:
            logger.warning(f"[TalkTheme] broadcast failed: {broadcast_err}")

        return ""  # Success
    except Exception as e:
        logger.error(f"[TalkTheme] Failed to save theme for {character_id}: {e}")
        from backend.shared.prompt_i18n import prompt_text
        return prompt_text("res.talk_theme.save_failed", language, error=e)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_set_talk_theme(character_id: str, theme: str, language: str) -> str:
    """Set a new talk theme."""
    from backend.shared.prompt_i18n import prompt_text
    if not theme or not theme.strip():
        return prompt_text("res.talk_theme.empty", language)
    theme = theme.strip()
    error = _save_theme(character_id, theme, language)
    if error:
        return error
    return prompt_text("res.talk_theme.set", language, theme=theme)


def handle_clear_talk_theme(character_id: str, language: str) -> str:
    """Clear the current talk theme."""
    error = _save_theme(character_id, "", language)
    if error:
        return error
    from backend.shared.prompt_i18n import prompt_text
    return prompt_text("res.talk_theme.cleared", language)


def dispatch_talk_theme_tool(character_id: str, tool_call: Dict, language: str) -> str:
    """Dispatch a talk theme tool call to the appropriate handler."""
    name = tool_call.get("name", "")
    args = tool_call.get("arguments", {})

    if name == "set_talk_theme":
        return handle_set_talk_theme(character_id, args.get("theme", ""), language)
    elif name == "clear_talk_theme":
        return handle_clear_talk_theme(character_id, language)
    else:
        from backend.shared.prompt_i18n import prompt_text
        return prompt_text("res.talk_theme.unknown_tool", language, name=name)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_talk_theme_prompt(talk_theme: str, language: str) -> str:
    """
    Build the unified talk theme prompt for API providers.
    Includes FC tool descriptions, current theme status, and behavioral guidance.

    Args:
        talk_theme: Current theme text (empty string if unset)
        language: Language code ("ja" or "en")

    Returns:
        Complete talk theme prompt string
    """
    from backend.shared.prompt_i18n import prompt_section, prompt_text

    theme_display = talk_theme if talk_theme else prompt_text("talk_theme.none", language)
    prompt = prompt_section("talk_theme_api_base", language, theme_display=theme_display)

    if talk_theme:
        prompt += "\n\n" + prompt_section("talk_theme_api_discussion", language)

    return prompt
