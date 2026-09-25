"""
conversation_manager.py

Manages all conversation-related functionality including message generation,
conversation state, memory management, and memory extraction.
"""

import os
import time
import threading
import uuid
import shutil
import logging
import traceback
import concurrent.futures
from typing import Any, Dict, List, Optional, Callable
from pathlib import Path

# Backend utilities
from .shared.backend_utils import (
    standardize_response, validate_input, check_resources_cached,
    validate_character_id, validate_string_param,
    validate_int_param, validate_message, RECOVERABLE_ERRORS
)

# Import required modules
from backend.memory.memory_manager import MemoryManager
from backend.shared.constants import ATTACHMENTS_DIR, MEMORY_DIR, MEMORY_TASK_QUEUE_TIMEOUT
from backend.shared.timing_logger import start_turn, timing_log, timing_block

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def remove_attachment_files(character_id: str) -> None:
    """Delete the character's attachment folder (attachments/<uuid>/).

    履歴クリア(reset_short_term_history)/会話リセット(reset_conversation)で
    メッセージ(=参照側)を全消しするとき、参照だけ死んで実体ファイルが
    永久に残るのを防ぐ(稜裁定 2026-08-20 の逆方向=参照を捨てたら実体も捨てる)。
    キャラ削除時は character_manager._remove_memory_artifacts が同じ
    フォルダを消す。フォルダが無ければ何もしない。
    """
    target = ATTACHMENTS_DIR / character_id
    if not target.is_dir():
        return
    try:
        shutil.rmtree(target)
        logger.info(f"Removed attachment files for {character_id}")
    except Exception as e:
        logger.error(f"Failed to remove attachment files for {character_id}: {e}")


