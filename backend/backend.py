#!/usr/bin/env python3
# backend.py
"""
Artificial Girlfriend - Backend Module

This module orchestrates:
1. Character management (create, edit, remove, list configs).
2. Conversation logic with multi-turn memory (via LangGraph + SQLite .db).
3. LLM calls through langchain-ollama (ensuring only one operation at a time).
4. STT/TTS integration (configure STT language, TTS model) when activating characters.

All functionality in this file is based on the requirements and function list
defined in the project documentation.

Important Notes:
- Summaries of older messages happen every 50 messages (long-term memory),
  while the last 20 messages remain in short-term memory for prompt construction.
- Only one LLM operation (reply generation or memory extraction) can run at a time,
  enforced by a single FIFO queue.
- Each character has its own .db file for memory (via LangGraph's SQLite checkpointer).
- Each character config (.json or .yaml) references:
  - STT language
  - TTS model paths
  - Ollama model name (e.g., "llama2" or "vicuna:13b-q4_0")
  - system_prompt (defines the persona)
  - db_file_path (e.g., "memory/<character_id>.db")
"""

import os
import json
import shutil
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
from collections import OrderedDict

# --------------------------------------------------------------------------
# External modules (assumed to exist in the same project structure).
# Depending on your setup, adjust import paths accordingly.
# --------------------------------------------------------------------------
try:
    # Backend internal modules
    from backend.shared.queue_manager import LLMTaskQueue    # Import QueueManager for LLM task handling
    from backend.conversation.character_manager import (
        _find_config_file_by_id,
        list_ollama_models as _list_ollama_models_impl,
        list_tts_models as _list_tts_models_impl,
        list_character_icons as _list_character_icons_impl,
        list_stt_languages as _list_stt_languages_impl
    )
    from backend.conversation_manager import ConversationManager  # Import ConversationManager
except ImportError:
    pass


# --------------------------------------------------------------------------
# Import constants from centralized location
# --------------------------------------------------------------------------
from backend.shared.constants import (
    CHARACTER_CONFIGS_DIR, MEMORY_DIR, OLLAMA_SERVER_URL,
    ensure_directories_exist
)

# Import shared utilities from backend_utils
from backend.shared.backend_utils import (
    standardize_response, validate_input,
    validate_character_id, validate_dict_param, validate_bool_param,
    RECOVERABLE_ERRORS
)

# Logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Error handling infrastructure is now imported from backend_utils

# --------------------------------------------------------------------------
# Type Definitions for Input Validation
# --------------------------------------------------------------------------

# Input validation infrastructure is now imported from backend_utils

