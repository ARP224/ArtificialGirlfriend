"""
backend/relationship_manager.py

Relationship section manager for AI characters — tracks and updates
the dynamic relationship between each character and the user.
全プロバイダ対象(C8: 旧API限定を撤去。素の生成なのでcapability非依存・
2026-08-11 稜裁定。パース失敗=前テキスト温存の安全弁つき)。

The relationship is a ~1000 char text with 3 sections:
  - Impression of the user
  - Feelings toward the user
  - How the character wants to interact going forward

Updated via background LLM calls alongside memory extraction.
"""

import os
import re
import logging
from typing import Optional, Tuple

from backend.shared.constants import RELATIONSHIP_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELATIONSHIP_MAX_CHARS = 1000

# ---------------------------------------------------------------------------
# Initial template (per-language, from the prompt catalog)
# Replaced after ~30 unprocessed messages (user+assistant counted separately,
# i.e. ~15 exchanges; RELATIONSHIP_INITIAL_THRESHOLD) with LLM-generated content
# in the character's language.
# ---------------------------------------------------------------------------

def _initial_template(language: str) -> str:
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("relationship_initial_template", language)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def load_relationship(character_id: str, language: str) -> str:
    """Load relationship text for the character. Returns initial template if no file."""
    path = RELATIONSHIP_DIR / f"{character_id}.txt"
    if not path.exists():
        return _initial_template(language)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content if content.strip() else _initial_template(language)
    except Exception as e:
        logger.error(f"Failed to load relationship for {character_id}: {e}")
        return _initial_template(language)