class ConversationManager:
    """
    Manages conversation state and operations for the AI girlfriend application.
    This class encapsulates all conversation-related functionality previously in backend.py.
    """
    
    def __init__(self, backend_state, character_config_loader: Callable,
                 character_activator: Callable, find_config_file_func: Callable):
        """
        Initialize the conversation manager with required dependencies.

        Args:
            backend_state: The backend state singleton containing all state data
            character_config_loader: Function to load character configuration
            character_activator: Function to activate a character
            find_config_file_func: Function to find config file by character ID
        """
        self.state = backend_state
        self.load_character_config = character_config_loader
        self.activate_character = character_activator
        self._find_config_file_by_id = find_config_file_func
        
        # Cache for resource checking
        self._resource_cache = {'time': 0, 'data': None}

        # Track pending memory save threads per character (for async memory save)
        self._pending_memory_save_threads: Dict[str, threading.Thread] = {}

    def _check_resources_cached(self) -> Optional[Dict[str, float]]:
        """Check system resources with 5-second cache to reduce overhead"""
        return check_resources_cached(self._resource_cache)

    def _ensure_llm_in_cache(self, character_id: str, config: Dict) -> Optional[Any]:
        """
        Ensure LLM is in cache. If not, recreate it with current tuning parameters.

        Args:
            character_id: Character ID
            config: Character configuration dictionary

        Returns:
            LLM instance or None if creation failed
        """
        chat_llm = self.state.active_llm_cache.get(character_id)

        if chat_llm:
            # Refresh LRU position: eviction (which closes clients) must never
            # target the LLM a turn is currently using.
            self.state.active_llm_cache.move_to_end(character_id)
            return chat_llm

        # LLM not in cache - recreate with current tuning parameters
        logger.info(f"LLM not in cache for {character_id}, recreating...")

        from backend.llm.api_integration import create_llm_client
        from backend.shared.constants import OLLAMA_GENERATION_TIMEOUT

        model_provider = config.get("model_provider", "ollama")
        model_name = config.get("model_name", "") or config.get("ollama_model_name", "llama2")
        tuning = config.get("tuning", None)

        result = create_llm_client(
            model_provider=model_provider,
            model_name=model_name,
            tuning=tuning,
            usage_type='conversation',
            timeout=OLLAMA_GENERATION_TIMEOUT
        )

        if result.get("success"):
            self.state.active_llm_cache[character_id] = result["response"]
            logger.info(f"Successfully recreated LLM for {character_id} (provider={model_provider})")
            return result["response"]
        else:
            # warning: 呼出元(_generate_reply_task の chat_llm 不在分岐)が
            # ERRORログする=コールドパス3段トーストの解消(稜裁定 2026-08-02)
            logger.warning(f"Failed to recreate LLM for {character_id}: {result.get('error')}")
            return None

    def _enqueue_llm_task(self, task_callable, *args, **kwargs):
        """
        Helper to place a new LLM-related job on the queue and block until it completes.
        Uses QueueManager to ensure sequential LLM operations.
        Returns the result (or raises the exception if an error occurred).

        Added features:
        - Task tracking with unique request_id
        - Timeout handling for LLM operations
        - Cancellation capability for long-running tasks
        """
        # Generate a unique ID for this request if not provided
        request_id = kwargs.pop('request_id', str(uuid.uuid4()))
        timeout = kwargs.pop('timeout', 90.0)  # Safety margin for longer generation

        # Store request tracking info. pending_requests is touched from multiple
        # real threads (UI + extraction/relationship background workers), so all
        # init/iterate/mutate goes under pending_requests_lock to avoid
        # "dictionary changed size during iteration". Both names are
        # backward-compat properties owned by state.llm_requests (M13 §3).
        with self.state.pending_requests_lock:
            self.state.pending_requests[request_id] = {
                "status": "pending",
                "task_name": task_callable.__name__ if hasattr(task_callable, "__name__") else "unknown_task",
                "timestamp": time.time(),
                "args": args,
                "timeout": timeout
            }

            # Log task submission
            logger.debug(f"Enqueueing LLM task {request_id}: {self.state.pending_requests[request_id]['task_name']}")

            # Check for queue overflow
            MAX_PENDING_REQUESTS = 10

            # Clean up old completed requests on every enqueue. This replaces the
            # per-task cleanup threads (one 5-minute sleeper per LLM task) that
            # used to do the same job in the finally block below.
            old_request_ids = [req_id for req_id, req in self.state.pending_requests.items()
                              if req.get("status") in ["completed", "error", "timeout"] and
                              time.time() - req.get("timestamp", 0) > 300]  # 5 minutes old

            for req_id in old_request_ids:
                del self.state.pending_requests[req_id]

            # Count active requests (exclude completed/error/timeout ones)
            active_requests = sum(1 for req in self.state.pending_requests.values()
                                 if req.get("status") in ["pending", "submitted"])

            if active_requests >= MAX_PENDING_REQUESTS:
                # Clean up stuck requests (pending for more than 2 minutes)
                stuck_request_ids = [req_id for req_id, req in self.state.pending_requests.items()
                                    if req.get("status") in ["pending", "submitted"] and
                                    time.time() - req.get("timestamp", 0) > 120]  # 2 minutes

                for req_id in stuck_request_ids:
                    logger.warning(f"Removing stuck request {req_id} (age: {time.time() - self.state.pending_requests[req_id].get('timestamp', 0):.1f}s)")
                    self.state.pending_requests[req_id]["status"] = "timeout"
                    self.state.pending_requests[req_id]["error"] = "Request timed out after 2 minutes"

                # Recount after cleanup
                active_requests = sum(1 for req in self.state.pending_requests.values()
                                     if req.get("status") in ["pending", "submitted"])

                if active_requests >= MAX_PENDING_REQUESTS:
                    logger.warning(f"Queue overflow: {active_requests} active requests")
                    del self.state.pending_requests[request_id]  # Remove the request we just added
                    raise RuntimeError(f"Too many pending requests ({active_requests}). Please wait for current requests to complete.")

        try:
            # Check if queue_manager is available
            if not self.state.queue_manager:
                raise RuntimeError("LLMTaskQueue is not initialized. Backend initialization may have failed.")

            # Update status to submitted
            self.state.pending_requests[request_id]["status"] = "submitted"

            # QueueManager.enqueue_llm_task returns a Future that we can wait on.
            # timeout はキュー側にも渡す: 渡さないとワーカーは既定300秒まで
            # タスクを抱え続け、待ち手が諦めた数分後に queue_manager 側の
            # タイムアウトログが遅れて飛ぶ(稜裁定 2026-08-02)
            future = self.state.queue_manager.enqueue_llm_task(
                task_callable, *args, timeout=timeout, **kwargs)

            try:
                # Wait for result with timeout and return it (or raise exception)
                # スライス待ち: TTS再生中は残り時間を減らさない(稜裁定
                # 2026-08-12: 読み上げ=生成成功後なのでタイムアウトさせない。
                # 対の停止判定が queue_manager 側の時計にもある=時計は2つ)。
                # 停止は有界(playback_state の docstring 参照)。
                from backend.shared.playback_state import is_tts_active
                remaining = float(timeout)
                while True:
                    try:
                        result = future.result(timeout=0.5)
                        break
                    except concurrent.futures.TimeoutError:
                        if not is_tts_active():
                            remaining -= 0.5
                        if remaining <= 0:
                            raise
                self.state.pending_requests[request_id]["status"] = "completed"
                self.state.pending_requests[request_id]["completed_at"] = time.time()
                return result
            except concurrent.futures.TimeoutError:
                # Handle timeout
                self.state.pending_requests[request_id]["status"] = "timeout"
                # ERROR ログは最上位 except(generate_reply 側)に一本化 —
                # 同一障害の多段トースト防止(稜裁定 2026-08-02)。詳細は
                # warning でファイルログに残す。
                logger.warning(f"LLM task {request_id} timed out after {timeout}s")

                # Attempt to cancel the future if possible
                future.cancel()

                raise RuntimeError(f"LLM task timed out after {timeout} seconds")
        except Exception as e:
            # Update status on error
            if request_id in self.state.pending_requests:
                self.state.pending_requests[request_id]["status"] = "error"
                self.state.pending_requests[request_id]["error"] = str(e)
            # 直上の timeout except や呼出元 except と同一例外を重ねてログ
            # しない(ERROR は最上位に一本化・稜裁定 2026-08-02)
            logger.debug(f"Error in LLM task {request_id}: {e}")
            raise

    @validate_input({
        'user_text': validate_string_param,
        'character_id': validate_character_id
    })
    def generate_reply(self, user_text: str, character_id: Optional[str] = None,
                       images: list = None, documents: list = None,
                       is_auto_prompt: bool = False) -> Dict[str, Any]:
        """
        Produce an AI response while maintaining multi-turn memory.
        This function enqueues a single LLM operation to avoid concurrency issues.
        Returns a dictionary with the AI response and status information.

        Args:
            user_text: The user's input text
            character_id: The character to respond as
            images: Optional list of image file paths
            documents: Optional list of document dicts [{filename, text, char_count}]
            is_auto_prompt: True when user_text is the AutoPrompt fixed message
                (保存時にUI非表示用のmetadata印を付ける。プロンプト組み立ては不変)

        Return format:
        {
            "success": bool,         # Whether the operation was successful
            "response": str,         # The AI's response text (if successful)
            "error": str,            # Error message (if unsuccessful)
            "error_type": str,       # Type of error (RESOURCE_LIMIT, VALIDATION, SERVICE, etc.)
            "details": dict          # Additional details about the error or response
        }
        """
        # Check if conversation is active
        if not self.state.conversation_active:
            logger.error("Attempted to generate reply without active conversation")
            return {
                "success": False,
                "error": "No active conversation. Please start a conversation first.",
                "error_type": "NO_CONVERSATION"
            }

        # Start timing for this conversation turn
        start_turn()

        # Resource validation using cached check
        resources = self._check_resources_cached()
        if resources:
            # Memory threshold check
            if resources['memory'] > 90:
                logger.warning(f"System memory critically low: {resources['memory']}%")
                return {
                    "success": False,
                    "error": "System resources limited. Please try again later.",
                    "error_type": "RESOURCE_LIMIT",
                    "details": {"memory_usage": resources['memory']}
                }
            
            # CPU threshold check
            if resources['cpu'] > 95:
                logger.warning(f"CPU usage critically high: {resources['cpu']}%")
                return {
                    "success": False,
                    "error": "System resources limited. Please try again later.",
                    "error_type": "RESOURCE_LIMIT",
                    "details": {"cpu_usage": resources['cpu']}
                }
            
            # Disk space check
            if 'disk_free' in resources and isinstance(resources['disk_free'], dict):
                disk_info = resources['disk_free']
                if not disk_info.get('healthy', True):
                    logger.warning(f"Low disk space detected: {disk_info}")
                    return {
                        "success": False,
                        "error": "I'm running out of space to store our memories. Please free up some disk space.",
                        "error_type": "RESOURCE_LIMIT",
                        "details": {"disk_warnings": disk_info.get('warnings', [])}
                    }

        timing_log("resource_check")

        # Character validation
        if not character_id:
            character_id = self.state.active_character_id
        if not character_id:
            logger.error("No active character to generate reply.")
            return {
                "success": False,
                "error": "No active character selected.",
                "error_type": "VALIDATION"
            }

        # Wait for previous memory save to complete (if any)
        pending_thread = self._pending_memory_save_threads.get(character_id)
        if pending_thread and pending_thread.is_alive():
            timing_log("waiting_for_previous_memory_save")
            pending_thread.join(timeout=10.0)
            if pending_thread.is_alive():
                logger.warning("Previous memory save still running after 10s, proceeding anyway")

        # Validate user input
        if not user_text or not user_text.strip():
            return {
                "success": False,
                "error": "Please enter a message to send.",
                "error_type": "VALIDATION"
            }
        
        MAX_INPUT_LENGTH = 10000  # ~2000 words
        if len(user_text) > MAX_INPUT_LENGTH:
            return {
                "success": False,
                "error": f"Message too long. Please keep messages under {MAX_INPUT_LENGTH} characters.",
                "error_type": "VALIDATION",
                "details": {"length": len(user_text), "max_length": MAX_INPUT_LENGTH}
            }
        
        # Sanitize user input to prevent prompt injection
        from .shared.backend_utils import sanitize_user_input
        sanitized_text = sanitize_user_input(user_text)
        
        # Log if sanitization occurred
        if sanitized_text != user_text:
            logger.info(f"User input was sanitized: {len(user_text)} -> {len(sanitized_text)} chars")
        
        # Proceed with LLM task
        try:
            # Log the request for traceability
            logger.info(f"Generating reply for character {character_id} with input length {len(sanitized_text)}")
            # Timeout must accommodate grey-list approval wait (user interaction)
            # and multi-step tool turns. タイムアウト後もワーカーは完走する設計
            # (queue_manager がスレッド完了を待つ)ため、短すぎると
            # 「UIはエラー・履歴には応答が記録」の二重記録になる。
            # ツールが載りうるターンは全て長タイムアウトにする
            # (command/deep_search に限定していた旧条件は image_gen/camera/map/ELYTH を取りこぼしていた。
            #  notes/talk_theme も同種の取りこぼし=ツールループが走るのに90秒枠に
            #  落ちていた。Ollamaの遅い生成×複数往復で顕在化=2026-08-12 実機)。
            # 既定値はゲート側(_should_include_note/talk_theme_tools)と同じ True。
            from backend.tools.location_manager import has_valid_location
            tools_possible = (
                getattr(self.state, 'command_execution_enabled', False)
                or getattr(self.state, 'deep_search_enabled', False)
                or getattr(self.state, 'image_generation_enabled', False)
                or getattr(self.state, 'camera_capture_enabled', False)
                or getattr(self.state, 'elyth_enabled', False)
                or getattr(self.state, 'notes_enabled', True)
                or getattr(self.state, 'talk_theme_enabled', True)
                or has_valid_location()  # map tools はフラグでなく位置情報の有無でゲート
            )
            task_timeout = 300.0 if tools_possible else 90.0
            result = self._enqueue_llm_task(self._generate_reply_task, sanitized_text, character_id, images, documents, is_auto_prompt, timeout=task_timeout)

            # The _generate_reply_task should always return a dictionary now,
            # but we'll verify this and normalize if needed
            if isinstance(result, str):
                # Legacy string response - normalize to standard format
                logger.warning(f"Legacy string response detected from _generate_reply_task: {result[:50]}...")
                # Check if it's an error message
                if result.startswith("Error:"):
                    return {
                        "success": False,
                        "error": result[7:],  # Remove "Error: " prefix
                        "error_type": "INTERNAL",
                        "details": {"legacy_response": True}
                    }
                else:
                    return {
                        "success": True,
                        "response": result,
                        "details": {"legacy_response": True}
                    }
            elif isinstance(result, dict):
                # Ensure the dictionary follows our standard structure
                if "success" not in result:
                    logger.warning("Dictionary response missing 'success' field, adding it")
                    result["success"] = "response" in result and bool(result.get("response"))

                # Ensure error fields exist when success is False
                if not result.get("success", False) and "error" not in result:
                    logger.warning("Failed response missing 'error' field, adding it")
                    result["error"] = "Unknown error occurred"

                if not result.get("success", False) and "error_type" not in result:
                    logger.warning("Failed response missing 'error_type' field, adding it")
                    result["error_type"] = "INTERNAL"

                # Ensure we have a timestamp for all responses
                if "details" not in result:
                    result["details"] = {}

                result["details"]["timestamp"] = time.time()

                return result
            else:
                # This should never happen with the updated _generate_reply_task
                logger.error(f"Unexpected result type from _generate_reply_task: {type(result)}")
                return {
                    "success": False,
                    "error": f"Unexpected response format from LLM task: {type(result)}",
                    "error_type": "INTERNAL",
                    "details": {
                        "actual_type": str(type(result)),
                        "timestamp": time.time()
                    }
                }
        except Exception as e:
            error_details = traceback.format_exc()
            logger.error(f"Error enqueueing LLM task: {e}")
            logger.debug(f"Error details: {error_details}")

            # Categorize the error type
            error_msg = str(e).lower()
            # "timed out" も判定: キュータイムアウトの実メッセージは
            # "LLM task timed out after Ns" で "timeout" を含まず、従来は
            # INTERNAL に誤分類されていた(稜裁定 2026-08-02)
            if "timeout" in error_msg or "timed out" in error_msg:
                error_type = "TIMEOUT"
            elif "memory" in error_msg or "resource" in error_msg:
                error_type = "RESOURCE_LIMIT"
            elif "queue" in error_msg and ("full" in error_msg or "overflow" in error_msg):
                error_type = "RESOURCE_LIMIT"
            else:
                error_type = "INTERNAL"

            return {
                "success": False,
                "error": f"Failed to process request: {str(e)}",
                "error_type": error_type,
                "details": {
                    "timestamp": time.time()
                }
            }

    # ====================================================================
    # Command Execution helpers
    # ====================================================================

    def _build_connection_info(self, language: str) -> str:
        """Build connection info string for prompt (API only)."""
        from backend.shared.prompt_i18n import prompt_text
        if not getattr(self.state, 'server_mode', False):
            return prompt_text("connection.local", language)
        try:
            from backend.server.session_manager import get_session_manager
            status = get_session_manager().get_status()
            device_name = status['primary_session'].get('device_name', '')
            if device_name:
                return prompt_text("connection.remote", language, device_name=device_name)
        except Exception as e:
            logger.warning(f"Failed to get connection info: {e}")
        return prompt_text("connection.remote_fallback", language)

    @staticmethod
    def _ollama_capability_gate(config: Dict, needs_tools: bool = False,
                                needs_vision: bool = False) -> bool:
        """Ollamaキャラのcapability軸ゲート(2026-08-11 稜裁定=per-model 2軸)。

        非Ollamaプロバイダは常にTrue。判定不能(未照会・旧Ollama・停止)は
        fail-closed=False。get_caps はメモリキャッシュ命中が平常のため
        ターン毎のHTTP/ファイルI/Oは発生しない。feature_availability の
        FEATURE_AXES と同じ軸判定(あちらはUI可用性・こちらはLLMへの提供)。
        """
        if config.get("model_provider", "ollama") != "ollama":
            return True
        model = config.get("model_name", "") or config.get("ollama_model_name", "")
        if not model:
            return False
        from backend.llm.ollama_capabilities import get_caps
        caps = get_caps(model)
        if not caps.get("known"):
            return False
        if needs_tools and not caps.get("tools"):
            return False
        if needs_vision and not caps.get("vision"):
            return False
        return True

    def _should_include_command_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if command execution tools should be included in the LLM request."""
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        if not getattr(self.state, 'command_execution_enabled', False):
            return False
        if getattr(self.state, 'server_mode', False):
            return False
        # Rate limit check
        if not self._check_command_rate(character_id):
            return False
        return True

    def _check_command_rate(self, character_id: str) -> bool:
        """Check if command execution is within rate limits."""
        from backend.shared.constants import COMMAND_RATE_WINDOW, COMMAND_RATE_MAX
        if not hasattr(self, '_command_tracker'):
            self._command_tracker = {}
        tracker = self._command_tracker.get(character_id, [])
        recent = tracker[-COMMAND_RATE_WINDOW:]
        total = sum(recent)
        if total >= COMMAND_RATE_MAX:
            logger.info(f"[Command] Rate limit reached for {character_id}: {total}/{COMMAND_RATE_MAX} in last {COMMAND_RATE_WINDOW} turns")
            return False
        return True

    def _should_include_note_tools(self, config: Dict) -> bool:
        """Determine if note tools should be included (tools軸)."""
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        return getattr(self.state, 'notes_enabled', True)

    def _should_include_talk_theme_tools(self, config: Dict) -> bool:
        """Determine if talk theme tools should be included (tools軸)。

        非tools Ollamaはツール無しの手動テーマ注入のみ=部分対応
        (2026-07-25裁定)。tools対応OllamaはAPI同様のフル対応(2026-08-11裁定)。
        """
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        return getattr(self.state, 'talk_theme_enabled', True)

    def _should_include_camera_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if camera capture tools should be included (tools+vision軸).

        Phase 3 (2026-07-16): the server-mode block is gone — capture now runs
        on the client browser camera (Live Camera provider), so the tool
        works in both modes. Instead, the tool is only offered when a provider
        page is actually connected; the not_connected note covers the race
        where it disconnects between prompt build and the tool call.

        vision-onlyのOllamaモデルではUI上camera機能は有効(ambient一括
        スイッチ=稜裁定)だが、撮影ツール自体はtools軸も要るためここで絞る。
        """
        if not self._ollama_capability_gate(config, needs_tools=True, needs_vision=True):
            return False
        if not getattr(self.state, 'camera_capture_enabled', False):
            return False
        from backend.shared.ambient_camera_state import is_provider_available
        if not is_provider_available():
            return False
        # Rate limit check
        if not self._check_camera_rate(character_id):
            return False
        return True

    def _should_include_image_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if image generation tools should be included (tools+vision軸=
        生成画像を自分でも見るため両対応が必要・稜裁定)."""
        if not self._ollama_capability_gate(config, needs_tools=True, needs_vision=True):
            return False
        if not getattr(self.state, 'image_generation_enabled', False):
            return False
        # Check Google API key and model are configured
        from backend.shared.api_settings import get_image_generation_model, load_api_settings
        ig_provider, ig_model = get_image_generation_model()
        if not ig_provider or not ig_model:
            return False
        settings = load_api_settings()
        google_key = settings.get("google", {}).get("api_key", "")
        if not google_key:
            return False
        # Rate limit check
        if not self._check_image_gen_rate(character_id):
            return False
        return True

    def _check_image_gen_rate(self, character_id: str) -> bool:
        """Check if image generation is within rate limits."""
        from backend.shared.constants import IMAGE_GEN_RATE_WINDOW, IMAGE_GEN_RATE_MAX
        if not hasattr(self, '_image_gen_tracker'):
            self._image_gen_tracker = {}
        tracker = self._image_gen_tracker.get(character_id, [])
        recent = tracker[-IMAGE_GEN_RATE_WINDOW:]
        total = sum(recent)
        if total >= IMAGE_GEN_RATE_MAX:
            logger.info(f"[ImageGen] Rate limit reached for {character_id}: {total}/{IMAGE_GEN_RATE_MAX} in last {IMAGE_GEN_RATE_WINDOW} turns")
            return False
        return True

    def _update_image_gen_tracker(self, character_id: str, count: int) -> None:
        """Record image generation count for this turn."""
        if not hasattr(self, '_image_gen_tracker'):
            self._image_gen_tracker = {}
        tracker = self._image_gen_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import IMAGE_GEN_RATE_WINDOW
        if len(tracker) > IMAGE_GEN_RATE_WINDOW * 2:
            self._image_gen_tracker[character_id] = tracker[-IMAGE_GEN_RATE_WINDOW:]

    def reset_image_gen_tracker(self) -> None:
        """Clear image generation rate tracker for all characters."""
        if hasattr(self, '_image_gen_tracker'):
            self._image_gen_tracker.clear()
            logger.info("[ImageGen] Rate limit tracker reset")

    def _check_camera_rate(self, character_id: str) -> bool:
        """Check if camera capture is within rate limits."""
        from backend.shared.constants import CAMERA_RATE_WINDOW, CAMERA_RATE_MAX
        if not hasattr(self, '_camera_tracker'):
            self._camera_tracker = {}
        tracker = self._camera_tracker.get(character_id, [])
        recent = tracker[-CAMERA_RATE_WINDOW:]
        total = sum(recent)
        if total >= CAMERA_RATE_MAX:
            logger.info(f"[CameraCapture] Rate limit reached for {character_id}: {total}/{CAMERA_RATE_MAX} in last {CAMERA_RATE_WINDOW} turns")
            return False
        return True

    def _update_camera_tracker(self, character_id: str, count: int) -> None:
        """Record camera capture count for this turn."""
        if not hasattr(self, '_camera_tracker'):
            self._camera_tracker = {}
        tracker = self._camera_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import CAMERA_RATE_WINDOW
        if len(tracker) > CAMERA_RATE_WINDOW * 2:
            self._camera_tracker[character_id] = tracker[-CAMERA_RATE_WINDOW:]

    def reset_camera_tracker(self) -> None:
        """Clear camera capture rate tracker for all characters."""
        if hasattr(self, '_camera_tracker'):
            self._camera_tracker.clear()
            logger.info("[CameraCapture] Rate limit tracker reset")

    def _should_include_deep_search_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if deep search tools should be included (tools軸)."""
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        if not getattr(self.state, 'deep_search_enabled', False):
            return False
        # Exclude tools only when BOTH search_web AND read_webpage are over limits
        search_ok = self._check_search_web_rate(character_id)
        read_ok = self._check_read_webpage_rate(character_id)
        if not search_ok and not read_ok:
            return False
        return True

    def _should_include_elyth_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if ELYTH tools should be included in normal conversation (tools軸)."""
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        if not getattr(self.state, 'elyth_enabled', False):
            return False
        # Check character has ELYTH API key (引数のconfigをそのまま使う。
        # 旧実装のconfig再ロードはconfig引数化=C5で不要になった)
        try:
            if not config.get("elyth_api_key"):
                return False
        except Exception:
            return False
        return True

    def _check_search_web_rate(self, character_id: str) -> bool:
        """Check if search_web is within rate limits."""
        from backend.shared.constants import SEARCH_WEB_RATE_WINDOW, SEARCH_WEB_RATE_MAX
        if not hasattr(self, '_search_web_tracker'):
            self._search_web_tracker = {}
        tracker = self._search_web_tracker.get(character_id, [])
        recent = tracker[-SEARCH_WEB_RATE_WINDOW:]
        total = sum(recent)
        if total >= SEARCH_WEB_RATE_MAX:
            logger.info(f"[DeepSearch] search_web rate limit reached for {character_id}: {total}/{SEARCH_WEB_RATE_MAX} in last {SEARCH_WEB_RATE_WINDOW} turns")
            return False
        return True

    def _update_search_web_tracker(self, character_id: str, count: int) -> None:
        """Record search_web count for this turn."""
        if not hasattr(self, '_search_web_tracker'):
            self._search_web_tracker = {}
        tracker = self._search_web_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import SEARCH_WEB_RATE_WINDOW
        if len(tracker) > SEARCH_WEB_RATE_WINDOW * 2:
            self._search_web_tracker[character_id] = tracker[-SEARCH_WEB_RATE_WINDOW:]

    def reset_search_web_tracker(self) -> None:
        """Clear search_web rate tracker for all characters."""
        if hasattr(self, '_search_web_tracker'):
            self._search_web_tracker.clear()
            logger.info("[DeepSearch] search_web rate limit tracker reset")

    def _check_read_webpage_rate(self, character_id: str) -> bool:
        """Check if read_webpage is within rate limits."""
        from backend.shared.constants import READ_WEBPAGE_RATE_WINDOW, READ_WEBPAGE_RATE_MAX
        if not hasattr(self, '_read_webpage_tracker'):
            self._read_webpage_tracker = {}
        tracker = self._read_webpage_tracker.get(character_id, [])
        recent = tracker[-READ_WEBPAGE_RATE_WINDOW:]
        total = sum(recent)
        if total >= READ_WEBPAGE_RATE_MAX:
            logger.info(f"[DeepSearch] read_webpage rate limit reached for {character_id}: {total}/{READ_WEBPAGE_RATE_MAX} in last {READ_WEBPAGE_RATE_WINDOW} turns")
            return False
        return True

    def _update_read_webpage_tracker(self, character_id: str, count: int) -> None:
        """Record read_webpage count for this turn."""
        if not hasattr(self, '_read_webpage_tracker'):
            self._read_webpage_tracker = {}
        tracker = self._read_webpage_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import READ_WEBPAGE_RATE_WINDOW
        if len(tracker) > READ_WEBPAGE_RATE_WINDOW * 2:
            self._read_webpage_tracker[character_id] = tracker[-READ_WEBPAGE_RATE_WINDOW:]

    def reset_read_webpage_tracker(self) -> None:
        """Clear read_webpage rate tracker for all characters."""
        if hasattr(self, '_read_webpage_tracker'):
            self._read_webpage_tracker.clear()
            logger.info("[DeepSearch] read_webpage rate limit tracker reset")

    # --- Map Search tools (search_places / get_place_details / get_directions) ---

    def _should_include_map_tools(self, config: Dict, character_id: str) -> bool:
        """Determine if map search tools should be included (tools軸, valid location only)."""
        if not self._ollama_capability_gate(config, needs_tools=True):
            return False
        from backend.tools.location_manager import has_valid_location
        if not has_valid_location():
            return False
        from backend.shared.api_settings import get_google_maps_api_key
        if not get_google_maps_api_key():
            return False
        # Exclude tools only when ALL three tools are over limits
        sp_ok = self._check_search_places_rate(character_id)
        pd_ok = self._check_place_details_rate(character_id)
        dr_ok = self._check_directions_rate(character_id)
        if not sp_ok and not pd_ok and not dr_ok:
            return False
        return True

    def _build_tool_definitions(self, include_flags: Dict[str, bool],
                                model_provider: str, character_id: str,
                                config: Dict):
        """ゲート済み include_flags からプロバイダ形式のツール定義を組む。

        旧: _generate_reply_task 内のインラインブロック。C5でプロンプト
        組み立て前へ移動(Ollamaのtools推定トークンをtoken manager予約として
        渡すため)し、メソッド化した。定義内容・結合順序は移動前とバイト同一。
        Returns None when no tools are included.
        """
        if not any(include_flags.values()):
            return None
        from backend.shared.prompt_i18n import get_prompt_language
        tool_language = get_prompt_language(config)
        tools = []
        if include_flags["command"]:
            from backend.tools.command_executor import get_tool_definitions_for_provider
            tools.extend(get_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["note"]:
            from backend.memory.note_manager import get_note_tool_definitions_for_provider
            tools.extend(get_note_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["talk_theme"]:
            from backend.tools.talk_theme_tools import get_talk_theme_tool_definitions_for_provider
            tools.extend(get_talk_theme_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["deep_search"]:
            from backend.tools.deep_search_tools import get_deep_search_tool_definitions_for_provider
            tools.extend(get_deep_search_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["image"]:
            from backend.tools.image_generator import get_image_tool_definitions_for_provider
            tools.extend(get_image_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["camera"]:
            from backend.tools.camera_capture import get_camera_tool_definitions_for_provider
            tools.extend(get_camera_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["map"]:
            from backend.tools.map_search_tools import get_map_search_tool_definitions_for_provider
            tools.extend(get_map_search_tool_definitions_for_provider(model_provider, tool_language))
        if include_flags["elyth"]:
            from backend.elyth.elyth_tools import get_elyth_tool_definitions_for_provider
            tools.extend(get_elyth_tool_definitions_for_provider(model_provider, mode="conversation", language=tool_language))
        return tools

    def _check_search_places_rate(self, character_id: str) -> bool:
        """Check if search_places is within rate limits."""
        from backend.shared.constants import SEARCH_PLACES_RATE_WINDOW, SEARCH_PLACES_RATE_MAX
        if not hasattr(self, '_search_places_tracker'):
            self._search_places_tracker = {}
        tracker = self._search_places_tracker.get(character_id, [])
        recent = tracker[-SEARCH_PLACES_RATE_WINDOW:]
        total = sum(recent)
        if total >= SEARCH_PLACES_RATE_MAX:
            logger.info(f"[MapSearch] search_places rate limit reached for {character_id}: {total}/{SEARCH_PLACES_RATE_MAX} in last {SEARCH_PLACES_RATE_WINDOW} turns")
            return False
        return True

    def _update_search_places_tracker(self, character_id: str, count: int) -> None:
        """Record search_places count for this turn."""
        if not hasattr(self, '_search_places_tracker'):
            self._search_places_tracker = {}
        tracker = self._search_places_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import SEARCH_PLACES_RATE_WINDOW
        if len(tracker) > SEARCH_PLACES_RATE_WINDOW * 2:
            self._search_places_tracker[character_id] = tracker[-SEARCH_PLACES_RATE_WINDOW:]


    def _check_place_details_rate(self, character_id: str) -> bool:
        """Check if get_place_details is within rate limits."""
        from backend.shared.constants import GET_PLACE_DETAILS_RATE_WINDOW, GET_PLACE_DETAILS_RATE_MAX
        if not hasattr(self, '_place_details_tracker'):
            self._place_details_tracker = {}
        tracker = self._place_details_tracker.get(character_id, [])
        recent = tracker[-GET_PLACE_DETAILS_RATE_WINDOW:]
        total = sum(recent)
        if total >= GET_PLACE_DETAILS_RATE_MAX:
            logger.info(f"[MapSearch] get_place_details rate limit reached for {character_id}: {total}/{GET_PLACE_DETAILS_RATE_MAX} in last {GET_PLACE_DETAILS_RATE_WINDOW} turns")
            return False
        return True

    def _update_place_details_tracker(self, character_id: str, count: int) -> None:
        """Record get_place_details count for this turn."""
        if not hasattr(self, '_place_details_tracker'):
            self._place_details_tracker = {}
        tracker = self._place_details_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import GET_PLACE_DETAILS_RATE_WINDOW
        if len(tracker) > GET_PLACE_DETAILS_RATE_WINDOW * 2:
            self._place_details_tracker[character_id] = tracker[-GET_PLACE_DETAILS_RATE_WINDOW:]


    def _check_directions_rate(self, character_id: str) -> bool:
        """Check if get_directions is within rate limits."""
        from backend.shared.constants import GET_DIRECTIONS_RATE_WINDOW, GET_DIRECTIONS_RATE_MAX
        if not hasattr(self, '_directions_tracker'):
            self._directions_tracker = {}
        tracker = self._directions_tracker.get(character_id, [])
        recent = tracker[-GET_DIRECTIONS_RATE_WINDOW:]
        total = sum(recent)
        if total >= GET_DIRECTIONS_RATE_MAX:
            logger.info(f"[MapSearch] get_directions rate limit reached for {character_id}: {total}/{GET_DIRECTIONS_RATE_MAX} in last {GET_DIRECTIONS_RATE_WINDOW} turns")
            return False
        return True

    def _update_directions_tracker(self, character_id: str, count: int) -> None:
        """Record get_directions count for this turn."""
        if not hasattr(self, '_directions_tracker'):
            self._directions_tracker = {}
        tracker = self._directions_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import GET_DIRECTIONS_RATE_WINDOW
        if len(tracker) > GET_DIRECTIONS_RATE_WINDOW * 2:
            self._directions_tracker[character_id] = tracker[-GET_DIRECTIONS_RATE_WINDOW:]


    def _update_command_tracker(self, character_id: str, count: int) -> None:
        """Record command execution count for this turn."""
        if not hasattr(self, '_command_tracker'):
            self._command_tracker = {}
        tracker = self._command_tracker.setdefault(character_id, [])
        tracker.append(count)
        from backend.shared.constants import COMMAND_RATE_WINDOW
        if len(tracker) > COMMAND_RATE_WINDOW * 2:
            self._command_tracker[character_id] = tracker[-COMMAND_RATE_WINDOW:]

    def reset_command_tracker(self) -> None:
        """Clear command execution rate tracker for all characters."""
        if hasattr(self, '_command_tracker'):
            self._command_tracker.clear()
            logger.info("[Command] Rate limit tracker reset")

    def _set_pending_approval(self, character_id: str, tool_call: Dict, reason: str) -> None:
        """Set the approval-pending state for a grey-list command."""
        self.state.command_approval_pending = True
        command = tool_call["arguments"].get("command", "")
        self.state.command_pending_info = {
            "command": command,
            "reason": reason,
            "tool_call_id": tool_call.get("id", ""),
        }
        self.state.command_approval_result = None
        if hasattr(self.state, 'command_approval_event'):
            self.state.command_approval_event.clear()

        # Send WebSocket notification to enable Accept/Deny buttons and show approval UI
        try:
            from backend.server.websocket_server import send_ui_update
            # Get character name from config
            character_name = "キャラクター"
            try:
                config_response = self.load_character_config(character_id)
                if isinstance(config_response, dict) and 'success' in config_response:
                    if config_response.get('success'):
                        config = config_response.get('result', config_response)
                        character_name = config.get('name', 'キャラクター')
                elif isinstance(config_response, dict):
                    character_name = config_response.get('name', 'キャラクター')
            except Exception:
                pass
            send_ui_update("command_approval_pending", data={
                "command": command,
                "reason": reason,
                "character_name": character_name,
            })
        except Exception as e:
            logger.warning(f"[Command] Failed to send approval pending WS notification: {e}")

    def _wait_for_approval(self, character_id: str) -> str:
        """Block until the user approves, denies, or interrupts. Returns result string.

        承認待ちは COMMAND_APPROVAL_TIMEOUT で必ず戻り、超過時は拒否扱い。
        旧実装の無期限 wait は、呼出側が task_timeout(300s) で TimeoutError に
        なっても実行スレッドが承認イベントを待ち続け、単一FIFOワーカーの
        LLM キュー全体が停止していた(WS通知失敗時は復帰手段なし)。
        """
        from backend.shared.constants import COMMAND_APPROVAL_TIMEOUT
        approved_in_time = True
        if hasattr(self.state, 'command_approval_event'):
            approved_in_time = self.state.command_approval_event.wait(
                timeout=COMMAND_APPROVAL_TIMEOUT
            )
        if not approved_in_time:
            logger.warning(
                f"[Command] Approval wait timed out after {COMMAND_APPROVAL_TIMEOUT:.0f}s - treating as denied"
            )
            self.state.command_approval_result = 'denied'
        result = getattr(self.state, 'command_approval_result', 'denied') or 'denied'
        self.state.command_approval_pending = False
        self.state.command_pending_info = None

        # Notify frontend to remove approval UI
        try:
            from backend.server.websocket_server import send_ui_update
            send_ui_update("command_approval_resolved", data={"result": result})
        except Exception as e:
            logger.warning(f"[Command] Failed to send approval resolved WS notification: {e}")

        return result

    def _check_interrupted(self, character_id: str) -> bool:
        """Check if the user has interrupted with a new utterance."""
        return getattr(self.state, '_pending_interruption', False)

    def _run_tool_call_loop(
        self, chat_llm, converted_msgs: list, tools: list,
        model_provider: str, character_id: str, config: dict
    ) -> Dict[str, Any]:
        """
        Run the tool-call loop for command execution, note operations, and talk theme management.

        Returns:
            Dict with 'all_steps', 'note_steps', 'theme_steps', 'final_text', 'tool_call_count'.
        """
        from backend.shared.constants import MAX_COMMANDS_PER_TURN
        from backend.tools.command_security import check_command, SecurityVerdict
        from backend.tools.command_executor import (
            execute_command, format_tool_result_text, get_command_log
        )
        from backend.llm.api_integration import (
            format_tool_result_message, build_assistant_msg_with_tool_calls
        )
        from backend.memory.note_manager import NOTE_TOOL_NAMES, dispatch_note_tool
        from backend.tools.talk_theme_tools import TALK_THEME_TOOL_NAMES, dispatch_talk_theme_tool
        from backend.shared.prompt_i18n import get_prompt_language, prompt_text

        language = get_prompt_language(config)
        all_steps = []
        note_steps = []
        theme_steps = []
        tool_call_count = 0
        loop_messages = list(converted_msgs)
        final_text = ""

        for iteration in range(MAX_COMMANDS_PER_TURN + 1):
            # Invoke LLM
            result_msg = chat_llm.invoke(loop_messages, tools=tools)

            # Process text content
            if result_msg.content:
                text = result_msg.content
                step = {"type": "ai_text", "content": text}
                all_steps.append(step)
                final_text = text

                # If tool_calls follow, display this text NOW so TTS plays
                # during tool execution (e.g. image generation) rather than after.
                if result_msg.tool_calls:
                    self._display_pending_steps(all_steps, config)

            # No tool_calls → display final steps and done
            if not result_msg.tool_calls:
                self._display_pending_steps(all_steps, config)
                break

            # Separate tool calls into categories
            from backend.tools.image_generator import IMAGE_GEN_TOOL_NAMES
            from backend.tools.camera_capture import CAMERA_CAPTURE_TOOL_NAMES
            from backend.tools.deep_search_tools import DEEP_SEARCH_TOOL_NAMES
            from backend.tools.map_search_tools import MAP_SEARCH_TOOL_NAMES
            from backend.elyth.elyth_tools import ELYTH_API_TOOL_NAMES
            theme_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in TALK_THEME_TOOL_NAMES]
            note_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in NOTE_TOOL_NAMES]
            deep_search_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in DEEP_SEARCH_TOOL_NAMES]
            map_search_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in MAP_SEARCH_TOOL_NAMES]
            image_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in IMAGE_GEN_TOOL_NAMES]
            camera_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in CAMERA_CAPTURE_TOOL_NAMES]
            elyth_tcs = [tc for tc in result_msg.tool_calls if tc["name"] in ELYTH_API_TOOL_NAMES]
            _known_tool_names = (NOTE_TOOL_NAMES | TALK_THEME_TOOL_NAMES | DEEP_SEARCH_TOOL_NAMES
                                 | MAP_SEARCH_TOOL_NAMES | IMAGE_GEN_TOOL_NAMES | CAMERA_CAPTURE_TOOL_NAMES
                                 | ELYTH_API_TOOL_NAMES)
            command_tcs = [tc for tc in result_msg.tool_calls
                          if tc["name"] not in _known_tool_names]

            # Sort note tool calls by index descending (safe for multi-operation)
            note_tcs.sort(
                key=lambda tc: tc.get("arguments", {}).get("index", 0),
                reverse=True
            )

            # Collect all tool results (theme + note + command)
            tool_results = []
            interrupted = False
            cmd_log = get_command_log()

            # --- Process talk theme tool calls (immediate, no security check) ---
            for tc in theme_tcs:
                result_text = dispatch_talk_theme_tool(character_id, tc, language)
                tool_results.append({
                    "tc": tc, "status": "executed", "result_text": result_text
                })
                args = tc.get("arguments", {})
                action = tc["name"]
                theme_value = args.get("theme", "") if action == "set_talk_theme" else ""
                step_data = {
                    "type": "talk_theme",
                    "action": action,
                    "theme": theme_value,
                    "result": result_text,
                }
                theme_steps.append(step_data)
                all_steps.append(step_data)
                logger.info(f"[TalkTheme] {action} processed for {character_id}")

            # --- Process note tool calls (immediate, no security check) ---
            for tc in note_tcs:
                result_text = dispatch_note_tool(character_id, tc, language)
                tool_results.append({
                    "tc": tc, "status": "executed", "result_text": result_text
                })
                # Build content description for memory record
                args = tc.get("arguments", {})
                if tc["name"] == "add_note":
                    content_desc = args.get("content", "")
                elif tc["name"] == "remove_note":
                    content_desc = f"index={args.get('index', '')}"
                else:  # replace_note
                    content_desc = f"index={args.get('index', '')} -> {args.get('content', '')}"
                note_steps.append({
                    "type": "note",
                    "action": tc["name"],
                    "content": content_desc,
                    "result": result_text,
                })
                logger.info(f"[Note] {tc['name']} processed for {character_id}")

            # --- Process deep search tool calls (immediate, rate-limited per tool) ---
            if deep_search_tcs:
                from backend.tools.deep_search_tools import dispatch_deep_search_tool
                from backend.shared.constants import SEARCH_WEB_PER_TURN_MAX, READ_WEBPAGE_PER_TURN_MAX

                turn_search_count = 0
                turn_read_count = 0

                for tc in deep_search_tcs:
                    tool_name = tc["name"]
                    args = tc.get("arguments", {})
                    status = "error"
                    result_text = ""

                    if tool_name == "search_web":
                        if turn_search_count >= SEARCH_WEB_PER_TURN_MAX:
                            result_text = prompt_text("res.deep_search.turn_limit_search", language)
                            status = "rate_limited"
                        elif not self._check_search_web_rate(character_id):
                            result_text = prompt_text("res.deep_search.rate_limit_search", language)
                            status = "rate_limited"
                        else:
                            # Show "Searching..." spinner before executing
                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("deep_search_spinner", data={
                                    "tool": "search_web",
                                    "query": args.get("query", ""),
                                })
                            except Exception:
                                pass
                            dispatch_result = dispatch_deep_search_tool(tool_name, args, language)
                            result_text = dispatch_result["result_text"]
                            status = dispatch_result["status"]
                            if status == "success":
                                turn_search_count += 1

                    elif tool_name == "read_webpage":
                        if turn_read_count >= READ_WEBPAGE_PER_TURN_MAX:
                            result_text = prompt_text("res.deep_search.turn_limit_read", language)
                            status = "rate_limited"
                        elif not self._check_read_webpage_rate(character_id):
                            result_text = prompt_text("res.deep_search.rate_limit_read", language)
                            status = "rate_limited"
                        else:
                            # Show "Reading page..." spinner before executing
                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("deep_search_spinner", data={
                                    "tool": "read_webpage",
                                    "url": args.get("url", ""),
                                })
                            except Exception:
                                pass
                            dispatch_result = dispatch_deep_search_tool(tool_name, args, language)
                            result_text = dispatch_result["result_text"]
                            status = dispatch_result["status"]
                            if status == "success":
                                turn_read_count += 1

                    else:
                        result_text = prompt_text("res.deep_search.unknown_tool", language, name=tool_name)
                        status = "error"

                    tool_results.append({
                        "tc": tc, "status": status, "result_text": result_text,
                    })

                    step_data = {"type": "deep_search", "tool": tool_name, "status": status}
                    if tool_name == "search_web":
                        step_data["query"] = args.get("query", "")
                        # Extract hit URLs from result_text for display
                        if status == "success" and result_text:
                            import re as _re_mod
                            urls = _re_mod.findall(r'\]\((https?://[^\)]+)\)', result_text)
                            step_data["hit_urls"] = urls[:5]
                    else:
                        step_data["url"] = args.get("url", "")
                        # Extract page title from result_text for display.
                        # 接頭辞はカタログ(fmt.deep.page_title)から言語別に導出 —
                        # 日本語直値だと英語キャラでタイトルが永久に取れない
                        if status == "success" and result_text:
                            title_prefix = prompt_text(
                                "fmt.deep.page_title", language, title="").strip()
                            for line in result_text.split("\n"):
                                if line.startswith(title_prefix):
                                    step_data["page_title"] = line[len(title_prefix):].strip()
                                    break
                    all_steps.append(step_data)
                    logger.info(f"[DeepSearch] {tool_name} processed for {character_id} (status={status})")

            # --- Process map search tool calls (search_places / get_place_details / get_directions) ---
            if map_search_tcs:
                from backend.tools.map_search_tools import dispatch_map_search_tool
                from backend.tools.location_manager import get_current_location
                from backend.shared.api_settings import get_google_maps_api_key
                from backend.shared.constants import (
                    SEARCH_PLACES_PER_TURN_MAX,
                    GET_PLACE_DETAILS_PER_TURN_MAX,
                    GET_DIRECTIONS_PER_TURN_MAX,
                )

                loc = get_current_location()
                ms_api_key = get_google_maps_api_key()
                turn_search_places_count = 0
                turn_place_details_count = 0
                turn_directions_count = 0

                for tc in map_search_tcs:
                    tool_name = tc["name"]
                    args = tc.get("arguments", {})
                    status = "error"
                    result_text = ""

                    if not loc or not ms_api_key:
                        result_text = prompt_text("res.map_search.unavailable", language)
                        status = "error"
                    elif tool_name == "search_places":
                        if turn_search_places_count >= SEARCH_PLACES_PER_TURN_MAX:
                            result_text = prompt_text("res.map_search.turn_limit_search", language)
                            status = "rate_limited"
                        elif not self._check_search_places_rate(character_id):
                            result_text = prompt_text("res.map_search.rate_limit_search", language)
                            status = "rate_limited"
                        else:
                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("map_search_spinner", data={
                                    "tool": "search_places",
                                    "query": args.get("query", ""),
                                })
                            except Exception:
                                pass
                            dispatch_result = dispatch_map_search_tool(tool_name, args, loc["lat"], loc["lng"], ms_api_key, language)
                            result_text = dispatch_result["result_text"]
                            status = dispatch_result["status"]
                            if status == "success":
                                turn_search_places_count += 1

                    elif tool_name == "get_place_details":
                        if turn_place_details_count >= GET_PLACE_DETAILS_PER_TURN_MAX:
                            result_text = prompt_text("res.map_search.turn_limit_details", language)
                            status = "rate_limited"
                        elif not self._check_place_details_rate(character_id):
                            result_text = prompt_text("res.map_search.rate_limit_details", language)
                            status = "rate_limited"
                        else:
                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("map_search_spinner", data={
                                    "tool": "get_place_details",
                                    "place_id": args.get("place_id", ""),
                                })
                            except Exception:
                                pass
                            dispatch_result = dispatch_map_search_tool(tool_name, args, loc["lat"], loc["lng"], ms_api_key, language)
                            result_text = dispatch_result["result_text"]
                            status = dispatch_result["status"]
                            if status == "success":
                                turn_place_details_count += 1

                    elif tool_name == "get_directions":
                        if turn_directions_count >= GET_DIRECTIONS_PER_TURN_MAX:
                            result_text = prompt_text("res.map_search.turn_limit_directions", language)
                            status = "rate_limited"
                        elif not self._check_directions_rate(character_id):
                            result_text = prompt_text("res.map_search.rate_limit_directions", language)
                            status = "rate_limited"
                        else:
                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("map_search_spinner", data={
                                    "tool": "get_directions",
                                    "mode": args.get("mode", "walking"),
                                })
                            except Exception:
                                pass
                            dispatch_result = dispatch_map_search_tool(tool_name, args, loc["lat"], loc["lng"], ms_api_key, language)
                            result_text = dispatch_result["result_text"]
                            status = dispatch_result["status"]
                            if status == "success":
                                turn_directions_count += 1

                    else:
                        result_text = prompt_text("res.map_search.unknown_tool", language, name=tool_name)
                        status = "error"

                    tool_results.append({
                        "tc": tc, "status": status, "result_text": result_text,
                    })

                    step_data = {
                        "type": "map_search", "tool": tool_name, "status": status,
                        "result_text": result_text,
                    }
                    if tool_name == "search_places":
                        step_data["query"] = args.get("query", "")
                    elif tool_name == "get_place_details":
                        step_data["place_id"] = args.get("place_id", "")
                    elif tool_name == "get_directions":
                        step_data["place_id"] = args.get("place_id", "")
                        step_data["mode"] = args.get("mode", "walking")
                    all_steps.append(step_data)
                    logger.info(f"[MapSearch] {tool_name} processed for {character_id} (status={status})")

                # Update rate trackers
                if turn_search_places_count > 0:
                    self._update_search_places_tracker(character_id, turn_search_places_count)
                if turn_place_details_count > 0:
                    self._update_place_details_tracker(character_id, turn_place_details_count)
                if turn_directions_count > 0:
                    self._update_directions_tracker(character_id, turn_directions_count)

            # --- Process image generation tool calls ---
            generated_images = []
            if image_tcs:
                from backend.tools.image_generator import dispatch_image_tool
                from backend.shared.api_settings import get_image_generation_model, load_api_settings
                from backend.shared.constants import IMAGE_GEN_PER_TURN_MAX

                ig_provider, ig_model = get_image_generation_model()
                ig_settings = load_api_settings()
                ig_api_key = ig_settings.get("google", {}).get("api_key", "")

                image_gen_count_this_turn = 0
                for tc in image_tcs:
                    if image_gen_count_this_turn >= IMAGE_GEN_PER_TURN_MAX:
                        tool_results.append({
                            "tc": tc, "status": "rate_limited",
                            "result_text": prompt_text("res.image.per_turn_limit", language)
                        })
                        logger.info(f"[ImageGen] Per-turn limit reached, skipping: {tc.get('arguments', {}).get('prompt', '')[:50]}")
                        continue

                    # Show "Generating image..." indicator in chat
                    try:
                        from backend.server.websocket_server import send_ui_update
                        send_ui_update("image_generating_spinner", data={})
                    except Exception:
                        pass

                    dispatch_result = dispatch_image_tool(
                        character_id, tc, ig_model, ig_api_key, language
                    )
                    result_text = dispatch_result.get("result", "")
                    tool_results.append({
                        "tc": tc, "status": dispatch_result["status"],
                        "result_text": result_text
                    })

                    step_data = {
                        "type": "image_gen",
                        "prompt": dispatch_result.get("prompt", ""),
                        "status": dispatch_result["status"],
                        "result": result_text,
                        "image_path": dispatch_result.get("image_path", ""),
                        "thumbnail_path": dispatch_result.get("thumbnail_path", ""),
                    }
                    all_steps.append(step_data)

                    if dispatch_result["status"] == "success":
                        image_gen_count_this_turn += 1
                        thumb = dispatch_result.get("thumbnail_path", "")
                        img = dispatch_result.get("image_path", "")
                        inject_path = thumb or img
                        if inject_path:
                            generated_images.append(inject_path)

                    logger.info(f"[ImageGen] generate_image {dispatch_result['status']} for {character_id}")

            # --- Process camera capture tool calls ---
            captured_images = []
            if camera_tcs:
                from backend.tools.camera_capture import dispatch_camera_tool
                from backend.shared.constants import CAMERA_PER_TURN_MAX

                camera_count_this_turn = 0
                for tc in camera_tcs:
                    if camera_count_this_turn >= CAMERA_PER_TURN_MAX:
                        tool_results.append({
                            "tc": tc, "status": "rate_limited",
                            "result_text": prompt_text("res.camera.per_turn_limit", language)
                        })
                        logger.info("[CameraCapture] Per-turn limit reached, skipping")
                        continue

                    dispatch_result = dispatch_camera_tool(character_id, tc, language=language)
                    result_text = dispatch_result.get("result", "")
                    tool_results.append({
                        "tc": tc, "status": dispatch_result["status"],
                        "result_text": result_text
                    })

                    step_data = {
                        "type": "camera_capture",
                        "reason": dispatch_result.get("reason", ""),
                        "status": dispatch_result["status"],
                        "result": result_text,
                        "image_path": dispatch_result.get("image_path", ""),
                    }
                    all_steps.append(step_data)

                    if dispatch_result["status"] == "success" and dispatch_result.get("image_path"):
                        camera_count_this_turn += 1
                        captured_images.append(dispatch_result["image_path"])

                    logger.info(f"[CameraCapture] capture_camera {dispatch_result['status']} for {character_id}")

            # --- Process ELYTH tool calls ---
            if elyth_tcs:
                from backend.elyth.elyth_tools import dispatch_elyth_tool
                elyth_api_key = config.get("elyth_api_key", "")
                for tc in elyth_tcs:
                    res = dispatch_elyth_tool(tc["name"], tc["arguments"], character_id, elyth_api_key, language)
                    tool_results.append({
                        "tc": tc, "status": "executed",
                        "result_text": res["result_text"]
                    })
                    elyth_status = "success" if res.get("status") == "success" else "error"
                    all_steps.append({
                        "type": "elyth",
                        "tool": tc["name"],
                        "status": elyth_status,
                        "content": tc["arguments"].get("content", ""),
                        "result": res["result_text"][:500],
                    })

            # --- Process command tool calls (existing security flow) ---
            for i, tc in enumerate(command_tcs):
                if self._check_interrupted(character_id):
                    # Mark this and all remaining command tool_calls as interrupted
                    for remaining_tc in command_tcs[i:]:
                        tool_results.append({
                            "tc": remaining_tc, "status": "interrupted",
                            "result_text": format_tool_result_text("interrupted", language)
                        })
                    interrupted = True
                    break

                command = tc["arguments"].get("command", "")
                reason = tc["arguments"].get("reason", "")
                verdict = check_command(command, language)

                if verdict.verdict == SecurityVerdict.ALLOW:
                    # White list — execute immediately
                    exec_result = execute_command(command, language)
                    if exec_result.success:
                        result_text = format_tool_result_text("executed", language, output=exec_result.output)
                        status = "executed"
                    else:
                        result_text = format_tool_result_text("error", language, error_message=exec_result.error_message)
                        status = "error"
                    tool_results.append({"tc": tc, "status": status, "result_text": result_text})
                    all_steps.append({
                        "type": "command", "command": command, "reason": reason,
                        "status": status, "result": exec_result.output or exec_result.error_message
                    })
                    cmd_log.add(command, reason, status, exec_result.output or exec_result.error_message, character_id)
                    tool_call_count += 1

                elif verdict.verdict == SecurityVerdict.REQUIRE_APPROVAL:
                    # Display ALL pending steps before showing approval UI
                    self._display_pending_steps(all_steps, config)

                    # Grey list — wait for approval, execute or deny
                    approval_result = self._handle_grey_approval(tc, character_id, cmd_log, language)
                    tool_results.append(approval_result)
                    tool_call_count += 1

                    if approval_result["status"] == "interrupted":
                        # Mark remaining command tool_calls as interrupted
                        for remaining_tc in command_tcs[i + 1:]:
                            tool_results.append({
                                "tc": remaining_tc, "status": "interrupted",
                                "result_text": format_tool_result_text("interrupted", language)
                            })
                        interrupted = True
                        break

                    # Add command step (will be displayed by _display_pending_steps)
                    all_steps.append({
                        "type": "command", "command": command, "reason": reason,
                        "status": approval_result["status"],
                        "result": approval_result.get("raw_output", ""),
                    })

                else:
                    # Blocked
                    result_text = format_tool_result_text("blocked", language, block_reason=verdict.reason)
                    tool_results.append({"tc": tc, "status": "blocked", "result_text": result_text})
                    all_steps.append({
                        "type": "command", "command": command, "reason": reason,
                        "status": "blocked", "result": verdict.reason
                    })
                    cmd_log.add(command, reason, "blocked", verdict.reason, character_id)

            # Display all pending steps from this iteration
            self._display_pending_steps(all_steps, config)

            # Add assistant message with tool_calls + all tool_results to loop messages
            assistant_msg = build_assistant_msg_with_tool_calls(result_msg, model_provider)
            loop_messages.append(assistant_msg)

            for tr in tool_results:
                loop_messages.append(format_tool_result_message(
                    model_provider, tr["tc"]["id"], tr["tc"]["name"], tr["result_text"]
                ))

            # Inject captured camera images into the conversation so the LLM can "see" them
            if captured_images:
                loop_messages.append({
                    "role": "user",
                    "content": prompt_text("sysnote.camera_image", language),
                    "images": list(captured_images)
                })

            # Inject generated images into the conversation so the LLM can "see" them
            if generated_images:
                loop_messages.append({
                    "role": "user",
                    "content": prompt_text("sysnote.generated_image", language),
                    "images": list(generated_images)
                })

            if interrupted:
                break

            # Show spinner for next LLM call
            self._send_loop_spinner()

        return {
            "all_steps": all_steps,
            "note_steps": note_steps,
            "theme_steps": theme_steps,
            "final_text": final_text,
            "tool_call_count": tool_call_count
        }

    def _display_pending_steps(self, all_steps: list, config: dict) -> None:
        """
        Display all pending (not yet shown) steps via WebSocket.
        Handles ai_text (spinner → TTS → text) and command/image blocks.

        The first ai_text step reuses the page's initial spinner.
        Subsequent ai_text steps send an explicit spinner first so
        that `command_pre_response` has a target to replace.
        """
        from backend.server.websocket_server import send_ui_update
        char_name = config.get('name', 'AI')

        # The first pending ai_text can reuse the existing page spinner.
        first_ai_text_seen = False

        for step in all_steps:
            if step.get("pre_displayed"):
                continue

            if step.get("type") == "ai_text":
                text = step["content"]

                if first_ai_text_seen:
                    # Not the first ai_text → insert a fresh spinner
                    self._send_loop_spinner()
                first_ai_text_seen = True

                # 1) TTS (spinner stays visible during playback)
                self._play_tts_blocking(text)
                # 2) Show text (replaces spinner)
                try:
                    send_ui_update("command_pre_response", data={
                        "text": text,
                        "character_name": char_name,
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send text: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "command":
                try:
                    send_ui_update("command_loop_block", data={
                        "command": step.get("command", ""),
                        "reason": step.get("reason", ""),
                        "status": step.get("status", ""),
                        "result": (step.get("result", "") or "")[:1000],
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send command block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "talk_theme":
                try:
                    send_ui_update("talk_theme_block", data={
                        "action": step.get("action", ""),
                        "theme": step.get("theme", ""),
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send theme block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "image_gen":
                try:
                    thumb_path = step.get("thumbnail_path", "")
                    thumb_b64 = ""
                    if thumb_path and step.get("status") == "success":
                        try:
                            import base64
                            with open(thumb_path, "rb") as f:
                                thumb_b64 = base64.b64encode(f.read()).decode("ascii")
                        except Exception as e:
                            logger.warning(f"[Display] Failed to read thumbnail: {e}")

                    full_b64 = ""
                    full_path = step.get("image_path", "")
                    if full_path and step.get("status") == "success":
                        try:
                            import base64
                            with open(full_path, "rb") as f:
                                full_b64 = base64.b64encode(f.read()).decode("ascii")
                        except Exception as e:
                            logger.warning(f"[Display] Failed to read full image: {e}")

                    send_ui_update("image_generation_block", data={
                        "status": step.get("status", "error"),
                        "prompt": step.get("prompt", ""),
                        "thumbnail_base64": thumb_b64,
                        "full_base64": full_b64,
                        "image_path": full_path,
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send image gen block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "camera_capture":
                try:
                    img_b64 = ""
                    img_path = step.get("image_path", "")
                    if img_path and step.get("status") == "success":
                        try:
                            import base64
                            with open(img_path, "rb") as f:
                                img_b64 = base64.b64encode(f.read()).decode("ascii")
                        except Exception as e:
                            logger.warning(f"[Display] Failed to read captured image: {e}")

                    send_ui_update("camera_capture_block", data={
                        "status": step.get("status", "error"),
                        "reason": step.get("reason", ""),
                        "image_base64": img_b64,
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send camera capture block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "deep_search":
                try:
                    tool_name = step.get("tool", "")
                    send_ui_update("deep_search_block", data={
                        "tool": tool_name,
                        "status": step.get("status", "error"),
                        "query": step.get("query", ""),
                        "url": step.get("url", ""),
                        "hit_urls": step.get("hit_urls", []),
                        "page_title": step.get("page_title", ""),
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send deep search block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "map_search":
                try:
                    send_ui_update("map_search_block", data={
                        "tool": step.get("tool", ""),
                        "status": step.get("status", "error"),
                        "query": step.get("query", ""),
                        "place_id": step.get("place_id", ""),
                        "mode": step.get("mode", ""),
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send map search block: {e}")
                step["pre_displayed"] = True

            elif step.get("type") == "elyth":
                try:
                    send_ui_update("elyth_block", data={
                        "tool": step.get("tool", ""),
                        "status": step.get("status", "error"),
                        "content": step.get("content", ""),
                        "result": (step.get("result", "") or "")[:500],
                    })
                except Exception as e:
                    logger.warning(f"[Display] Failed to send ELYTH block: {e}")
                step["pre_displayed"] = True

    def _play_tts_blocking(self, text: str) -> None:
        """Generate TTS, send audio via WS, and block until playback completes.

        Phase 2.5: blocks on browser-side `tts_playback_completed` notification
        instead of a pessimistic timer. Falls back to timeout if the browser is
        unreachable (disconnected, slow, etc.) so the loop never hangs.
        If the WS send itself fails (server down / broadcast timeout), skips
        the wait entirely instead of dead-waiting for an ACK that cannot come.
        """
        if self.state.speechless_enabled:
            return
        # TTS区間(合成+再生)はLLMタスクのタイムアウト時計を止める(稜裁定
        # 2026-08-12: 読み上げ開始=生成成功後なのでタイムアウトさせない)。
        # 停止は有界: 下の再生待ちは duration+5s の内部タイムアウトを持つ。
        from backend.shared.playback_state import clear_tts_active, mark_tts_active
        mark_tts_active()
        try:
            import uuid
            import audio_output.audio_output as audio_output_mod
            from backend.server.websocket_server import send_ui_update, get_websocket_manager
            from backend.shared.settings_store import get_setting
            audio_data, sr = audio_output_mod.text_to_speech(text)
            if audio_data is not None and len(audio_data) > 0:
                audio_base64 = audio_output_mod.audio_to_aac_mp4_base64(audio_data, sr)
                duration_ms = int(len(audio_data) / sr * 1000)
                lipsync_frames = audio_output_mod.calculate_lipsync_frames(audio_data, sr)
                playback_id = uuid.uuid4().hex
                mgr = get_websocket_manager()
                mgr.register_playback(playback_id)
                try:
                    sent = send_ui_update("tts_audio", data={
                        "audio_base64": audio_base64,
                        "codec": "aac",
                        "sample_rate": sr,
                        "duration_ms": duration_ms,
                        # Spoken text for the MotionPNGPlayer speech bubble
                        "text": text,
                        # Volume single source = settings file (the UI slider
                        # persists every change there; BackendState never held
                        # a tts_volume, so the old getattr always fell to 1.0).
                        "volume": get_setting('audio', 'tts_volume', 1.0),
                        "lipsync_frames": lipsync_frames,
                        "playback_id": playback_id,
                    })
                    if not sent:
                        # Delivery failed (server down / broadcast timeout) →
                        # no ACK will ever arrive; don't dead-wait the reply thread.
                        mgr.wait_playback(playback_id, 0.001)
                        logger.warning("[Display] TTS audio not delivered; skipping playback wait")
                        return
                    timeout = duration_ms / 1000 + 5.0
                    mgr.wait_playback(playback_id, timeout)
                except Exception:
                    # Cleanup orphaned event on send failure
                    mgr.wait_playback(playback_id, 0.001)
                    raise
        except Exception as e:
            # error: サーバーモード本流のTTS失敗が warning だと完全無言で、
            # ユーザーは「声が出ない」理由を知る術がなかった(稜裁定
            # 2026-08-02)。会話自体は継続する(non-fatal は不変)。
            logger.error(f"[Display] TTS playback failed (non-fatal, reply shown without voice): {e}")
        finally:
            clear_tts_active()

    def _send_loop_spinner(self) -> None:
        """Send a 'Generating Response...' spinner via WebSocket for the next loop iteration."""
        try:
            from backend.server.websocket_server import send_ui_update
            send_ui_update("command_loop_spinner", data={})
        except Exception as e:
            logger.warning(f"[Display] Failed to send spinner: {e}")

    def _handle_grey_approval(self, tc: dict, character_id: str, cmd_log, language: str) -> dict:
        """
        Handle grey-list command approval: wait for user → execute or deny.

        Returns dict with 'tc', 'status', 'result_text' (same shape as whitelist results).
        The main loop handles LLM reaction — no LLM call here.
        """
        from backend.tools.command_executor import execute_command, format_tool_result_text

        command = tc["arguments"].get("command", "")
        reason = tc["arguments"].get("reason", "")

        # Show approval UI and wait
        self._set_pending_approval(character_id, tc, reason)
        approval = self._wait_for_approval(character_id)

        if approval == "accepted":
            exec_result = execute_command(command, language)
            if exec_result.success:
                result_text = format_tool_result_text("executed", language, output=exec_result.output)
                status = "executed"
            else:
                result_text = format_tool_result_text("error", language, error_message=exec_result.error_message)
                status = "error"
            raw_output = exec_result.output or exec_result.error_message
            cmd_log.add(command, reason, status, raw_output, character_id)
            return {"tc": tc, "status": status, "result_text": result_text, "raw_output": raw_output}

        elif approval == "denied":
            result_text = format_tool_result_text("denied", language)
            cmd_log.add(command, reason, "denied", "", character_id)
            return {"tc": tc, "status": "denied", "result_text": result_text, "raw_output": ""}

        else:  # interrupted
            result_text = format_tool_result_text("interrupted", language)
            cmd_log.add(command, reason, "interrupted", "", character_id)
            return {"tc": tc, "status": "interrupted", "result_text": result_text, "raw_output": ""}

    def _generate_reply_task(self, user_text: str, character_id: str,
                             images: list = None, documents: list = None,
                             is_auto_prompt: bool = False) -> Dict[str, Any]:
        """
        Actual LLM generation logic run inside the worker thread.

        Args:
            user_text: The user's input text
            character_id: The character ID
            images: Optional list of image file paths
            documents: Optional list of document dicts [{filename, text, char_count}]
            is_auto_prompt: True when user_text is the AutoPrompt fixed message
                (user保存時にmetadata印を付ける)

        Returns:
            Dict[str, Any]: A dictionary with standardized response format:
                - success (bool): Whether generation was successful
                - response (str, optional): AI reply text if successful
                - error (str, optional): Error message if failed
                - error_type (str, optional): Category of error
                - details (dict, optional): Additional context information
        """
        # 中断フラグはこのターンより前の(承認待ち中に割り込まれた)ターン向け。
        # キューは逐次実行なので、新ターン開始時点で前ターンは消費済み。
        # ここで戻さないと一度立ったフラグが再起動まで残り、以後の全ターンで
        # コマンドツール実行が即 interrupted になる(沈黙故障)。
        self.state._pending_interruption = False

        config_response = self.load_character_config(character_id)
        
        # Handle standardized response format
        if isinstance(config_response, dict) and 'success' in config_response:
            if not config_response.get('success'):
                logger.error(f"Failed to load character config: {config_response.get('error', 'Unknown error')}")
                return {
                    "success": False,
                    "error": f"Character config not found for ID: {character_id}",
                    "error_type": "NOT_FOUND"
                }
            config = config_response.get('result', {})
        else:
            # Direct config (for backward compatibility)
            config = config_response
            
        if not config:
            logger.error(f"Character config not found for ID: {character_id}")
            return {
                "success": False,
                "error": f"Character config not found for ID: {character_id}",
                "error_type": "NOT_FOUND"
            }

        timing_log("config_loaded")

        # Use MemoryManager if available
        memory_manager = self.state.memory_managers.get(character_id)

        pending_ai_message = None  # Will be filled after successful generation

        # Record unrecorded location update to conversation history (before building prompt)
        from backend.tools.location_manager import consume_unrecorded_location
        loc_update = consume_unrecorded_location()
        if loc_update and memory_manager:
            from backend.shared.prompt_i18n import get_prompt_language, prompt_text
            _loc_lang = get_prompt_language(config)
            if loc_update.get("address"):
                loc_content = prompt_text("res.location.updated", _loc_lang, address=loc_update['address'])
            else:
                loc_content = prompt_text(
                    "res.location.update_failed", _loc_lang,
                    error=loc_update.get('error', prompt_text("res.location.unknown_error", _loc_lang)))
            memory_manager.add_message("assistant", loc_content, metadata={"is_tool_message": True, "tool_type": "location_update"})
            logger.info("[Location] Recorded location update to conversation history")

        # 3) Build final prompt: system prompt + summaries + last 20 messages + user text
        screenshot_tmp_paths = []  # Track temp files for cleanup
        try:
            # PC Status機能が有効な場合、PC状況テキストを取得
            if self.state.pc_status_enabled:
                from backend.tools.pc_status_manager import get_pc_status_for_prompt
                from backend.shared.prompt_i18n import get_prompt_language
                logger.info("[PC Status] Fetching current PC status for prompt")
                pc_status, pc_errors = get_pc_status_for_prompt(
                    timeout=2.0, max_tokens=1300, language=get_prompt_language(config))
                config["_dynamic_pc_status"] = pc_status
                if pc_errors:
                    logger.warning(f"[PC Status] Errors during fetch: {pc_errors}")

            # Screen Capture: 有効ならスクリーンショットを撮影
            screenshot_images = []
            if self.state.screen_capture_enabled:
                from backend.tools.screen_capture import capture_screenshots
                screenshot_images = capture_screenshots()
                if screenshot_images:
                    logger.info(f"[Screen Capture] Captured {len(screenshot_images)} screenshots")

            # Live Camera: 生成確定時のフレーム待ちバリア。撮影要求は
            # ユーザー起点の経路(録音停止/テキスト送信)が発火済み。未発火の
            # 経路(自動プロンプト/ELYTH/YouTube等)ではゼロコストで素通り。
            # フレームは本ターンのプロンプト専用=履歴非保存(Screen Captureと
            # 同じ扱いで screenshot_tmp_paths の後始末に相乗り)。
            ambient_frame_path = None
            try:
                from backend.shared.ambient_camera_state import get_ambient_camera_state
                from backend.shared.constants import AMBIENT_CAPTURE_TIMEOUT
                ambient_frame_path = get_ambient_camera_state().consume_frame_if_pending(
                    AMBIENT_CAPTURE_TIMEOUT)
            except Exception as ambient_error:
                logger.error(f"[LiveCamera] Frame consume failed: {ambient_error}")
            if ambient_frame_path:
                # プロンプト注記(周囲の自動撮影である旨)の条件フラグ
                config["_ambient_frame_present"] = True

            # Tool-inclusion policy: evaluate each gate once per turn and
            # share the result between prompt assembly (section text) and the
            # tool-schema decision further below. The gates were previously
            # evaluated twice per turn (inside prompt_builder and again for
            # the tool list), doubling their config/API-settings file reads.
            include_flags = {
                "command": self._should_include_command_tools(config, character_id),
                "note": self._should_include_note_tools(config),
                "talk_theme": self._should_include_talk_theme_tools(config),
                "image": self._should_include_image_tools(config, character_id),
                "camera": self._should_include_camera_tools(config, character_id),
                "deep_search": self._should_include_deep_search_tools(config, character_id),
                "map": self._should_include_map_tools(config, character_id),
                "elyth": self._should_include_elyth_tools(config, character_id),
            }

            # ツール定義をプロンプト組み立て「前」に構築する(C5): Ollamaでは
            # tools ペイロードがチャットテンプレート経由でプロンプトへ練り
            # 込まれるため、その推定トークンを token manager の予約として
            # 渡す必要がある。API プロバイダはこの並べ替えの影響なし
            # (同じ定義を同じ入力から作るだけ)。
            model_provider = config.get("model_provider", "ollama")
            tools = self._build_tool_definitions(
                include_flags, model_provider, character_id, config)
            tools_reserve_tokens = 0
            if tools and model_provider == "ollama":
                import json as _json
                from backend.shared.token_manager import estimate_token_count
                tools_reserve_tokens = estimate_token_count(
                    _json.dumps(tools, ensure_ascii=False))

            with timing_block("build_prompt"):
                final_messages = self._build_prompt_messages(
                    character_id, user_text=user_text, config=config,
                    include_flags=include_flags,
                    tools_reserve_tokens=tools_reserve_tokens
                )

            # スクリーンキャプチャ画像をユーザーメッセージに追加（ユーザー添付画像と結合）
            all_prompt_images = list(images or [])  # ユーザー添付画像（ファイルパス）
            if screenshot_images:
                import tempfile
                for i, img_bytes in enumerate(screenshot_images):
                    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False, prefix=f'sc_mon{i}_')
                    tmp.write(img_bytes)
                    tmp.close()
                    all_prompt_images.append(tmp.name)
                    screenshot_tmp_paths.append(tmp.name)

            # Live Cameraフレームも本ターン専用画像として結合(末尾=最新視界)
            if ambient_frame_path:
                all_prompt_images.append(ambient_frame_path)
                screenshot_tmp_paths.append(ambient_frame_path)

            # Add the pending user message to the prompt (not yet in memory)
            # Embed document text directly into content (LLM only sees content + images)
            # 全プロバイダ対象(C7: 旧Ollama除外を撤去=テキスト化されるので
            # capability非依存に動く・2026-08-11 稜裁定)
            prompt_content = user_text
            if documents:
                doc_parts = []
                for doc in documents:
                    fname = doc.get("filename", "unknown")
                    text = doc.get("text", "")
                    doc_parts.append(
                        f'<attached_document filename="{fname}">\n{text}\n</attached_document>'
                    )
                prompt_content = user_text + "\n\n" + "\n\n".join(doc_parts)
            pending_user_message = {"role": "user", "content": prompt_content}
            if all_prompt_images:
                pending_user_message["images"] = all_prompt_images
            final_messages.append(pending_user_message)
        except Exception as prompt_error:
            logger.error(f"Error building prompt messages for character {character_id}: {prompt_error}")
            # Clean up screenshot temp files on error
            for tmp_path in screenshot_tmp_paths:
                try:
                    import os
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return {
                "success": False,
                "error": f"Failed to build prompt messages: {str(prompt_error)}",
                "error_type": "INTERNAL",
                "details": {"stage": "prompt_building"}
            }

        # 4) Generate AI reply - ensure LLM is in cache (recreate if needed)
        chat_llm = self._ensure_llm_in_cache(character_id, config)

        if not chat_llm:
            logger.error(f"Failed to get or create LLM for character {character_id}")
            return {
                "success": False,
                "error": "Failed to create LLM instance. Please check Ollama connection.",
                # SERVICE: 実態はLLMサービス未接続。NOT_FOUNDだとUI側の
                # マッピングに乗らず英語原文表示になっていた(稜裁定 2026-08-03:
                # Ollama停止中の遅延生成失敗は err.service_unavailable で表示)
                "error_type": "SERVICE",
                "details": {"stage": "llm_access"}
            }

        try:
            # Prepare messages for direct API (dict format)
            converted_msgs = []
            for m in final_messages:
                r = m["role"]
                c = m["content"]
                if r not in ("system", "user", "assistant"):
                    r = "assistant"
                msg_dict = {"role": r, "content": c}
                if m.get("images"):
                    msg_dict["images"] = m["images"]
                converted_msgs.append(msg_dict)

            # 現在時刻は送信専用コピーの最新userメッセージ先頭にのみ付与
            # (2026-08-01 稜裁定)。converted_msgs は final_messages と別dict
            # なので、履歴保存(永続=add_message / フォールバック短期バッファ=
            # pending_user_message)にも記憶検索クエリ(user_text)にも残らない。
            # system先頭に置く旧方式は分をまたぐたびOllamaのプレフィックス
            # KVキャッシュを全滅させ毎ターンのフルprompt evalを誘発していた。
            # ツールループ(:loop_messages)も素invokeもこの器を食うため、
            # 全送信経路をこの1箇所で覆う。
            if converted_msgs and converted_msgs[-1]["role"] == "user":
                from backend.conversation.prompt_builder import build_current_time_context
                converted_msgs[-1]["content"] = (
                    build_current_time_context() + converted_msgs[-1]["content"]
                )

            # Measure LLM generation time
            start_time = time.time()

            all_steps = []  # For command/theme execution UI display

            # tools はプロンプト組み立て前に構築済み(上のC5ブロック=予約計算と
            # 共用)。ここではそのまま使う。

            # Generate response with timeout protection
            try:
                note_steps = []  # For note tool results (memory only, not UI)

                if tools:
                    # Tool call loop (API with command/note/theme tools)
                    with timing_block("llm_tool_loop"):
                        loop_result = self._run_tool_call_loop(
                            chat_llm, converted_msgs, tools,
                            model_provider, character_id, config
                        )
                    ai_reply = loop_result["final_text"]
                    all_steps = loop_result["all_steps"]
                    note_steps = loop_result.get("note_steps", [])
                    # theme_steps は all_steps に時系列で含まれており保存もそこで行う
                    # (loop_result["theme_steps"] は現在未消費・_run_tool_call_loop の報告契約として温存)

                    # Update command rate tracker
                    command_count = sum(
                        1 for step in all_steps if step["type"] == "command"
                    )
                    self._update_command_tracker(character_id, command_count)

                    # Update image generation rate tracker
                    image_gen_count = sum(
                        1 for step in all_steps
                        if step.get("type") == "image_gen"
                    )
                    self._update_image_gen_tracker(character_id, image_gen_count)

                    # Update camera capture rate tracker
                    camera_count = sum(
                        1 for step in all_steps
                        if step.get("type") == "camera_capture"
                    )
                    self._update_camera_tracker(character_id, camera_count)

                    # Update deep search rate trackers
                    search_web_count = sum(
                        1 for step in all_steps
                        if step.get("type") == "deep_search" and step.get("tool") == "search_web"
                    )
                    self._update_search_web_tracker(character_id, search_web_count)

                    read_webpage_count = sum(
                        1 for step in all_steps
                        if step.get("type") == "deep_search" and step.get("tool") == "read_webpage"
                    )
                    self._update_read_webpage_tracker(character_id, read_webpage_count)

                    # Extract the final AI text from steps if ai_reply is empty
                    if not ai_reply:
                        for step in reversed(all_steps):
                            if step["type"] == "ai_text" and step["content"]:
                                ai_reply = step["content"]
                                break
                else:
                    # Standard invoke (Ollama or API without tools)
                    with timing_block("llm_invoke"):
                        result_msg = chat_llm.invoke(converted_msgs)
                        ai_reply = result_msg.content

                    if tools is None and model_provider != "ollama":
                        # API without tools — still track command rate
                        self._update_command_tracker(character_id, 0)

                # Check for empty response
                if not ai_reply or not ai_reply.strip():
                    logger.warning("LLM returned empty response, using fallback")
                    from backend.shared.prompt_i18n import get_prompt_language, prompt_text
                    ai_reply = prompt_text("res.llm.empty_reply", get_prompt_language(config))
            except Exception as llm_error:
                error_str = str(llm_error).lower()
                # フォールバック応答は会話履歴に永続保存される=カタログ必須。
                # 日本語直値だと英語キャラの履歴を汚染し、以後のターンで
                # モデルが日本語に引きずられる(サブOS実機 2026-08-01)
                # ERROR ログはフォールバックで会話を続行する分岐でのみ出す。
                # raise 分岐は外側 except の logger.exception に一本化 —
                # 同一例外の二重トースト防止(稜裁定 2026-08-02)
                from backend.shared.prompt_i18n import get_prompt_language, prompt_text
                if "timeout" in error_str:
                    logger.error(f"LLM generation failed: {llm_error}")
                    ai_reply = prompt_text("res.llm.timeout", get_prompt_language(config))
                elif "memory" in error_str or "resource" in error_str:
                    logger.error(f"LLM generation failed: {llm_error}")
                    ai_reply = prompt_text("res.llm.resource", get_prompt_language(config))
                else:
                    raise

            # Single-speaker design (ST6-§7⑤): replies that skipped the tool
            # loop (Ollama / API without tools) are spoken and shown by the
            # backend through the same steps pipeline as tool-loop replies.
            # The UI renders pre_displayed steps without speaking, so the
            # backend is the only TTS trigger for conversation replies.
            if not tools:
                step = {"type": "ai_text", "content": ai_reply}
                all_steps.append(step)
                self._display_pending_steps(all_steps, config)

            generation_time = time.time() - start_time
            pending_ai_message = {"role": "assistant", "content": ai_reply}

            # Log LLM generation information
            logger.info(f"Generated reply for character {character_id} in {generation_time:.2f}s")
        except Exception as e:
            error_str = str(e).lower()
            logger.exception(f"LLM generation error: {e}")

            # Categorize common error types
            if "memory" in error_str or "cuda" in error_str or "gpu" in error_str:
                error_type = "RESOURCE_LIMIT"
            elif "timeout" in error_str or "timed out" in error_str:
                error_type = "TIMEOUT"
            elif "ollama" in error_str or "connection" in error_str or "api" in error_str:
                error_type = "SERVICE"
            else:
                error_type = "INTERNAL"

            # LLM generation failed - don't save any messages to memory
            logger.warning("LLM generation failed, conversation state preserved (no messages added)")
            # Clean up screenshot temp files on error
            for tmp_path in screenshot_tmp_paths:
                try:
                    import os
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return {
                "success": False,
                "error": f"Error during LLM generation: {str(e)}",
                "error_type": error_type,
                "details": {"stage": "llm_generation"}
            }

        # 5) Now that generation succeeded, commit both messages to memory atomically
        try:
            if memory_manager:
                # Capture variables for async closure
                _memory_manager = memory_manager
                _user_text = user_text
                _ai_reply = ai_reply
                _character_id = character_id
                _images = images  # Original user-attached images only
                _documents = documents  # Original user-attached documents
                _is_auto_prompt = is_auto_prompt
                _screenshot_tmp_paths = list(screenshot_tmp_paths)  # Copy for cleanup

                _all_steps = list(all_steps)  # Copy for async closure
                _note_steps = list(note_steps)  # Copy for async closure

                def save_messages_async():
                    """Save messages to memory asynchronously."""
                    save_start = time.time()
                    try:
                        # Copy user-attached images to permanent storage (exclude screenshots)
                        permanent_image_paths = []
                        if _images:
                            permanent_image_paths = self._copy_images_to_permanent_storage(
                                _character_id, _images
                            )

                        # Copy user-attached documents to permanent storage
                        permanent_doc_records = None
                        if _documents:
                            permanent_doc_paths = self._copy_documents_to_permanent_storage(
                                _character_id, _documents
                            )
                            # Build document records for memory (text stored in record)
                            permanent_doc_records = []
                            for i, doc in enumerate(_documents):
                                doc_record = {
                                    "filename": doc.get("filename", "unknown"),
                                    "text": doc.get("text", ""),
                                    "char_count": doc.get("char_count", 0),
                                }
                                if i < len(permanent_doc_paths):
                                    doc_record["file_path"] = permanent_doc_paths[i]
                                permanent_doc_records.append(doc_record)

                        # Add user message
                        # AutoPrompt固定文にはUI非表示用の印を付ける(会話ページの
                        # 履歴ロードがスキップ・Historyページは全量表示のまま・
                        # LLM文脈にも残る=稜裁定 2026-08-21)。既定経路(kwargs空)は
                        # 従来とバイト同一の呼出し。
                        user_msg_kwargs = {}
                        if _is_auto_prompt:
                            user_msg_kwargs["metadata"] = {"is_auto_prompt": True}
                        last_add_result = _memory_manager.add_message(
                            "user", _user_text,
                            images=permanent_image_paths if permanent_image_paths else None,
                            documents=permanent_doc_records if permanent_doc_records else None,
                            **user_msg_kwargs,
                        )

                        # Collect generated image paths for attaching to final ai_text
                        generated_image_paths = [
                            step["image_path"] for step in _all_steps
                            if step.get("type") == "image_gen"
                            and step.get("status") == "success"
                            and step.get("image_path")
                        ]

                        # Save all steps in chronological order (ai_text + command + image_gen interleaved)
                        if _all_steps:
                            for step in _all_steps:
                                if step.get("type") == "ai_text" and step.get("content"):
                                    # Attach generated image paths to the final ai_text message
                                    extra_kwargs = {}
                                    if generated_image_paths:
                                        extra_kwargs["images"] = list(generated_image_paths)
                                        generated_image_paths = []  # Only attach once
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", step["content"], **extra_kwargs
                                    )
                                elif step.get("type") == "command":
                                    tool_record = (
                                        f"[Command: {step['command']}] "
                                        f"Reason: {step.get('reason', '')} | "
                                        f"Status: {step['status']} | "
                                        f"Result: {step.get('result', '')[:500]}"
                                    )
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", tool_record,
                                        metadata={"is_tool_message": True}
                                    )
                                elif step.get("type") == "image_gen":
                                    img_record = (
                                        f"[ImageGen: {step.get('status', 'unknown')}] "
                                        f"Prompt: {step.get('prompt', '')[:200]}"
                                    )
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", img_record,
                                        metadata={"is_tool_message": True}
                                    )
                                elif step.get("type") == "camera_capture":
                                    cam_record = (
                                        f"[Camera: {step.get('status', 'unknown')}] "
                                        f"Reason: {step.get('reason', '')[:200]}"
                                    )
                                    extra_kwargs = {}
                                    if step.get("status") == "success" and step.get("image_path"):
                                        extra_kwargs["images"] = [step["image_path"]]
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", cam_record,
                                        metadata={"is_tool_message": True},
                                        **extra_kwargs
                                    )
                                elif step.get("type") == "deep_search":
                                    tool_name = step.get("tool", "unknown")
                                    if tool_name == "search_web":
                                        ds_record = f"[DeepSearch: search_web] Query: {step.get('query', '')}"
                                    elif tool_name == "read_webpage":
                                        ds_record = f"[DeepSearch: read_webpage] URL: {step.get('url', '')}"
                                    else:
                                        ds_record = f"[DeepSearch: {tool_name}]"
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", ds_record,
                                        metadata={"is_tool_message": True}
                                    )
                                elif step.get("type") == "map_search":
                                    ms_tool = step.get("tool", "unknown")
                                    ms_result_text = step.get("result_text", "")
                                    if ms_tool == "search_places":
                                        ms_record = f"[MapSearch: search_places] Query: {step.get('query', '')}\n{ms_result_text}"
                                        last_add_result = _memory_manager.add_message(
                                            "assistant", ms_record,
                                            metadata={"is_tool_message": True, "tool_type": "map_search_result"}
                                        )
                                    elif ms_tool == "get_place_details":
                                        ms_record = f"[MapSearch: get_place_details] Place: {step.get('place_id', '')}\n{ms_result_text}"
                                        last_add_result = _memory_manager.add_message(
                                            "assistant", ms_record,
                                            metadata={"is_tool_message": True}
                                        )
                                    elif ms_tool == "get_directions":
                                        ms_record = f"[MapSearch: get_directions] Place: {step.get('place_id', '')} Mode: {step.get('mode', 'walking')}\n{ms_result_text}"
                                        last_add_result = _memory_manager.add_message(
                                            "assistant", ms_record,
                                            metadata={"is_tool_message": True}
                                        )
                                elif step.get("type") == "elyth":
                                    el_tool = step.get("tool", "unknown")
                                    el_status = step.get("status", "error")
                                    el_content = step.get("content", "")
                                    el_record = f"[ELYTH: {el_tool}] Status: {el_status} | Content: {el_content}"
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", el_record,
                                        metadata={"is_tool_message": True}
                                    )
                                elif step.get("type") == "talk_theme":
                                    # ここで時系列位置のまま保存する(応答1→テーマ→応答2)。
                                    # 旧実装はループ外の末尾一括保存で、リロード時に
                                    # カードが必ず応答2の後ろに復元されていた。
                                    if step.get("action") == "set_talk_theme":
                                        theme_record = f"[TalkTheme: set] Theme: {step.get('theme', '')}"
                                    else:
                                        theme_record = "[TalkTheme: clear]"
                                    last_add_result = _memory_manager.add_message(
                                        "assistant", theme_record,
                                        metadata={"is_tool_message": True}
                                    )
                        else:
                            extra_kwargs = {}
                            if generated_image_paths:
                                extra_kwargs["images"] = list(generated_image_paths)
                            last_add_result = _memory_manager.add_message(
                                "assistant", _ai_reply, **extra_kwargs
                            )

                        # Save note steps (memory only, not displayed in UI)
                        if _note_steps:
                            for step in _note_steps:
                                note_record = (
                                    f"[Note: {step['action']}] "
                                    f"Content: {step.get('content', '')} | "
                                    f"Result: {step.get('result', '')}"
                                )
                                last_add_result = _memory_manager.add_message(
                                    "assistant", note_record,
                                    metadata={"is_tool_message": True}
                                )

                        # Check if we need to trigger memory extraction
                        if last_add_result and last_add_result.get("needs_extraction"):
                            logger.info(f"Scheduling background memory extraction for {_character_id} (unprocessed: {last_add_result.get('unprocessed_count', '?')})")

                            extraction_thread = threading.Thread(
                                target=lambda: self._enqueue_llm_task(
                                    self._extract_memories_task, _character_id,
                                    timeout=MEMORY_TASK_QUEUE_TIMEOUT),
                                daemon=False
                            )
                            extraction_thread.start()

                            self.state.add_background_task({
                                'thread': extraction_thread,
                                'character_id': _character_id,
                                'task_type': 'extraction',
                                'start_time': time.time()
                            })

                            try:
                                from backend.server.websocket_server import send_ui_update
                                send_ui_update("extraction_started", reason="task_added")
                                logger.debug(f"Extraction start notification sent for {_character_id}")
                            except Exception as e:
                                logger.warning(f"Failed to send extraction start notification: {e}")

                            # Relationship update: trigger alongside extraction
                            # (全プロバイダ。C8: 旧Ollama除外を撤去=素の生成
                            # なのでcapability非依存・2026-08-11 稜裁定。パース
                            # 失敗時は前テキスト温存の安全弁が既設)
                            if not hasattr(self.state, '_relationship_update_pending'):
                                self.state._relationship_update_pending = set()
                            if _character_id not in self.state._relationship_update_pending:
                                self.state._relationship_update_pending.add(_character_id)
                                logger.info(f"Scheduling relationship update for {_character_id} (alongside extraction)")
                                relationship_thread = threading.Thread(
                                    target=lambda: self._enqueue_llm_task(
                                        self._update_relationship_task, _character_id,
                                        timeout=MEMORY_TASK_QUEUE_TIMEOUT
                                    ),
                                    daemon=False
                                )
                                relationship_thread.start()
                                self.state.add_background_task({
                                    'thread': relationship_thread,
                                    'character_id': _character_id,
                                    'task_type': 'relationship_update',
                                    'start_time': time.time()
                                })

                        # Relationship: initial early update (30 messages, initial
                        # template only。C8で全プロバイダ化)
                        elif last_add_result and not last_add_result.get("needs_extraction"):
                            _unprocessed = last_add_result.get("unprocessed_count", 0)
                            from backend.shared.constants import RELATIONSHIP_INITIAL_THRESHOLD
                            if _unprocessed >= RELATIONSHIP_INITIAL_THRESHOLD:
                                from backend.memory.relationship_manager import load_relationship, is_initial_template
                                from backend.shared.prompt_i18n import get_prompt_language
                                _current_rel = load_relationship(_character_id, get_prompt_language(config))
                                if is_initial_template(_current_rel):
                                    if not hasattr(self.state, '_relationship_update_pending'):
                                        self.state._relationship_update_pending = set()
                                    if _character_id not in self.state._relationship_update_pending:
                                        self.state._relationship_update_pending.add(_character_id)
                                        logger.info(f"Scheduling initial relationship update for {_character_id} (unprocessed: {_unprocessed})")
                                        relationship_thread = threading.Thread(
                                            target=lambda: self._enqueue_llm_task(
                                                self._update_relationship_task, _character_id,
                                                timeout=MEMORY_TASK_QUEUE_TIMEOUT
                                            ),
                                            daemon=False
                                        )
                                        relationship_thread.start()
                                        self.state.add_background_task({
                                            'thread': relationship_thread,
                                            'character_id': _character_id,
                                            'task_type': 'relationship_update',
                                            'start_time': time.time()
                                        })

                                        # 開始通知(抽出分岐と同形): 早期 relationship 更新も
                                        # 記憶タスク=ボタンのグレーアウト対象(is_memory_task_running)。
                                        # 完了通知は status_monitor が述語の True→False で送る。
                                        try:
                                            from backend.server.websocket_server import send_ui_update
                                            send_ui_update("extraction_started", reason="relationship_update")
                                            logger.debug(f"Relationship update start notification sent for {_character_id}")
                                        except Exception as e:
                                            logger.warning(f"Failed to send relationship update start notification: {e}")

                        new_msg_count = _memory_manager.get_message_count()
                        logger.info(f"Successfully committed conversation turn (message #{new_msg_count}) in {time.time() - save_start:.2f}s")

                        # Clean up completed background tasks (locked: guards against
                        # losing concurrent appends between filter and reassign)
                        with self.state.background_tasks_lock:
                            active_tasks = [t for t in self.state.background_tasks if t['thread'].is_alive()]
                            if len(active_tasks) > 20:
                                active_tasks = active_tasks[-20:]
                            self.state.background_tasks = active_tasks

                    except Exception as e:
                        logger.error(f"Async memory save failed for {_character_id}: {e}")
                    finally:
                        # Clean up screenshot temp files
                        for tmp_path in _screenshot_tmp_paths:
                            try:
                                import os
                                os.unlink(tmp_path)
                            except Exception:
                                pass

                # Start async memory save thread
                save_thread = threading.Thread(target=save_messages_async, daemon=False)
                save_thread.start()

                # Track the memory save thread
                self._pending_memory_save_threads[_character_id] = save_thread
                self.state.add_background_task({
                    'thread': save_thread,
                    'character_id': _character_id,
                    'task_type': 'memory_save',
                    'start_time': time.time()
                })

            else:
                # Fallback to direct memory management (no extraction in fallback mode)
                with self.state.memory_lock:
                    buffer = self.state.short_term_buffer.setdefault(character_id, [])
                    buffer.append(pending_user_message)
                    buffer.append(pending_ai_message)
                    self.state.message_count_cache[character_id] = self.state.message_count_cache.get(character_id, 0) + 2
                    new_msg_count = self.state.message_count_cache[character_id]

                logger.info(f"Successfully committed conversation turn (messages #{new_msg_count-1} and #{new_msg_count})")
        except Exception as memory_error:
            logger.error(f"Error storing conversation in memory for character {character_id}: {memory_error}")
            # We have the reply but couldn't save it - still return success but warn
            logger.warning("Generated response but could not save to conversation history")

        # 6) Don't attempt TTS here - that should be handled by the UI
        # The UI layer should be responsible for TTS after receiving the response

        # Include feedback message if theme was set
        result = {
            "success": True,
            "response": ai_reply,
            "details": {
                "generated_length": len(ai_reply),
                "generation_time": generation_time,
                "messages_saved": new_msg_count if 'new_msg_count' in locals() else "unknown"
            }
        }

        # Add command/theme execution steps for UI display
        if all_steps:
            result["steps"] = all_steps

        return result

    def _build_prompt_messages(self, character_id: str, user_text: str = None, config: Dict = None,
                               include_flags: Dict[str, bool] = None,
                               tools_reserve_tokens: int = 0) -> List[Dict[str, str]]:
        """Build the prompt messages array.

        Domain-layer assembly was extracted to backend.conversation.prompt_builder in
        ST4/B10 (first spine seam). Tool-inclusion policy + rate trackers stay
        on this manager (app-layer turn state) and are injected via
        PromptBuildDeps, so the call order/side effects are byte-exact with the
        pre-split behavior. See backend/prompt_builder.py for the full
        section-by-section assembly.

        include_flags: pre-evaluated gate results keyed command/note/image/
        camera/deep_search/map/elyth (from _generate_reply_task, which reuses
        the same values for the tool-schema decision). When None, the live
        gate methods are injected as before.
        """
        from backend.conversation.prompt_builder import build_prompt_messages, PromptBuildDeps

        def _gate(name, live):
            if include_flags is None:
                return live
            return lambda *args: include_flags[name]

        deps = PromptBuildDeps(
            state=self.state,
            load_character_config=self.load_character_config,
            build_connection_info=self._build_connection_info,
            should_include_command_tools=_gate("command", self._should_include_command_tools),
            should_include_note_tools=_gate("note", self._should_include_note_tools),
            should_include_talk_theme_tools=_gate("talk_theme", self._should_include_talk_theme_tools),
            should_include_image_tools=_gate("image", self._should_include_image_tools),
            should_include_camera_tools=_gate("camera", self._should_include_camera_tools),
            should_include_deep_search_tools=_gate("deep_search", self._should_include_deep_search_tools),
            should_include_map_tools=_gate("map", self._should_include_map_tools),
            should_include_elyth_tools=_gate("elyth", self._should_include_elyth_tools),
        )
        return build_prompt_messages(
            deps, character_id, user_text=user_text, config=config,
            tools_reserve_tokens=tools_reserve_tokens
        )


    def _copy_images_to_permanent_storage(self, character_id: str, image_paths: list) -> list:
        """
        Copy attached images from temporary location to permanent storage.

        Args:
            character_id: The character ID for organizing storage
            image_paths: List of source image file paths (may be Gradio temp paths)

        Returns:
            List of permanent image file paths
        """
        permanent_paths = []
        images_dir = ATTACHMENTS_DIR / character_id / "images"

        try:
            images_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create images directory {images_dir}: {e}")
            return permanent_paths

        import hashlib
        from datetime import datetime

        for src_path in image_paths:
            try:
                src = Path(src_path)
                if not src.exists():
                    logger.warning(f"Source image not found: {src_path}")
                    continue

                # Generate unique filename using timestamp + hash
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                with open(src, "rb") as f:
                    file_hash = hashlib.md5(f.read(8192)).hexdigest()[:8]
                dest_name = f"{timestamp}_{file_hash}{src.suffix}"
                dest_path = images_dir / dest_name

                shutil.copy2(str(src), str(dest_path))
                permanent_paths.append(str(dest_path))
                logger.debug(f"Copied image to permanent storage: {dest_path}")

            except Exception as e:
                logger.error(f"Failed to copy image {src_path}: {e}")

        if permanent_paths:
            logger.info(f"Copied {len(permanent_paths)} images to permanent storage for {character_id}")
            # Cleanup: keep only MAX_PERMANENT_IMAGES files (delete oldest)
            from backend.shared.constants import MAX_PERMANENT_IMAGES
            if self._cleanup_permanent_storage(images_dir, MAX_PERMANENT_IMAGES):
                self._prune_attachment_refs(character_id)

        return permanent_paths

    def _copy_documents_to_permanent_storage(self, character_id: str, documents: list) -> list:
        """
        Copy attached documents from temporary location to permanent storage.

        Args:
            character_id: The character ID for organizing storage
            documents: List of document dicts with 'file_path' and 'filename' keys

        Returns:
            List of permanent document file paths
        """
        permanent_paths = []
        docs_dir = ATTACHMENTS_DIR / character_id / "documents"

        try:
            docs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create documents directory {docs_dir}: {e}")
            return permanent_paths

        import hashlib
        from datetime import datetime

        for doc in documents:
            src_path = doc.get("file_path", "")
            if not src_path:
                continue
            try:
                src = Path(src_path)
                if not src.exists():
                    logger.warning(f"Source document not found: {src_path}")
                    continue

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                with open(src, "rb") as f:
                    file_hash = hashlib.md5(f.read(8192)).hexdigest()[:8]
                dest_name = f"{timestamp}_{file_hash}{src.suffix}"
                dest_path = docs_dir / dest_name

                shutil.copy2(str(src), str(dest_path))
                permanent_paths.append(str(dest_path))
                logger.debug(f"Copied document to permanent storage: {dest_path}")

            except Exception as e:
                logger.error(f"Failed to copy document {src_path}: {e}")

        if permanent_paths:
            logger.info(f"Copied {len(permanent_paths)} documents to permanent storage for {character_id}")
            # Cleanup: keep only MAX_PERMANENT_DOCUMENTS files (delete oldest)
            from backend.shared.constants import MAX_PERMANENT_DOCUMENTS
            if self._cleanup_permanent_storage(docs_dir, MAX_PERMANENT_DOCUMENTS):
                self._prune_attachment_refs(character_id)

        return permanent_paths

    def _prune_attachment_refs(self, character_id: str) -> None:
        """Drop message references to attachment files that no longer exist.

        ファイル削除(上限クリーンアップ/カメラ一時画像の全消し)の直後に呼ぶ。
        MemoryManager 未ロード(短期バッファのみのフォールバック)なら参照は
        DB に無いので何もしない。prune 自体はエラーを内部で握る。
        """
        memory_manager = self.state.memory_managers.get(character_id)
        if memory_manager is not None:
            memory_manager.prune_missing_attachment_refs()

    @staticmethod
    def _cleanup_permanent_storage(directory: Path, max_files: int) -> int:
        """
        Keep only the newest `max_files` files in the directory.
        Deletes oldest files first (sorted by modification time).

        Returns:
            int: Number of files actually deleted (0 on failure). Callers use
            this to prune the now-dead message references (稜裁定 2026-08-20:
            ファイルを捨てるときは参照も一緒に消す).
        """
        deleted = 0
        try:
            files = sorted(directory.iterdir(), key=lambda f: f.stat().st_mtime)
            excess = len(files) - max_files
            if excess > 0:
                for f in files[:excess]:
                    try:
                        f.unlink()
                        deleted += 1
                        logger.debug(f"Cleaned up old file: {f}")
                    except OSError as e:
                        logger.warning(f"Failed to delete {f}: {e}")
                logger.info(f"Cleaned up {deleted} old file(s) from {directory}")
        except Exception as e:
            logger.warning(f"Cleanup failed for {directory}: {e}")
        return deleted


    def _extract_memories_task(self, character_id: str) -> None:
        """
        Background task to extract long-term memories from unprocessed messages.
        Called when unprocessed message count reaches the extraction threshold.

        Thin delegate (B10): the worker body now lives in its SL3 Memory home
        (backend.memory.memory_manager.run_memory_extraction); app-layer state is injected.
        """
        from backend.memory.memory_manager import run_memory_extraction
        run_memory_extraction(self.state, character_id)

    def _update_relationship_task(self, character_id: str) -> None:
        """
        Background task to update relationship summary (all providers; C8 lifted
        the old Ollama exclusion). Called alongside memory extraction, or earlier
        for initial template replacement.

        Thin delegate (B10): the worker body now lives in its SL18 Relationship home
        (backend.memory.relationship_manager.run_relationship_update); app-layer state and
        the load_character_config callable are injected.
        """
        from backend.memory.relationship_manager import run_relationship_update
        run_relationship_update(self.state, character_id, self.load_character_config)

    @standardize_response
    @validate_input({
        'character_id': validate_character_id
    })
    def save_character_memory(self, character_id: str) -> None:
        """
        Force a commit of the current conversation state via the
        character's MemoryManager, if one exists.

        :param character_id: The ID of the character whose memory to save
        """
        logger.info(f"Manually saving conversation state for character: {character_id}")

        # Try memory manager first
        memory_manager = self.state.memory_managers.get(character_id)
        if memory_manager:
            try:
                memory_manager.store.commit()
                logger.info(f"Memory saved via MemoryManager for character {character_id}")
                return
            except Exception as e:
                logger.warning(f"Failed to save via MemoryManager: {e}")

        # No MemoryManager for this character — nothing to save.
        logger.warning(f"Cannot save memory for unknown character: {character_id}")

    @standardize_response
    @validate_input({
        'character_id': validate_character_id
    })
    def reset_conversation(self, character_id: str) -> Dict[str, Any]:
        """
        Clear in-memory conversation state for the character. Remove or recreate the .db file.
        Re-activate the character to start fresh.

        Args:
            character_id: ID of the character whose conversation to reset

        Returns:
            Dict with status information:
            {
                "success": bool,       # Whether operation succeeded
                "error": str,          # Error message if unsuccessful
                "warnings": List[str]  # Any non-critical issues
            }
        """
        logger.info(f"Resetting conversation for {character_id}")
        result = {
            "success": False,
            "warnings": []
        }

        # Validate character exists
        if not self._find_config_file_by_id(character_id):
            logger.error(f"Cannot reset conversation: character {character_id} not found")
            result["error"] = f"Character {character_id} not found"
            return result

        # Try to use MemoryManager if available
        memory_manager = self.state.memory_managers.get(character_id)
        if memory_manager:
            try:
                memory_manager.clear_all_memory()
                logger.info(f"Reset conversation using MemoryManager for {character_id}")
            except Exception as e:
                logger.warning(f"Could not reset conversation with MemoryManager: {e}")
                result["warnings"].append(f"Could not reset using MemoryManager: {str(e)}")
                # Fall back to direct memory reset
                with self.state.memory_lock:
                    self.state.short_term_buffer[character_id] = []
                    self.state.message_count_cache[character_id] = 0
        else:
            # Direct memory reset
            with self.state.memory_lock:
                if character_id in self.state.short_term_buffer:
                    self.state.short_term_buffer[character_id] = []
                if character_id in self.state.message_count_cache:
                    self.state.message_count_cache[character_id] = 0
                logger.info(f"Reset direct memory state for {character_id}")

        # メッセージ(=参照側)を全消ししたので添付の実体も消す(稜裁定 2026-08-20)
        remove_attachment_files(character_id)

        # Remove the .db file to fully reset:
        try:
            cfg_response = self.load_character_config(character_id)
            # Handle standardized response format
            if isinstance(cfg_response, dict) and 'success' in cfg_response:
                cfg = cfg_response.get('result', {})
            else:
                cfg = cfg_response
            db_file = cfg.get("db_file_path", "")
            if db_file:
                if os.path.exists(db_file):
                    try:
                        # Create backup before removal
                        backup_path = f"{db_file}.bak.{int(time.time())}"
                        try:
                            shutil.copy2(db_file, backup_path)
                            logger.info(f"Backed up memory DB to {backup_path}")
                            result["warnings"].append(f"Memory database backed up to {backup_path}")
                        except Exception as backup_error:
                            logger.warning(f"Could not backup DB file {db_file}: {backup_error}")
                            result["warnings"].append(f"Could not create database backup: {str(backup_error)}")

                        # Remove the file
                        os.remove(db_file)
                        logger.info(f"Removed old memory DB: {db_file}")
                    except PermissionError as e:
                        logger.error(f"Permission denied when removing DB file {db_file}: {e}")
                        result["warnings"].append(f"Could not remove database file (permission denied): {db_file}")
                    except FileNotFoundError:
                        logger.warning(f"DB file {db_file} already removed")
                    except Exception as e:
                        logger.warning(f"Could not remove DB file {db_file}: {e}")
                        result["warnings"].append(f"Could not remove database file: {str(e)}")
            else:
                logger.warning(f"No db_file_path found in config for {character_id}")
                result["warnings"].append("No database file path found in character config")
        except Exception as e:
            logger.error(f"Error handling DB file: {e}")
            result["warnings"].append(f"Error handling database file: {str(e)}")

        # Cleanup memory manager reference
        if character_id in self.state.memory_managers:
            try:
                del self.state.memory_managers[character_id]  # Remove old memory manager
                logger.info(f"Removed old memory manager for {character_id}")
            except Exception as e:
                logger.warning(f"Could not remove memory manager reference: {e}")
                result["warnings"].append(f"Could not remove memory manager reference: {str(e)}")

        # Re-activate character
        try:
            activate_result = self.activate_character(character_id)
            if isinstance(activate_result, dict) and not activate_result.get("success", True):
                logger.error(f"Failed to re-activate character after reset: {activate_result.get('error')}")
                result["error"] = f"Could not re-activate character: {activate_result.get('error')}"
                return result
            logger.info(f"Successfully re-activated character {character_id}")

            # Start a fresh conversation after reset
            start_result = self.start_conversation(character_id)
            if not start_result.get("success", False):
                result["warnings"].append("Character reactivated but could not start conversation automatically")
            else:
                logger.info(f"Started fresh conversation after reset for character {character_id}")
            
            result["success"] = True
            return result
        except Exception as e:
            logger.error(f"Failed to re-activate character after reset: {e}")
            result["error"] = f"Could not re-activate character: {str(e)}"
            return result

    @standardize_response
    @validate_input({
        'character_id': validate_character_id,
        'limit': validate_int_param
    })
    def get_conversation_history(self, character_id: str = None, limit: int = 20) -> Dict[str, Any]:
        """
        Retrieve the recent conversation history for the specified character.

        :param character_id: Character ID (uses active character if None)
        :param limit: Maximum number of messages to return
        :return: Dictionary with success status, history list, and any warnings/errors
        """
        # Initialize return structure
        result = {
            "success": False,
            "history": [],
            "warnings": []
        }

        if not character_id:
            character_id = self.state.active_character_id
        if not character_id:
            logger.error("No active character for history retrieval.")
            result["error"] = "No active character selected"
            return result

        try:
            # Try to use MemoryManager first if available
            memory_manager = self.state.memory_managers.get(character_id)
            if memory_manager:
                try:
                    # Import SQLite error types here to check for corruption
                    import sqlite3

                    try:
                        messages = memory_manager.get_recent_messages(limit=limit)
                        result["success"] = True
                        result["history"] = messages
                        return result
                    except sqlite3.DatabaseError as e:
                        # Handle database corruption specifically
                        logger.error(f"Database corruption detected for {character_id}: {e}")

                        # Backup corrupted file if possible
                        try:
                            import shutil
                            import time

                            # Get database path from memory manager or config
                            config_response = self.load_character_config(character_id)
                            # Handle standardized response format
                            if isinstance(config_response, dict) and 'success' in config_response:
                                config = config_response.get('result', {})
                            else:
                                config = config_response
                            db_path = config.get("db_file_path", f"{MEMORY_DIR}/{character_id}.db")

                            if os.path.exists(db_path):
                                backup_path = f"{db_path}.corrupt.{int(time.time())}"
                                shutil.copy2(db_path, backup_path)
                                logger.info(f"Created backup of corrupted database at {backup_path}")
                                result["warnings"].append(f"Corrupted database backed up to {backup_path}")
                        except Exception as backup_error:
                            logger.error(f"Failed to backup corrupted database: {backup_error}")

                        # Attempt recovery by resetting database and restoring from memory
                        try:
                            # First, try to salvage recent messages from in-memory buffer
                            salvaged_messages = []
                            with self.state.memory_lock:
                                if character_id in self.state.short_term_buffer:
                                    salvaged_messages = self.state.short_term_buffer[character_id].copy()
                                    logger.info(f"Salvaged {len(salvaged_messages)} messages from in-memory buffer")
                            
                            # Remove corrupted file
                            if os.path.exists(db_path):
                                os.remove(db_path)
                                logger.info(f"Removed corrupted database: {db_path}")

                            # Initialize a new memory manager
                            new_memory_manager = MemoryManager(db_path, character_id)
                            
                            # Validate and restore salvaged messages if any
                            if salvaged_messages:
                                # Validate messages before restoration
                                valid_messages = []
                                invalid_count = 0
                                
                                for msg in salvaged_messages:
                                    if validate_message(msg):
                                        valid_messages.append(msg)
                                    else:
                                        invalid_count += 1
                                        logger.warning(f"Skipping corrupted message during recovery: {msg.get('role', 'unknown')} role")
                                
                                if invalid_count > 0:
                                    logger.warning(f"Found {invalid_count} corrupted messages during recovery")
                                    result["warnings"].append(f"Skipped {invalid_count} corrupted messages during recovery")
                                
                                if valid_messages:
                                    logger.info(f"Restoring {len(valid_messages)} validated messages to new database")
                                    for msg in valid_messages:
                                        try:
                                            new_memory_manager.add_message(msg.get("role"), msg.get("content"))
                                        except Exception as restore_error:
                                            logger.warning(f"Failed to restore message: {restore_error}")
                                    
                                    result["warnings"].append(f"Recovered {len(valid_messages)} valid messages from memory")
                            
                            self.state.memory_managers[character_id] = new_memory_manager
                            
                            # Return the salvaged messages as history
                            if salvaged_messages:
                                result["history"] = salvaged_messages[-limit:] if len(salvaged_messages) > limit else salvaged_messages
                                result["success"] = True
                                result["warnings"].append("Database was corrupted but recent conversation was recovered")
                            else:
                                result["success"] = True
                                result["warnings"].append("Conversation history was corrupted and has been reset")
                            
                            logger.info(f"Successfully recovered from database corruption for {character_id}")
                            return result
                        except Exception as reset_error:
                            logger.critical(f"Failed to recover from database corruption: {reset_error}")
                            result["error"] = "Database corruption - unable to recover conversation history"
                            return result
                except ImportError:
                    # Can't specifically detect SQLite errors, use generic approach
                    logger.warning("sqlite3 module not available for specific corruption detection")
                    try:
                        messages = memory_manager.get_recent_messages(limit=limit)
                        result["success"] = True
                        result["history"] = messages
                        return result
                    except Exception as e:
                        logger.error(f"Error using MemoryManager for history: {e}")
                        result["warnings"].append("Using fallback memory access due to MemoryManager error")
                        # Fall through to fallback

            # Fall back to direct memory access with proper variable references
            with self.state.memory_lock:
                st_buffer = self.state.short_term_buffer.get(character_id, [])
                if st_buffer:
                    history = st_buffer[-limit:] if len(st_buffer) > limit else st_buffer[:]
                    result["success"] = True
                    result["history"] = history
                    if not memory_manager:
                        result["warnings"].append("Using direct memory access (MemoryManager not available)")
                else:
                    result["success"] = True
                    result["warnings"].append("No conversation history found")

                return result

        except Exception as e:
            logger.error(f"Error retrieving conversation history: {e}")
            result["error"] = f"Failed to retrieve conversation history: {str(e)}"
            return result

    def _check_character_ready(self, character_id: str) -> Optional[Dict[str, Any]]:
        """会話開始前のキャラクター設定ガード(稜裁定 2026-09-25)。

        LLM モデルとキャラクターボイスが未設定なら開始をブロックし、
        「既存キャラクター編集」へ誘導する {"success": False, "error_code": ...}
        を返す(設定済みなら None)。同梱モデルキャラクターは LLM 未設定
        (日本語2人は声も)で出荷されるので、ここが最初の案内になる。
        判定は設定値の有無だけ(実照会はしない: モデル不在・声の読み込み失敗は
        従来どおり活性化/生成側が扱う)。設定が読めないときはガードを縮退(None)。
        """
        try:
            config_response = self.load_character_config(character_id)
        except Exception as e:
            logger.warning(f"Character guard: config unreadable, skipping: {e}")
            return None
        if isinstance(config_response, dict) and 'success' in config_response:
            if not config_response.get('success'):
                return None
            config = config_response.get('result') or {}
        elif isinstance(config_response, dict):
            config = config_response
        else:
            return None

        has_llm = bool(config.get("model_name") or config.get("ollama_model_name"))
        tts = config.get("tts_model_config") or {}
        provider = tts.get("provider", "sbv2")
        if provider == "kokoro":
            has_voice = bool(tts.get("voice_name"))
        elif provider == "elevenlabs":
            has_voice = bool(tts.get("voice_id"))
        else:
            has_voice = bool(tts.get("model_path"))
        if has_llm and has_voice:
            return None
        if not has_llm and not has_voice:
            code = "start_no_llm_voice"
        elif not has_llm:
            code = "start_no_llm"
        else:
            code = "start_no_voice"
        logger.error(f"Character {character_id} is not ready to start a conversation ({code})")
        return {
            "success": False,
            "error": "Character setup is incomplete (LLM model / character voice). "
                     "Set them in Edit Existing Character first.",
            "error_code": code,
        }

    def _check_embedding_ready(self) -> Optional[Dict[str, Any]]:
        """会話開始前の埋め込みモデルガード(稜裁定 2026-07-25=案A採用)。

        未設定または使用不能なら {"success": False, "error_code": ...} を返し、
        使用可能なら None。判定は設定値の存在でなく実照会
        (2026-07-19 インジケーター改修の「キー存在でなく実照会」裁定に整合):
        Ollama系=モデル一覧照会+存在確認(ローカルHTTP・リトライ1回)、
        API系=providerプローブ(timeout5秒・保存なし)。
        設定読み取り自体の失敗では会話を止めない(ガードは縮退)。
        """
        try:
            from backend.shared.api_settings import get_embedding_model
            embedding_model = get_embedding_model()
        except Exception as e:
            logger.warning(f"Embedding guard: settings unreadable, skipping: {e}")
            return None

        if embedding_model is None:
            return {
                "success": False,
                "error": "Embedding model is not configured. Set it in the API Setting tab.",
                "error_code": "embedding_unset",
            }

        provider, model_name = embedding_model
        try:
            if provider == "ollama":
                from backend.llm.ollama_integration import list_ollama_models
                models, status = list_ollama_models(max_retries=1)
                if not models:
                    logger.error(f"Embedding guard: Ollama unreachable ({status})")
                    return {
                        "success": False,
                        "error": f"Embedding model '{model_name}' is unavailable (Ollama: {status}).",
                        "error_code": "embedding_ollama_unreachable",
                        "error_params": {"model": model_name},
                    }
                # タグ表記ゆれ吸収: 一覧は "name:tag" で返る。ベース名一致で判定
                base = model_name.split(":")[0]
                if not any(m == model_name or m.split(":")[0] == base for m in models):
                    logger.error(f"Embedding guard: model '{model_name}' not in Ollama")
                    return {
                        "success": False,
                        "error": f"Embedding model '{model_name}' is not installed in Ollama.",
                        "error_code": "embedding_model_missing",
                        "error_params": {"model": model_name},
                    }
            else:
                from backend.shared.api_settings import probe_provider_api
                probe = probe_provider_api(provider)
                if probe.get("state") == "unreachable":
                    # 一過性のネットワーク不調で会話開始を止めない:
                    # unreachable だけ1回リトライ(2026-08-12実機: OpenAIへの
                    # 一時的な到達失敗がこのガードで表面化した=稜依頼2026-08-14)。
                    # auth_error / no_key は確定的な状態なので再試行しない。
                    time.sleep(1.0)
                    probe = probe_provider_api(provider)
                    if probe.get("state") == "ok":
                        logger.info(
                            f"Embedding guard: provider '{provider}' probe recovered on retry")
                if probe.get("state") != "ok":
                    logger.error(
                        f"Embedding guard: provider '{provider}' probe={probe.get('state')}")
                    return {
                        "success": False,
                        "error": (f"Embedding model '{model_name}' is unavailable "
                                  f"(provider '{provider}': {probe.get('state')})."),
                        "error_code": "embedding_api_unavailable",
                        "error_params": {"model": model_name, "provider": provider},
                    }
        except Exception as e:
            # 照会機構自体の想定外failはログのみ(ガードで会話全体を殺さない)
            logger.warning(f"Embedding guard: probe failed unexpectedly, skipping: {e}")
        return None

    @standardize_response
    @validate_input({
        'character_id': validate_character_id
    })
    def start_conversation(self, character_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Start a conversation session with the active or specified character.
        This is called after a character is selected/activated.
        
        Returns:
            Dict[str, Any]: Status information about the operation
            {
                "success": bool,       # Whether operation succeeded
                "character_id": str,   # The character ID for the conversation
                "error": str,          # Error message if unsuccessful
                "message": str         # Status message
            }
        """
        # Block if ELYTH session is active
        if getattr(self.state, 'elyth_session_active', False):
            return {
                "success": False,
                "error": "ELYTHセッション実行中のため会話を開始できません。セッション終了後に再度お試しください。",
                "error_code": "start_elyth_active"
            }

        # Block if a YouTube reply-generation phase is active (相互排他:
        # 生成フェーズのみ。投稿ワーカーはLLM不要のため会話をブロックしない)
        try:
            from backend.shared.youtube_state import get_youtube_state
            if get_youtube_state().session_active:
                return {
                    "success": False,
                    "error": "YouTube返信の生成中のため会話を開始できません。生成終了後に再度お試しください。",
                    "error_code": "start_youtube_active"
                }
        except Exception:
            pass

        # Use provided character_id or fallback to active one
        if character_id:
            # Ensure the character is activated first
            if character_id != self.state.active_character_id:
                try:
                    # activate_character は @standardize_response で失敗を例外でなく
                    # {success: False} で返す(RATE_LIMIT/BUSY 等)。戻り値を検査しないと
                    # 活性化失敗のまま conversation_active=True にしてしまう。
                    activate_result = self.activate_character(character_id)
                    if isinstance(activate_result, dict) and not activate_result.get("success", True):
                        logger.error(f"Failed to activate character {character_id}: {activate_result.get('error')}")
                        return {
                            "success": False,
                            "error": activate_result.get("error", "Failed to activate character")
                        }
                except Exception as e:
                    logger.error(f"Failed to activate character {character_id}: {e}")
                    return {
                        "success": False,
                        "error": f"Failed to activate character: {str(e)}"
                    }
        else:
            character_id = self.state.active_character_id
        
        if not character_id:
            logger.error("No character selected for conversation")
            return {
                "success": False,
                "error": "No character selected. Please select a character first.",
                "error_code": "start_no_character"
            }

        # キャラクター設定ガード(LLM/声が未設定なら開始しない・稜裁定 2026-09-25)
        character_block = self._check_character_ready(character_id)
        if character_block is not None:
            return character_block

        # 埋め込みモデル必須ガード(未設定/使用不能なら開始ブロック+設定誘導)
        embedding_block = self._check_embedding_ready()
        if embedding_block is not None:
            return embedding_block

        # Clear location slot in local mode (server→local mode switch protection)
        if not getattr(self.state, 'server_mode', False):
            try:
                from backend.tools.location_manager import clear_location
                clear_location()
            except Exception:
                pass

        # Mark conversation as active
        self.state.conversation_active = True

        # Notify ELYTH session manager that conversation started (clears idle timer)
        try:
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            get_elyth_session_manager().on_conversation_started()
        except Exception:
            pass

        # 会話開始=ユーザー在席の確実な証拠 → 覚醒ゲートを即時再開
        # (スリープ復帰入力がゲート検出より前に済んでいた場合の脱出口・リモート操作も救う)
        try:
            from backend.shared.wake_gate import get_wake_gate
            get_wake_gate().notify_user_activity()
        except Exception:
            pass

        # Log conversation start
        logger.info(f"Started conversation with character {character_id}")
        
        return {
            "success": True,
            "character_id": character_id,
            "message": "Conversation started",
        }

    @standardize_response
    def stop_conversation(self) -> Dict[str, Any]:
        """
        Stop the current conversation session.
        Saves memory state but keeps character resources loaded.
        
        Returns:
            Dict[str, Any]: Status information about the operation
            {
                "success": bool,       # Whether operation succeeded
                "message": str,        # Status message
                "error": str           # Error message if unsuccessful
            }
        """
        if not self.state.conversation_active:
            logger.info("No active conversation to stop")
            return {
                "success": True,
                "message": "No active conversation to stop"
            }
        
        # Save current conversation state
        if self.state.active_character_id:
            try:
                self.save_character_memory(self.state.active_character_id)
                logger.info(f"Saved conversation state for character {self.state.active_character_id}")
            except (IOError, OSError) as e:
                logger.error(f"Failed to save conversation state due to I/O error: {e}")
                # Continue with stopping even if save fails
            except RuntimeError as e:
                logger.error(f"Failed to save conversation state: {e}")
                # Continue with stopping even if save fails
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                logger.exception(f"Unexpected error saving conversation state: {e}")
                # Continue with stopping even if save fails
        
        # Clean up captured camera images
        if self.state.active_character_id:
            try:
                from backend.tools.camera_capture import cleanup_captured_images
                cleanup_captured_images(self.state.active_character_id)
            except Exception as e:
                logger.debug(f"Camera capture cleanup skipped: {e}")
            # ファイルを消したら参照も畳む(稜裁定 2026-08-20)。カメラ分に加え、
            # 会話中に上限クリーンアップ/生成画像FIFOが消したファイルの参照も
            # ここでまとめて片付く(実在確認ベース)。shutdown 経路でも
            # close_all より前(ui/app.py: stop_conversation→shutdown_backend)。
            self._prune_attachment_refs(self.state.active_character_id)

        # Stop any ongoing audio capture
        try:
            import audio_input.audio_input as audio_input
            audio_input.stop_recording()
            logger.debug("Stopped audio input recording")
        except AttributeError:
            logger.debug("Audio input module not properly initialized")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logger.debug(f"No active audio to stop or error stopping audio: {e}")
        
        # Cancel any pending LLM tasks if possible
        if hasattr(self.state, 'pending_requests'):
            with self.state.pending_requests_lock:
                active_requests = [req_id for req_id, req in self.state.pending_requests.items()
                                  if req.get("status") in ["pending", "submitted"]]
            if active_requests:
                logger.info(f"Cancelling {len(active_requests)} pending LLM requests")
                # The actual cancellation would depend on queue_manager implementation
        
        # Clear location slot (map search tools become unavailable)
        try:
            from backend.tools.location_manager import clear_location
            clear_location()
            logger.debug("Location slot cleared on conversation stop")
        except Exception as e:
            logger.debug(f"Location clear skipped: {e}")

        # Mark conversation as inactive
        self.state.conversation_active = False

        # Notify ELYTH session manager to start idle timer immediately
        try:
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            get_elyth_session_manager().on_conversation_ended()
        except Exception as e:
            logger.warning(f"Failed to notify ELYTH on conversation end: {e}")

        logger.info("Conversation stopped")

        return {
            "success": True,
            "message": "Conversation stopped successfully"
        }