# --------------------------------------------------------------------------
# Backend State Class
# --------------------------------------------------------------------------
class BackendState:
    """
    Encapsulates all backend state to avoid global variables.
    This class is initialized as a singleton in the init_backend function.
    """
    def __init__(self):
        # Queue for LLM tasks (using QueueManager)
        self.queue_manager: Optional["LLMTaskQueue"] = None

        # Active character ID and relevant data
        self.active_character_id: Optional[str] = None

        # Conversation state management
        self.conversation_active: bool = False

        # Per-character LLM/memory caches (active_llm_cache + MAX_LLM_CACHE_SIZE /
        # message_count_cache / short_term_buffer /
        # memory_managers / memory_lock) carved out of BackendState into
        # MemoryCaches (ST5/B6 god-object split). Ownership now lives on
        # self.memory_caches; the legacy self.<field> access paths still resolve
        # via the backward-compat properties defined right after __init__.
        from backend.shared.memory_caches import MemoryCaches
        self.memory_caches: MemoryCaches = MemoryCaches()

        # Background task tracking. Mutated from multiple real OS threads
        # (UI/Gradio, extraction, relationship-update, memory-save), so a
        # lock guards append + filter-reassign against lost updates and
        # "dictionary/list changed size during iteration".
        self.background_tasks: List[Dict[str, Any]] = []
        self.background_tasks_lock = threading.Lock()
        # LLM request tracking (pending_requests + its lock) carved out of
        # BackendState into LlmRequestTracker (M13 §3 direct-field fix).
        # Ownership lives on self.llm_requests; the legacy
        # self.pending_requests / self.pending_requests_lock access paths
        # still resolve via the backward-compat properties defined right
        # after __init__.
        from backend.shared.llm_request_tracker import LlmRequestTracker
        self.llm_requests: LlmRequestTracker = LlmRequestTracker()
        self.shutdown_flag = threading.Event()

        # Rate limiting for rapid operations
        self.last_operation_times: Dict[str, float] = {}
        self.OPERATION_COOLDOWN = 0.5  # 500ms cooldown between same operations
        
        # Character-activation locks (activation_lock + per-character operation
        # locks) carved out of BackendState into ActivationRegistry (ST5/B6
        # god-object split). Ownership now lives on self.activation; the legacy
        # self.activation_lock / self.character_operation_locks /
        # self.character_locks_lock access paths still resolve via the
        # backward-compat properties defined right after __init__, and
        # get_character_operation_lock() delegates to self.activation.
        from backend.shared.activation_registry import ActivationRegistry
        self.activation: ActivationRegistry = ActivationRegistry()

        # Token / model-context tracking (model_context_cache / model_info_cache /
        # active_model_context_window /
        # prompt_truncation_history / last_full_prompt_before_truncation /
        # last_truncation_info) carved out of BackendState into TokenState (ST5/B6
        # god-object split). Ownership now lives on self.token; the legacy
        # self.<field> access paths still resolve via the backward-compat
        # properties defined right after __init__.
        from backend.shared.token_state import TokenState
        self.token: TokenState = TokenState()

        # Runtime feature-toggle flags (pc_status / screen_capture / talk_theme /
        # speechless / notes / image_generation / camera_capture / ambient_camera /
        # deep_search / elyth / command_execution) carved out of BackendState into
        # FeatureState (ST5/B6 god-object split). Ownership now lives on
        # self.features; the legacy `self.<flag>` access paths still resolve via
        # the backward-compat properties defined right after __init__.
        from backend.shared.feature_state import FeatureState
        self.features: FeatureState = FeatureState()

        # Server mode flag (set at startup; suppresses command execution)
        self.server_mode: bool = False

        # ELYTH integration (runtime session state; the elyth_enabled toggle
        # lives on self.features)
        self.elyth_session_active: bool = False
        self.elyth_stop_event: threading.Event = threading.Event()

        # Cross-client generating lock: prevents concurrent text submissions
        self.is_generating: bool = False
        self.is_generating_lock: threading.Lock = threading.Lock()

        # Command approval (runtime handshake) carved out of BackendState into
        # CommandState (ST5/B6 god-object split). Ownership now lives on
        # self.command; the legacy `self.command_*` / `self._pending_interruption`
        # access paths still resolve via the backward-compat properties defined
        # right after __init__. The command_execution_enabled toggle lives on
        # self.features (carved out in ST5-S1).
        from backend.shared.command_state import CommandState
        self.command: CommandState = CommandState()

        # Image buffer for accumulating user-attached images
        from backend.shared.image_buffer import ImageBuffer
        self.image_buffer: ImageBuffer = ImageBuffer()

        # Document buffer for accumulating user-attached documents
        from backend.shared.document_buffer import DocumentBuffer
        self.document_buffer: DocumentBuffer = DocumentBuffer()

    # ------------------------------------------------------------------
    # Backward-compat properties (ST5/B6 god-object split): the feature
    # toggles now live on self.features (FeatureState). These redirect the
    # legacy _backend_state.<flag> read/write paths to the owning object so
    # existing call sites are unchanged. New code should use
    # self.features.<flag> directly.
    # ------------------------------------------------------------------
    @property
    def pc_status_enabled(self) -> bool:
        return self.features.pc_status_enabled

    @pc_status_enabled.setter
    def pc_status_enabled(self, value: bool) -> None:
        self.features.pc_status_enabled = value

    @property
    def screen_capture_enabled(self) -> bool:
        return self.features.screen_capture_enabled

    @screen_capture_enabled.setter
    def screen_capture_enabled(self, value: bool) -> None:
        self.features.screen_capture_enabled = value

    @property
    def talk_theme_enabled(self) -> bool:
        return self.features.talk_theme_enabled

    @talk_theme_enabled.setter
    def talk_theme_enabled(self, value: bool) -> None:
        self.features.talk_theme_enabled = value

    @property
    def speechless_enabled(self) -> bool:
        return self.features.speechless_enabled

    @speechless_enabled.setter
    def speechless_enabled(self, value: bool) -> None:
        self.features.speechless_enabled = value

    @property
    def notes_enabled(self) -> bool:
        return self.features.notes_enabled

    @notes_enabled.setter
    def notes_enabled(self, value: bool) -> None:
        self.features.notes_enabled = value

    @property
    def command_execution_enabled(self) -> bool:
        return self.features.command_execution_enabled

    @command_execution_enabled.setter
    def command_execution_enabled(self, value: bool) -> None:
        self.features.command_execution_enabled = value

    @property
    def image_generation_enabled(self) -> bool:
        return self.features.image_generation_enabled

    @image_generation_enabled.setter
    def image_generation_enabled(self, value: bool) -> None:
        self.features.image_generation_enabled = value

    @property
    def camera_capture_enabled(self) -> bool:
        return self.features.camera_capture_enabled

    @camera_capture_enabled.setter
    def camera_capture_enabled(self, value: bool) -> None:
        self.features.camera_capture_enabled = value

    @property
    def ambient_camera_enabled(self) -> bool:
        return self.features.ambient_camera_enabled

    @ambient_camera_enabled.setter
    def ambient_camera_enabled(self, value: bool) -> None:
        self.features.ambient_camera_enabled = value

    @property
    def deep_search_enabled(self) -> bool:
        return self.features.deep_search_enabled

    @deep_search_enabled.setter
    def deep_search_enabled(self, value: bool) -> None:
        self.features.deep_search_enabled = value

    @property
    def elyth_enabled(self) -> bool:
        return self.features.elyth_enabled

    @elyth_enabled.setter
    def elyth_enabled(self, value: bool) -> None:
        self.features.elyth_enabled = value

    # ------------------------------------------------------------------
    # Backward-compat properties (ST5/B6 god-object split): the command
    # approval handshake now lives on self.command (CommandState). These
    # redirect the legacy _backend_state.<field> read/write paths to the
    # owning object so existing call sites are unchanged. New code should
    # use self.command.<field> directly.
    # ------------------------------------------------------------------
    @property
    def command_approval_pending(self) -> bool:
        return self.command.command_approval_pending

    @command_approval_pending.setter
    def command_approval_pending(self, value: bool) -> None:
        self.command.command_approval_pending = value

    @property
    def command_approval_event(self) -> threading.Event:
        return self.command.command_approval_event

    @command_approval_event.setter
    def command_approval_event(self, value: threading.Event) -> None:
        self.command.command_approval_event = value

    @property
    def command_approval_result(self) -> Optional[str]:
        return self.command.command_approval_result

    @command_approval_result.setter
    def command_approval_result(self, value: Optional[str]) -> None:
        self.command.command_approval_result = value

    @property
    def command_pending_info(self) -> Optional[Dict]:
        return self.command.command_pending_info

    @command_pending_info.setter
    def command_pending_info(self, value: Optional[Dict]) -> None:
        self.command.command_pending_info = value

    @property
    def _pending_interruption(self) -> bool:
        return self.command._pending_interruption

    @_pending_interruption.setter
    def _pending_interruption(self, value: bool) -> None:
        self.command._pending_interruption = value

    # ------------------------------------------------------------------
    # Backward-compat properties (M13 §3 direct-field fix): LLM request
    # tracking now lives on self.llm_requests (LlmRequestTracker). These
    # redirect the legacy _backend_state.<field> read/write paths to the
    # owning object so existing call sites are unchanged. New code should
    # use self.llm_requests.<field> directly.
    # ------------------------------------------------------------------
    @property
    def pending_requests(self) -> Dict[str, Dict]:
        return self.llm_requests.requests

    @pending_requests.setter
    def pending_requests(self, value: Dict[str, Dict]) -> None:
        self.llm_requests.requests = value

    @property
    def pending_requests_lock(self) -> threading.Lock:
        return self.llm_requests.lock

    @pending_requests_lock.setter
    def pending_requests_lock(self, value: threading.Lock) -> None:
        self.llm_requests.lock = value

    # ------------------------------------------------------------------
    # Backward-compat properties (ST5/B6 god-object split): the character-
    # activation locks now live on self.activation (ActivationRegistry). These
    # redirect the legacy _backend_state.<field> read/write paths to the owning
    # object so existing call sites are unchanged. New code should use
    # self.activation.<field> directly.
    # ------------------------------------------------------------------
    @property
    def activation_lock(self) -> threading.Lock:
        return self.activation.activation_lock

    @activation_lock.setter
    def activation_lock(self, value: threading.Lock) -> None:
        self.activation.activation_lock = value

    @property
    def character_operation_locks(self) -> Dict[str, threading.RLock]:
        return self.activation.character_operation_locks

    @character_operation_locks.setter
    def character_operation_locks(self, value: Dict[str, threading.RLock]) -> None:
        self.activation.character_operation_locks = value

    @property
    def character_locks_lock(self) -> threading.Lock:
        return self.activation.character_locks_lock

    @character_locks_lock.setter
    def character_locks_lock(self, value: threading.Lock) -> None:
        self.activation.character_locks_lock = value

    # ------------------------------------------------------------------
    # Backward-compat properties (ST5/B6 god-object split): the per-character
    # LLM/memory caches now live on self.memory_caches (MemoryCaches). These
    # redirect the legacy _backend_state.<field> read/write paths to the owning
    # object so existing call sites are unchanged. New code should use
    # self.memory_caches.<field> directly.
    # ------------------------------------------------------------------
    @property
    def active_llm_cache(self) -> "OrderedDict[str, Any]":
        return self.memory_caches.active_llm_cache

    @active_llm_cache.setter
    def active_llm_cache(self, value: "OrderedDict[str, Any]") -> None:
        self.memory_caches.active_llm_cache = value

    @property
    def MAX_LLM_CACHE_SIZE(self) -> int:
        return self.memory_caches.MAX_LLM_CACHE_SIZE

    @MAX_LLM_CACHE_SIZE.setter
    def MAX_LLM_CACHE_SIZE(self, value: int) -> None:
        self.memory_caches.MAX_LLM_CACHE_SIZE = value

    @property
    def message_count_cache(self) -> Dict[str, int]:
        return self.memory_caches.message_count_cache

    @message_count_cache.setter
    def message_count_cache(self, value: Dict[str, int]) -> None:
        self.memory_caches.message_count_cache = value

    @property
    def short_term_buffer(self) -> Dict[str, List[Dict[str, Any]]]:
        return self.memory_caches.short_term_buffer

    @short_term_buffer.setter
    def short_term_buffer(self, value: Dict[str, List[Dict[str, Any]]]) -> None:
        self.memory_caches.short_term_buffer = value

    @property
    def memory_managers(self) -> Dict[str, Any]:
        return self.memory_caches.memory_managers

    @memory_managers.setter
    def memory_managers(self, value: Dict[str, Any]) -> None:
        self.memory_caches.memory_managers = value

    @property
    def memory_lock(self) -> threading.Lock:
        return self.memory_caches.memory_lock

    @memory_lock.setter
    def memory_lock(self, value: threading.Lock) -> None:
        self.memory_caches.memory_lock = value

    # ------------------------------------------------------------------
    # Backward-compat properties (ST5/B6 god-object split): the token /
    # model-context tracking fields now live on self.token (TokenState). These
    # redirect the legacy _backend_state.<field> read/write paths to the owning
    # object so existing call sites are unchanged. New code should use
    # self.token.<field> directly.
    # ------------------------------------------------------------------
    @property
    def model_context_cache(self) -> Dict[str, int]:
        return self.token.model_context_cache

    @model_context_cache.setter
    def model_context_cache(self, value: Dict[str, int]) -> None:
        self.token.model_context_cache = value

    @property
    def model_info_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.token.model_info_cache

    @model_info_cache.setter
    def model_info_cache(self, value: Dict[str, Dict[str, Any]]) -> None:
        self.token.model_info_cache = value

    @property
    def active_model_context_window(self) -> Optional[int]:
        return self.token.active_model_context_window

    @active_model_context_window.setter
    def active_model_context_window(self, value: Optional[int]) -> None:
        self.token.active_model_context_window = value

    @property
    def prompt_truncation_history(self) -> List[Dict]:
        return self.token.prompt_truncation_history

    @prompt_truncation_history.setter
    def prompt_truncation_history(self, value: List[Dict]) -> None:
        self.token.prompt_truncation_history = value

    @property
    def last_full_prompt_before_truncation(self) -> Optional[str]:
        return self.token.last_full_prompt_before_truncation

    @last_full_prompt_before_truncation.setter
    def last_full_prompt_before_truncation(self, value: Optional[str]) -> None:
        self.token.last_full_prompt_before_truncation = value

    @property
    def last_truncation_info(self) -> Optional[Dict]:
        return self.token.last_truncation_info

    @last_truncation_info.setter
    def last_truncation_info(self, value: Optional[Dict]) -> None:
        self.token.last_truncation_info = value

    def add_background_task(self, task: Dict[str, Any]):
        """Append a background task under the lock (called from multiple threads)."""
        with self.background_tasks_lock:
            self.background_tasks.append(task)

    def cleanup_background_tasks(self):
        """Remove completed background tasks to prevent memory leak"""
        with self.background_tasks_lock:
            active_tasks = []
            for task in self.background_tasks:
                if task['thread'].is_alive():
                    active_tasks.append(task)
                else:
                    logger.debug(f"Cleaned up completed background task for {task.get('character_id', 'unknown')}")
            self.background_tasks = active_tasks
        
    def check_rate_limit(self, operation: str) -> bool:
        """Check if operation is rate limited"""
        now = time.time()
        last_time = self.last_operation_times.get(operation, 0)
        if now - last_time < self.OPERATION_COOLDOWN:
            return False
        self.last_operation_times[operation] = now
        return True
    
    def get_character_operation_lock(self, character_id: str) -> threading.RLock:
        """Get lock for character operations (activate/remove) to prevent race conditions.

        Delegates to the owning ActivationRegistry (ST5/B6 god-object split).

        Args:
            character_id: The character ID to get a lock for

        Returns:
            threading.RLock: The lock for this character's operations
        """
        return self.activation.get_character_operation_lock(character_id)


