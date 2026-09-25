"""
backend/elyth_note_manager.py

ELYTH note system for AI characters — allows characters to record,
update, and remove notes about AITubers and SNS experiences during
ELYTH sessions. Pattern follows backend/note_manager.py.
"""

import logging
from typing import List

from backend.shared.constants import ELYTH_NOTE_DIR
from backend.shared.note_io import load_note_entries, save_note_entries, remove_note_entries

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELYTH_NOTE_MAX_CHARS = 3000
ELYTH_NOTE_TOOL_NAMES = frozenset({"add_elyth_note", "remove_elyth_note", "replace_elyth_note"})

# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_elyth_note(character_id: str) -> List[str]:
    """Load ELYTH note file and return list of entry strings."""
    return load_note_entries(ELYTH_NOTE_DIR, character_id, logger, label="ELYTH note")


def save_elyth_note(character_id: str, entries: List[str]) -> None:
    """Save entries as numbered list to ELYTH note file (atomic write)."""
    save_note_entries(ELYTH_NOTE_DIR, character_id, entries, logger, label="ELYTH note")


def remove_elyth_note(character_id: str) -> None:
    """Remove the ELYTH note file (character deletion). No error if absent."""
    remove_note_entries(ELYTH_NOTE_DIR, character_id, logger, label="ELYTH note")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_add_elyth_note(character_id: str, content: str, language: str) -> str:
    from backend.shared.prompt_i18n import prompt_text
    if not content or not content.strip():
        return prompt_text("res.note.empty_add", language)
    content = content.strip()
    entries = load_elyth_note(character_id)
    total = sum(len(e) for e in entries) + len(content)
    if total > ELYTH_NOTE_MAX_CHARS:
        return prompt_text("res.note.limit_add", language, max=ELYTH_NOTE_MAX_CHARS)
    entries.append(content)
    save_elyth_note(character_id, entries)
    logger.info(f"[ELYTH Note] Added entry #{len(entries)} for {character_id}")
    return prompt_text("res.elyth_note.added", language, count=len(entries))


def handle_remove_elyth_note(character_id: str, index: int, language: str) -> str:
    from backend.shared.prompt_i18n import prompt_text
    entries = load_elyth_note(character_id)
    if index < 1 or index > len(entries):
        return prompt_text("res.note.bad_index", language, index=index, count=len(entries))
    removed = entries.pop(index - 1)
    save_elyth_note(character_id, entries)
    logger.info(f"[ELYTH Note] Removed entry #{index} for {character_id}: {removed[:50]}")
    return prompt_text("res.elyth_note.removed", language, content=removed)


def handle_replace_elyth_note(character_id: str, index: int, content: str, language: str) -> str:
    from backend.shared.prompt_i18n import prompt_text
    if not content or not content.strip():
        return prompt_text("res.note.empty_replace", language)
    content = content.strip()
    entries = load_elyth_note(character_id)
    if index < 1 or index > len(entries):
        return prompt_text("res.note.bad_index", language, index=index, count=len(entries))
    old = entries[index - 1]
    entries[index - 1] = content
    total = sum(len(e) for e in entries)
    if total > ELYTH_NOTE_MAX_CHARS:
        entries[index - 1] = old
        return prompt_text("res.note.limit_replace", language, max=ELYTH_NOTE_MAX_CHARS)
    save_elyth_note(character_id, entries)
    logger.info(f"[ELYTH Note] Replaced entry #{index} for {character_id}")
    return prompt_text("res.elyth_note.replaced", language, old=old, new=content)


def dispatch_elyth_note_tool(character_id: str, tool_call: dict, language: str) -> str:
    """Dispatch an ELYTH note tool call to the appropriate handler."""
    name = tool_call.get("name", "")
    args = tool_call.get("arguments", {})

    if name == "add_elyth_note":
        return handle_add_elyth_note(character_id, args.get("content", ""), language)
    elif name == "remove_elyth_note":
        return handle_remove_elyth_note(character_id, args.get("index", 0), language)
    elif name == "replace_elyth_note":
        return handle_replace_elyth_note(
            character_id, args.get("index", 0), args.get("content", ""), language
        )
    else:
        from backend.shared.prompt_i18n import prompt_text
        return prompt_text("res.elyth_note.unknown_tool", language, name=name)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_elyth_note_prompt(character_id: str, include_system_prompt: bool = True, *, language: str) -> str:
    """Build ELYTH note content for inclusion in prompt.

    Args:
        character_id: Character ID
        include_system_prompt: If True, include the ELYTH note system prompt
            (for ELYTH session). If False, include only note content
            with create_post guidance (for normal conversation).
        language: Prompt language code (see backend.shared.prompt_i18n)
    """
    from backend.shared.prompt_i18n import prompt_section, prompt_text

    entries = load_elyth_note(character_id)
    if not entries:
        note_text = prompt_text("note.empty", language)
    else:
        note_text = "\n".join(f"{i}. {e}" for i, e in enumerate(entries, 1))

    heading = prompt_text("elyth_note.heading", language)
    if include_system_prompt:
        return f"{prompt_section('elyth_note_system', language)}\n\n{heading}\n{note_text}"
    else:
        return (
            f"{heading}\n{note_text}\n\n"
            + prompt_text("elyth_note.conversation_hint", language)
        )


# ---------------------------------------------------------------------------
# UI display
# ---------------------------------------------------------------------------

def get_elyth_note_display_html(character_id: str) -> str:
    """Return HTML for displaying ELYTH notes in the History page."""
    entries = load_elyth_note(character_id)

    css = """
    <style>
    .elyth-note-container {
        padding: 12px;
        font-family: sans-serif;
    }
    .elyth-note-empty {
        color: #888;
        font-style: italic;
        padding: 20px;
        text-align: center;
    }
    .elyth-note-entry {
        padding: 8px 12px;
        margin: 4px 0;
        border-left: 3px solid #e84393;
        background: rgba(232, 67, 147, 0.05);
        border-radius: 0 4px 4px 0;
    }
    .elyth-note-entry .elyth-note-index {
        color: #e84393;
        font-weight: bold;
        margin-right: 8px;
    }
    .elyth-note-footer {
        margin-top: 12px;
        padding-top: 8px;
        border-top: 1px solid #eee;
        font-size: 0.85em;
        color: #888;
    }
    </style>
    """

    if not entries:
        return css + '<div class="elyth-note-container"><div class="elyth-note-empty">ELYTHノートはまだありません。</div></div>'

    total_chars = sum(len(e) for e in entries)
    items_html = ""
    for i, entry in enumerate(entries, 1):
        safe_entry = entry.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        items_html += f'<div class="elyth-note-entry"><span class="elyth-note-index">{i}.</span>{safe_entry}</div>\n'

    footer = f'<div class="elyth-note-footer">{len(entries)} items / {total_chars} / {ELYTH_NOTE_MAX_CHARS} chars</div>'

    return css + f'<div class="elyth-note-container">{items_html}{footer}</div>'
