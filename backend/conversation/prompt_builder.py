"""
prompt_builder.py

Domain-layer prompt assembly extracted from conversation_manager.py (ST4/B10,
first spine seam). Builds the provider-agnostic ``messages`` array for the final
LLM prompt: system message (character system prompt + tool instructions +
memory) plus role-based conversation history. The current-time note is NOT in
the system message: ``build_current_time_context()`` is prepended to the
outgoing latest user message by the caller at send time (2026-08-01 稜裁定).

This module owns ONLY the deterministic assembly (golden-protected by
tests/golden/test_prompt_messages.py). Turn-state policy that gates which tools
to include (rate trackers + ``_should_include_*``) stays on ConversationManager
(app layer) and is injected via :class:`PromptBuildDeps`, so the dependency
direction is domain ← (injected) app policy, not a back-reference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

from backend.shared.timing_logger import timing_log

logger = logging.getLogger(__name__)


def build_current_time_context() -> str:
    """現在時刻の注記 ``(Current time: YYYY/MM/DD(Www) HH:MM)\\n\\n`` を返す。

    2026-08-01 稜裁定で system 先頭から「送信直前の最新userメッセージの
    先頭・非永続」へ移設。付与は conversation_manager._generate_reply_task
    が送信専用コピー(converted_msgs)にのみ行い、履歴保存・記憶検索クエリ
    には残らない。system 先頭に置く旧方式は分をまたぐたび Ollama の
    プレフィックスKVキャッシュを全滅させ、毎ターンのフル prompt eval を
    誘発していた。書式は移設前とバイト同一。
    """
    # 関数内import: tests/conftest.py の凍結時計は datetime モジュール属性の
    # 差し替えで効く。呼び出し時に属性解決するこの形を崩すと frozen_time が
    # 効かずゴールデンが実時刻で割れる
    from datetime import datetime
    now = datetime.now()
    # Hardcoded English names: strftime('%a') is locale-dependent and
    # would break the prompt byte contract on non-C locales (macOS).
    # Same scheme as elyth_session_manager.py.
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()]
    return f"(Current time: {now.strftime('%Y/%m/%d')}({weekday}) {now.strftime('%H:%M')})\n\n"


@dataclass
class PromptBuildDeps:
    """Injected app-layer collaborators required to assemble a prompt.

    ``state`` is the backend state singleton (toggle flags, memory_managers,
    short_term_buffer, prompt_truncation_history). The callables are bound
    methods of ConversationManager (tool-inclusion policy + config/connection
    helpers); calling them here preserves the exact original side effects and
    ordering.
    """

    state: Any
    load_character_config: Callable[[str], Any]
    build_connection_info: Callable[[str], str]
    # ゲートは config(dict) を受ける(C5: Ollamaのcapability判定にモデル名が
    # 要るため model_provider 文字列から config 渡しへ変更)。include_flags
    # 経由の主経路では定数化された lambda が入る(引数は無視される)。
    should_include_command_tools: Callable[[Dict, str], bool]
    should_include_note_tools: Callable[[Dict], bool]
    should_include_talk_theme_tools: Callable[[Dict], bool]
    should_include_image_tools: Callable[[Dict, str], bool]
    should_include_camera_tools: Callable[[Dict, str], bool]
    should_include_deep_search_tools: Callable[[Dict, str], bool]
    should_include_map_tools: Callable[[Dict, str], bool]
    should_include_elyth_tools: Callable[[Dict, str], bool]


def build_prompt_messages(
    deps: PromptBuildDeps,
    character_id: str,
    user_text: str = None,
    config: Dict = None,
    tools_reserve_tokens: int = 0,
) -> List[Dict[str, str]]:
    """
    Build prompt messages with dynamic token management.

    Constructs the messages array for the final prompt:

    Message structure:
    1. SystemMessage (1 message):
       - System prompt (character definition)
       - Talk theme
       - Long-term memory entries from semantic search

    2. User/Assistant messages (0-20 messages):
       - Recent conversation history
       - Each with proper role: "user" or "assistant"

    3. Current user input: added by caller (_generate_reply_task).
       The current-time note (build_current_time_context) is prepended to
       the SEND-ONLY copy of that message by the caller — not persisted to
       history (2026-08-01 稜裁定: system先頭の時刻は分をまたぐたび
       OllamaのプレフィックスKVキャッシュを全滅させていた)

    Note: Conversation history uses proper role-based messages
    (not embedded as text in SystemMessage) to improve model
    understanding and reduce repetitive responses.

    Uses dynamic token allocation based on model's context window.

    Args:
        deps: Injected app-layer collaborators (see PromptBuildDeps)
        character_id: The character ID to build prompt for
        user_text: The current user input for semantic search (optional)
        config: Pre-loaded character config (optional, will load if not provided)
        tools_reserve_tokens: ツール定義の推定トークン数(Ollamaのみ意味を持つ)。
            ペイロードの tools は Ollama がチャットテンプレートでプロンプトへ
            練り込む=token manager に見えない消費のため、上限から予約として
            差し引く(2026-08-11 稜承認計画 C4.5)
    """
    # Use provided config or load from file
    if config is None:
        # Load config outside try block for fallback access
        config_response = deps.load_character_config(character_id)

        # Handle standardized response format
        if isinstance(config_response, dict) and 'success' in config_response:
            if not config_response.get('success'):
                logger.error(f"Failed to load character config: {config_response.get('error', 'Unknown error')}")
                config = {}
            else:
                config = config_response.get('result', {})
        else:
            # Direct config (for backward compatibility)
            config = config_response

    if not config:
        logger.error(f"Config is None/empty for character {character_id}")
        config = {}

    try:
        system_prompt = config.get("system_prompt", "")

        logger.debug(f"Config loaded: system_prompt={repr(system_prompt[:50] if system_prompt else 'None')}")

        # Token management: 方式はAPI/Ollama統一(上限+削減優先順位・
        # 2026-08-11 稜裁定)。違いは上限だけ: APIは128K、Ollamaは num_ctx
        # から生成余白(num_predict)とツール定義予約(マネージャに見えない
        # 消費)を引いた値。
        model_provider = config.get("model_provider", "ollama")

        from backend.shared.token_manager import APIPromptTokenManager
        from backend.shared.constants import API_CONVERSATION_CONFIG
        if model_provider == "ollama":
            from backend.shared.constants import get_tuning_defaults
            from backend.llm.ollama_capabilities import get_effective_num_ctx
            tuning = config.get("tuning") or {}
            num_predict = int(tuning.get(
                "num_predict", get_tuning_defaults().get("num_predict", 800)))
            effective_num_ctx = get_effective_num_ctx(
                config.get("model_name", "") or config.get("ollama_model_name", ""))
            api_token_manager = APIPromptTokenManager(
                max_context=effective_num_ctx,
                trim_priority=API_CONVERSATION_CONFIG["trim_priority"],
                reserved_tokens=num_predict + max(0, int(tools_reserve_tokens)),
            )
            logger.info(
                f"Using unified token management for Ollama: "
                f"num_ctx={effective_num_ctx}, "
                f"reserved={api_token_manager.reserved_tokens}")
        else:
            api_token_manager = APIPromptTokenManager(
                max_context=API_CONVERSATION_CONFIG["max_context"],
                trim_priority=API_CONVERSATION_CONFIG["trim_priority"]
            )
            logger.info(f"Using API token management (max {API_CONVERSATION_CONFIG['max_context']} tokens)")

        # 現在時刻はここ(system)には入れない: 送信直前の最新userメッセージへ
        # _generate_reply_task が build_current_time_context() を前置する
        # (2026-08-01 稜裁定 — 移設理由は同関数のdocstring参照)
        system_content = system_prompt or ""
        if not system_prompt:
            logger.warning(f"system_prompt is empty or None: {repr(system_prompt)}")

        # Debug: Check if system_prompt already contains formatting
        if system_content and '<system_prompt>' in system_content[:200]:
            logger.warning(f"Original system_prompt from config already contains <system_prompt>: {system_content[:200]}...")

        logger.debug(f"System content length: {len(system_content)}")

        # Language setting (used by talk theme, PC status, and summaries)
        from backend.shared.prompt_i18n import get_prompt_language, prompt_section, prompt_text
        language = get_prompt_language(config)

        from backend.tools.pc_status_manager import is_pc_status_mode

        # Build talk theme content
        talk_theme_content = ""
        if deps.state.talk_theme_enabled:
            talk_theme = config.get("talk_theme", "")

            # 旧PC Statusモードのtalk_themeをクリーンアップ
            if talk_theme and is_pc_status_mode(talk_theme):
                talk_theme = ""
                logger.info("Cleared legacy PC Status talk_theme")

            if deps.should_include_talk_theme_tools(config):
                # tools対応プロバイダ: unified prompt with FC tool management
                # (API全部+tools対応Ollama=2026-08-11 稜裁定でフル対応化)
                from backend.tools.talk_theme_tools import build_talk_theme_prompt
                talk_theme_content = build_talk_theme_prompt(talk_theme, language)
            else:
                # 非tools Ollama: discussion instructions only when theme is set
                # (user manages theme=部分対応・2026-07-25裁定のまま)
                if talk_theme:
                    talk_theme_content = prompt_section("talk_theme_ollama", language, talk_theme=talk_theme)

        logger.debug(f"Talk theme content length: {len(talk_theme_content)}")

        # Build PC Status content
        pc_status_content = ""
        if deps.state.pc_status_enabled:
            dynamic_pc_status = config.get("_dynamic_pc_status", "")
            if dynamic_pc_status:
                pc_status_content = dynamic_pc_status
                if deps.state.screen_capture_enabled:
                    pc_status_content += "\n\n" + prompt_text("pc_status.screen_capture_note", language)
            else:
                pc_status_content = (prompt_text("pc_status.prefix", language)
                                     + prompt_text("pc_status.fetch_failed", language))
        logger.debug(f"PC status content length: {len(pc_status_content)}")

        # Build location content
        from backend.tools.location_manager import get_location_for_prompt
        location_content = get_location_for_prompt(language)
        logger.debug(f"Location content length: {len(location_content)}")

        # Build connection info (API only, always included)
        connection_info_content = ""
        if model_provider != "ollama":
            connection_info_content = deps.build_connection_info(language)
        logger.debug(f"Connection info content length: {len(connection_info_content)}")

        # Build command instructions content (tools-gated, when command execution is enabled)
        command_instructions_content = ""
        if deps.should_include_command_tools(config, character_id):
            command_instructions_content = prompt_section("command_instructions", language)
        logger.debug(f"Command instructions content length: {len(command_instructions_content)}")

        # Build notes content (tools-gated)
        notes_content = ""
        if deps.should_include_note_tools(config):
            from backend.memory.note_manager import build_note_prompt
            notes_content = build_note_prompt(character_id, language)
        logger.debug(f"Notes content length: {len(notes_content)}")

        # Build relationship content (全プロバイダ。C8: 旧Ollama除外を撤去=
        # 素の生成なのでcapability非依存・2026-08-11 稜裁定)
        from backend.memory.relationship_manager import build_relationship_prompt
        relationship_content = build_relationship_prompt(character_id, language)
        logger.debug(f"Relationship content length: {len(relationship_content)}")

        # Build image generation instructions (tools+vision-gated)
        image_gen_instructions = ""
        if deps.should_include_image_tools(config, character_id):
            from backend.tools.image_generator import build_image_generation_prompt
            image_gen_instructions = build_image_generation_prompt(language)
        logger.debug(f"Image gen instructions length: {len(image_gen_instructions)}")

        # Build camera capture instructions (tools+vision-gated, local mode only)
        camera_instructions = ""
        if deps.should_include_camera_tools(config, character_id):
            from backend.tools.camera_capture import build_camera_capture_prompt
            camera_instructions = build_camera_capture_prompt(language)
        logger.debug(f"Camera instructions length: {len(camera_instructions)}")

        # Build deep search instructions (tools-gated)
        deep_search_instructions = ""
        if deps.should_include_deep_search_tools(config, character_id):
            from backend.tools.deep_search_tools import build_deep_search_prompt
            deep_search_instructions = build_deep_search_prompt(language)
        logger.debug(f"Deep search instructions length: {len(deep_search_instructions)}")

        # Build map search instructions (tools-gated, requires valid location)
        map_search_instructions = ""
        if deps.should_include_map_tools(config, character_id):
            from backend.tools.map_search_tools import build_map_search_prompt
            map_search_instructions = build_map_search_prompt(language)
        logger.debug(f"Map search instructions length: {len(map_search_instructions)}")

        # Get long-term memory entries
        memory_entries = []
        memory_manager = deps.state.memory_managers.get(character_id)

        if memory_manager and user_text:
            model_provider = config.get("model_provider", "ollama")
            raw_memories = memory_manager.get_memories_for_prompt(
                user_text=user_text,
                provider=model_provider
            )

            # Format memory entries for token manager: "[category] content"
            for entry in raw_memories:
                formatted = f"[{entry['category']}] {entry['content']}"
                memory_entries.append(formatted)

            timing_log("memories_retrieved")
            logger.info(f"Retrieved {len(memory_entries)} relevant memories for prompt")

        # Get recent messages
        recent_messages = []
        if memory_manager:
            raw_messages = memory_manager.get_messages_for_prompt(limit=20)

            # 20-turn location expiry check: if location slot has data but
            # no location update message exists in the last 20 messages, clear it.
            # This must run AFTER raw_messages is populated and BEFORE manage_prompt().
            from backend.tools.location_manager import has_valid_location, clear_location
            if has_valid_location():
                has_recent_location = any(
                    msg.get("metadata", {}).get("is_tool_message")
                    and (
                        msg.get("metadata", {}).get("tool_type") == "location_update"
                        # tool_type導入(2026-07-08)以前の保存済みレコードは日本語文言のみ
                        or "位置情報更新" in msg.get("content", "")
                    )
                    for msg in raw_messages
                )
                if not has_recent_location:
                    clear_location()
                    location_content = ""  # Override the previously built value
                    logger.info("[MapSearch] Location slot cleared: no location update in recent 20 messages")

            # Pre-pass: find the latest map_search_result index for dedup filtering
            _latest_map_search_idx = None
            for _i, _msg in enumerate(raw_messages):
                if _msg.get("metadata", {}).get("tool_type") == "map_search_result":
                    _latest_map_search_idx = _i  # overwrite keeps the latest

            for _msg_idx, msg in enumerate(raw_messages):
                # theme_feedbackタイプはプロンプトから除外（UI表示用のみ）
                if msg.get("metadata", {}).get("type") == "theme_feedback":
                    continue

                # Map search result dedup: keep only the newest search_places result as full text
                if msg.get("metadata", {}).get("tool_type") == "map_search_result":
                    if _latest_map_search_idx is not None and _msg_idx != _latest_map_search_idx:
                        # Older search result → replace with short message
                        recent_messages.append({
                            "role": "user",
                            "content": prompt_text("sysnote.map_cleared", language)
                        })
                        continue
                    else:
                        # Newest search result → include full text
                        content = msg.get("content", "")
                        recent_messages.append({
                            "role": "user",
                            "content": prompt_text("sysnote.wrap", language, content=content)
                        })
                        continue

                # tool_call/tool_result messages: include as user-role system note
                # to prevent LLM from mimicking the format in text output
                if msg.get("metadata", {}).get("is_tool_message"):
                    content = msg.get("content", "")
                    # Strip "| Result: ..." portion to save context space
                    result_idx = content.find(" | Result: ")
                    if result_idx != -1:
                        content = content[:result_idx]
                    msg_dict = {
                        "role": "user",
                        "content": prompt_text("sysnote.wrap", language, content=content)
                    }
                    recent_messages.append(msg_dict)
                    continue
                msg_dict = {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                }
                # Include images if present in message record (skip deleted files)
                msg_images = [p for p in msg.get("images", []) if Path(p).exists()]
                if msg_images:
                    msg_dict["images"] = msg_images
                # Include documents if present in message record
                msg_docs = msg.get("documents", [])
                if msg_docs:
                    msg_dict["documents"] = msg_docs
                recent_messages.append(msg_dict)
            timing_log("messages_retrieved")
            logger.info(f"Retrieved {len(recent_messages)} recent messages for prompt")

            # Migrate assistant-message images (AI-generated) to the next user message.
            # API providers don't support images in assistant role messages.
            # Use thumbnails instead of full-size images to save context tokens.
            from backend.shared.image_storage import is_generated_image, get_thumbnail_path_for
            pending_images = []
            for msg_dict in recent_messages:
                if msg_dict.get("role") == "assistant" and msg_dict.get("images"):
                    for img_path in msg_dict["images"]:
                        if is_generated_image(img_path):
                            # Use thumbnail if available
                            thumb = get_thumbnail_path_for(img_path)
                            migrated_path = thumb if thumb else img_path
                            if Path(migrated_path).exists():
                                pending_images.append(migrated_path)
                    del msg_dict["images"]
                elif msg_dict.get("role") == "user" and pending_images:
                    existing = msg_dict.get("images", [])
                    msg_dict["images"] = pending_images + existing
                    pending_images = []
            # If there are still pending images after the last message, discard them
            # (they'll be attached by the current turn's prompt construction)
        else:
            # Fallback to direct memory access (no long-term memory available)
            st_buffer = deps.state.short_term_buffer.get(character_id, [])
            recent_messages = st_buffer[-20:] if len(st_buffer) > 20 else st_buffer[:]

        # Debug logging
        logger.debug(f"Memory entries type: {type(memory_entries)}, length: {len(memory_entries)}")
        logger.debug(f"Recent messages type: {type(recent_messages)}, length: {len(recent_messages)}")

        # Ensure types are correct
        if not isinstance(memory_entries, list):
            logger.error(f"Memory entries is not a list: {type(memory_entries)}")
            memory_entries = []
        if not isinstance(recent_messages, list):
            logger.error(f"Recent messages is not a list: {type(recent_messages)}")
            recent_messages = []

        # Manage tokens to fit within budget (統一マネージャ・全プロバイダ共通)
        managed, truncation_info = api_token_manager.manage_prompt(
            system_content=system_content,
            talk_theme_content=talk_theme_content,
            long_term_memory=memory_entries,
            messages=recent_messages,
            user_input=user_text or "",
            pc_status_content=pc_status_content,
            command_instructions=command_instructions_content,
            notes_content=notes_content,
            location_content=location_content,
            relationship_content=relationship_content,
            connection_info_content=connection_info_content
        )

        # Log truncation if occurred
        if truncation_info["truncated"]:
            logger.warning(f"Prompt truncated: {truncation_info}")
            deps.state.prompt_truncation_history.append({
                "timestamp": datetime.now().isoformat(),
                "character_id": character_id,
                "truncation_info": truncation_info
            })
            # Keep only last 100 truncation events
            if len(deps.state.prompt_truncation_history) > 100:
                deps.state.prompt_truncation_history = deps.state.prompt_truncation_history[-100:]

        # Build final messages array
        messages = []

        # System message: assemble sections with XML tags
        system_text = managed['system']

        # Debug logging to identify duplication source
        if system_text and '<system_prompt>' in system_text[:100]:
            logger.warning(f"System prompt already contains <system_prompt> tag. First 200 chars: {system_text[:200]}")

        if system_text and system_text.strip().startswith('<system_prompt>'):
            logger.info("System prompt already has <system_prompt> tag, not adding another")
            final_system_content = system_text
        else:
            logger.debug("Wrapping system prompt with <system_prompt> tag")
            final_system_content = f"<system_prompt>\n{system_text}\n</system_prompt>"

        # Add notes (right after system_prompt)
        if managed.get("notes"):
            final_system_content += f"\n\n<notes>\n{managed['notes']}\n</notes>"

        # Add relationship summary (right after notes; all providers, never trimmed)
        if managed.get("relationship"):
            final_system_content += f"\n\n<relationship>\n{managed['relationship']}\n</relationship>"

        # Add talk theme
        if managed["talk_theme"]:
            final_system_content += f"\n\n<talk_theme>\n{managed['talk_theme']}\n</talk_theme>"

        # Add PC Status
        if managed["pc_status"]:
            final_system_content += f"\n\n<pc_status>\n{managed['pc_status']}\n</pc_status>"

        # Add Live Camera note — only on turns where a frame was actually
        # attached (conversation_manager sets the flag after the barrier), so
        # the AI can tell auto-captured surroundings from user attachments.
        if config.get("_ambient_frame_present"):
            ambient_note = prompt_text("ambient_camera.frame_note", language)
            final_system_content += f"\n\n<ambient_camera_note>\n{ambient_note}\n</ambient_camera_note>"

        # Add user location
        if managed.get("location"):
            final_system_content += f"\n\n<user_location>\n{managed['location']}\n</user_location>"

        # Add connection info (API only)
        if managed.get("connection_info"):
            final_system_content += f"\n\n<connection_info>\n{managed['connection_info']}\n</connection_info>"

        # Add command instructions
        ci = managed.get("command_instructions", "") or command_instructions_content
        if ci:
            final_system_content += f"\n\n<command_instructions>\n{ci}\n</command_instructions>"

        # Add image generation instructions
        if image_gen_instructions:
            final_system_content += f"\n\n<image_generation_instructions>\n{image_gen_instructions}\n</image_generation_instructions>"

        # Add camera capture instructions
        if camera_instructions:
            final_system_content += f"\n\n<camera_instructions>\n{camera_instructions}\n</camera_instructions>"

        # Add deep search instructions
        if deep_search_instructions:
            final_system_content += f"\n\n<deep_search_instructions>\n{deep_search_instructions}\n</deep_search_instructions>"

        # Add map search instructions
        if map_search_instructions:
            final_system_content += f"\n\n<map_search_instructions>\n{map_search_instructions}\n</map_search_instructions>"

        # Add ELYTH instructions and notes (before long-term memory)
        if deps.should_include_elyth_tools(config, character_id):
            try:
                elyth_instructions = prompt_section("elyth_conversation", language)
                final_system_content += f"\n\n<elyth_instructions>\n{elyth_instructions}\n</elyth_instructions>"
            except Exception as e:
                logger.warning(f"Failed to build ELYTH instructions: {e}")
            try:
                from backend.elyth.elyth_note_manager import build_elyth_note_prompt
                elyth_notes_content = build_elyth_note_prompt(character_id, include_system_prompt=False, language=language)
                if elyth_notes_content:
                    final_system_content += f"\n\n<elyth_notes>\n{elyth_notes_content}\n</elyth_notes>"
            except Exception as e:
                logger.warning(f"Failed to build ELYTH notes prompt: {e}")

        # Add long-term memory
        if managed["long_term_memory"]:
            memory_description = prompt_text("memory.description", language)

            memory_section = f"{memory_description}\n"
            for entry in managed["long_term_memory"]:
                memory_section += f"\n{entry}"
            final_system_content += f"\n\n<long_term_memory>\n{memory_section}\n</long_term_memory>"

        # 1. System message (settings + talk theme + summaries only)
        if final_system_content:
            messages.append({"role": "system", "content": final_system_content})

        # 2. Conversation history as separate user/assistant messages
        #    Previously embedded as text in SystemMessage with [01] USER: format.
        #    Now uses proper role-based messages for better model understanding.
        if managed["messages"]:
            # --- Document limit enforcement (all providers) ---
            # Collect all documents across messages, enforce MAX_DOCUMENTS_IN_PROMPT
            # and MAX_DOCUMENT_CHARS_IN_PROMPT, removing oldest first.
            # C7: 旧Ollama除外(全文書剥がし)を撤去。テキスト化されるので
            # capability非依存に全Ollamaモデルで動く(2026-08-11 稜裁定)。
            from backend.shared.constants import MAX_DOCUMENTS_IN_PROMPT, MAX_DOCUMENT_CHARS_IN_PROMPT
            # Build flat list of (msg_index, doc_index, doc) sorted oldest first
            all_docs = []
            for msg_idx, msg in enumerate(managed["messages"]):
                for doc_idx, doc in enumerate(msg.get("documents", [])):
                    all_docs.append((msg_idx, doc_idx, doc))

            # Calculate totals
            total_doc_count = len(all_docs)
            total_doc_chars = sum(d[2].get("char_count", 0) for d in all_docs)

            # Remove oldest documents until both limits are satisfied
            removed_indices = set()
            while (total_doc_count > MAX_DOCUMENTS_IN_PROMPT or
                   total_doc_chars > MAX_DOCUMENT_CHARS_IN_PROMPT):
                if not all_docs:
                    break
                oldest = all_docs.pop(0)
                removed_indices.add((oldest[0], oldest[1]))
                total_doc_count -= 1
                total_doc_chars -= oldest[2].get("char_count", 0)

            if removed_indices:
                logger.info(
                    f"[Documents] Trimmed {len(removed_indices)} documents from history "
                    f"(remaining: {total_doc_count} docs, {total_doc_chars} chars)"
                )

            # Rebuild documents on each message (only keep non-removed)
            for msg_idx, msg in enumerate(managed["messages"]):
                msg_docs = msg.get("documents", [])
                if not msg_docs:
                    continue
                kept = [
                    doc for doc_idx, doc in enumerate(msg_docs)
                    if (msg_idx, doc_idx) not in removed_indices
                ]
                if kept:
                    msg["documents"] = kept
                else:
                    msg.pop("documents", None)

            # Embed remaining document text into message content
            for msg in managed["messages"]:
                msg_docs = msg.get("documents", [])
                if msg_docs:
                    doc_parts = []
                    for doc in msg_docs:
                        fname = doc.get("filename", "unknown")
                        text = doc.get("text", "")
                        doc_parts.append(
                            f'<attached_document filename="{fname}">\n{text}\n</attached_document>'
                        )
                    msg["content"] = msg.get("content", "") + "\n\n" + "\n\n".join(doc_parts)
                    # Remove documents field (now embedded in content)
                    msg.pop("documents", None)

            # Apply MAX_IMAGES_IN_PROMPT limit: count images from newest to oldest
            from backend.shared.constants import MAX_IMAGES_IN_PROMPT
            image_count = 0
            # Process in reverse to count from newest first
            processed_msgs = []
            for msg in reversed(managed["messages"]):
                msg_dict = {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                }
                msg_images = msg.get("images", [])
                if msg_images:
                    remaining_slots = MAX_IMAGES_IN_PROMPT - image_count
                    if remaining_slots > 0:
                        # Include up to remaining_slots images
                        included_images = msg_images[:remaining_slots]
                        msg_dict["images"] = included_images
                        image_count += len(included_images)
                    # If no remaining slots, images are simply not included
                processed_msgs.append(msg_dict)
            # Reverse back to original order
            processed_msgs.reverse()
            messages.extend(processed_msgs)

        return messages

    except Exception as e:
        logger.error(f"Error in _build_prompt_messages: {e}", exc_info=True)
        # Return minimal fallback prompt without user message (will be added by caller)
        return [
            {"role": "system", "content": config.get("system_prompt", "You are a helpful assistant.")}
        ]
