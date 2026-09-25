"""
backend/note_manager.py

Note system for AI characters — allows characters to autonomously
record, update, and remove short-to-medium term memory notes.
API providers and tools-capable Ollama models (2026-08-11).
"""

import logging
from typing import List, Dict

from backend.shared.constants import NOTE_DIR
from backend.shared.note_io import load_note_entries, save_note_entries, remove_note_entries
from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTE_MAX_CHARS = 3000
NOTE_TOOL_NAMES = frozenset({"add_note", "remove_note", "replace_note"})

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

ADD_NOTE_TOOL = {
    "name": "add_note",
    "description": "ノートに新しい項目を追加する。ユーザーについて覚えておくべき重要な情報があるときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "追加する項目の内容（1行、簡潔に、日本語で）"
            }
        },
        "required": ["content"]
    }
}

REMOVE_NOTE_TOOL = {
    "name": "remove_note",
    "description": "ノートから不要になった項目を削除する。古くなった情報や誤りを取り除くときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "削除する項目の番号"
            }
        },
        "required": ["index"]
    }
}

REPLACE_NOTE_TOOL = {
    "name": "replace_note",
    "description": "ノートの既存の項目を更新する。情報が変わったときや、より正確な情報が得られたときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "置き換える項目の番号"
            },
            "content": {
                "type": "string",
                "description": "新しい内容（1行、簡潔に、日本語で）"
            }
        },
        "required": ["index", "content"]
    }
}

_NOTE_TOOLS = [ADD_NOTE_TOOL, REMOVE_NOTE_TOOL, REPLACE_NOTE_TOOL]


def get_note_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return note tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_NOTE_TOOLS, language))


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_note(character_id: str) -> List[str]:
    """Load note file and return list of entry strings."""
    return load_note_entries(NOTE_DIR, character_id, logger, label="note")


def save_note(character_id: str, entries: List[str]) -> None:
    """Save entries as numbered list to note file (atomic write)."""
    save_note_entries(NOTE_DIR, character_id, entries, logger, label="note")


def remove_note(character_id: str) -> None:
    """Remove the note file (character deletion). No error if absent."""
    remove_note_entries(NOTE_DIR, character_id, logger, label="note")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_add_note(character_id: str, content: str, language: str) -> str:
    """Add a new entry to the note."""
    from backend.shared.prompt_i18n import prompt_text
    if not content or not content.strip():
        return prompt_text("res.note.empty_add", language)
    content = content.strip()
    entries = load_note(character_id)
    total = sum(len(e) for e in entries) + len(content)
    if total > NOTE_MAX_CHARS:
        return prompt_text("res.note.limit_add", language, max=NOTE_MAX_CHARS)
    entries.append(content)
    save_note(character_id, entries)
    logger.info(f"[Note] Added entry #{len(entries)} for {character_id}")
    return prompt_text("res.note.added", language, count=len(entries))


def handle_remove_note(character_id: str, index: int, language: str) -> str:
    """Remove an entry from the note by 1-based index."""
    from backend.shared.prompt_i18n import prompt_text
    entries = load_note(character_id)
    if index < 1 or index > len(entries):
        return prompt_text("res.note.bad_index", language, index=index, count=len(entries))
    removed = entries.pop(index - 1)
    save_note(character_id, entries)
    logger.info(f"[Note] Removed entry #{index} for {character_id}: {removed[:50]}")
    return prompt_text("res.note.removed", language, content=removed)


def handle_replace_note(character_id: str, index: int, content: str, language: str) -> str:
    """Replace an entry in the note by 1-based index."""
    from backend.shared.prompt_i18n import prompt_text
    if not content or not content.strip():
        return prompt_text("res.note.empty_replace", language)
    content = content.strip()
    entries = load_note(character_id)
    if index < 1 or index > len(entries):
        return prompt_text("res.note.bad_index", language, index=index, count=len(entries))
    old = entries[index - 1]
    entries[index - 1] = content
    total = sum(len(e) for e in entries)
    if total > NOTE_MAX_CHARS:
        entries[index - 1] = old  # Rollback
        return prompt_text("res.note.limit_replace", language, max=NOTE_MAX_CHARS)
    save_note(character_id, entries)
    logger.info(f"[Note] Replaced entry #{index} for {character_id}")
    return prompt_text("res.note.replaced", language, old=old, new=content)


def dispatch_note_tool(character_id: str, tool_call: dict, language: str) -> str:
    """Dispatch a note tool call to the appropriate handler."""
    name = tool_call.get("name", "")
    args = tool_call.get("arguments", {})

    if name == "add_note":
        return handle_add_note(character_id, args.get("content", ""), language)
    elif name == "remove_note":
        return handle_remove_note(character_id, args.get("index", 0), language)
    elif name == "replace_note":
        return handle_replace_note(
            character_id, args.get("index", 0), args.get("content", ""), language
        )
    else:
        from backend.shared.prompt_i18n import prompt_text
        return prompt_text("res.note.unknown_tool", language, name=name)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_note_prompt(character_id: str, language: str) -> str:
    """Build instructions + note content for inclusion in system prompt."""
    from backend.shared.prompt_i18n import prompt_section, prompt_text
    entries = load_note(character_id)
    if not entries:
        note_text = prompt_text("note.empty", language)
    else:
        note_text = "\n".join(f"{i}. {e}" for i, e in enumerate(entries, 1))

    return (f"{prompt_section('note_system', language)}\n\n"
            f"{prompt_text('note.heading', language)}\n{note_text}")


# ---------------------------------------------------------------------------
# UI display
# ---------------------------------------------------------------------------

def get_note_display_html(character_id: str) -> str:
    """Return HTML for displaying notes in the History page."""
    entries = load_note(character_id)

    css = """
    <style>
    .note-container {
        padding: 12px;
        font-family: sans-serif;
    }
    .note-empty {
        color: #888;
        font-style: italic;
        padding: 20px;
        text-align: center;
    }
    .note-entry {
        padding: 8px 12px;
        margin: 4px 0;
        border-left: 3px solid #6c5ce7;
        background: rgba(108, 92, 231, 0.05);
        border-radius: 0 4px 4px 0;
    }
    .note-entry .note-index {
        color: #6c5ce7;
        font-weight: bold;
        margin-right: 8px;
    }
    .note-footer {
        margin-top: 12px;
        padding-top: 8px;
        border-top: 1px solid #eee;
        font-size: 0.85em;
        color: #888;
    }
    </style>
    """

    if not entries:
        return css + '<div class="note-container"><div class="note-empty">ノートはまだありません。</div></div>'

    total_chars = sum(len(e) for e in entries)
    items_html = ""
    for i, entry in enumerate(entries, 1):
        safe_entry = entry.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        items_html += f'<div class="note-entry"><span class="note-index">{i}.</span>{safe_entry}</div>\n'

    footer = f'<div class="note-footer">{len(entries)} items / {total_chars} / {NOTE_MAX_CHARS} chars</div>'

    return css + f'<div class="note-container">{items_html}{footer}</div>'