# Global singleton instance
_backend_state = BackendState()

# Global conversation manager instance (initialized in init_backend)
_conversation_manager: Optional[ConversationManager] = None

def _get_conversation_manager() -> ConversationManager:
    """Get the global conversation manager instance"""
    global _conversation_manager
    if _conversation_manager is None:
        raise RuntimeError("ConversationManager not initialized. Call init_backend() first.")
    return _conversation_manager

# Helper functions moved to backend_utils.py and conversation_manager.py

# --------------------------------------------------------------------------
# Context Managers for Resource Management
# --------------------------------------------------------------------------

@contextmanager
def atomic_file_operation(file_path: Path, operation: str = "write"):
    """Enhanced atomic file operations with better recovery"""
    temp_path = None
    backup_path = None
    
    try:
        if operation == "write":
            # Create in same directory to ensure same filesystem
            temp_path = file_path.parent / f".{file_path.name}.tmp.{os.getpid()}"
            yield temp_path
            
            # Use os.replace for true atomic operation
            try:
                os.replace(str(temp_path), str(file_path))
            except OSError:
                # Fallback for cross-filesystem moves
                shutil.move(str(temp_path), str(file_path))
                
        elif operation == "update":
            # Create backup
            if file_path.exists():
                backup_path = file_path.with_suffix(f'.bak.{int(time.time())}')
                shutil.copy2(file_path, backup_path)
            
            yield file_path
            
            # Success - remove backup
            if backup_path and backup_path.exists():
                backup_path.unlink()
    except Exception as e:
        logger.error(f"Atomic operation failed for {file_path}: {e}")
        # Restore from backup if available
        if operation == "update" and backup_path and backup_path.exists():
            try:
                shutil.copy2(backup_path, file_path)
                logger.info(f"Restored file from backup: {file_path}")
            except Exception as restore_error:
                logger.error(f"Failed to restore from backup: {restore_error}")
        
        # Clean up temp file
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except (OSError, PermissionError) as e:
                logger.debug(f"Failed to delete temp file {temp_path}: {e}")
        
        raise
    finally:
        # Clean up any remaining temp files
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except (OSError, PermissionError) as e:
                # Schedule for cleanup later
                logger.warning(f"Could not clean up temp file {temp_path}: {e}")


# _enqueue_llm_task moved to conversation_manager.py


# --------------------------------------------------------------------------
# Utility Functions
# --------------------------------------------------------------------------
def cleanup_temp_files():
    """Remove orphaned temp files from previous runs"""
    cleaned_count = 0
    for dir_path in [CHARACTER_CONFIGS_DIR, MEMORY_DIR]:
        try:
            for temp_file in Path(dir_path).glob(".*.tmp.*"):
                try:
                    # Check if file is older than 1 hour
                    if time.time() - temp_file.stat().st_mtime > 3600:
                        temp_file.unlink()
                        logger.info(f"Cleaned up orphaned temp file: {temp_file}")
                        cleaned_count += 1
                except Exception as e:
                    logger.debug(f"Could not clean up {temp_file}: {e}")
        except Exception as e:
            logger.warning(f"Error scanning directory {dir_path} for temp files: {e}")
    
    if cleaned_count > 0:
        logger.info(f"Cleaned up {cleaned_count} orphaned temp files")
    return cleaned_count


def cleanup_old_recovery_files():
    """Remove old emergency recovery files to prevent disk space accumulation"""
    cleaned_count = 0
    try:
        recovery_pattern = Path(MEMORY_DIR).glob("*.recovery.*.json")
        for recovery_file in recovery_pattern:
            try:
                # Check if file is older than 24 hours
                if time.time() - recovery_file.stat().st_mtime > 86400:
                    recovery_file.unlink()
                    logger.info(f"Cleaned up old recovery file: {recovery_file}")
                    cleaned_count += 1
            except Exception as e:
                logger.debug(f"Could not clean up recovery file {recovery_file}: {e}")
    except Exception as e:
        logger.warning(f"Error scanning for recovery files: {e}")
    
    if cleaned_count > 0:
        logger.info(f"Cleaned up {cleaned_count} old recovery files")
    return cleaned_count


