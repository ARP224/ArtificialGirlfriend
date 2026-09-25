"""
backend/youtube/youtube_session_manager.py

YouTube comment auto-reply: scheduler + session execution + posting worker
(spec §3/§4 — internal design doc, see backend/youtube/__init__.py).

Shape mirrors ELYTHSessionManager (independent implementation, no common
base) with these decided differences:

- J2: deterministic pipeline. Code fetches/selects comments; the LLM only
  does "1 comment → 1 reply". No tool loop.
- J3: generation and posting are split. The generation phase is one
  enqueue_llm_task (conversation-blocking, minutes); the posting worker is
  an LLM-free daemon thread with 1-5 min random cooldowns.
- J9: pure awake-time timer. It advances whenever AG runs (tray-minimized
  included), pauses during conversations (and sleep, via awake_clock), and
  fires at zero. No idle threshold, no off-hours window.
- auth_error is a runtime scheduler state (never persisted): no sessions
  start until token.json changes on disk (mtime watch) and re-validates —
  so a stage-A CLI re-auth in another process recovers without a restart.
"""

import json
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional

from backend.shared.awake_clock import awake_seconds
from backend.shared.wake_gate import get_wake_gate
from backend.shared.youtube_state import get_youtube_state
from backend.youtube import auth, youtube_api, youtube_store as store
from backend.youtube.youtube_api import (
    AuthRequiredError, PermanentAPIError, QuotaExceededError, TransientAPIError,
)
from backend.youtube.youtube_prompt import build_reply_messages, is_deny
from backend.youtube.youtube_session_logger import YouTubeSessionLogger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEDULER_CHECK_INTERVAL = 60.0     # seconds between scheduler ticks
SESSION_QUEUE_TIMEOUT = 1800        # generation task ceiling (≤10 LLM calls)
MIN_INTERVAL_SECONDS = 3600         # J8: セッション間隔は60分未満に設定不可（下限）
DEFAULT_INTERVAL_SECONDS = 10800    # 未設定時の既定=180分（稜裁定 2026-08-11）
FETCH_MAX_RESULTS = 50              # comments fetched per session
FALLBACK_MAX_VIDEOS = 10            # 予備案の監視対象動画数 (spec §4)
POST_RETRY_LIMIT = 3                # transient-error retries per queue item
EXTRACTION_TRIGGER = 50             # memory extraction after >50 posted replies


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

