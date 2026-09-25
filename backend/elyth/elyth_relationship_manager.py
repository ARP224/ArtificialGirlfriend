"""
backend/elyth_relationship_manager.py

ELYTH relationship management — stores subjective relationship data
with other AITubers, updated every 3 sessions via LLM extraction.
Also handles long-term memory fragment extraction.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.shared.constants import ELYTH_RELATIONSHIP_DIR, OLLAMA_GENERATION_TIMEOUT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_elyth_relationships(character_id: str) -> Dict[str, Any]:
    """Load ELYTH relationships file.

    Returns:
        Dict with "relationships" (handle→text) and "last_updated".
    """
    path = ELYTH_RELATIONSHIP_DIR / f"{character_id}.json"
    if not path.exists():
        return {"relationships": {}, "last_updated": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[ELYTH Rel] Failed to load for {character_id}: {e}")
        return {"relationships": {}, "last_updated": None}


def save_elyth_relationships(character_id: str, data: Dict[str, Any]) -> None:
    """Save ELYTH relationships file (atomic write)."""
    ELYTH_RELATIONSHIP_DIR.mkdir(parents=True, exist_ok=True)
    path = ELYTH_RELATIONSHIP_DIR / f"{character_id}.json"
    tmp_path = path.with_suffix(".tmp")
    try:
        data["last_updated"] = datetime.now().isoformat()
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp_path), str(path))
        logger.info(f"[ELYTH Rel] Saved for {character_id}")
    except Exception as e:
        logger.error(f"[ELYTH Rel] Failed to save for {character_id}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def remove_elyth_relationships(character_id: str) -> None:
    """Remove the ELYTH relationships file (character deletion). No error if absent."""
    path = ELYTH_RELATIONSHIP_DIR / f"{character_id}.json"
    if not path.exists():
        return
    try:
        os.remove(path)
        logger.info(f"[ELYTH Rel] Removed for {character_id}")
    except Exception as e:
        logger.error(f"[ELYTH Rel] Failed to remove for {character_id}: {e}")


def build_elyth_relationship_prompt(character_id: str) -> str:
    """Build ELYTH relationship text for prompt inclusion."""
    data = load_elyth_relationships(character_id)
    rels = data.get("relationships", {})
    if not rels:
        return ""
    lines = [f"[{handle}] {text}" for handle, text in rels.items()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory extraction (LLM call)
# ---------------------------------------------------------------------------
# プロンプト本文は locales/prompts/<lang>/elyth_extraction_{system,user}.txt
# (旧・日本語ハードコードのカタログ移行。通常会話/YouTubeの抽出と同型)。


def run_elyth_memory_extraction(
    state: "BackendState",
    character_id: str,
    config: Dict[str, Any],
) -> None:
    """Run synchronous memory extraction after ELYTH session.

    Executes a single LLM call to generate:
    1. Updated ELYTH relationships
    2. Long-term memory fragments (if any)

    Runs synchronously within the session flow to keep
    elyth_session_active=True during processing.
    """
    from backend.llm.api_integration import create_llm_client
    from backend.elyth.elyth_memory import load_session_logs
    from backend.shared.prompt_i18n import get_prompt_language, prompt_section, prompt_text

    model_provider = config.get("model_provider", "ollama")
    model_name = config.get("model_name", "")

    language = get_prompt_language(config)

    # Load recent session logs for extraction
    sessions = load_session_logs(character_id, limit=3)
    if not sessions:
        logger.warning(f"[ELYTH Rel] No session logs for extraction: {character_id}")
        return

    # Build session summary text
    label_thought = prompt_text("elyth.extraction_label_thought", language)
    label_tool = prompt_text("elyth.extraction_label_tool", language)
    label_result = prompt_text("elyth.extraction_label_result", language)
    session_lines = []
    for session in sessions:
        session_lines.append(f"--- Session ({session.get('timestamp', '?')}) ---")
        for turn in session.get("turns", []):
            content = turn.get("content", "")
            if content:
                session_lines.append(f"{label_thought} {content}")
            metadata = turn.get("metadata", {})
            for tc in metadata.get("elyth_tool_calls", []):
                session_lines.append(f"{label_tool} {tc.get('name', '')}({json.dumps(tc.get('arguments', {}), ensure_ascii=False)[:100]})")
            for tr in metadata.get("elyth_tool_results", []):
                result_preview = tr.get("content", "")[:200]
                session_lines.append(f"{label_result} {tr.get('name', '')}: {result_preview}")

    session_text = "\n".join(session_lines)

    # Load previous relationships
    prev_data = load_elyth_relationships(character_id)
    prev_rels_text = ""
    if prev_data.get("relationships"):
        prev_rels_text = json.dumps(prev_data["relationships"], ensure_ascii=False, indent=2)

    # Build user prompt
    user_prompt = prompt_section(
        "elyth_extraction_user", language,
        session_text=session_text,
        prev_rels=prev_rels_text if prev_rels_text
        else prompt_text("elyth.extraction_no_relationships", language),
    )

    # Create extraction LLM client
    llm_result = create_llm_client(
        model_provider=model_provider,
        model_name=model_name,
        usage_type='extraction',
        timeout=OLLAMA_GENERATION_TIMEOUT,
    )
    if not llm_result.get("success"):
        logger.error(f"[ELYTH Rel] Failed to create LLM client: {llm_result.get('error')}")
        return

    llm = llm_result["response"]

    try:
        response = llm.invoke([
            {"role": "system", "content": prompt_section("elyth_extraction_system", language)},
            {"role": "user", "content": user_prompt},
        ])
        response_text = response.content if hasattr(response, 'content') else str(response)

        # Parse JSON response
        result = _parse_extraction_response(response_text)
        if not result:
            logger.warning(f"[ELYTH Rel] Failed to parse extraction response for {character_id}")
            return

        # Update relationships
        new_rels = result.get("relationships", {})
        if new_rels:
            # Merge with existing (new values overwrite)
            merged = prev_data.get("relationships", {})
            merged.update(new_rels)
            save_elyth_relationships(character_id, {"relationships": merged})
            logger.info(
                f"[ELYTH Rel] Updated {len(new_rels)} relationships for {character_id}"
            )

        # Save long-term memories
        memories = result.get("memories", [])
        if memories:
            _save_elyth_long_term_memories(state, character_id, memories)

    except Exception as e:
        logger.error(f"[ELYTH Rel] Extraction failed for {character_id}: {e}", exc_info=True)


def _parse_extraction_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse the LLM extraction response (JSON from markdown code block)."""
    import re

    # Try to extract JSON from markdown code block
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        text = match.group(1)

    # Try direct JSON parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in text
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _save_elyth_long_term_memories(
    state: "BackendState",
    character_id: str,
    memories: List[str],
) -> None:
    """Save extracted memory fragments to the long-term memory store."""
    memory_manager = state.memory_managers.get(character_id)
    if not memory_manager:
        logger.warning(f"[ELYTH Rel] No MemoryManager for {character_id}, skipping memory save")
        return

    for fragment in memories:
        if not fragment or not fragment.strip():
            continue
        try:
            result = memory_manager.add_memory_manual(
                category="elyth",
                content=fragment.strip(),
            )
            if result.get("success"):
                logger.info(f"[ELYTH Rel] Saved memory fragment for {character_id}: {fragment[:50]}")
            else:
                logger.warning(f"[ELYTH Rel] add_memory_manual failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[ELYTH Rel] Failed to save memory fragment: {e}")