# --------------------------------------------------------------------------
# Backend Initialization
# --------------------------------------------------------------------------
def init_backend():
    """
    Prepare the backend at app startup.
    - Initialize the QueueManager for LLM task handling.
    - Check if Ollama is running and validate connectivity.
    - Initialize the LLM worker thread for concurrency control (fallback).
    - Create required directories.
    - (Optional) Load a list of characters or do other bootstrapping.
    """
    logger.info("Initializing backend...")
    state = _backend_state  # Get state singleton

    # Server-mode self-load (settings 自己ロード=S17 と同じ思想)。UI 側の
    # create_gradio_interface が _backend_state.server_mode を設定するのは
    # init_backend より後なので、起動時のサーバーモード分岐はここで
    # launch_config から読む(UI側の後段設定は同値の再代入になるだけ)。
    try:
        from backend.shared.launch_config import get_launch_config_value
        state.server_mode = bool(get_launch_config_value("server_mode", "enabled", False))
        if state.server_mode:
            logger.info("[ServerMode] BackendState.server_mode self-loaded from launch_config")
    except Exception as e:
        logger.warning(f"Failed to self-load server_mode from launch_config: {e}")

    # Single source of truth (ST5-S17): load the persisted feature toggles
    # directly from the settings file into BackendState. Previously the UI loaded
    # these into ``app_state`` (a second copy) and pushed them here after init;
    # that UI mirror + startup-push / shutdown-pull was removed and the backend
    # now owns its own startup load (file -> _backend_state). Runtime toggles keep
    # persisting straight to the file via feature_toggle_service
    # (update_setting), so the settings file stays the canonical store and the UI
    # reads runtime values back via get_feature_status().
    try:
        from backend.shared.settings_store import load_settings
        _settings = load_settings()
        _features = _settings.get("features", {})
        state.pc_status_enabled = _features.get("pc_status_enabled", state.pc_status_enabled)
        state.screen_capture_enabled = _features.get("screen_capture_enabled", state.screen_capture_enabled)
        state.talk_theme_enabled = _features.get("talk_theme_enabled", state.talk_theme_enabled)
        state.speechless_enabled = _features.get("speechless_enabled", state.speechless_enabled)
        state.command_execution_enabled = _features.get("command_execution_enabled", state.command_execution_enabled)
        # Mac 3-6 layer 3: unsupported on this platform -> forced OFF even if
        # the settings file says otherwise (file may come from a Windows setup).
        from backend.shared.platform_caps import is_feature_supported
        if not is_feature_supported("command_execution"):
            state.command_execution_enabled = False
        state.notes_enabled = _features.get("notes_enabled", state.notes_enabled)
        state.image_generation_enabled = _features.get("image_generation_enabled", state.image_generation_enabled)
        state.camera_capture_enabled = _features.get("camera_capture_enabled", state.camera_capture_enabled)
        state.ambient_camera_enabled = _features.get("ambient_camera_enabled", state.ambient_camera_enabled)
        state.deep_search_enabled = _features.get("deep_search_enabled", state.deep_search_enabled)
        state.elyth_enabled = _features.get("elyth_enabled", state.elyth_enabled)
        # Server mode: host-side features (PC Status / Screen Capture /
        # Command Execution) are forced OFF for the runtime — the file is
        # untouched so local mode keeps the user's choice. UI greys the
        # buttons out; feature_toggle_service rejects stale-client enables.
        if state.server_mode:
            state.pc_status_enabled = False
            state.screen_capture_enabled = False
            state.command_execution_enabled = False
        logger.info(
            f"Loaded feature toggles from settings into BackendState: "
            f"pc_status={state.pc_status_enabled}, "
            f"screen_capture={state.screen_capture_enabled}, "
            f"talk_theme={state.talk_theme_enabled}, "
            f"speechless={state.speechless_enabled}, "
            f"command_execution={state.command_execution_enabled}, "
            f"notes={state.notes_enabled}, "
            f"image_generation={state.image_generation_enabled}, "
            f"camera_capture={state.camera_capture_enabled}, "
            f"deep_search={state.deep_search_enabled}, "
            f"elyth={state.elyth_enabled}"
        )
    except Exception as e:
        logger.warning(f"Failed to load feature toggles from settings: {e}")

    # Check if Ollama service is running
    ollama_available = False
    try:
        # First check if requests library is available
        try:
            import requests
            # Set a reasonable timeout for Ollama API
            OLLAMA_API_TIMEOUT = 5.0  # seconds

            try:
                response = requests.get(f"{OLLAMA_SERVER_URL}/api/version", timeout=OLLAMA_API_TIMEOUT)
                if response.status_code == 200:
                    ollama_version = response.json().get('version', 'unknown')
                    logger.info(f"Ollama service detected: version {ollama_version}")
                    ollama_available = True
                else:
                    logger.error(f"Ollama service returned unexpected status: {response.status_code}")
                    logger.warning("LLM operations may fail. Please check if Ollama is running correctly.")
            except requests.ConnectionError:
                logger.error(f"Could not connect to Ollama service at {OLLAMA_SERVER_URL}")
                logger.warning("Please ensure Ollama is installed and running")
            except requests.Timeout:
                logger.error(f"Connection to Ollama service timed out after {OLLAMA_API_TIMEOUT}s")
                logger.warning("Ollama service appears to be running too slowly or is unresponsive")
            except RECOVERABLE_ERRORS as e:
                logger.error(f"Error when connecting to Ollama service: {e}")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                logger.exception(f"Unexpected error when connecting to Ollama service: {e}")
        except ImportError:
            logger.warning("Requests library not available, skipping Ollama service check")
            logger.warning("Install 'requests' package for better service validation")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        logger.exception(f"Critical error checking Ollama service: {e}")

    # Initialize the QueueManager
    try:
        state.queue_manager = LLMTaskQueue()
        logger.info("Initialized LLMTaskQueue for managing LLM operations.")
    except MemoryError as e:
        logger.critical(f"Insufficient memory to initialize LLMTaskQueue: {e}")
        raise RuntimeError("Backend initialization failed: Insufficient memory for task queue")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        logger.critical(f"Failed to initialize LLMTaskQueue: {e}")
        raise RuntimeError(f"Backend initialization failed: Cannot initialize LLM task queue: {e}")

    # Create necessary directories if they don't exist
    try:
        ensure_directories_exist()
        logger.info("All required directories verified/created successfully")
        
        # Clean up orphaned temp files from previous runs
        cleanup_temp_files()

        # Clean up old recovery files to prevent disk space accumulation
        cleanup_old_recovery_files()

        # Clean up any leftover camera captures from previous runs
        try:
            from backend.tools.camera_capture import cleanup_captured_images
            cleanup_captured_images()
        except Exception as e:
            logger.debug(f"Camera capture cleanup skipped: {e}")
    except RuntimeError as e:
        logger.critical(f"Failed to initialize backend directory structure: {e}")
        raise
    except PermissionError as e:
        logger.critical(f"Permission denied creating backend directories: {e}")
        raise RuntimeError("Backend initialization failed: Permission denied for directory creation")
    except OSError as e:
        logger.critical(f"OS error initializing backend directories: {e}")
        raise RuntimeError(f"Backend initialization failed: {e}")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        logger.critical(f"Unexpected error initializing backend directories: {e}")
        logger.warning("Backend may not function correctly without required directories")

    # Migrate character configs to new model_provider/model_name format
    try:
        from .conversation.character_manager import migrate_character_configs
        migration_result = migrate_character_configs()
        if migration_result["migrated"] > 0:
            logger.info(f"Character config migration: {migration_result['migrated']} configs migrated")
        if migration_result["errors"]:
            for err in migration_result["errors"]:
                logger.warning(f"Migration error: {err}")
    except Exception as e:
        logger.warning(f"Character config migration failed (non-fatal): {e}")

    # Bundled model characters (Momo / Cecilia / Stella): seeded once per
    # install so a fresh clone has characters to talk to. After the config
    # migration (they are created in the current format) and before the WAL
    # sweep (their new .db files are then swept like everyone else's).
    try:
        from .conversation.model_characters import seed_model_characters
        seed_result = seed_model_characters(atomic_file_operation)
        if seed_result["seeded"]:
            logger.info(f"Model characters seeded: {', '.join(seed_result['seeded'])}")
        for err in seed_result["errors"]:
            logger.warning(f"Model character seeding error: {err}")
    except Exception as e:
        logger.warning(f"Model character seeding failed (non-fatal): {e}")

    # Startup WAL sweep (db_housekeeping): open→checkpoint→close every
    # character DB while nothing else holds it, so a -wal left by a crash or
    # by os._exit is replayed into the .db and the -wal/-shm side files are
    # retired. After config migration (db_file_path final), before anything
    # opens a DB for real.
    try:
        from backend.memory.db_housekeeping import sweep_wal_side_files
        sweep_wal_side_files()
    except Exception as e:
        logger.warning(f"WAL sweep failed (non-fatal): {e}")

    # Ensure api_settings.json exists
    try:
        from .shared.api_settings import load_api_settings
        load_api_settings()
        logger.info("API settings loaded/initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize API settings (non-fatal): {e}")

    # Initialize the ConversationManager
    global _conversation_manager
    try:
        _conversation_manager = ConversationManager(
            backend_state=_backend_state,
            character_config_loader=load_character_config,
            character_activator=activate_character,
            find_config_file_func=_find_config_file_by_id
        )
        logger.info("ConversationManager initialized successfully")
    except Exception as e:
        logger.critical(f"Failed to initialize ConversationManager: {e}")
        raise RuntimeError(f"Backend initialization failed: Cannot initialize conversation manager: {e}")

    # Log Ollama warning at the end if service wasn't available
    if not ollama_available:
        logger.warning("⚠️ Ollama service not detected. LLM operations will likely fail.")
        logger.warning(f"Please ensure Ollama is installed and running ({OLLAMA_SERVER_URL})")

    # Start status monitoring service for WebSocket event-driven updates
    try:
        from backend.shared.status_monitor import start_status_monitoring
        start_status_monitoring()
        logger.info("Status monitoring service started - WebSocket event-driven updates enabled")
    except Exception as e:
        logger.warning(f"Failed to start status monitoring: {e} - falling back to timer-based updates")

    # Start PC Status WebSocket server for Chrome extension communication.
    # Only when the feature is enabled — a disabled feature must not keep a
    # listener open on 5002. Runtime toggling starts/stops the server in
    # feature_state.set_pc_status_enabled.
    if state.pc_status_enabled:
        try:
            from backend.tools.pc_status_manager import start_pc_status_server
            if start_pc_status_server():
                logger.info("PC Status WebSocket server started on port 5002")
            else:
                logger.warning("Failed to start PC Status WebSocket server - PC Status feature may not work")
        except Exception as e:
            logger.warning(f"Failed to start PC Status WebSocket server: {e}")
    else:
        logger.info("PC Status feature disabled - WebSocket server not started")

    # Initialize ELYTH session manager
    try:
        from backend.elyth.elyth_session_manager import get_elyth_session_manager
        elyth_mgr = get_elyth_session_manager(state)
        elyth_mgr.configure()
        logger.info("ELYTH session manager initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize ELYTH session manager: {e}")

    # Initialize YouTube session manager (comment auto-reply)
    try:
        from backend.youtube.youtube_session_manager import get_youtube_session_manager
        youtube_mgr = get_youtube_session_manager(state)
        youtube_mgr.configure()
        logger.info("YouTube session manager initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize YouTube session manager: {e}")

    # Resume any interrupted embedding-model migration (ST6 §7-4 sequel):
    # memories stamped with a different embedding model are dormant for search
    # until re-embedded; the stamp itself is the persisted migration state, so
    # a cheap read-only scan finds leftovers and restarts the background run.
    try:
        from backend.memory.embedding_migration import start_embedding_migration as _resume_migration
        _resume_migration(state)
    except Exception as e:
        logger.warning(f"Embedding migration startup scan failed (non-fatal): {e}")

    logger.info("Backend initialized.")


def get_queue_status() -> Dict[str, Any]:
    """
    Get the current status of the LLM task queue.
    
    Returns:
        Dict containing queue status information
    """
    state = _backend_state
    if state.queue_manager:
        return state.queue_manager.get_queue_status()
    return {"queue_size": 0, "is_shutdown": True, "worker_alive": False}


def get_backend_state() -> dict:
    """
    Get the current state of the backend including background tasks.

    Returns:
        Dict containing backend state information
    """
    state = _backend_state
    result = {
        "background_tasks": [],
        "queue_status": get_queue_status()
    }

    # Include background task information (locked snapshot to avoid iterating
    # a list being mutated by worker threads)
    with state.background_tasks_lock:
        tasks_snapshot = list(state.background_tasks)
    if tasks_snapshot:
        for task in tasks_snapshot:
            task_info = {
                "task_type": task.get('task_type', 'unknown'),
                "character_id": task.get('character_id', 'unknown'),
                "is_alive": task['thread'].is_alive() if 'thread' in task else False,
                "start_time": task.get('start_time', 0)
            }
            result["background_tasks"].append(task_info)

    return result


def _any_background_task_alive(task_types) -> bool:
    """Return True if any background task of the given task_types has a live thread.

    背景タスク台帳(state.background_tasks)は完了済みエントリを即時削除しない
    (掃除はキャラロード時のみ)ため、エントリ有無でなく thread.is_alive() で
    判定する。ロック下でスナップショットを取り、走査中の追記と競合しない。
    """
    state = _backend_state

    with state.background_tasks_lock:
        tasks_copy = list(state.background_tasks)

    for task in tasks_copy:
        if task.get('task_type') in task_types:
            thread = task.get('thread')
            if thread and thread.is_alive():
                return True
    return False