class YouTubeSessionManager:
    """Schedules and executes YouTube reply sessions + the posting worker."""

    def __init__(self, state: Any):  # state: BackendState (annotated loosely to
        # avoid a module-load-time upward import)
        self.state = state
        self.yt = get_youtube_state()

        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None

        # J9 awake-time timer (accumulates while enabled & no conversation)
        self._timer_elapsed: float = 0.0
        self._last_tick: float = awake_seconds()

        # UI status extras
        self._waiting_reason: str = ""
        self._last_session_summary: str = ""
        # ELYTH型アクティビティログ用: 直近に処理した1件（コメント→返信/判定）。
        # JS側がcomment_processedイベントで受けて履歴表示に積む
        self._last_comment_activity: Optional[Dict[str, str]] = None
        self._session_done: int = 0
        self._session_total: int = 0

        # Settings snapshot (reloaded on UI save via reload_settings)
        self._settings: Dict[str, Any] = self._load_settings()

    # ----- Lifecycle --------------------------------------------------------

    def configure(self) -> None:
        """Load settings and start the scheduler thread (app startup)."""
        self._settings = self._load_settings()
        self._timer_elapsed = 0.0
        self._last_tick = awake_seconds()
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="youtube-scheduler", daemon=True,
        )
        self._scheduler_thread.start()
        logger.info("[YouTube] Session manager configured and scheduler started")

    def stop(self) -> None:
        """Stop scheduler + worker (app shutdown)."""
        self._stop_event.set()
        self.yt.worker_stop_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=10.0)
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10.0)
        logger.info("[YouTube] Session manager stopped")

    # ----- Settings ---------------------------------------------------------

    def _load_settings(self) -> Dict[str, Any]:
        """Read the youtube settings namespace with defaults + J8 floor."""
        try:
            from backend.shared.settings_store import get_setting
            interval = int(get_setting("youtube", "interval_seconds", DEFAULT_INTERVAL_SECONDS))
            return {
                "enabled": bool(get_setting("youtube", "enabled", False)),
                "character_id": get_setting("youtube", "character_id", "") or "",
                "target_channel_id": get_setting("youtube", "target_channel_id", "") or "",
                # J8 fail-safe: the floor is enforced on read as well as in UI
                "interval_seconds": max(MIN_INTERVAL_SECONDS, interval),
                "cooldown_min": max(1, int(get_setting("youtube", "cooldown_min", 60))),
                "cooldown_max": max(1, int(get_setting("youtube", "cooldown_max", 300))),
                "daily_post_limit": max(1, int(get_setting("youtube", "daily_post_limit", 30))),
                "rules": get_setting("youtube", "rules", "") or "",
                "dry_run": bool(get_setting("youtube", "dry_run", False)),
            }
        except Exception as e:
            logger.warning(f"[YouTube] Failed to load settings, using defaults: {e}")
            return {
                "enabled": False, "character_id": "", "target_channel_id": "",
                "interval_seconds": DEFAULT_INTERVAL_SECONDS, "cooldown_min": 60,
                "cooldown_max": 300, "daily_post_limit": 30, "rules": "",
                "dry_run": False,
            }

    def reload_settings(self) -> None:
        """Called when the UI saves settings. Toggle OFF stops the worker
        immediately (spec §4: キューは保持・ONで次セッション〔1〕から再開)."""
        old_enabled = self._settings.get("enabled", False)
        self._settings = self._load_settings()
        if old_enabled and not self._settings["enabled"]:
            self.yt.worker_stop_event.set()
        logger.info("[YouTube] Settings reloaded")
        self._broadcast_status_update("settings_reloaded")

    def set_enabled(self, enabled: bool) -> None:
        """Master toggle (status-area WS button, ELYTH auto-loop 同型).
        Persists youtube.enabled; reload_settings handles worker stop on OFF
        and broadcasts the new state to every client."""
        try:
            from backend.shared.settings_store import update_setting
            update_setting("youtube", "enabled", bool(enabled))
        except Exception as e:
            logger.error(f"[YouTube] Failed to persist enabled={enabled}: {e}")
        self.reload_settings()

    # ----- Scheduler (J9 timer) ---------------------------------------------

    def _scheduler_loop(self) -> None:
        logger.info("[YouTube] Scheduler loop started")
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=SCHEDULER_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[YouTube] Scheduler error: {e}", exc_info=True)

    def _tick(self) -> None:
        now = awake_seconds()
        settings = self._settings

        if not settings["enabled"]:
            self._last_tick = now
            self._waiting_reason = ""
            self._broadcast_status_update("tick")
            return

        # auth_error state: no sessions; watch token.json for out-of-band
        # re-authorization (CLI in another process) and self-recover (spec §2-4).
        if self.yt.auth_error:
            self._last_tick = now
            self._check_auth_recovery()
            self._broadcast_status_update("tick")
            return

        # J9: the timer advances while AG runs, pauses during conversations.
        # Sleep never counts because awake_seconds() excludes it.
        # 覚醒ゲート: 無人覚醒(DarkWake/自動メンテ起床)中は数えず、
        # セッション開始(enqueue)もしない — 無人時間帯の実走/投稿を防ぐ。
        user_present = get_wake_gate().is_open()
        if user_present and not self._is_conversation_active():
            self._timer_elapsed += max(0.0, now - self._last_tick)
        self._last_tick = now

        if not user_present:
            self._broadcast_status_update("tick")
            return

        if self._timer_elapsed < settings["interval_seconds"]:
            self._waiting_reason = ""
            self._broadcast_status_update("tick")
            return

        # Timer expired — start unless blocked; when blocked, surface WHY
        # (spec §7 waiting 状態表示・稜指示 2026-07-11: 無言にしない).
        reason = self._blocked_reason()
        if reason:
            self._waiting_reason = reason
            self._broadcast_status_update("waiting")
            return

        self._waiting_reason = ""
        self._start_session(settings)

    def _blocked_reason(self) -> str:
        """Why a timer-expired session cannot start right now ('' if it can)."""
        # Server mode: YouTube返信セッションは全面無効(稜裁定 2026-07-31・
        # ELYTHと同判断)。自動(タイマー)と手動(start_session_manually)の
        # 両方がこの理由で止まる。
        if getattr(self.state, "server_mode", False):
            return "server_mode"
        if self._is_conversation_active():
            return "conversation"
        if getattr(self.state, "elyth_session_active", False):
            return "elyth_session"
        if self.yt.session_active:
            return "session_running"
        if self.yt.worker_running:
            return "worker_running"
        # 当日上限到達＝投稿できない＝取得も生成もしない(稜指示 2026-08-11)。
        # 生成は最大 SELECTION_CAP 件の LLM 呼び出しを伴うので、セッション起動の
        # 手前で止める(旧: 起動して取得・生成まで進み、投稿ワーカーだけが拒否＝
        # 投稿できない日にコメント取得と返信生成を空撃ちしていた)。
        # タイマーは満了のまま保持する＝日付が変わった後の最初の tick で即再開
        # (覚醒ゲートがあるので深夜の無人再開にはならない)。ファイル読みが要る
        # 判定なので、上の安価な判定をすべて通過したときだけ評価する。
        if (store.get_daily_count(store.load_session_state())
                >= self._settings["daily_post_limit"]):
            return "daily_limit"
        return ""

    def _is_conversation_active(self) -> bool:
        """Mode-aware conversation check (same rule as ELYTH)."""
        if getattr(self.state, "server_mode", False):
            try:
                from backend.server.session_manager import get_session_manager
                return get_session_manager().has_primary_session()
            except Exception:
                return False
        return getattr(self.state, "conversation_active", False)

    def _check_auth_recovery(self) -> None:
        """token.json mtime watch → re-validate → auto-recover (spec §2-4)."""
        current = auth.token_file_mtime()
        recorded = self.yt.auth_error_token_mtime
        changed = (
            (recorded is None and current is not None)
            or (recorded is not None and current is not None and current > recorded)
        )
        if not changed:
            return
        result = auth.verify_token()
        if result["success"]:
            self.yt.auth_error = False
            self.yt.auth_error_token_mtime = None
            logger.info("[YouTube] Re-authorization detected — auth_error cleared")
            self._broadcast_status_update("auth_recovered")
        else:
            # Record the new mtime so we don't re-verify the same file every tick
            self.yt.auth_error_token_mtime = current

    def _enter_auth_error(self) -> None:
        """invalid_grant → stop starting sessions until re-authorized (spec §2)."""
        self.yt.auth_error = True
        self.yt.auth_error_token_mtime = auth.token_file_mtime()
        logger.warning("[YouTube] auth_error: re-authorization required")
        self._broadcast_status_update("auth_error")

    # ----- Session start ----------------------------------------------------

    def _start_session(self, settings: Dict[str, Any]) -> bool:
        """Enqueue the generation task. session_active is set BEFORE the
        enqueue and cleared in the task's try/finally, so a queued-but-not-
        yet-running task can't be doubled by the next tick (spec §4)."""
        # Timer resets at START (enqueue) time — a completion-time reset
        # would allow re-triggering while the task waits in the LLM queue.
        self._timer_elapsed = 0.0
        self.yt.session_active = True
        self._broadcast_status_update("session_start")
        try:
            settings_snapshot = dict(settings)
            self.state.queue_manager.enqueue_llm_task(
                self._session_task, settings_snapshot,
                timeout=SESSION_QUEUE_TIMEOUT,
            )
            return True
        except Exception as e:
            self.yt.session_active = False
            logger.error(f"[YouTube] Failed to enqueue session: {e}")
            self._broadcast_status_update("session_end")
            return False

    def start_session_manually(self) -> Dict[str, Any]:
        """UI-triggered one-shot session (same exclusions as the scheduler,
        EXCEPT the master toggle: a manual run works while auto-reply is OFF
        — 稜指示 2026-07-12。投稿ワーカーも走り切る=一回分を完全に実行する)."""
        settings = self._load_settings()
        self._settings = settings
        if self.yt.auth_error:
            return {"success": False, "error": "auth_error"}
        reason = self._blocked_reason()
        if reason:
            return {"success": False, "error": reason}
        if not self._start_session(settings):
            return {"success": False, "error": "enqueue_failed"}
        return {"success": True}

    # ----- Generation phase (runs inside the LLM queue) ----------------------

    def _record_comment_activity(self, comment: Dict[str, str], status: str,
                                 reply_text: str = "", skip_reason: str = "",
                                 progress: bool = True) -> None:
        """Push one processed comment to the UI activity log (WS broadcast).
        progress=False for worker-side events (posted) so the n/m counter
        stays a generation-phase progress indicator."""
        self._last_comment_activity = {
            "author": comment.get("author_name", ""),
            "text": (comment.get("text", "") or "")[:100],
            "status": status,
            "skip_reason": skip_reason,
            "reply": (reply_text or "")[:200],
        }
        if progress:
            self._session_done += 1
        self._broadcast_status_update("comment_processed")

    def _session_task(self, settings: Dict[str, Any]) -> None:
        slog = YouTubeSessionLogger()
        end_reason = "exception"
        self._session_done = 0
        self._session_total = 0
        try:
            end_reason = self._run_generation(settings, slog)
        finally:
            # 正常完了・例外死・auth_error終了のすべてで確実に落とす (spec §4)
            self.yt.session_active = False
            try:
                slog.end_session(end_reason)
            except Exception:
                pass
            self._last_session_summary = end_reason
            logger.info(f"[YouTube] Session ended: {end_reason}")
            self._broadcast_status_update("session_end")

    def _run_generation(self, settings: Dict[str, Any],
                        slog: YouTubeSessionLogger) -> str:
        character_id = settings["character_id"]
        target_channel = settings["target_channel_id"]
        dry_run = settings["dry_run"]

        config = self._load_character_config(character_id)
        char_name = (config or {}).get("name", character_id[:8])
        slog.start_session(character_id, char_name, dry_run, target_channel)

        if not config:
            slog.log_event("error", f"character config not found: {character_id}")
            return "no_character"
        if not target_channel:
            slog.log_event("error", "target channel not configured")
            return "no_target_channel"

        # 0. Auth check (spec §4 セッション開始時の分岐)
        tok = auth.get_access_token()
        if not tok["success"]:
            if tok.get("invalid_grant"):
                self._enter_auth_error()
                return "auth_error"
            slog.log_event("error", f"token refresh failed: {tok['error']}")
            return "auth_transient"
        own = auth.get_authorized_channel() or {}
        own_channel_id = own.get("channel_id", "")

        # 1. Daily limit → neither fetch nor generate (spec §4-1)。通常は
        # _blocked_reason がセッション起動そのものを止めるので、ここへ来るのは
        # 「tick の判定後、このタスクが LLM キューで実際に走るまでの間に投稿
        # ワーカーが上限へ到達した」競合のみ＝取得の手前に置くフェイルセーフ。
        state = store.load_session_state()
        pending = store.pending_queue_items()
        if store.get_daily_count(state) >= settings["daily_post_limit"]:
            note = ("queue kept for tomorrow" if pending
                    else "no fetch/generation this session")
            slog.log_event("skip", f"daily post limit reached; {note}")
            return "daily_limit"

        # 1b. Queue residue → drain first, no new fetch (spec §4-1)
        if pending:
            slog.log_event("info", f"draining existing queue ({len(pending)} items)")
            self._start_worker(settings)
            return "drain_queue"

        # 2. Fetch latest top-level comments
        try:
            comments = youtube_api.fetch_latest_comments(
                target_channel, FETCH_MAX_RESULTS, FALLBACK_MAX_VIDEOS)
        except AuthRequiredError:
            self._enter_auth_error()
            return "auth_error"
        except (QuotaExceededError, TransientAPIError) as e:
            slog.log_event("error", f"fetch interrupted: {e}")
            return "interrupted"
        except PermanentAPIError as e:
            slog.log_event("error", f"fetch failed permanently (check settings): {e}")
            return "fetch_failed"
        slog.log_event("info", f"fetched {len(comments)} comments")

        # 3-4. Exclusion filter + newness judgement (+ initial baseline)
        candidates = store.filter_candidates(comments, state, own_channel_id)
        if state.get("baseline") is None:
            if not comments:
                return "initial_no_comments"  # stay initial (spec §4-4)
            store.record_initial_baseline(state, comments)
            store.save_session_state(state)
            slog.log_event(
                "info",
                f"initial baseline set to {state['baseline']} (no replies this session)",
            )
            return "initial_baseline"
        if not candidates:
            return "no_new_comments"
        targets = store.select_targets(candidates)
        slog.log_event(
            "info", f"{len(candidates)} new comments, {len(targets)} selected")
        self._session_total = len(targets)
        self._broadcast_status_update("targets_selected")

        # 5. Generate replies (1 comment = 1 LLM call)
        llm = self._create_llm(config)
        if llm is None:
            slog.log_event("error", "failed to create LLM client")
            return "llm_unavailable"
        from backend.shared.prompt_i18n import get_prompt_language
        language = get_prompt_language(config)
        rules = settings["rules"]
        prompt_logged = False

        for comment in targets:
            if self._stop_event.is_set():
                return "shutdown"  # unprocessed分は台帳/基準点未更新→次回再判定

            # 5a. Video context (permanent cache; first fetch via videos.list)
            ctx = store.get_video_context(comment["video_id"])
            if ctx is None:
                try:
                    info = youtube_api.fetch_video_info(comment["video_id"])
                    store.set_video_context(
                        comment["video_id"], info["title"], info["description"])
                    ctx = info
                except AuthRequiredError:
                    self._enter_auth_error()
                    return "auth_error"
                except PermanentAPIError:
                    self._record_skip(state, comment, "video_unavailable",
                                      slog, dry_run)
                    continue
                except (QuotaExceededError, TransientAPIError) as e:
                    # 中断: 台帳・基準点を進めない→次セッションで再試行 (spec §4-5a)
                    slog.log_event("error", f"video info interrupted: {e}")
                    return "interrupted"

            # 5b. Generate
            messages = build_reply_messages(
                config, comment, ctx,
                store.video_history(comment["video_id"]),
                store.user_history(comment["author_channel_id"]),
                rules, language,
            )
            # セッション最初の1件は組み立てた全メッセージをログへ残す
            # （全プロンプトセクションが本当に入っているかの事後確認用 — 稜指示）
            if not prompt_logged:
                slog.log_event("prompt_sample",
                               "full prompt of the first LLM call this session",
                               messages=messages)
                prompt_logged = True
            started = time.time()
            try:
                result = llm.invoke(messages)
                text = (getattr(result, "content", "") or "").strip()
                usage = getattr(result, "usage_metadata", None)
            except Exception as e:
                logger.warning(f"[YouTube] LLM call failed: {e}")
                text, usage = "", None
            elapsed_ms = int((time.time() - started) * 1000)

            if not text:
                self._record_skip(state, comment, "generation_failed",
                                  slog, dry_run, elapsed_ms=elapsed_ms)
                continue

            # 5c. Deny judgement (skip時も生成文をログに残す — spec §5)
            if is_deny(text):
                self._record_skip(state, comment, "denied", slog, dry_run,
                                  reply_text=text, usage=usage,
                                  elapsed_ms=elapsed_ms)
                continue

            if dry_run:
                slog.log_comment(comment, "dry_run", reply_text=text,
                                 usage=usage, elapsed_ms=elapsed_ms)
                self._record_comment_activity(comment, "dry_run", reply_text=text)
                continue

            # 5d. Persist: queue item FIRST, ledger SECOND (逆順だと
            # 「台帳にIDあり・キューに返信なし」＝黙って永久未返信 — spec §4-5d)
            store.append_queue_item(store.make_queue_item(comment, text))
            store.register_processed_ids(state, [comment["comment_id"]])
            store.save_session_state(state)
            slog.log_comment(comment, "generated", reply_text=text,
                             usage=usage, elapsed_ms=elapsed_ms)
            self._record_comment_activity(comment, "generated", reply_text=text)

        if dry_run:
            return "dry_run_completed"

        # 6. Completion bookkeeping: ledger ALL newness-judged candidates
        # (非選定分の永久スキップ含む) + advance the baseline (spec §4-6)
        store.finalize_baseline(state, candidates)
        state["session_count"] = int(state.get("session_count", 0)) + 1
        store.save_session_state(state)

        # Memory extraction (J5: 蓄積方向のみ) — we are already inside the
        # LLM queue task, so run inline (ELYTH runs extraction the same way).
        try:
            self._run_memory_extraction_if_needed(config, slog)
        except Exception as e:
            logger.error(f"[YouTube] Memory extraction failed: {e}", exc_info=True)

        self._start_worker(settings)
        return "completed"

    def _record_skip(self, state: Dict[str, Any], comment: Dict[str, str],
                     reason: str, slog: YouTubeSessionLogger, dry_run: bool,
                     reply_text: str = "", usage: Optional[Dict[str, Any]] = None,
                     elapsed_ms: int = 0) -> None:
        """Log a generation-phase skip and (unless dry-run) ledger the id so
        it is never re-processed (割り切り §10: skippedも再試行しない)."""
        slog.log_comment(comment, "skipped", skip_reason=reason,
                         reply_text=reply_text, usage=usage, elapsed_ms=elapsed_ms)
        self._record_comment_activity(comment, "skipped", reply_text=reply_text,
                                      skip_reason=reason)
        if not dry_run:
            store.register_processed_ids(state, [comment["comment_id"]])
            store.save_session_state(state)

    # ----- LLM helpers ------------------------------------------------------

    def _load_character_config(self, character_id: str) -> Optional[Dict[str, Any]]:
        if not character_id:
            return None
        try:
            from backend.conversation.character_manager import (
                _find_config_file_by_id, _load_config_file,
            )
            config_file = _find_config_file_by_id(character_id)
            if not config_file:
                return None
            return _load_config_file(config_file)
        except Exception as e:
            logger.error(f"[YouTube] Failed to load character config: {e}")
            return None

    def _create_llm(self, config: Dict[str, Any]):
        """Create the reply-generation LLM client (Ollama included)."""
        from backend.llm.api_integration import create_llm_client
        provider = config.get("model_provider", "ollama")
        result = create_llm_client(
            model_provider=provider,
            model_name=config.get("model_name", ""),
            tuning=config.get("tuning"),
            timeout=180.0,
            auto_inject_web_search=False,
            enable_prompt_caching=(provider != "ollama"),
        )
        if not result.get("success"):
            logger.error(f"[YouTube] LLM client creation failed: {result.get('error')}")
            return None
        return result["response"]

    # ----- Memory extraction (J5: accumulate only) ---------------------------

    def _run_memory_extraction_if_needed(self, config: Dict[str, Any],
                                         slog: YouTubeSessionLogger) -> None:
        """Every >50 posted replies, summarize the un-extracted history into
        long-term memory chunks for the CURRENT character (spec §5/§7-3).

        Source is history.json = posted のみ, so denied/skipped攻撃コメント
        本文は構造的に記憶へ入らない."""
        state = store.load_session_state()
        count = int(state.get("posts_since_extraction", 0))
        if count <= EXTRACTION_TRIGGER:
            return
        entries = store.recent_history(count)
        if not entries:
            return

        from backend.llm.api_integration import create_llm_client
        from backend.shared.prompt_i18n import (
            get_prompt_language, prompt_section, prompt_text,
        )
        language = get_prompt_language(config)
        system_prompt = prompt_section("youtube_memory_extraction", language)
        lines = []
        for e in entries:
            lines.append(
                f"[{e.get('timestamp', '')}] {e.get('author_name', '')}: "
                f"{e.get('comment_text', '')}\n"
                f"  → {e.get('reply_text', '')}"
            )
        user_prompt = prompt_text(
            "youtube.extraction_request", language, history="\n".join(lines))

        llm_result = create_llm_client(
            model_provider=config.get("model_provider", "ollama"),
            model_name=config.get("model_name", ""),
            usage_type="extraction",
            timeout=120.0,
        )
        if not llm_result.get("success"):
            logger.error("[YouTube] Extraction LLM client creation failed")
            return
        response = llm_result["response"].invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        text = getattr(response, "content", "") or ""
        memories = self._parse_extraction_response(text)
        if memories is None:
            logger.warning("[YouTube] Failed to parse extraction response")
            return

        character_id = config.get("character_id", "")
        self._ensure_memory_manager(character_id)
        memory_manager = self.state.memory_managers.get(character_id)
        if not memory_manager:
            logger.warning(f"[YouTube] No MemoryManager for {character_id}")
            return
        saved = 0
        for fragment in memories:
            if not isinstance(fragment, str) or not fragment.strip():
                continue
            result = memory_manager.add_memory_manual(
                category="youtube", content=fragment.strip())
            if result.get("success"):
                saved += 1
        slog.log_event("memory_extraction", f"saved {saved} fragments")
        logger.info(f"[YouTube] Memory extraction saved {saved} fragments")

        # Reset the counter only after a successful extraction pass
        state = store.load_session_state()
        state["posts_since_extraction"] = 0
        store.save_session_state(state)

    @staticmethod
    def _parse_extraction_response(text: str) -> Optional[List[str]]:
        """Parse {"memories": [...]} from the LLM output (code-block tolerant)."""
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1)
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        memories = data.get("memories", [])
        return memories if isinstance(memories, list) else None

    def _ensure_memory_manager(self, character_id: str) -> None:
        if not character_id or character_id in self.state.memory_managers:
            return
        try:
            from pathlib import Path

            from backend.memory.memory_manager import MemoryManager
            from backend.shared.constants import MEMORY_DIR
            db_file = str(MEMORY_DIR / f"{character_id}.db")
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            self.state.memory_managers[character_id] = MemoryManager(
                db_file, character_id)
        except Exception as e:
            logger.error(f"[YouTube] Failed to create MemoryManager: {e}")

    # ----- Posting worker (LLM-free, spec §4 投稿フェーズ) --------------------

    def _start_worker(self, settings: Dict[str, Any]) -> bool:
        """Start the posting worker if allowed (単一走行・キュー残あり・
        当日上限未達). Returns True if a worker was started."""
        state = store.load_session_state()
        if store.get_daily_count(state) >= settings["daily_post_limit"]:
            logger.info("[YouTube] Worker not started: daily post limit reached")
            return False
        with self.yt.worker_lock:
            if self.yt.worker_running:
                return False
            if not store.pending_queue_items():
                return False
            self.yt.worker_running = True
            self.yt.worker_stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop, args=(dict(settings),),
                name="youtube-post-worker", daemon=True,
            )
            self._worker_thread.start()
        self._broadcast_status_update("worker_start")
        return True

    def _worker_should_stop(self) -> bool:
        return self.yt.worker_stop_event.is_set() or self._stop_event.is_set()

    def _worker_loop(self, settings: Dict[str, Any]) -> None:
        end_reason = "queue_empty"
        try:
            end_reason = self._run_worker(settings)
        except Exception as e:
            end_reason = "exception"
            logger.error(f"[YouTube] Worker crashed: {e}", exc_info=True)
        finally:
            # 例外死でフラグが立ちっぱなし→全セッション恒久開始不能、を防ぐ
            self.yt.worker_running = False
            logger.info(f"[YouTube] Worker ended: {end_reason}")
            self._broadcast_status_update("worker_end")

    def _run_worker(self, settings: Dict[str, Any]) -> str:
        own = auth.get_authorized_channel() or {}
        own_channel_id = own.get("channel_id", "")

        # --- posting-orphan recovery, EVERY start (spec §4-7 / 二重投稿防止).
        # Verify by API whether our reply actually exists; never re-post on
        # guesswork. A failed verification keeps the item at posting.
        for item in store.pending_queue_items(["posting"]):
            if self._worker_should_stop():
                return "stopped"
            comment_id = item.get("comment_id", "")
            try:
                authors = youtube_api.list_reply_author_channel_ids(comment_id)
            except AuthRequiredError:
                self._enter_auth_error()
                return "auth_error"
            except (QuotaExceededError, TransientAPIError) as e:
                logger.warning(f"[YouTube] posting verification failed, kept: {e}")
                return "verify_failed"  # posting のまま保持→次回再検証 (spec §4)
            except PermanentAPIError:
                # Parent comment gone — the reply can never be verified nor
                # posted. Same treatment as a permanent posting error.
                self._skip_queue_item(item, "permanent_error")
                continue
            if own_channel_id and own_channel_id in authors:
                logger.info(f"[YouTube] posting orphan confirmed posted: {comment_id}")
                self._confirm_posted(item)
            else:
                logger.info(f"[YouTube] posting orphan reverted to generated: {comment_id}")
                store.update_queue_item(comment_id, status="generated")

        # --- main posting loop
        while True:
            if self._worker_should_stop():
                return "stopped"
            # enabled はここでは読まない: OFF「への切替」は reload_settings が
            # worker_stop_event で即停止させる。値の常時ポーリングにすると
            # OFFのままの手動一回分実行(稜指示 2026-07-12)が投稿できなくなる。
            current = self._load_settings()
            state = store.load_session_state()
            if store.get_daily_count(state) >= current["daily_post_limit"]:
                return "daily_limit"
            items = store.pending_queue_items(["generated"])
            if not items:
                return "queue_empty"
            item = items[0]
            comment_id = item.get("comment_id", "")

            # posting を書いて永続化してから insert (クラッシュ窓の限定 — spec §4-7)
            store.update_queue_item(comment_id, status="posting")
            try:
                youtube_api.insert_reply(comment_id, item.get("reply_text", ""))
            except AuthRequiredError:
                # stays at posting — the recovery pass will resolve it
                self._enter_auth_error()
                return "auth_error"
            except QuotaExceededError:
                store.update_queue_item(comment_id, status="generated")
                return "quota_exceeded"  # 翌日以降のセッション〔1〕で再開
            except PermanentAPIError as e:
                logger.info(f"[YouTube] permanent posting error for {comment_id}: {e}")
                self._skip_queue_item(item, "permanent_error")
            except TransientAPIError as e:
                retry_count = int(item.get("retry_count", 0)) + 1
                if retry_count >= POST_RETRY_LIMIT:
                    logger.warning(
                        f"[YouTube] retry exhausted for {comment_id}: {e}")
                    self._skip_queue_item(item, "retry_exhausted")
                else:
                    logger.info(
                        f"[YouTube] transient posting error for {comment_id} "
                        f"(retry {retry_count}/{POST_RETRY_LIMIT}): {e}")
                    store.update_queue_item(
                        comment_id, status="generated", retry_count=retry_count)
            else:
                self._confirm_posted(item)
                self._record_comment_activity(
                    {"author_name": item.get("author_name", ""),
                     "text": item.get("comment_text", "")},
                    "posted", reply_text=item.get("reply_text", ""),
                    progress=False)

            # 1〜5分のランダムクールタイム (stop要求で即中断 — spec §4-9)
            wait_s = random.uniform(
                min(current["cooldown_min"], current["cooldown_max"]),
                max(current["cooldown_min"], current["cooldown_max"]),
            )
            if self.yt.worker_stop_event.wait(timeout=wait_s):
                return "stopped"

    def _confirm_posted(self, item: Dict[str, Any]) -> None:
        """posted 確定: 状態→履歴転記→カウンタ→台帳(冪等)→キュー掃除."""
        comment_id = item.get("comment_id", "")
        store.update_queue_item(comment_id, status="posted")
        store.append_history(item)
        state = store.load_session_state()
        store.increment_daily_count(state)
        state["posts_since_extraction"] = int(
            state.get("posts_since_extraction", 0)) + 1
        # 5d の極小窓(キュー書込後・台帳追加前のプロセス死)をここで閉じる
        store.register_processed_ids(state, [comment_id])
        store.save_session_state(state)
        store.remove_queue_item(comment_id)

    def _skip_queue_item(self, item: Dict[str, Any], reason: str) -> None:
        """skipped 確定: ログ記録→台帳(冪等)→キュー掃除（キューを詰まらせない）."""
        comment_id = item.get("comment_id", "")
        store.update_queue_item(comment_id, status="skipped", skip_reason=reason)
        state = store.load_session_state()
        store.register_processed_ids(state, [comment_id])
        store.save_session_state(state)
        store.remove_queue_item(comment_id)

    # ----- Status / UI ------------------------------------------------------

    def get_status(self, event: str = "status") -> Dict[str, Any]:
        settings = self._settings
        # running/posting は paused より優先: OFF中の手動一回分実行でも
        # 「実行中」を表示する（「停止中のまま動いて見えない」— 稜指摘 2026-07-12）
        if self.yt.auth_error:
            state_name = "auth_error"
        elif self.yt.session_active:
            state_name = "running"
        elif self.yt.worker_running:
            state_name = "posting"
        elif not settings["enabled"]:
            state_name = "paused"  # enabled OFF の表示名 (spec §7)
        elif self._waiting_reason:
            state_name = "waiting"
        else:
            state_name = "idle"

        session_state = store.load_session_state()
        return {
            "event": event,
            "state": state_name,
            "waiting_reason": self._waiting_reason,
            "enabled": settings["enabled"],
            "dry_run": settings["dry_run"],
            "interval_seconds": settings["interval_seconds"],
            "timer_elapsed": self._timer_elapsed,
            "timer_remaining": max(
                0.0, settings["interval_seconds"] - self._timer_elapsed),
            # 会話状態中はタイマーが進まない(J9) — JSはこの間カウントダウンを止める
            "timer_paused": self._is_conversation_active(),
            "queue_generated": len(store.pending_queue_items(["generated"])),
            "queue_posting": len(store.pending_queue_items(["posting"])),
            "daily_count": store.get_daily_count(session_state),
            "daily_post_limit": settings["daily_post_limit"],
            "authorized_channel": auth.get_authorized_channel(),
            "last_session": self._last_session_summary,
            # アクティビティログ用(JSはcomment_processedイベント時のみ積む)
            "last_comment": self._last_comment_activity,
            "session_done": self._session_done,
            "session_total": self._session_total,
            "timestamp": awake_seconds(),
        }

    def _broadcast_status_update(self, event: str = "status") -> None:
        """Push status to the browser via the ui_events interface
        (WS transport self-subscribes — spec §3/§7; direct websocket_server
        import is forbidden by the layer rules)."""
        try:
            from backend.shared.ui_events import publish_ui_update
            publish_ui_update("youtube_status", event, self.get_status(event))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_youtube_session_manager: Optional[YouTubeSessionManager] = None
_ysm_lock = threading.Lock()


def get_youtube_session_manager(state=None) -> YouTubeSessionManager:
    """Get the global YouTubeSessionManager instance."""
    global _youtube_session_manager
    with _ysm_lock:
        if _youtube_session_manager is None:
            if state is None:
                raise RuntimeError(
                    "YouTubeSessionManager not initialized. Pass state on first call.")
            _youtube_session_manager = YouTubeSessionManager(state)
    return _youtube_session_manager