def save_relationship(character_id: str, content: str) -> None:
    """Save relationship text (atomic write via temp file + os.replace)."""
    RELATIONSHIP_DIR.mkdir(parents=True, exist_ok=True)
    path = RELATIONSHIP_DIR / f"{character_id}.txt"
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(str(tmp_path), str(path))
        logger.debug(f"Saved relationship for {character_id} ({len(content)} chars)")
    except Exception as e:
        logger.error(f"Failed to save relationship for {character_id}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def remove_relationship(character_id: str) -> None:
    """Remove relationship file for a character. No error if file doesn't exist."""
    path = RELATIONSHIP_DIR / f"{character_id}.txt"
    if path.exists():
        try:
            os.remove(path)
            logger.info(f"Removed relationship file for {character_id}")
        except Exception as e:
            logger.error(f"Failed to remove relationship for {character_id}: {e}")


# ---------------------------------------------------------------------------
# Template detection
# ---------------------------------------------------------------------------


# 旧版初期テンプレの凍結文字列。テンプレ改稿(例: 2026-08-02 en の【】→[ ]化)
# 後も、旧版のまま保存済みのファイルを「初期状態」と認識し続けるための比較
# 対象(完全一致比較のため、改稿のたびに旧全文をここへ追加する)。
_LEGACY_INITIAL_TEMPLATES = (
    "【Impression of the user】We've only just met, so no impression has formed yet.\n"
    "【Feelings toward the user】We've just met, so I don't have any particular feelings yet.\n"
    "【How I want to interact going forward】First, I want us to get to know each other.",
)


def is_initial_template(content: str) -> bool:
    """Check if the content is the initial template (not yet updated by LLM).

    保存済みファイルはどの言語の初期テンプレでもありうるため全言語と比較する
    (テンプレ改稿前に保存された旧版も凍結リストで拾う)。
    """
    from backend.shared.prompt_i18n import available_prompt_languages
    stripped = content.strip()
    if stripped in _LEGACY_INITIAL_TEMPLATES:
        return True
    return any(stripped == _initial_template(lang).strip()
               for lang in available_prompt_languages())


# ---------------------------------------------------------------------------
# Prompt construction (for inclusion in conversation prompt)
# ---------------------------------------------------------------------------


def build_relationship_prompt(character_id: str, language: str) -> str:
    """Return relationship text for inclusion in the system prompt."""
    return load_relationship(character_id, language)


# ---------------------------------------------------------------------------
# Extraction prompt construction (for background LLM call)
# ---------------------------------------------------------------------------


def build_relationship_extraction_prompt(
    system_prompt: str,
    notes: str,
    memories: str,
    previous_relationship: str,
    conversation: str,
    language: str,
) -> Tuple[str, str]:
    """
    Build the (system_prompt, user_prompt) pair for the relationship extraction LLM call.

    Args:
        system_prompt: Character's system prompt (personality definition)
        notes: Character's note entries (may be empty)
        memories: Formatted long-term memory entries (may be empty)
        previous_relationship: Current relationship text (or initial template)
        conversation: Formatted recent conversation ("User: ... / Character: ...")
        language: Language code ("ja" or "en")

    Returns:
        Tuple of (system_prompt_text, user_prompt_text) for the LLM call
    """
    from backend.shared.prompt_i18n import prompt_section
    system_prompt_text = prompt_section("relationship_extraction_system", language)
    user_prompt_template = prompt_section("relationship_extraction_user", language)

    # Build optional sections
    notes_section = ""
    if notes.strip():
        notes_section = f"<notes>\n{notes}\n</notes>\n\n"

    memories_section = ""
    if memories.strip():
        memories_section = f"<long_term_memory>\n{memories}\n</long_term_memory>\n\n"

    user_prompt = (
        user_prompt_template
        .replace("{system_prompt}", system_prompt)
        .replace("{notes_section}", notes_section)
        .replace("{memories_section}", memories_section)
        .replace("{previous_relationship}", previous_relationship)
        .replace("{conversation}", conversation)
    )

    return system_prompt_text, user_prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_relationship_response(response_text: str) -> Optional[str]:
    """
    Parse the LLM response into a clean relationship text.

    Strategy:
    1. Try to extract content with 【】 (ja) / line-leading [ ] (en) headers
       (expected formats) — trims any LLM preamble before the first header
    2. If no markers found, strip code blocks and use the full response
    3. Truncate to RELATIONSHIP_MAX_CHARS if needed

    Returns:
        Cleaned relationship text, or None if response is empty/invalid
    """
    if not response_text or not response_text.strip():
        return None

    text = response_text.strip()

    # Remove markdown code blocks if present
    text = re.sub(r"^```[\w]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    # Check if it contains the expected header format. ja uses 【】 (unambiguous
    # anywhere); en uses ASCII [ ] which is anchored to line starts to avoid
    # matching citations/links in the body (2026-08-02 en template ASCII化).
    markers = re.findall(r"【[^】]+】", text) or re.findall(r"^\[[^\]\n]+\]", text, re.M)
    if len(markers) >= 2:
        # Extract from first header to end of text
        first_marker_pos = text.index(markers[0])
        text = text[first_marker_pos:].strip()

    if not text:
        return None

    # Truncate if over limit
    if len(text) > RELATIONSHIP_MAX_CHARS:
        text = text[:RELATIONSHIP_MAX_CHARS]
        logger.warning(
            f"Relationship text truncated from {len(response_text)} to {RELATIONSHIP_MAX_CHARS} chars"
        )

    return text


# ---------------------------------------------------------------------------
# Background worker (moved from conversation_manager — B10 / SL18 Relationship)
# ---------------------------------------------------------------------------


def run_relationship_update(state, character_id: str, load_character_config) -> None:
    """
    Background task to update relationship summary (all providers since C8).
    Called alongside memory extraction, or earlier for initial template replacement.

    Moved verbatim from ConversationManager._update_relationship_task (B10):
    app-layer state and the load_character_config callable are injected so the
    worker lives in its SL18 Relationship home while the spine keeps a thin delegate.
    """
    if state.shutdown_flag.is_set():
        logger.info(f"Skipping relationship update for {character_id} - shutdown in progress")
        return

    memory_manager = state.memory_managers.get(character_id)
    if not memory_manager:
        if hasattr(state, '_relationship_update_pending'):
            state._relationship_update_pending.discard(character_id)
        logger.warning(f"No memory manager for {character_id}, skipping relationship update")
        return

    try:
        # 1. Load character config
        config_response = load_character_config(character_id)
        if isinstance(config_response, dict) and 'success' in config_response:
            config = config_response.get('result', {}) if config_response.get('success') else {}
        else:
            config = config_response or {}
        if not config:
            logger.warning(f"No config for {character_id}, skipping relationship update")
            return

        system_prompt = config.get("system_prompt", "")
        model_provider = config.get("model_provider", "ollama")
        model_name = config.get("model_name", "")
        from backend.shared.prompt_i18n import get_prompt_language
        language = get_prompt_language(config)

        # 2. Load notes (if any)
        notes_text = ""
        from backend.memory.note_manager import load_note
        entries = load_note(character_id)
        if entries:
            notes_text = "\n".join(f"{i}. {e}" for i, e in enumerate(entries, 1))

        # 3. Long-term memory: latest 20 entries (broad overview, not semantic search)
        memories_text = ""
        all_memories = memory_manager.get_all_memories()
        recent_memories = all_memories[-20:] if len(all_memories) > 20 else all_memories
        if recent_memories:
            mem_lines = [f"[{m['category']}] {m['content']}" for m in recent_memories]
            memories_text = "\n".join(mem_lines)

        # 4. Previous relationship text
        previous_relationship = load_relationship(character_id, language)

        # 5. Conversation history: last 50 messages
        recent_msgs = memory_manager.get_messages_for_prompt(limit=50)
        conversation_lines = []
        for msg in recent_msgs:
            role = "User" if msg.get("role") == "user" else "Character"
            conversation_lines.append(f"{role}: {msg.get('content', '')}")
        conversation_text = "\n".join(conversation_lines)

        # 6. Build extraction prompt
        sys_prompt, user_prompt = build_relationship_extraction_prompt(
            system_prompt=system_prompt,
            notes=notes_text,
            memories=memories_text,
            previous_relationship=previous_relationship,
            conversation=conversation_text,
            language=language
        )

        # 7. Call LLM (extraction tuning: temperature=0.3)
        from backend.llm.api_integration import create_llm_client
        from backend.shared.constants import OLLAMA_GENERATION_TIMEOUT
        llm_result = create_llm_client(
            model_provider=model_provider,
            model_name=model_name,
            usage_type='extraction',
            timeout=OLLAMA_GENERATION_TIMEOUT
        )
        if not llm_result.get("success"):
            raise RuntimeError(f"Failed to create LLM client: {llm_result.get('error')}")

        llm = llm_result["response"]
        llm_messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ]
        response = llm.invoke(llm_messages)
        response_text = response.content if hasattr(response, 'content') else response[-1].content

        logger.info(f"Relationship extraction LLM response: {len(response_text)} chars for {character_id}")

        # 8. Parse and save
        new_relationship = parse_relationship_response(response_text)
        if new_relationship:
            save_relationship(character_id, new_relationship)
            logger.info(f"Relationship updated for {character_id} ({len(new_relationship)} chars)")
        else:
            logger.warning(f"Failed to parse relationship response for {character_id}")

    except Exception as e:
        logger.error(f"Relationship update failed for {character_id}: {e}", exc_info=True)
        # On failure, preserve previous relationship text (do nothing)
    finally:
        # Clear pending flag to allow future triggers
        if hasattr(state, '_relationship_update_pending'):
            state._relationship_update_pending.discard(character_id)