# 「中断させてはいけない記憶タスク」= 抽出 + relationship 更新。
# 両者は同じ LLM キューで直列に走り、どちらの中断も記憶/関係性の更新1回分を
# 失う(2026-08-16 Mac 実機: relationship 更新中だけ終了・再起動・モード切替が
# 素通りしていた=稜裁定でブロック対象に統合)。
_MEMORY_TASK_TYPES = ('extraction', 'relationship_update')


def is_memory_task_running() -> bool:
    """
    Check if a memory task that must not be interrupted is currently running
    (long-term memory extraction or relationship update).

    終了/再起動/モード切替/会話履歴リセットのガード述語。ボタンのグレーアウトと
    status_monitor の完了通知もこの1本を見る(真実源)。ターン毎の記憶保存
    (memory_save)は含めない=毎ターン数秒ブロックされて操作感が壊れるため。

    Returns:
        True if an extraction or relationship_update task is alive
    """
    return _any_background_task_alive(_MEMORY_TASK_TYPES)


def has_pending_memory_tasks() -> bool:
    """
    Check if any memory persistence task is still running.

    is_memory_task_running と同じ台帳走査で、ターン毎の記憶保存(memory_save=
    埋め込み計算を伴う)も含めて見る。会話終了後のOllama VRAM解放
    (ui/handlers/ollama_vram.py)が「埋め込み/チャットモデルをまだ使う
    背景タスクが残っていないか」を照会するための薄い読み取り。

    Returns:
        True if a memory_save, extraction or relationship_update task is alive
    """
    return _any_background_task_alive(('memory_save',) + _MEMORY_TASK_TYPES)


def reset_short_term_history(character_id: str) -> Dict[str, Any]:
    """
    Reset short-term conversation history for a character.

    This clears the recent conversation messages that are sent to the LLM in prompts,
    while preserving long-term memory (extracted memories) and talk theme.

    Used to resolve "format stuck" situations where the LLM is too heavily
    influenced by recent conversation patterns.

    Args:
        character_id: The character ID to reset history for

    Returns:
        Dict with:
        - success: bool
        - error: str (if failed)
        - message: str (if successful)
    """
    state = _backend_state

    # Validate character_id
    if not character_id:
        return {
            "success": False,
            "error": "No character ID provided"
        }

    # Check if a memory task (extraction / relationship update) is in progress
    if is_memory_task_running():
        return {
            "success": False,
            "error": "Cannot reset while a memory task (extraction / relationship update) is in progress"
        }

    # Wait for any pending memory save to complete
    cm = _get_conversation_manager()
    if cm:
        pending_thread = cm._pending_memory_save_threads.get(character_id)
        if pending_thread and pending_thread.is_alive():
            logger.info(f"Waiting for pending memory save to complete for {character_id}")
            pending_thread.join(timeout=10.0)
            if pending_thread.is_alive():
                logger.warning("Memory save still running after 10s, proceeding anyway")

    try:
        # 1. Clear MemoryManager data (if available)
        memory_manager = state.memory_managers.get(character_id)
        if memory_manager:
            success = memory_manager.clear_short_term_only()
            if not success:
                return {
                    "success": False,
                    "error": "Failed to clear short-term memory in database"
                }
            logger.info(f"Cleared MemoryManager short-term data for {character_id}")

        # 2. Clear BackendState memory buffers
        with state.memory_lock:
            if character_id in state.short_term_buffer:
                state.short_term_buffer[character_id] = []
                logger.info(f"Cleared short_term_buffer for {character_id}")

            if character_id in state.message_count_cache:
                state.message_count_cache[character_id] = 0
                logger.info(f"Reset message_count_cache for {character_id}")

        # 3. メッセージ(=添付参照側)を全消ししたので実体ファイルも消す
        # (稜裁定 2026-08-20: 参照を捨てたら実体も捨てる。残すと到達不能の
        # 孤児ファイルが永久に溜まる=旧 test4 の 17MB が前例)
        from .conversation_manager import remove_attachment_files
        remove_attachment_files(character_id)

        logger.info(f"Successfully reset short-term history for character {character_id}")
        return {
            "success": True,
            "message": "Short-term history has been reset"
        }

    except Exception as e:
        logger.error(f"Error resetting short-term history for {character_id}: {e}")
        return {
            "success": False,
            "error": f"Failed to reset history: {str(e)}"
        }


