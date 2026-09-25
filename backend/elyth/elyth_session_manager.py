"""
backend/elyth_session_manager.py

Autonomous ELYTH session manager.
Schedules and executes ELYTH SNS sessions for characters when idle.
Contains: scheduler thread, dedicated tool call loop, prompt builder,
session lifecycle management, and manual stop support.
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.shared.awake_clock import awake_seconds
from backend.shared.wake_gate import get_wake_gate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ELYTH_TURNS = 10
SESSION_QUEUE_TIMEOUT = 600  # 10 minutes max per character session
SCHEDULER_CHECK_INTERVAL = 60.0  # Check every 60 seconds
CUTOFF_MARGIN_SECONDS = 15 * 60  # 15 minutes before next cycle

RAG_TRIGGER_TOOLS = frozenset({
    "get_timeline", "get_notifications", "get_thread",
    "get_aituber", "get_my_posts", "search_posts",
})


# ---------------------------------------------------------------------------
# Prompt privacy settings (spec v6 §8 — 稜裁定 2026-08-15)
# ---------------------------------------------------------------------------
# What user-derived context enters ELYTH sessions. Defaults are the safe side:
# YouTube's J5 ruling (third-party-writable text as a retrieval query into
# private memories that flow to a public surface) applies to ELYTH's per-post
# RAG identically, so out of the box only activity-derived memories are used
# and user notes / user relationship stay out of the session prompt.

ELYTH_RAG_MODES = ("off", "activity_only", "all")
ELYTH_ACTIVITY_MEMORY_CATEGORIES = ["elyth", "youtube"]


def get_elyth_prompt_settings() -> Dict[str, Any]:
    """Resolve the 3 prompt-privacy settings (session mode only;
    applied from the next session)."""
    try:
        from backend.shared.settings_store import get_setting
        include_notes = bool(get_setting("elyth", "include_user_notes", False))
        include_rel = bool(get_setting("elyth", "include_user_relationship", False))
        rag_mode = get_setting("elyth", "memory_rag_mode", "activity_only")
    except Exception:
        include_notes, include_rel, rag_mode = False, False, "activity_only"
    if rag_mode not in ELYTH_RAG_MODES:
        rag_mode = "activity_only"
    return {
        "include_user_notes": include_notes,
        "include_user_relationship": include_rel,
        "memory_rag_mode": rag_mode,
    }


# ---------------------------------------------------------------------------
# Common instructions (app-wide setting, UI-language default)
# ---------------------------------------------------------------------------

def resolve_elyth_instructions() -> str:
    """ELYTH共通指示文の解決。

    未編集(キー無し)なら使用時にカタログから解決する。アプリ全体設定なので
    キャラ別プロンプト言語ではなくUI言語(再起動で追従)。ユーザー編集値は
    空文字含めそのまま尊重する(空=セクション自体を出さない従来挙動)。
    """
    try:
        from backend.shared.settings_store import get_setting
        stored = get_setting("elyth", "instructions", None)
    except Exception:
        stored = None
    if isinstance(stored, str):
        return stored
    from backend.shared.i18n import current_language
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("elyth_default_instructions", current_language())


def _migrate_unedited_default_instructions() -> None:
    """焼き込まれた無編集デフォルト指示文をキー削除へ移行する(一度きり)。

    いずれかの言語の現行原文とstrip一致=ユーザー編集ではない。編集済みの
    値(原文と不一致)はカスタムとして温存される。
    """
    from backend.shared.settings_store import delete_setting, get_setting
    stored = get_setting("elyth", "instructions", None)
    if not isinstance(stored, str):
        return
    from backend.shared.prompt_i18n import available_prompt_languages, prompt_section
    defaults = {prompt_section("elyth_default_instructions", lang).strip()
                for lang in available_prompt_languages()}
    if stored.strip() in defaults:
        delete_setting("elyth", "instructions")
        logger.info("[ELYTH] Unedited default instructions migrated to catalog resolution")


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

class ELYTHSessionManager:
    """Manages autonomous ELYTH sessions for AI characters."""

    def __init__(self, state: "BackendState"):
        self.state = state

        # Scheduler state
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._cycle_running = False
        self._last_cycle_time: float = 0.0
        # J9 timer (YE, 2026-07-11): pure awake-time countdown. Advances while
        # AG runs (tray-minimized included), pauses during conversations (and
        # sleep via awake_clock), fires when elapsed >= interval. The old idle
        # threshold (10 min) and off-time window are GONE.
        self._timer_elapsed: float = 0.0
        self._last_timer_tick: float = awake_seconds()

        # Session activity tracking (for real-time UI)
        self._current_character_id: Optional[str] = None
        self._current_character_name: Optional[str] = None
        self._session_start_time: Optional[float] = None
        self._current_turn: int = 0
        self._last_activity_text: str = ""
        self._last_tool_name: str = ""

        # Loop control
        self._loop_paused: bool = False

        # Settings (loaded from user_settings.json)
        self._interval_seconds = 3600
        self._character_order: List[str] = []

        # Operational cycle logger (always-on, owned by _run_cycle)
        self._current_cycle_logger: Optional["ELYTHCycleLogger"] = None

    def configure(self) -> None:
        """Load settings from user_settings.json and start scheduler thread."""
        self._load_settings()
        # Timer starts from zero at app start (J9: fires after one full interval)
        self._timer_elapsed = 0.0
        self._last_timer_tick = awake_seconds()
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="elyth-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        logger.info("[ELYTH] Session manager configured and scheduler started")

    def stop(self) -> None:
        """Stop the scheduler thread and wait for completion."""
        self._stop_event.set()
        # Also signal any running session to stop
        if hasattr(self.state, 'elyth_stop_event'):
            self.state.elyth_stop_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=30.0)
        logger.info("[ELYTH] Session manager stopped")

    # ----- Settings ---------------------------------------------------------

    def _load_settings(self) -> None:
        """Load ELYTH settings from user_settings.json."""
        try:
            from backend.shared.settings_store import get_setting
            self._interval_seconds = get_setting("elyth", "interval_seconds", 3600)
            self._character_order = get_setting("elyth", "character_order", [])
            self._loop_paused = get_setting("elyth", "loop_paused", False)

            # Auto-pause if no enabled characters
            if not self._character_order and not self._loop_paused:
                self._loop_paused = True

            # 旧実装(〜2026-07-30)は初回起動時のUI言語でデフォルト指示文を
            # ここへ焼き込み、以後言語を変えても二度と追従しなかった。現行仕様は
            # 「未編集=キー無し・使用時にUI言語で解決」(resolve_elyth_instructions)。
            # 過去に焼き込まれた無編集デフォルトはキー削除へ移行する。
            _migrate_unedited_default_instructions()
        except Exception as e:
            logger.warning(f"[ELYTH] Failed to load settings, using defaults: {e}")

    def reload_settings(self) -> None:
        """Reload settings (called when UI saves new settings)."""
        old_paused = self._loop_paused
        old_chars = list(self._character_order)
        self._load_settings()

        # Auto-pause/resume based on character availability
        if not self._character_order and not self._loop_paused:
            self.pause_loop()
        elif self._character_order and old_paused and not old_chars:
            # Characters went from 0 to >0 — auto-resume
            self.resume_loop()

        logger.info("[ELYTH] Settings reloaded")
        self._broadcast_status_update("settings_reloaded")

    def _force_off_character(self, char_id: str) -> None:
        """character_order から外す＝強制OFF（確定非対応モデルのキャラ用）。

        セッション実行時のC9地点から呼ばれる。orderが空になれば
        reload_settings の既存分岐が自動ポーズする。
        """
        from backend.shared.settings_store import get_setting, update_setting
        order = [cid for cid in get_setting("elyth", "character_order", [])
                 if cid != char_id]
        update_setting("elyth", "character_order", order)
        self.reload_settings()

    # ----- Scheduler --------------------------------------------------------

    def _scheduler_loop(self) -> None:
        """Main scheduler loop — checks conditions every 60 seconds."""
        logger.info("[ELYTH] Scheduler loop started")
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=SCHEDULER_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break

            try:
                # J9 timer: fold awake time since the last tick into the
                # accumulator — unless paused or in a conversation (those
                # spans are discarded; sleep never counts via awake_clock).
                # 覚醒ゲート: スリープ後〜ユーザー入力までの無人覚醒(DarkWake/
                # 自動メンテ起床)も数えず、セッションも開始しない(蓋閉じ夜間の
                # 実走=API浪費の根治)。
                now = awake_seconds()
                user_present = get_wake_gate().is_open()
                if (user_present and not self._loop_paused
                        and not self._is_conversation_active()):
                    self._timer_elapsed += max(0.0, now - self._last_timer_tick)
                self._last_timer_tick = now

                # Periodic status broadcast for timer updates
                self._broadcast_status_update("tick")

                if self._loop_paused or not user_present:
                    continue

                # Check all conditions
                if not self._should_start_session():
                    continue

                # Run a cycle
                self._run_cycle()

            except Exception as e:
                logger.error(f"[ELYTH] Scheduler error: {e}", exc_info=True)

    def _should_start_session(self) -> bool:
        """Check all conditions for starting an ELYTH session cycle (J9:
        タイマー満了のみ。アイドル条件・オフ時間帯は廃止＝YE)."""
        # Server mode: ELYTH sessions are disabled entirely (稜裁定 2026-07-31)。
        # クライアント接続中は会話中扱いで走れず、未接続の隙間にヘッドレスで
        # 走るのも意図しない — 判定でなくモードで止める。
        if getattr(self.state, 'server_mode', False):
            return False
        if self._cycle_running:
            return False
        if self._is_conversation_active():
            return False
        if self._timer_elapsed < self._interval_seconds:
            return False

        # YouTube reply-generation phase running → don't start (相互排他.
        # 逆方向はYouTube側の_blocked_reasonがelyth_session_activeを見る)
        try:
            from backend.shared.youtube_state import get_youtube_state
            if get_youtube_state().session_active:
                return False
        except Exception:
            pass

        # Need at least one ELYTH-enabled character
        enabled_chars = self._get_enabled_characters()
        if not enabled_chars:
            return False

        return True

    def _is_conversation_active(self) -> bool:
        """Check if a conversation is active (mode-aware)."""
        if getattr(self.state, 'server_mode', False):
            try:
                from backend.server.session_manager import get_session_manager
                return get_session_manager().has_primary_session()
            except Exception:
                return False
        else:
            return getattr(self.state, 'conversation_active', False)

    def _timer_remaining(self) -> float:
        """Seconds until the timer fires. Counts the live span since the last
        tick fold (the scheduler thread is busy during a cycle, so ticks pause
        while a session runs) unless paused or in a conversation."""
        elapsed = self._timer_elapsed
        if not self._loop_paused and not self._is_conversation_active():
            elapsed += max(0.0, awake_seconds() - self._last_timer_tick)
        return max(0.0, self._interval_seconds - elapsed)

    def _is_near_next_cycle(self) -> bool:
        """Check if we're within 15 minutes of the next timer expiry."""
        return self._timer_remaining() <= CUTOFF_MARGIN_SECONDS

    def _get_enabled_characters(self) -> List[str]:
        """Get list of ELYTH-enabled characters with valid API keys."""
        enabled = []
        for char_id in self._character_order:
            try:
                from backend.conversation.character_manager import _find_config_file_by_id, _load_config_file
                config_file = _find_config_file_by_id(char_id)
                if not config_file:
                    continue
                config = _load_config_file(config_file)
                if config.get("elyth_api_key"):
                    enabled.append(char_id)
            except Exception:
                continue
        return enabled

    def no_characters_reason(self) -> str:
        """Why no character can run a session ('' if at least one can).

        'no_eligible_characters' = ELYTH APIキー付きキャラが1人もいない
        'no_enabled_characters'  = 対象キャラはいるが誰もONになっていない
        """
        if self._get_enabled_characters():
            return ""
        if self._has_eligible_characters():
            return "no_enabled_characters"
        return "no_eligible_characters"

    def _has_eligible_characters(self) -> bool:
        """Any character config with an ELYTH API key, regardless of ON/OFF
        (same eligibility rule as the character list on the ELYTH tab)."""
        import glob
        from backend.shared.constants import CHARACTER_CONFIGS_DIR
        for f in glob.glob(str(CHARACTER_CONFIGS_DIR / "*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    if json.load(fh).get("elyth_api_key"):
                        return True
            except Exception:
                continue
        return False

    # ----- Cycle execution --------------------------------------------------

    def _run_cycle(self) -> None:
        """Execute one session cycle for all enabled characters."""
        self._cycle_running = True
        self._last_cycle_time = awake_seconds()
        # J9: the timer resets at cycle START, so the next expiry is one full
        # interval from now regardless of how long this cycle runs.
        self._timer_elapsed = 0.0
        self._last_timer_tick = awake_seconds()

        enabled_chars = self._get_enabled_characters()
        logger.info(f"[ELYTH] Starting cycle with {len(enabled_chars)} characters: {enabled_chars}")

        # Initialize the cycle logger only when there's actually work to do.
        # Empty cycles intentionally leave the previous file untouched.
        if enabled_chars:
            try:
                from backend.elyth.elyth_cycle_logger import ELYTHCycleLogger
                self._current_cycle_logger = ELYTHCycleLogger()
                self._current_cycle_logger.start_cycle(
                    enabled_character_ids=enabled_chars,
                    interval_seconds=self._interval_seconds,
                )
            except Exception as e:
                logger.warning(f"[ELYTH] Cycle logger init failed: {e}")
                self._current_cycle_logger = None

        try:
            for char_id in enabled_chars:
                # Check stop conditions between characters
                if self._stop_event.is_set():
                    logger.info("[ELYTH] Cycle interrupted: shutdown")
                    break
                if self.state.elyth_stop_event.is_set():
                    logger.info("[ELYTH] Cycle interrupted: manual stop")
                    self.state.elyth_stop_event.clear()
                    break
                if self._is_conversation_active():
                    logger.info("[ELYTH] Cycle interrupted: conversation started")
                    break

                try:
                    self._run_character_session(char_id)
                except Exception as e:
                    logger.error(f"[ELYTH] Session failed for {char_id}: {e}", exc_info=True)

        finally:
            self._cycle_running = False
            # Clear stop event if it was set during this cycle
            if self.state.elyth_stop_event.is_set():
                self.state.elyth_stop_event.clear()
            if self._current_cycle_logger is not None:
                try:
                    self._current_cycle_logger.end_cycle()
                except Exception as e:
                    logger.warning(f"[ELYTH] Cycle logger end failed: {e}")
                self._current_cycle_logger = None
            logger.info("[ELYTH] Cycle completed")

    def _run_character_session(self, character_id: str) -> None:
        """Run a single ELYTH session for one character."""
        logger.info(f"[ELYTH] Starting session for {character_id}")

        # Resolve character name for UI
        char_name = character_id[:8]
        try:
            from backend.conversation.character_manager import _find_config_file_by_id, _load_config_file
            cf = _find_config_file_by_id(character_id)
            if cf:
                cfg = _load_config_file(cf)
                char_name = cfg.get("name", char_name)
        except Exception:
            pass

        self.state.elyth_session_active = True
        self._current_character_id = character_id
        self._current_character_name = char_name
        self._session_start_time = awake_seconds()
        self._current_turn = 0
        self._last_activity_text = ""
        self._last_tool_name = ""
        self._broadcast_status_update("session_start")
        try:
            # Ensure MemoryManager exists
            self._ensure_memory_manager(character_id)

            # Run session as a queue task
            future = self.state.queue_manager.enqueue_llm_task(
                self._session_task, character_id, timeout=SESSION_QUEUE_TIMEOUT
            )
            future.result(timeout=SESSION_QUEUE_TIMEOUT)

        except Exception as e:
            logger.error(f"[ELYTH] Session error for {character_id}: {e}", exc_info=True)
        finally:
            self.state.elyth_session_active = False
            self._broadcast_status_update("session_end")
            self._current_character_id = None
            self._current_character_name = None
            self._session_start_time = None
            self._last_activity_text = ""
            self._last_tool_name = ""
            logger.info(f"[ELYTH] Session ended for {character_id}")

    def _session_task(self, character_id: str) -> None:
        """The actual session task that runs inside the LLM queue."""
        from backend.conversation.character_manager import _find_config_file_by_id, _load_config_file
        from backend.llm.api_integration import create_llm_client

        # Load config
        config_file = _find_config_file_by_id(character_id)
        if not config_file:
            logger.error(f"[ELYTH] No config for {character_id}")
            return
        config = _load_config_file(config_file)

        model_provider = config.get("model_provider", "ollama")
        model_name = config.get("model_name", "")
        api_key = config.get("elyth_api_key", "")

        # C9(2026-08-11 稜裁定): tools対応モデルのみセッション実行(ループは
        # C2のollama分岐が担う)。判定は elyth_availability に一本化。
        # 確定非対応(モデル差し替え等)はスキップでなく強制OFF・判定不能は
        # fail-closedスキップでON設定温存(いずれも稜裁定 2026-08-15)。
        from backend.elyth.elyth_availability import BLOCK_NO_TOOLS, block_reason
        reason = block_reason(model_provider, model_name)
        if reason == BLOCK_NO_TOOLS:
            logger.warning(
                f"[ELYTH] Force-disabling {character_id}: Ollama model "
                f"'{model_name}' lacks tools capability")
            self._force_off_character(character_id)
            return
        if reason is not None:
            logger.warning(
                f"[ELYTH] Skipping {character_id}: capability unknown for "
                f"Ollama model '{model_name}' (fail-closed)")
            return
        if not api_key:
            logger.warning(f"[ELYTH] Skipping {character_id}: No ELYTH API key")
            return

        # Create LLM client — ELYTH sessions disable web_search injection
        # (not used by ELYTH, avoids wasted tokens and misuse risk) and
        # enable prompt caching to amortize the long stable prefix across
        # the multi-turn tool-call loop.
        llm_result = create_llm_client(
            model_provider=model_provider,
            model_name=model_name,
            timeout=180.0,
            auto_inject_web_search=False,
            enable_prompt_caching=True,
        )
        if not llm_result.get("success"):
            logger.error(f"[ELYTH] Failed to create LLM client: {llm_result.get('error')}")
            return

        chat_llm = llm_result["response"]

        # Build prompt
        system_msg, past_session_msgs = self._build_elyth_prompt(
            character_id, config, model_provider
        )

        # Build tools
        from backend.elyth.elyth_tools import get_elyth_tool_definitions_for_provider
        from backend.shared.prompt_i18n import get_prompt_language as _gpl
        tools = get_elyth_tool_definitions_for_provider(
            model_provider, mode="session", language=_gpl(config))

        # Assemble initial messages
        messages = []
        messages.append({"role": "system", "content": system_msg})
        messages.extend(past_session_msgs)
        from backend.shared.prompt_i18n import get_prompt_language, prompt_text
        messages.append({"role": "user", "content": prompt_text(
            "elyth.session_start", get_prompt_language(config))})

        # Run tool call loop
        from backend.elyth.elyth_memory import create_session_data, save_session_log
        from backend.elyth.elyth_thread_state import fold_pending_thread_replies

        # 前回ハードkillで pending のまま残ったスレッド計上をここで counts に
        # 反映してから始める(初回フィルタの cap 判定を正しくする)
        try:
            fold_pending_thread_replies(character_id)
        except Exception as e:
            logger.warning(f"[ELYTH] startup fold_pending_thread_replies failed: {e}")

        session_data = create_session_data(character_id)

        # Register this character with the cycle logger before any turns run.
        cycle_logger = self._current_cycle_logger
        if cycle_logger is not None:
            try:
                cycle_logger.start_character(
                    character_id=character_id,
                    character_name=self._current_character_name or character_id,
                    provider=model_provider,
                    past_msg_count=len(past_session_msgs),
                )
            except Exception as e:
                logger.warning(f"[ELYTH] start_character failed: {e}")

        end_reason = "exception"
        try:
            end_reason = self._elyth_tool_call_loop(
                chat_llm, messages, tools, model_provider, character_id, config, session_data,
                cycle_logger=cycle_logger,
            )
        finally:
            session_data["end_reason"] = end_reason
            # 確定した end_reason で最終保存(upsert)。ループ例外が下の
            # _post_session_processing を素通りする経路もここでカバーする。
            try:
                save_session_log(character_id, session_data)
            except Exception as e:
                logger.warning(f"[ELYTH] Final session save failed: {e}")
            if cycle_logger is not None:
                try:
                    cycle_logger.end_character(end_reason)
                except Exception as e:
                    logger.warning(f"[ELYTH] end_character failed: {e}")

        # Post-session processing
        self._post_session_processing(character_id, config)

    # ----- Prompt construction ----------------------------------------------

    def _build_elyth_prompt(
        self,
        character_id: str,
        config: Dict[str, Any],
        provider: str,
    ) -> tuple:
        """Build system message and past session messages for ELYTH session.

        Returns:
            (system_message_text, list_of_past_session_messages)
        """
        from backend.elyth.elyth_note_manager import build_elyth_note_prompt
        from backend.memory.relationship_manager import build_relationship_prompt
        from backend.shared.prompt_i18n import get_prompt_language, prompt_text

        language = get_prompt_language(config)

        # Current time
        now = datetime.now()
        weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()]
        time_str = f"(Current time: {now.strftime('%Y/%m/%d')}({weekday}) {now.strftime('%H:%M')})"

        # Character-specific ELYTH prompt
        elyth_system_prompt = config.get("elyth_system_prompt", "")

        # Common instructions (未編集ならUI言語のカタログ原文を毎回解決)
        instructions = resolve_elyth_instructions()

        # ELYTH notes (with system prompt for session mode)
        elyth_notes = build_elyth_note_prompt(
            character_id, include_system_prompt=True, language=language)

        # ELYTH relationships
        try:
            from backend.elyth.elyth_relationship_manager import build_elyth_relationship_prompt
            elyth_relationships = build_elyth_relationship_prompt(character_id)
        except Exception:
            elyth_relationships = ""

        # User-derived context is opt-in (spec v6 §8, default OFF).
        prompt_settings = get_elyth_prompt_settings()

        # AG regular relationship
        relationship = ""
        if prompt_settings["include_user_relationship"]:
            relationship = build_relationship_prompt(character_id, language)

        # AG regular notes (content only, no NOTE_SYSTEM_PROMPT — note FC tools
        # are not available in ELYTH sessions, so including the management
        # instructions would be confusing)
        notes = ""
        if prompt_settings["include_user_notes"]:
            from backend.memory.note_manager import load_note
            note_entries = load_note(character_id)
            if note_entries:
                notes = prompt_text("elyth.user_notes_heading", language) + "\n" + "\n".join(
                    f"{i}. {e}" for i, e in enumerate(note_entries, 1)
                )

        # Long-term memory is no longer injected at session start. It is now
        # injected per-post into tool results during the session via _inject_rag
        # (per-post RAG), which matches each post/notification individually and
        # avoids the centroid-dilution / elyth_note-biased-query problems of a
        # single fixed query here.

        # Session policy note — tells the model up front about the reply cap and
        # the latter-half "wind-down" so it front-loads replies and uses the rest
        # of the session to post, instead of fruitlessly retrying blocked replies.
        # Values come from constants so this never drifts from the enforced rules.
        from backend.shared.constants import (
            ELYTH_POST_ONLY_FROM_TURN, ELYTH_MAX_REPLIES_PER_SESSION,
        )
        session_policy = prompt_text(
            "elyth.session_policy", language,
            max_replies=ELYTH_MAX_REPLIES_PER_SESSION,
            post_only_turn=ELYTH_POST_ONLY_FROM_TURN + 1,
        )

        # Assemble system message
        parts = []
        parts.append(f"<elyth_system_prompt>\n{time_str}\n\n{elyth_system_prompt}\n</elyth_system_prompt>")
        if instructions:
            parts.append(f"<elyth_instructions>\n{instructions}\n</elyth_instructions>")
        parts.append(f"<session_policy>\n{session_policy}\n</session_policy>")
        parts.append(f"<elyth_notes>\n{elyth_notes}\n</elyth_notes>")
        if elyth_relationships:
            parts.append(f"<elyth_relationships>\n{elyth_relationships}\n</elyth_relationships>")
        if relationship:
            parts.append(f"<relationship>\n{relationship}\n</relationship>")
        if notes:
            parts.append(f"<notes>\n{notes}\n</notes>")

        system_msg = "\n\n".join(parts)

        # Past session logs
        past_msgs = []
        try:
            from backend.elyth.elyth_memory import load_session_logs, convert_session_to_messages
            past_sessions = load_session_logs(character_id, limit=3)
            for session in past_sessions:
                past_msgs.extend(
                    convert_session_to_messages(
                        session, provider, omit_get_info=True, language=language)
                )
        except Exception as e:
            logger.warning(f"[ELYTH] Failed to load past sessions: {e}")

        return system_msg, past_msgs

    # ----- Tool call loop ---------------------------------------------------

    def _elyth_tool_call_loop(
        self,
        chat_llm,
        messages: list,
        tools: list,
        provider: str,
        character_id: str,
        config: Dict[str, Any],
        session_data: Dict[str, Any],
        cycle_logger: Optional["ELYTHCycleLogger"] = None,
    ) -> str:
        """Dedicated ELYTH session tool call loop.

        Returns end_reason: "natural", "max_turns", "cutoff",
                            "manual_stop", "api_error", "auth_error"
        """
        from backend.elyth.elyth_tools import (
            dispatch_elyth_tool, ELYTH_API_TOOL_NAMES, ELYTH_NOTE_TOOL_NAMES,
            get_elyth_tool_definitions_for_provider, ELYTH_WINDDOWN_TOOL_NAMES,
        )
        from backend.elyth.elyth_note_manager import dispatch_elyth_note_tool
        from backend.llm.api_integration import (
            build_assistant_msg_with_tool_calls, format_tool_result_message,
        )
        from backend.elyth.elyth_memory import add_turn_to_session, save_session_log
        from backend.elyth.elyth_thread_state import (
            add_pending_thread_reply, fold_pending_thread_replies,
            extract_thread_map_from_notifications, extract_thread_map_from_posts,
        )
        from backend.shared.constants import (
            ELYTH_POST_ONLY_FROM_TURN, ELYTH_MAX_REPLIES_PER_SESSION,
        )

        api_key = config.get("elyth_api_key", "")
        end_reason = "max_turns"
        loop_messages = list(messages)

        # v2: /me/profile is the canonical identity source — register our own
        # ELYTH handle up front (replaces the v1 hack of sniffing create_*
        # responses). Without it, sibling exemption and reply attribution run
        # blind, so a failure ends the session with an explicit status (the
        # client already retried retryable failures once).
        startup_error = self._register_own_handle(character_id, api_key)
        if startup_error:
            try:
                fold_pending_thread_replies(character_id)
            except Exception as e:
                logger.warning(f"[ELYTH] fold_pending_thread_replies failed: {e}")
            return startup_error

        # Reply/post balancing state (per session).
        from backend.shared.prompt_i18n import get_prompt_language
        language = get_prompt_language(config)
        winddown_tools = get_elyth_tool_definitions_for_provider(
            provider, mode="winddown", language=language)
        postid2thread: Dict[str, str] = {}   # post_id -> thread_id (for attribution)
        replied_threads: set = set()          # threads we replied into this session
        reply_count = 0                       # create_reply calls made this session

        for turn in range(MAX_ELYTH_TURNS):
            # 1. Stop check
            if self.state.elyth_stop_event.is_set():
                end_reason = "manual_stop"
                logger.info(f"[ELYTH] {character_id}: Manual stop at turn {turn}")
                break

            # 2. Cutoff check
            if self._is_near_next_cycle():
                end_reason = "cutoff"
                logger.info(f"[ELYTH] {character_id}: Cutoff at turn {turn}")
                break

            # 3. LLM call
            # Wind-down: from the latter half of the session, drop replies and
            # feed fetches so the session winds down with posting + housekeeping.
            active_tools = winddown_tools if turn >= ELYTH_POST_ONLY_FROM_TURN else tools

            if cycle_logger is not None:
                try:
                    cycle_logger.log_request(turn, loop_messages, active_tools)
                except Exception:
                    pass

            _invoke_start = time.time()
            try:
                result = chat_llm.invoke(loop_messages, tools=active_tools)
            except Exception as e:
                logger.error(f"[ELYTH] {character_id}: LLM call failed at turn {turn}: {e}")
                end_reason = "api_error"
                break
            _invoke_ms = int((time.time() - _invoke_start) * 1000)

            if cycle_logger is not None:
                try:
                    cycle_logger.log_response(turn, result, _invoke_ms)
                except Exception:
                    pass

            content = result.content or ""
            self._current_turn = turn + 1

            # Broadcast AI response text
            if content:
                self._last_activity_text = content[:200]
                self._broadcast_status_update("turn_text")

            # 4. No tool calls → natural end
            if not result.tool_calls:
                if content:
                    add_turn_to_session(session_data, content)
                end_reason = "natural"
                logger.info(f"[ELYTH] {character_id}: Natural end at turn {turn}")
                break

            # 5. Classify tool calls
            api_tcs = [tc for tc in result.tool_calls if tc["name"] in ELYTH_API_TOOL_NAMES]
            note_tcs = [tc for tc in result.tool_calls if tc["name"] in ELYTH_NOTE_TOOL_NAMES]
            # Any tool the model emitted that we don't offer. It still appears in
            # the assistant message (built from the raw response below), so it
            # MUST get a matching tool_result or the next API call 400s on the
            # dangling tool_use and kills the session (same guard the normal
            # conversation loop has via command_tcs).
            unknown_tcs = [
                tc for tc in result.tool_calls
                if tc["name"] not in ELYTH_API_TOOL_NAMES
                and tc["name"] not in ELYTH_NOTE_TOOL_NAMES
            ]

            # Note ops must be applied index-descending: remove/replace shift the
            # numbering of later entries otherwise (same fix as the normal loop).
            note_tcs.sort(
                key=lambda tc: (tc.get("arguments") or {}).get("index", 0),
                reverse=True,
            )

            # 6. Dispatch API tools
            tool_call_records = []
            tool_result_records = []
            break_session = False

            for tc in api_tcs:
                # B' enforcement: in wind-down turns, reject any tool outside the
                # wind-down allowlist even if the model emits it. Dropping the
                # tool from the offered set is not enough — in-context priming
                # from earlier reply-heavy turns makes the model call create_reply
                # (etc.) anyway, and the dispatcher would otherwise execute it.
                if turn >= ELYTH_POST_ONLY_FROM_TURN and tc["name"] not in ELYTH_WINDDOWN_TOOL_NAMES:
                    from backend.shared.prompt_i18n import prompt_text
                    winddown_msg = prompt_text("res.elyth.winddown_blocked", language)
                    tool_call_records.append({
                        "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]
                    })
                    tool_result_records.append({
                        "id": tc["id"], "name": tc["name"], "content": winddown_msg
                    })
                    continue

                # C: per-session reply cap. Reject excess create_reply in-code
                # (no API hit) and nudge the model to post or finish. Still emit a
                # tool_result so the assistant's tool_use has a matching result.
                if tc["name"] == "create_reply" and reply_count >= ELYTH_MAX_REPLIES_PER_SESSION:
                    from backend.shared.prompt_i18n import prompt_text
                    limit_msg = prompt_text(
                        "res.elyth.reply_limit", language,
                        max=ELYTH_MAX_REPLIES_PER_SESSION)
                    tool_call_records.append({
                        "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]
                    })
                    tool_result_records.append({
                        "id": tc["id"], "name": tc["name"], "content": limit_msg
                    })
                    continue

                # Only broadcast tools we actually dispatch, so rejected calls
                # (wind-down / reply-cap) don't flash in the UI as if they ran.
                self._last_tool_name = tc["name"]
                self._broadcast_status_update("tool_call")

                res = dispatch_elyth_tool(tc["name"], tc["arguments"], character_id, api_key, language)

                # Auth errors are terminal — credentials won't fix themselves.
                if res["status"] == "auth_error":
                    end_reason = "auth_error"
                    break_session = True
                    break

                # All other outcomes (success or transient failure) are passed
                # through to the LLM as the tool_result. The LLM can decide
                # whether to retry the tool, switch strategy, or stop.
                # No automatic retry: max_turns caps cost regardless, and a
                # forced sleep here would only inflate session latency.
                result_text = res["result_text"]
                success = res["status"] == "success"

                if success:
                    # Thread attribution map. Dedupe / cap suppression / auto-
                    # mark-read now live inside the aggregation layer (spec v6
                    # §5.2) — here we only harvest post_id -> thread_id pairs
                    # from the AG-format results.
                    if tc["name"] == "get_notifications":
                        postid2thread.update(
                            extract_thread_map_from_notifications(result_text))
                    elif tc["name"] in ("get_thread", "get_timeline",
                                        "get_my_posts", "search_posts"):
                        postid2thread.update(extract_thread_map_from_posts(result_text))

                    # Reply accounting.
                    if tc["name"] == "create_reply":
                        reply_count += 1
                        rid = (tc.get("arguments") or {}).get("reply_to_id")
                        tid = postid2thread.get(rid)
                        if tid and tid not in replied_threads:
                            replied_threads.add(tid)
                            # 即時に pending として永続化(kill耐性)。counts への
                            # 加算はセッション末の fold まで行わない=cap判定不変
                            add_pending_thread_reply(character_id, tid)

                if success and tc["name"] in RAG_TRIGGER_TOOLS:
                    result_text = self._inject_rag(character_id, tc["name"], result_text, language)

                tool_call_records.append({
                    "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]
                })
                tool_result_records.append({
                    "id": tc["id"], "name": tc["name"], "content": result_text
                })

            if break_session:
                # Save partial turn
                if content or tool_call_records:
                    add_turn_to_session(session_data, content, tool_call_records, tool_result_records)
                break

            # 7. Dispatch note tools
            for tc in note_tcs:
                note_result = dispatch_elyth_note_tool(character_id, {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                }, language)
                tool_call_records.append({
                    "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]
                })
                tool_result_records.append({
                    "id": tc["id"], "name": tc["name"], "content": note_result
                })

            # 7.5 Emit error results for unknown tools so every tool_use in the
            # assistant message has a matching tool_result (avoids next-turn 400).
            for tc in unknown_tcs:
                logger.warning(f"[ELYTH] {character_id}: unknown tool '{tc['name']}' called; returning error result")
                from backend.shared.prompt_i18n import prompt_text
                tool_call_records.append({
                    "id": tc["id"], "name": tc["name"], "arguments": tc.get("arguments", {})
                })
                tool_result_records.append({
                    "id": tc["id"], "name": tc["name"],
                    "content": prompt_text("res.elyth.unsupported_tool", language, name=tc["name"]),
                })

            # 8. Record turn — persist progress immediately (hard-kill
            # resilience; upsert). 保存失敗で進行中セッションを殺さない。
            add_turn_to_session(session_data, content, tool_call_records, tool_result_records)
            try:
                save_session_log(character_id, session_data)
            except Exception as e:
                logger.warning(f"[ELYTH] Progress save failed: {e}")

            # 9. Append to loop messages
            assistant_msg = build_assistant_msg_with_tool_calls(result, provider)
            loop_messages.append(assistant_msg)
            for tr in tool_result_records:
                loop_messages.append(
                    format_tool_result_message(provider, tr["id"], tr["name"], tr["content"])
                )

            # 10. Notify progress
            self._notify_session_progress(character_id, turn)

        # Fold per-thread reply accounting (pending -> counts, one increment
        # per thread engaged this session). Covers all in-function exit paths;
        # a hard kill leaves pending, folded at the next session start.
        try:
            fold_pending_thread_replies(character_id)
        except Exception as e:
            logger.warning(f"[ELYTH] fold_pending_thread_replies failed: {e}")

        return end_reason

    @staticmethod
    def _register_own_handle(character_id: str, api_key: str) -> Optional[str]:
        """Fetch /me/profile and register this character's ELYTH handle.

        Returns None on success, or an end_reason ("auth_error" / "api_error")
        when the session must not proceed. The API client already applies the
        single-retry policy for retryable failures.
        """
        from backend.elyth.elyth_api import (
            ElythAPIClient, ElythAPIError, ElythAuthError,
        )
        from backend.elyth.elyth_thread_state import record_own_handle
        try:
            profile = (ElythAPIClient(api_key).get_me_profile()
                       .get("profile")) or {}
        except ElythAuthError as e:
            logger.error(f"[ELYTH] {character_id}: auth failed at /me/profile: {e}")
            return "auth_error"
        except ElythAPIError as e:
            logger.error(f"[ELYTH] {character_id}: /me/profile failed: {e}")
            return "api_error"
        record_own_handle(character_id, profile.get("handle"))
        return None

    # ----- RAG injection ----------------------------------------------------

    def _extract_post_contents(self, tool_name: str, result_text: str) -> List[str]:
        """Extract individual post/notification body texts from an ELYTH tool result.

        Returns a de-duplicated list of content strings for per-post RAG.
        Reads the AG stable format emitted by elyth_aggregates (spec v6 §5) —
        never raw v2 JSON. Falls back to [result_text] when the structure is
        unknown or unparseable (degraded mode).
        """
        try:
            data = json.loads(result_text)
        except Exception:
            return [result_text]
        if not isinstance(data, dict):
            return [result_text]

        contents: List[str] = []

        def add(value):
            if isinstance(value, str):
                s = value.strip()
                if s:
                    contents.append(s)

        if tool_name == "get_timeline":
            # AG aggregate (spec v6 §5.3): timeline + trends + topic
            for p in data.get("timeline") or []:
                if isinstance(p, dict):
                    add(p.get("content"))
            trends = data.get("trends")
            if isinstance(trends, dict):
                for p in trends.get("posts") or []:
                    if isinstance(p, dict):
                        add(p.get("content"))
            topic = data.get("today_topic")
            if isinstance(topic, dict):
                title = (topic.get("title") or "").strip()
                desc = (topic.get("description") or "").strip()
                add("\n".join(s for s in (title, desc) if s))
        elif tool_name == "get_notifications":
            for n in data.get("notifications") or []:
                if isinstance(n, dict):
                    add(n.get("post_content"))
                    # announcements (v1 elyth_news の後継): title + summary
                    title = (n.get("title") or "").strip()
                    summary = (n.get("summary") or "").strip()
                    add("\n".join(s for s in (title, summary) if s))
        elif tool_name in ("get_thread", "get_my_posts", "search_posts"):
            for p in data.get("posts") or []:
                if isinstance(p, dict):
                    add(p.get("content"))
        elif tool_name == "get_aituber":
            prof = data.get("profile")
            if isinstance(prof, dict):
                add(prof.get("bio"))
            for p in data.get("posts") or []:
                if isinstance(p, dict):
                    add(p.get("content"))
        else:
            return [result_text]

        # De-duplicate by content string, preserving order (handles the same
        # post appearing in both timeline and trends).
        seen = set()
        deduped = []
        for c in contents:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
        return deduped

    def _inject_rag(self, character_id: str, tool_name: str, result_text: str,
                    language: str) -> str:
        """Inject related long-term memories into tool result text (per-post)."""
        memory_manager = self.state.memory_managers.get(character_id)
        if not memory_manager:
            return result_text

        # RAG mode gate (spec v6 §8): "off" = no injection at all;
        # "activity_only" = only elyth/youtube-derived memories (J5-consistent
        # — the query text is third-party-writable, the output is public);
        # "all" = v1 behavior.
        rag_mode = get_elyth_prompt_settings()["memory_rag_mode"]
        if rag_mode == "off":
            return result_text
        categories = (ELYTH_ACTIVITY_MEMORY_CATEGORIES
                      if rag_mode == "activity_only" else None)

        try:
            from backend.shared.constants import MEMORY_TOKEN_BUDGET_API

            queries = self._extract_post_contents(tool_name, result_text)
            if not queries:
                return result_text

            # threshold omitted -> multi_query_search resolves the calibrated
            # per-embedding-model threshold (ST6 §7-4)
            selected = memory_manager.multi_query_search(
                queries=queries,
                token_budget=MEMORY_TOKEN_BUDGET_API,
                categories=categories,
            )
            if not selected:
                return result_text

            memory_text = "\n".join(selected)
            from backend.shared.prompt_i18n import prompt_text
            heading = prompt_text("res.elyth.related_memories", language)
            return result_text + f"\n\n{heading}\n{memory_text}"

        except Exception as e:
            logger.warning(f"[ELYTH] RAG injection failed: {e}")
            return result_text

    # ----- Post-session processing ------------------------------------------

    def _post_session_processing(
        self,
        character_id: str,
        config: Dict[str, Any],
    ) -> None:
        """Increment session count and run memory extraction if needed.

        セッションログの保存は _session_task の finally(確定 end_reason での
        upsert)へ移動済み — ここでは行わない。
        """
        # Increment session count
        session_count = self._increment_session_count(character_id)

        # Memory extraction: first session + every 3rd session
        if session_count == 1 or session_count % 3 == 0:
            logger.info(
                f"[ELYTH] Running memory extraction for {character_id} "
                f"(session #{session_count})"
            )
            try:
                from backend.elyth.elyth_relationship_manager import run_elyth_memory_extraction
                run_elyth_memory_extraction(self.state, character_id, config)
            except Exception as e:
                logger.error(f"[ELYTH] Memory extraction failed: {e}", exc_info=True)

    def _increment_session_count(self, character_id: str) -> int:
        """Increment and return the session count for a character."""
        try:
            from backend.shared.settings_store import get_setting, update_setting
            counts = get_setting("elyth", "session_counts", {})
            count = counts.get(character_id, 0) + 1
            counts[character_id] = count
            update_setting("elyth", "session_counts", counts)
            return count
        except Exception as e:
            logger.warning(f"[ELYTH] Failed to update session count: {e}")
            return 1

    # ----- Helpers ----------------------------------------------------------

    def _ensure_memory_manager(self, character_id: str) -> None:
        """Ensure MemoryManager exists for the character."""
        if character_id in self.state.memory_managers:
            return
        try:
            from backend.memory.memory_manager import MemoryManager
            from backend.shared.constants import MEMORY_DIR
            from pathlib import Path
            db_file = str(MEMORY_DIR / f"{character_id}.db")
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            mm = MemoryManager(db_file, character_id)
            self.state.memory_managers[character_id] = mm
            logger.info(f"[ELYTH] Created MemoryManager for {character_id}")
        except Exception as e:
            logger.error(f"[ELYTH] Failed to create MemoryManager for {character_id}: {e}")

    def _notify_session_progress(self, character_id: str, turn: int) -> None:
        """Send session progress update via WebSocket (legacy, kept for compatibility)."""
        self._broadcast_status_update("turn_end")

    def _broadcast_status_update(self, event: str = "status") -> None:
        """Broadcast ELYTH session status to all WebSocket clients (fire-and-forget).

        Uses fire-and-forget to avoid deadlock when called from within
        WebSocket handler context (same event loop thread).
        """
        try:
            import asyncio
            from backend.server.websocket_server import get_websocket_manager
            manager = get_websocket_manager()
            if not manager.running or not manager.loop:
                return
            status = self.get_status(event=event)
            message = {
                "action": "elyth_status",
                "data": status,
                "timestamp": status.get("timestamp", 0),
            }
            asyncio.run_coroutine_threadsafe(manager.broadcast(message), manager.loop)
            # Fire-and-forget: do NOT call .result() to avoid deadlock
        except Exception:
            pass

    def pause_loop(self) -> None:
        """Pause the automatic session loop."""
        if self._loop_paused:
            return
        # Fold accumulated time first so the paused span is discarded cleanly
        now = awake_seconds()
        self._timer_elapsed += max(0.0, now - self._last_timer_tick)
        self._last_timer_tick = now
        self._loop_paused = True
        self._save_loop_paused(True)
        logger.info("[ELYTH] Auto loop paused")
        self._broadcast_status_update("loop_paused")

    def resume_loop(self) -> None:
        """Resume the automatic session loop. The timer restarts from zero
        (J9: 純粋に稼働時間ベース — 次の起動は1インターバル後)."""
        if not self._loop_paused:
            return
        self._loop_paused = False
        self._timer_elapsed = 0.0
        self._last_timer_tick = awake_seconds()
        self._save_loop_paused(False)
        logger.info("[ELYTH] Auto loop resumed")
        self._broadcast_status_update("loop_resumed")

    def on_conversation_started(self) -> None:
        """Conversation start: fold awake time up to now, then the timer
        pauses (conversation spans are discarded at the next fold)."""
        if self._loop_paused:
            return
        now = awake_seconds()
        self._timer_elapsed += max(0.0, now - self._last_timer_tick)
        self._last_timer_tick = now
        self._broadcast_status_update("blocked")

    def on_conversation_ended(self) -> None:
        """Conversation end: restart the fold origin so the conversation
        span is never counted."""
        if self._loop_paused:
            return
        self._last_timer_tick = awake_seconds()
        self._broadcast_status_update("idle_start")

    def _save_loop_paused(self, paused: bool) -> None:
        """Persist loop_paused to settings."""
        try:
            from backend.shared.settings_store import update_setting
            update_setting("elyth", "loop_paused", paused)
        except Exception:
            pass

    def start_session_manually(self) -> Dict[str, Any]:
        """Manually trigger an ELYTH session cycle.
        Returns {"success": bool, "error": <reason code>} (YouTube 同型).

        自動ループOFF(paused)でも実行できる — 手動で一回分だけ回したい運用の
        ため(稜指示 2026-07-12・YouTube返信の手動一回分と同一仕様)。"""
        # Server mode: 制御タブ自体を出していないが、古いページ/自作
        # クライアントからの要求はここで理由付き拒否(インライン表示に乗る)
        if getattr(self.state, 'server_mode', False):
            return {"success": False, "error": "server_mode"}
        if self._cycle_running:
            return {"success": False, "error": "cycle_running"}
        if self.state.elyth_session_active:
            return {"success": False, "error": "session_active"}
        reason = self.no_characters_reason()
        if reason:
            return {"success": False, "error": reason}

        # Run cycle in a background thread (not the scheduler thread)
        def _manual_cycle():
            try:
                self._run_cycle()
            except Exception as e:
                logger.error(f"[ELYTH] Manual cycle error: {e}", exc_info=True)

        t = threading.Thread(target=_manual_cycle, name="elyth-manual-cycle", daemon=True)
        t.start()
        return {"success": True}

    def is_session_active_for(self, character_id: str) -> bool:
        """True while an ELYTH session is running **for this character**.

        remove_character の削除ガード用の述語(is_memory_task_running と同じ様式)。
        state.elyth_session_active は単一 bool でどのキャラか持たないため、
        実行中キャラ(_current_character_id)まで見て絞る — そうしないと A の
        セッション中に無関係な B を削除できなくなる。

        background_tasks 台帳(既存の記憶タスクガード)を流用できない理由:
        あの台帳は task['thread'].is_alive() を見るが、ELYTH セッションは
        queue_manager の長寿命ワーカー経由で走るため、そのスレッドを登録すると
        is_alive() が常に True になり全キャラの削除が永久にブロックされる。
        """
        if not getattr(self.state, 'elyth_session_active', False):
            return False
        return self._current_character_id == character_id

    def get_status(self, event: str = "status") -> Dict[str, Any]:
        """Get current ELYTH session status for UI."""
        if self._loop_paused and not self.state.elyth_session_active:
            state = "paused"
        elif self.state.elyth_session_active:
            state = "running"
        elif self._is_conversation_active():
            state = "blocked"
        else:
            state = "idle"

        return {
            "event": event,
            "state": state,
            "loop_paused": self._loop_paused,
            "cycle_running": self._cycle_running,
            "last_cycle_time": self._last_cycle_time,
            # J9 timer: remaining seconds as of `timestamp` (JS counts down
            # from this snapshot; the timer only advances in the idle state).
            "timer_remaining": self._timer_remaining(),
            "interval_seconds": self._interval_seconds,
            "character_order": self._character_order,
            # Running session details
            "current_character_name": self._current_character_name,
            "current_character_id": self._current_character_id,
            "session_start_time": self._session_start_time,
            "current_turn": self._current_turn,
            "max_turns": MAX_ELYTH_TURNS,
            "last_activity_text": self._last_activity_text,
            "last_tool_name": self._last_tool_name,
            # awake時計。timer_remaining/last_cycle_time/session_start_time と同一時計に
            # 揃える（JS側はこのtimestampを基準にオフセット補正して相対計算するため）。
            "timestamp": awake_seconds(),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_elyth_session_manager: Optional[ELYTHSessionManager] = None
_esm_lock = threading.Lock()


def get_elyth_session_manager(state=None) -> ELYTHSessionManager:
    """Get the global ELYTHSessionManager instance."""
    global _elyth_session_manager
    with _esm_lock:
        if _elyth_session_manager is None:
            if state is None:
                raise RuntimeError("ELYTHSessionManager not initialized. Pass state on first call.")
            _elyth_session_manager = ELYTHSessionManager(state)
    return _elyth_session_manager