def shutdown_backend():
    """
    Cleanly shut down the backend.
    - Wait for background tasks to complete.
    - Stop the QueueManager.
    - Save any unsaved memory data.
    - Release any resources.
    - Stop status monitoring service.
    """
    logger.info("Shutting down backend...")
    
    # Stop status monitoring service
    try:
        from backend.shared.status_monitor import stop_status_monitoring
        stop_status_monitoring()
        logger.info("Status monitoring service stopped")
    except Exception as e:
        logger.warning(f"Error stopping status monitoring: {e}")

    # Stop PC status WebSocket server
    try:
        from backend.tools.pc_status_manager import stop_pc_status_server
        stop_pc_status_server()
        logger.info("PC status WebSocket server stopped")
    except Exception as e:
        logger.warning(f"Error stopping PC status server: {e}")

    # Stop ELYTH session manager before QueueManager
    try:
        from backend.elyth.elyth_session_manager import get_elyth_session_manager
        elyth_mgr = get_elyth_session_manager()
        elyth_mgr.stop()
        logger.info("ELYTH session manager stopped")
    except Exception as e:
        logger.warning(f"Error stopping ELYTH session manager: {e}")

    # Stop YouTube session manager before QueueManager (scheduler + worker)
    try:
        from backend.youtube.youtube_session_manager import get_youtube_session_manager
        youtube_mgr = get_youtube_session_manager()
        youtube_mgr.stop()
        logger.info("YouTube session manager stopped")
    except Exception as e:
        logger.warning(f"Error stopping YouTube session manager: {e}")

    state = _backend_state  # Get state singleton

    # Set shutdown flag to signal background tasks
    state.shutdown_flag.set()

    # Wait for background tasks with timeout (locked snapshot; shutdown_flag is
    # already set so no new appends are expected, but snapshot to be safe)
    with state.background_tasks_lock:
        tasks_snapshot = list(state.background_tasks)
    if tasks_snapshot:
        # Log detailed task information
        task_types = {}
        for task in tasks_snapshot:
            task_type = task.get('task_type', 'unknown')
            task_types[task_type] = task_types.get(task_type, 0) + 1

        logger.info(f"Waiting for {len(tasks_snapshot)} background tasks to complete...")
        for task_type, count in task_types.items():
            logger.info(f"  - {count} {task_type} task(s)")

        wait_start = time.time()
        # Reduced timeout since extraction is now blocked at UI level
        # 15 seconds is sufficient for cleanup tasks
        MAX_WAIT = 15  # 15 seconds for all background tasks

        for task in tasks_snapshot:
            task_type = task.get('task_type', 'unknown')
            max_wait = MAX_WAIT
            remaining = max_wait - (time.time() - wait_start)

            if remaining > 0 and task['thread'].is_alive():
                char_id = task.get('character_id', 'unknown')
                elapsed = time.time() - task.get('start_time', time.time())
                logger.info(f"Waiting for {task_type} task for character {char_id} (running for {elapsed:.1f}s, max wait: {max_wait}s)")
                task['thread'].join(timeout=remaining)
                if task['thread'].is_alive():
                    logger.warning(f"{task_type} task for {char_id} interrupted after {elapsed:.1f}s (timeout reached)")

        # Log summary
        still_running = sum(1 for task in tasks_snapshot if task['thread'].is_alive())
        if still_running > 0:
            logger.warning(f"{still_running} background tasks still running after timeout")

    # Shut down QueueManager if it exists
    queue_quiescent = True  # False = worker may still be running a task
    if state.queue_manager:
        try:
            # Cancel pending tasks but wait up to 30 seconds for current task to complete
            logger.info("Shutting down LLMTaskQueue (waiting up to 30 seconds for current task)...")
            shutdown_result = state.queue_manager.shutdown(cancel_pending=True, timeout=30.0)
            if shutdown_result:
                logger.info("LLMTaskQueue shutdown complete.")
            else:
                queue_quiescent = False
                logger.warning("LLMTaskQueue shutdown timed out after 30 seconds, some tasks may have been interrupted.")
        except RECOVERABLE_ERRORS as e:
            queue_quiescent = False
            logger.warning(f"Error during QueueManager shutdown: {e}")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            queue_quiescent = False
            logger.exception(f"Unexpected error during QueueManager shutdown: {e}")

    # Flush any unsaved data for all active characters.
    # 永続化は autocommit+WAL で書込み毎に完了済み。ここでは WAL チェックポイント
    # (store.commit) でメインDBへ確実に反映する。
    # (旧 checkpointer.save_all は存在しないAPIで毎回 AttributeError→エラーログを
    #  出すだけの死経路だった)
    for char_id, memory_manager in state.memory_managers.items():
        try:
            if hasattr(memory_manager, 'store'):
                memory_manager.store.commit()
                logger.info(f"Saved memory state for character {char_id}")
            else:
                logger.warning(f"Memory manager for {char_id} does not have expected save interface")

        except (IOError, OSError) as e:
            logger.error(f"Failed to save memory state for {char_id}: {e}")
            logger.critical(f"Character {char_id} memory data may be lost or corrupted")
            
            # Attempt emergency recovery
            try:
                # Create more robust recovery data
                recovery_data = {
                    "character_id": char_id,
                    "timestamp": time.time(),
                    "error": str(e),
                    "error_type": type(e).__name__
                }
                
                # Add messages if available
                if char_id in state.short_term_buffer and state.short_term_buffer[char_id]:
                    recovery_data["messages"] = state.short_term_buffer[char_id][-50:]  # Last 50 messages
                    recovery_data["message_count"] = len(state.short_term_buffer[char_id])
                
                # Use more robust file naming
                recovery_filename = f"{char_id}.recovery.{int(time.time())}.{os.getpid()}.json"
                recovery_path = Path(MEMORY_DIR) / recovery_filename
                
                # Write with atomic operation. encoding='utf-8' is required:
                # recovery_data contains AI messages with emoji/non-cp932 chars,
                # and the default Windows cp932 codec raises UnicodeEncodeError
                # exactly when recovery is most needed.
                with atomic_file_operation(recovery_path, "write") as temp_file:
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(recovery_data, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Created emergency recovery file: {recovery_path}")
            except Exception as recovery_err:
                logger.error(f"Failed to create recovery file: {recovery_err}")
                
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logger.exception(f"Unexpected error saving memory for {char_id}: {e}")
            # Continue with other characters even if one fails

    # Close every memory DB connection so SQLite retires the WAL side files
    # (-wal/-shm): it does so only when the LAST connection closes, and this
    # process ends with os._exit (no finalizers run). Only when quiescent — a
    # background task/queue task still alive after the waits above may be
    # mid-write, and closing under it is worse than leaving the side files
    # for the startup sweep (db_housekeeping) to retire.
    with state.background_tasks_lock:
        tasks_alive = any(
            t.get('thread') is not None and t['thread'].is_alive()
            for t in state.background_tasks
        )
    if tasks_alive or not queue_quiescent:
        logger.warning("Memory DB connections left open (background work still running); "
                       "WAL side files are retired at next startup")
    else:
        closed_stores = 0
        for char_id, memory_manager in state.memory_managers.items():
            store = getattr(memory_manager, 'store', None)
            if store is None or not hasattr(store, 'close_all'):
                continue
            try:
                store.close_all()
                closed_stores += 1
            except Exception as e:
                logger.warning(f"Failed to close memory DB connections for {char_id}: {e}")
        logger.info(f"Closed memory DB connections ({closed_stores} stores)")

    logger.info("Backend shutdown complete.")


# --------------------------------------------------------------------------
# Character Management
# --------------------------------------------------------------------------
@standardize_response
def load_character_list() -> List[Dict[str, Any]]:
    """Scan character_configs/ for per-character metadata. Delegates to SL4 character_manager."""
    from backend.conversation.character_manager import load_character_list as _impl
    return _impl()


@standardize_response
@validate_input({
    'character_info': validate_dict_param
})
def create_character(character_info: Dict[str, Any]) -> str:
    """Create a new character. Delegates to SL4 character_manager (atomic_file_operation injected)."""
    from backend.conversation.character_manager import create_character as _impl
    return _impl(character_info, atomic_file_operation)


@standardize_response
@validate_input({
    'character_id': validate_character_id,
    'updated_info': validate_dict_param
})
def edit_character(character_id: str, updated_info: Dict[str, Any]) -> None:
    """Edit an existing character. Delegates to SL4 character_manager (state + collaborators injected)."""
    from backend.conversation.character_manager import edit_character as _impl
    result = _impl(_backend_state, character_id, updated_info, atomic_file_operation, activate_character)
    # 編集で可用性(Motionフォルダ/モデルprovider等)が変わりうる: 再ロードを
    # 省いた編集でも再計算して全クライアントへ再配信(remove_character と対称・
    # 非アクティブ編集でも再配信は無害)
    enforce_feature_availability()
    return result


@standardize_response
@validate_input({
    'character_id': validate_character_id
})
def remove_character(character_id: str) -> None:
    """Delete a character and its files. Delegates to SL4 character_manager (state injected)."""
    from backend.conversation.character_manager import remove_character as _impl
    result = _impl(_backend_state, character_id)
    # アクティブキャラの削除で可用性(Ollama連動グレー等)が変わる:
    # 再計算して全クライアントへ再配信(activate_character と対称。
    # 非アクティブ削除でも再配信は無害)
    enforce_feature_availability()
    return result


@standardize_response
@validate_input({
    'character_id': validate_character_id
})
def load_character_config(character_id: str) -> Dict[str, Any]:
    """Load a character config (with version migration). Delegates to SL4 character_manager."""
    from backend.conversation.character_manager import load_character_config_managed as _impl
    return _impl(character_id)


# --------------------------------------------------------------------------
# Conversation & Memory
# --------------------------------------------------------------------------
@standardize_response
@validate_input({
    'character_id': validate_character_id,
    'preserve_conversation': validate_bool_param
})
def activate_character(character_id: str, preserve_conversation: bool = False) -> Dict[str, Any]:
    """Activate a character for conversation. Delegates to SL4 character_manager
    (state + wrapped load_character_config + stop_conversation injected)."""
    from backend.conversation.character_manager import activate_character as _impl
    result = _impl(_backend_state, character_id, preserve_conversation, load_character_config, stop_conversation)
    # キャラ切替で機能可用性(Ollama連動グレー等)が変わる: 利用不能になった
    # 機能は強制OFFして全クライアントへ再配信
    if isinstance(result, dict) and result.get("success", True):
        enforce_feature_availability()
    return result


@standardize_response
@validate_input({
    'character_id': validate_character_id
})
def is_llm_ready(character_id: str) -> bool:
    """Return whether the character's LLM instance is created and cached.

    LLM生成失敗は有効化を止めず遅延再生成に委譲される(2026-08-03)ため、
    「キャラは開けたがLLMサービス未接続」をUIがキャラ読み込み時点で
    知るための窓口。False ならLLMサービス(Ollama等)に届いていない。
    """
    return character_id in _backend_state.active_llm_cache


# --------------------------------------------------------------------------
# Conversation Management - Thin Wrappers for External API
# --------------------------------------------------------------------------
def generate_reply(user_text: str, character_id: Optional[str] = None,
                   images: list = None, documents: list = None,
                   is_auto_prompt: bool = False) -> Dict[str, Any]:
    """Produce an AI response while maintaining multi-turn memory.

    This is a thin wrapper for backward compatibility. The actual implementation
    is in ConversationManager.
    """
    return _get_conversation_manager().generate_reply(user_text, character_id, images=images, documents=documents, is_auto_prompt=is_auto_prompt)

def start_conversation(character_id: Optional[str] = None) -> Dict[str, Any]:
    """Start a conversation session with the active or specified character.
    
    This is a thin wrapper for backward compatibility. The actual implementation
    is in ConversationManager.
    """
    return _get_conversation_manager().start_conversation(character_id)

def stop_conversation() -> Dict[str, Any]:
    """Stop the current conversation session.

    This is a thin wrapper for backward compatibility. The actual implementation
    is in ConversationManager.
    """
    if _backend_state:
        _backend_state.image_buffer.clear()
        # Also clear document buffer: an unsent attached document would
        # otherwise leak into the first message of the next conversation.
        _backend_state.document_buffer.clear()
    return _get_conversation_manager().stop_conversation()

def get_conversation_history(character_id: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """Retrieve the recent conversation history for the specified character.
    
    This is a thin wrapper for backward compatibility. The actual implementation
    is in ConversationManager.
    
    Args:
        character_id: Character ID (uses active character if None)
        limit: Maximum number of messages to return
        
    Returns:
        Dict with success status, history list, and any warnings/errors
    """
    return _get_conversation_manager().get_conversation_history(character_id, limit)


def get_last_llm_prompt() -> Dict[str, Any]:
    """Get the last LLM prompt for display in UI.

    Returns the raw JSON request that was sent to Ollama or API providers.
    Compares timestamps and returns whichever is more recent.

    Returns:
        Dict with success status and prompt information
    """
    from .llm.ollama_integration import get_last_ollama_request_json
    from .llm.api_integration import get_last_api_request_json

    ollama_data = get_last_ollama_request_json()
    api_data = get_last_api_request_json()

    # Pick the most recent source (ISO timestamps compare correctly as strings)
    ollama_ts = ollama_data.get("timestamp") or ""
    api_ts = api_data.get("timestamp") or ""

    if api_ts >= ollama_ts and api_data["json"]:
        request_data = api_data
        # APIはusage実測1本（常にフル値）。応答未着はpending。
        actual = api_data.get("actual_tokens")
        tokens = {
            "provider": "api",
            "count": actual,
            "source": "actual" if actual else None,
            "pending": not actual,
            "num_ctx": None,
            "model": api_data.get("model_label"),
        }
    elif ollama_data["json"]:
        request_data = ollama_data
        # Ollamaは「実測と推定の大きい方」を1本表示（稜裁定 2026-08-14・
        # 選択規則の真実源は shared/prompt_token_display）
        from .shared.prompt_token_display import select_ollama_display
        count, source = select_ollama_display(
            ollama_data.get("est_tokens"), ollama_data.get("actual_tokens"))
        tokens = {
            "provider": "ollama",
            "count": count,
            "source": source,
            "pending": False,
            "num_ctx": ollama_data.get("num_ctx"),
            "model": None,
        }
    else:
        return {
            "success": True,
            "prompt": "No prompts generated yet in this session.",
            "timestamp": None,
            "character": None,
            "tokens": None
        }

    return {
        "success": True,
        "prompt": request_data["json"],
        "timestamp": request_data["timestamp"],
        "character": _backend_state.active_character_id,
        "tokens": tokens
    }


# --------------------------------------------------------------------------
# Memory Data Access Functions
# --------------------------------------------------------------------------
@standardize_response
def get_character_memory_data(character_id: str) -> Dict[str, Any]:
    """
    Get all memory data for a character for the History page.

    Thin delegate — body moved to memory_manager.get_character_memory_data
    (B10 / SL3 Memory). App-layer state and the character config/list loaders
    are injected so the data-shaping logic lives in its Memory home.
    """
    from backend.memory.memory_manager import get_character_memory_data as _impl
    return _impl(
        _get_conversation_manager().state,
        character_id,
        load_character_config,
        load_character_list,
    )


# --------------------------------------------------------------------------
# Memory CRUD API (Phase 4)
# --------------------------------------------------------------------------

@standardize_response
@validate_input({'character_id': validate_character_id})
def add_memory(character_id: str, category: str, content: str) -> Dict[str, Any]:
    """Manually add a memory entry for a character.

    Thin delegate — body moved to memory_manager.add_memory_entry (B10 / SL3).
    """
    from backend.memory.memory_manager import add_memory_entry
    return add_memory_entry(
        _get_conversation_manager().state, character_id, category, content,
        load_character_config,
    )


@standardize_response
@validate_input({'character_id': validate_character_id})
def edit_memory(character_id: str, memory_id: str, content: str, category: str = None) -> Dict[str, Any]:
    """Edit a memory entry.

    Thin delegate — body moved to memory_manager.edit_memory_entry (B10 / SL3).
    """
    from backend.memory.memory_manager import edit_memory_entry
    return edit_memory_entry(
        _get_conversation_manager().state, character_id, memory_id, content, category,
        load_character_config=load_character_config,
    )


@standardize_response
@validate_input({'character_id': validate_character_id})
def delete_memory(character_id: str, memory_id: str) -> Dict[str, Any]:
    """Delete a memory entry.

    Thin delegate — body moved to memory_manager.delete_memory_entry (B10 / SL3).
    """
    from backend.memory.memory_manager import delete_memory_entry
    return delete_memory_entry(
        _get_conversation_manager().state, character_id, memory_id,
        load_character_config,
    )


@standardize_response
@validate_input({'character_id': validate_character_id})
def pin_memory(character_id: str, memory_id: str, pinned: bool) -> Dict[str, Any]:
    """Set pinned status of a memory entry.

    Thin delegate — body moved to memory_manager.pin_memory_entry (B10 / SL3).
    """
    from backend.memory.memory_manager import pin_memory_entry
    return pin_memory_entry(
        _get_conversation_manager().state, character_id, memory_id, pinned,
        load_character_config,
    )


@standardize_response
def start_embedding_migration() -> Dict[str, Any]:
    """Re-embed memories stamped with a different embedding model (ST6 §7-4 sequel).

    Thin delegate — body in embedding_migration.start_embedding_migration
    (SL3 Memory home); app-layer state is injected. Returns the pending count
    so the settings UI can report "migration started (N entries)".
    """
    from backend.memory.embedding_migration import start_embedding_migration as _impl
    return _impl(_get_conversation_manager().state)


# --------------------------------------------------------------------------
# Listing Local Resources
# --------------------------------------------------------------------------
@standardize_response
def list_ollama_models() -> List[str]:
    """
    Return a list of installed Ollama model names by querying the local Ollama server.
    Delegates to character_manager to avoid code duplication.
    """
    return _list_ollama_models_impl()


@standardize_response
def list_tts_models() -> List[str]:
    """
    Return a list of available Style-Bert-VITS2 models by scanning sbv2_models/ subfolders
    for .safetensors (or .pth), config.json, and style_vectors.npy.
    Delegates to character_manager to avoid code duplication.
    """
    return _list_tts_models_impl()


@standardize_response
def list_character_icons() -> List[str]:
    """
    Return a list of image files in character_icons/.
    Delegates to character_manager to avoid code duplication.
    """
    return _list_character_icons_impl()


@standardize_response
def list_stt_models() -> List[str]:
    """
    Return a list of available faster-whisper language settings.
    Currently supports English (en) and Japanese (ja) as per requirements.
    Delegates to character_manager to avoid code duplication.
    """
    return _list_stt_languages_impl()


# --------------------------------------------------------------------------
# Talk Theme Management APIs
# --------------------------------------------------------------------------

@standardize_response
def get_talk_theme(character_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get the current talk theme for a character.

    Thin delegate — body moved to character_manager.get_talk_theme
    (B10 / SL4 Character). The backend state container (active-character owner)
    is injected so the talk-theme logic lives next to its character-config home.
    """
    from backend.conversation.character_manager import get_talk_theme as _impl
    return _impl(_backend_state, character_id)


@standardize_response
def update_talk_theme(theme: str, character_id: Optional[str] = None, source: str = "user") -> Dict[str, Any]:
    """
    Update the talk theme for a character.

    Thin delegate — body moved to character_manager.update_talk_theme
    (B10 / SL4 Character).
    """
    from backend.conversation.character_manager import update_talk_theme as _impl
    return _impl(_backend_state, theme, character_id, source)


@standardize_response
def clear_talk_theme(character_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Clear the talk theme for a character.

    Thin delegate — body moved to character_manager.clear_talk_theme
    (B10 / SL4 Character).
    """
    from backend.conversation.character_manager import clear_talk_theme as _impl
    return _impl(_backend_state, character_id)


def set_pc_status_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle PC Status feature on/off.

    Thin delegate — body moved to feature_state.set_pc_status_enabled
    (B10 / SL15 Settings). The backend state container (toggle-flag owner) is
    injected so the runtime-flag logic lives in its shared Settings home.
    """
    from backend.shared.feature_state import set_pc_status_enabled as _impl
    return _impl(_backend_state, enabled)


def set_screen_capture_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Screen Capture feature on/off.

    Thin delegate — body moved to feature_state.set_screen_capture_enabled
    (B10 / SL15 Settings).
    """
    from backend.shared.feature_state import set_screen_capture_enabled as _impl
    return _impl(_backend_state, enabled)


def set_talk_theme_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Talk Theme feature on/off.

    Thin delegate — body moved to feature_state.set_talk_theme_enabled
    (B10 / SL15 Settings).
    """
    from backend.shared.feature_state import set_talk_theme_enabled as _impl
    return _impl(_backend_state, enabled)


def set_speechless_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Speechless mode on/off.

    Thin delegate — body moved to feature_state.set_speechless_enabled
    (B10 / SL15 Settings).
    """
    from backend.shared.feature_state import set_speechless_enabled as _impl
    return _impl(_backend_state, enabled)


def set_notes_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Notes feature on/off.

    Thin delegate — body moved to feature_state.set_notes_enabled
    (B10 / SL15 Settings).
    """
    from backend.shared.feature_state import set_notes_enabled as _impl
    return _impl(_backend_state, enabled)


def set_command_execution_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Command Execution feature on/off.

    Thin delegate — body moved to feature_state.set_command_execution_enabled
    (B10 / SL15 Settings). The conversation-manager accessor is injected for the
    rate-limit tracker reset.
    """
    from backend.shared.feature_state import set_command_execution_enabled as _impl
    return _impl(_backend_state, enabled, _get_conversation_manager)


def set_image_generation_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Image Generation feature on/off.

    Thin delegate — body moved to feature_state.set_image_generation_enabled
    (B10 / SL15 Settings). The conversation-manager accessor is injected for the
    rate-limit tracker reset.
    """
    from backend.shared.feature_state import set_image_generation_enabled as _impl
    return _impl(_backend_state, enabled, _get_conversation_manager)


def set_camera_capture_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Camera Capture feature on/off.

    Thin delegate — body moved to feature_state.set_camera_capture_enabled
    (B10 / SL15 Settings). The conversation-manager accessor is injected for the
    rate-limit tracker reset.
    """
    from backend.shared.feature_state import set_camera_capture_enabled as _impl
    return _impl(_backend_state, enabled, _get_conversation_manager)


def set_ambient_camera_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Live Camera (auto-attach on send) feature on/off.

    Thin delegate — body lives in feature_state.set_ambient_camera_enabled
    (same shape as the other feature toggles).
    """
    from backend.shared.feature_state import set_ambient_camera_enabled as _impl
    return _impl(_backend_state, enabled)


def set_deep_search_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle Deep Search feature on/off.

    Thin delegate — body moved to feature_state.set_deep_search_enabled
    (B10 / SL15 Settings). The conversation-manager accessor is injected for the
    rate-limit tracker resets.
    """
    from backend.shared.feature_state import set_deep_search_enabled as _impl
    return _impl(_backend_state, enabled, _get_conversation_manager)


def set_elyth_enabled(enabled: bool) -> Dict[str, Any]:
    """
    Toggle ELYTH feature on/off for normal conversation mode.

    Thin delegate — body moved to feature_state.set_elyth_enabled
    (B10 / SL15 Settings).
    """
    from backend.shared.feature_state import set_elyth_enabled as _impl
    return _impl(_backend_state, enabled)


def get_feature_status() -> Dict[str, Any]:
    """
    Get current feature status.

    Thin delegate — body moved to feature_state.get_feature_status
    (B10 / SL15 Settings). Reads the toggle flags from the injected state.
    """
    from backend.shared.feature_state import get_feature_status as _impl
    return _impl(_backend_state)


def _feature_availability_provider() -> Dict[str, Any]:
    """feature_availability レジストリへ登録する所有側 provider。

    アクティブキャラのプロバイダ/ELYTHキーとグローバルAPI設定を集めて
    純関数 compute_availability に渡す(shared からの上向き import を
    避けるための所有側組み立て。runtime_state provider と同じ形)。
    """
    from backend.shared.feature_availability import (
        IMAGE_ATTACH, MOTION_APPEAR, REASON_MOTION_FOLDER_MISSING,
        REASON_NO_CHARACTER, REASON_NO_MOTION_FOLDER, REASON_NO_OPENAI_KEY,
        REASON_OLLAMA_NO_VISION, REASON_OLLAMA_UNKNOWN, STT_OPENAI,
        compute_availability)
    from backend.shared.api_settings import load_api_settings, get_image_generation_model

    model_provider = None
    model_name = ""
    elyth_key_set = False
    motion_folder = ""
    char_id = _backend_state.active_character_id if _backend_state else None
    if char_id:
        try:
            config = load_character_config(char_id)
            if isinstance(config, dict) and "result" in config:
                config = config.get("result") or {}
            model_provider = config.get("model_provider", "ollama")
            model_name = (config.get("model_name", "")
                          or config.get("ollama_model_name", ""))
            elyth_key_set = bool(config.get("elyth_api_key"))
            motion_folder = (config.get("motion_pngtuber_folder") or "").strip()
        except Exception:
            # 「読めない」≠「未選択」: 未選択はブロックなし(2026-08-02)の
            # ため None に倒すと config 破損だけで全ゲートが開く(fail-open)。
            # 安全側(閉)の ollama 扱いへフォールバックする(モデル名不明=
            # capability判定不能=REASON_OLLAMA_UNKNOWN で全軸閉)。
            model_provider = "ollama"
            model_name = ""

    # Ollamaキャラはモデルのcapability(tools/vision)で機能を判定する
    # (2026-08-11 稜裁定=2軸独立。キャッシュ命中ならHTTPなし・照会失敗は
    # get_caps側の負キャッシュが吸収)
    ollama_caps = None
    if model_provider == "ollama" and model_name:
        try:
            from backend.llm.ollama_capabilities import get_caps
            ollama_caps = get_caps(model_name)
        except Exception:
            logger.exception("Ollama capability lookup failed; failing closed")
            ollama_caps = None

    settings = load_api_settings()
    google_key_set = bool(settings.get("google", {}).get("api_key"))
    ig_provider, ig_model = get_image_generation_model()
    reasons = compute_availability(
        model_provider, elyth_key_set, google_key_set,
        bool(ig_provider and ig_model), ollama_caps=ollama_caps)
    # 疑似機能: STTエンジン選択の「OpenAI API」(キャラ非依存・キー有無のみ)
    openai_key_set = bool(settings.get("openai", {}).get("api_key"))
    reasons[STT_OPENAI] = None if openai_key_set else REASON_NO_OPENAI_KEY
    # 疑似機能: 📎画像添付(vision軸。キャラ未選択は塞がない=添付はキャラ
    # 非依存の操作で、後からvision対応キャラを選べば有効に使えるため)
    reasons[IMAGE_ATTACH] = None
    if model_provider == "ollama":
        caps = ollama_caps or {}
        if not caps.get("known"):
            reasons[IMAGE_ATTACH] = REASON_OLLAMA_UNKNOWN
        elif not caps.get("vision"):
            reasons[IMAGE_ATTACH] = REASON_OLLAMA_NO_VISION
    # 疑似機能: Appear(MotionPNGPlayer起動)。Motionフォルダ未設定/実体なし/
    # キャラ未選択は起動が必ず失敗する=入口でグレーアウト(稜GO 2026-08-15)。
    # 表示中(Disappearモード)の無効化除外はJS側が担う。
    if not char_id:
        reasons[MOTION_APPEAR] = REASON_NO_CHARACTER
    elif not motion_folder:
        reasons[MOTION_APPEAR] = REASON_NO_MOTION_FOLDER
    else:
        try:
            from pathlib import Path
            from backend.tools.motion_pngtuber_launcher import resolve_character_folder
            reasons[MOTION_APPEAR] = (
                None if Path(resolve_character_folder(motion_folder)).is_dir()
                else REASON_MOTION_FOLDER_MISSING)
        except Exception:
            reasons[MOTION_APPEAR] = REASON_MOTION_FOLDER_MISSING
    return reasons


from backend.shared.feature_availability import register_availability_provider as _register_avail  # noqa: E402
_register_avail(_feature_availability_provider)


def publish_feature_availability() -> None:
    """機能可用性+トグル状態の現在値を全クライアントへWS配信する(best-effort)。

    JS側は utility panel のボタンへ disabled/横線/tooltip とON/OFF表示を
    再適用する(feature_status同梱は enforce の強制OFFを全クライアントへ
    反映するため)。
    """
    try:
        from backend.shared.feature_availability import get_block_reasons
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("feature_availability", data={
            "reasons": get_block_reasons(),
            "feature_status": get_feature_status(),
        })
    except Exception as e:
        logger.debug(f"feature_availability publish failed: {e}")


def invalidate_ollama_llm_cache() -> None:
    """num_ctx設定変更時: キャッシュ済み DirectOllamaChat を全て無効化する。

    num_ctx は構築時にLLMインスタンスへ焼き込まれるため、破棄して次の
    _ensure_llm_in_cache で新値により再作成させる(C4.6。次の生成でOllamaの
    モデル再ロードが1回走る)。APIプロバイダのインスタンスは触らない。
    """
    if _backend_state is None:
        return
    from backend.llm.ollama_integration import DirectOllamaChat
    stale = [cid for cid, llm in _backend_state.active_llm_cache.items()
             if isinstance(llm, DirectOllamaChat)]
    for cid in stale:
        del _backend_state.active_llm_cache[cid]
        logger.info(f"Ollama LLM cache invalidated for '{cid}' (num_ctx changed)")


def enforce_feature_availability() -> None:
    """利用不能になった機能を強制OFFし、可用性+トグル状態をWS配信する。

    キャラ切替等で前提を失った機能が「ON+横線」で残ると有効に見えて紛らわしい
    (稜実機 2026-07-25)。ブロックされた機能はOFFへ落とす(永続込み。前提が
    戻ってもOFFのまま=再有効化はユーザー操作)。キャラ切替・APIキー/
    画像生成モデル保存など前提条件が変わった直後に呼ぶ。
    """
    try:
        from backend.shared.feature_availability import GATED_FEATURES, get_availability
        from backend.shared.feature_commands import dispatch_feature_toggle
        import backend.shared.feature_toggle_service  # noqa: F401  ハンドラ自己登録(冪等)
        reasons = get_availability()
        status = get_feature_status()
        for feature in GATED_FEATURES:
            if reasons.get(feature) and status.get(f"{feature}_enabled"):
                dispatch_feature_toggle(feature, False)
                logger.info(
                    f"Feature '{feature}' force-disabled (unavailable: {reasons[feature]})")
    except Exception as e:
        logger.warning(f"feature availability enforcement failed: {e}")
    publish_feature_availability()


# ============================================================================
# Tuning Parameter Functions
# ============================================================================

def get_character_tuning(character_id: str) -> Dict[str, Any]:
    """
    Get tuning parameters for a character.

    Thin delegate — body moved to character_manager.get_character_tuning
    (B10 / SL4 Character).
    """
    from backend.conversation.character_manager import get_character_tuning as _impl
    return _impl(character_id)


@standardize_response
def save_character_tuning(character_id: str, tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Save tuning parameters for a character.

    Thin delegate — body moved to character_manager.save_character_tuning
    (B10 / SL4 Character). The backend state container (LLM cache owner) is
    injected so the tuning logic lives in its Character home.
    """
    from backend.conversation.character_manager import save_character_tuning as _impl
    return _impl(_backend_state, character_id, tuning)


def apply_tuning_to_all_characters(tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply tuning parameters to all characters and save as defaults.

    Thin delegate — body moved to character_manager.apply_tuning_to_all_characters
    (B10 / SL4 Character). The backend state container and the character-list
    loader are injected so the tuning logic lives in its Character home.
    """
    from backend.conversation.character_manager import apply_tuning_to_all_characters as _impl
    return _impl(_backend_state, tuning, load_character_list)


# All config file operations are now imported from character_manager module