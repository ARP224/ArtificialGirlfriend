"""
ui/state.py

Centralized application state management for the Artificial Girlfriend UI.
This module contains the AppState class and related state management utilities.
"""

import logging
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass, field
import numpy as np
import threading

from .constants import MAX_LOG_MESSAGES, MAX_CHAT_HISTORY_SIZE
from .data_state import DataState
from .audio_state import AudioState
from .auto_prompt_state import AutoPromptState
from .talk_theme_state import TalkThemeState
from .character_state import CharacterState
from .display_state import DisplayState
from .prompt_log_state import PromptLogState
from .ui_runtime_state import UIRuntimeState
from .chat_cache_state import ChatCacheState
from .status_state import StatusState

# Global logger configuration
logger = logging.getLogger(__name__)

# Thread-local storage to prevent circular logging
_thread_local = threading.local()


class UILogHandler(logging.Handler):
    """
    Custom logging handler that captures log messages for display in the UI.
    Prevents circular logging by using thread-local storage.
    """
    
    def __init__(self, app_state_ref):
        super().__init__()
        self.app_state_ref = app_state_ref
        
    def emit(self, record):
        """
        Emit a log record by adding it to the UI log messages.
        
        Args:
            record: LogRecord instance containing the log message
        """
        # Check if we're already in the process of adding a log message
        # to prevent circular logging
        if getattr(_thread_local, 'in_add_log_message', False):
            return
            
        try:
            # Get the app_state instance
            app_state = self.app_state_ref
            if app_state is None:
                return
                
            # Format the log message
            level_name = record.levelname.lower()
            # Map logging levels to our internal levels
            level_map = {
                'debug': 'debug',
                'info': 'info',
                'warning': 'warning',
                'warn': 'warning',
                'error': 'error',
                'critical': 'error'
            }
            level = level_map.get(level_name, 'info')
            
            # Get the formatted message
            message = self.format(record)
            
            # Since we're using a simple formatter in app.py ("%(name)s - %(message)s"),
            # the message should already be clean without timestamp/level duplication
            
            # Add to UI log messages directly (bypass add_log_message to avoid recursion)
            log_entry = f"[{level.upper()}] {message}"
            
            # Set flag to prevent recursion
            _thread_local.in_add_log_message = True
            try:
                # Add new message and trim to max size
                app_state.log_messages.append(log_entry)
                if len(app_state.log_messages) > MAX_LOG_MESSAGES:
                    app_state.log_messages = app_state.log_messages[-MAX_LOG_MESSAGES:]
            finally:
                _thread_local.in_add_log_message = False
                
        except Exception:
            # Silently ignore any errors to prevent logging system failure
            pass


@dataclass
class AppState:
    """Centralized application state management."""

    # UI runtime flags (ST5-S12: owned by UIRuntimeState; legacy
    # conversation_started / response_generating / current_request_id
    # / _needs_ui_update via compat properties below). The dead write-only
    # ``current_page`` field used to sit here; the real page navigation is the
    # Gradio State component in ui/app.py, this field had zero readers, so it was
    # removed as dead code in ST5-S12 (稜 decision 2026-06-28).
    ui_runtime: UIRuntimeState = field(default_factory=UIRuntimeState)

    # Audio State (ST5-S7: owned by AudioState; legacy names via compat properties below)
    audio: AudioState = field(default_factory=AudioState)

    # Display Settings (ST5-S12: owned by DisplayState; legacy chat_font_size via
    # compat property below)
    display: DisplayState = field(default_factory=DisplayState)

    # Character State (ST5-S12: owned by CharacterState; legacy active_character_*
    # via compat properties below)
    character: CharacterState = field(default_factory=CharacterState)

    # Data Collections (ST5-S6: owned by DataState; legacy names via compat properties below)
    data: DataState = field(default_factory=DataState)

    # Subsystem status / history-load state (ST5-S12: owned by StatusState; legacy
    # backend_available / audio_input_available / audio_output_available /
    # is_loading_history / history_load_error via compat properties below). The dead
    # write-only ``initialization_complete`` flag used to sit here; it had zero
    # readers and was removed as dead code in ST5-S12 (稜 decision 2026-06-28).
    status: StatusState = field(default_factory=StatusState)

    # current_request_id and _needs_ui_update moved to UIRuntimeState (ST5-S12);
    # reach them via the legacy app_state.current_request_id /
    # app_state._needs_ui_update compat properties below.

    # Chat HTML caching (ST5-S12: owned by ChatCacheState; legacy _cached_chat_html /
    # _chat_history_version via compat properties below)
    chat_cache: ChatCacheState = field(default_factory=ChatCacheState)
    
    # Prompt logging for display (ST5-S12: owned by PromptLogState; legacy
    # last_prompt_* via compat properties below)
    prompt_log: PromptLogState = field(default_factory=PromptLogState)
    
    # Auto prompt feature (ST5-S8: owned by AutoPromptState; legacy names via
    # compat properties below)
    auto_prompt: AutoPromptState = field(default_factory=AutoPromptState)

    # Talk Theme management (ST5-S9: owned by TalkThemeState; legacy names via
    # compat properties below)
    talk_theme_state: TalkThemeState = field(default_factory=TalkThemeState)

    # PC Status: the UI-side ``pc_status_error`` / ``pc_status_error_timestamp``
    # fields used to sit here but were never read or displayed anywhere — the live
    # PC-status error handling lives in backend/tools/pc_status_manager.py (its own
    # ``errors`` list / ``chrome_error`` / logging). They were a dead orphan UI-side
    # copy and were removed as dead code in ST5-S12 (稜 decision 2026-06-28). Do not
    # re-add UI-side PC-status error state here.

    # Server mode flag (set by create_gradio_interface). Kept as a raw AppState
    # field (ST5-S12, 稜 decision 2026-06-28, option A): a single startup-set
    # deployment flag, left as a genuine top-level container field rather than
    # wrapped in a one-field owner object (avoids gold-plating).
    server_mode_enabled: bool = False

    # Feature toggles: REMOVED in ST5-S17 (稜 decision 2026-06-28, option A). The
    # UI used to keep a mirror copy of the backend feature flags here (FeatureState),
    # synced via ui/app.py's startup-push / shutdown-pull. That dual copy was a pure
    # file<->backend staging buffer (grep proved no drawing/runtime code read it: the
    # UI reads runtime feature values via backend.get_feature_status()). The mirror,
    # the push/pull, and ui/feature_state.py were all removed; _backend_state.features
    # (backend/shared/feature_state.py) is now the single source of truth and the
    # backend loads/persists it directly through the settings file. Do not re-add a
    # UI-side feature mirror here; reach backend.get_feature_status() instead.

    # Command execution: the toggle flag ``command_execution_enabled`` lived on the
    # removed UI feature mirror (above). The approval *runtime* handshake state
    # (command_approval_pending/event/result, command_pending_info,
    # _pending_interruption, command_steps_queue) used to be mirrored here as a
    # dead UI-side copy: grep proved it was never read or written through
    # ``app_state`` anywhere — the live handshake runs entirely on the backend
    # ``_backend_state`` (CommandState, ST5-S2). Removed as dead code in ST5-S11
    # (稜 decision 2026-06-28). Do not re-add UI-side approval state here; reach
    # ``backend.backend._backend_state`` instead.

    # --- ST5-S6: data cluster compat shim (delegates to self.data) ---
    # These keep the legacy ``app_state.<field>`` access paths working while
    # ownership of the collections moves to DataState. Defined as properties
    # (no annotation) so the @dataclass machinery does not treat them as fields.
    @property
    def chat_history(self) -> List[Tuple[str, str, bool, str, list]]:
        return self.data.chat_history

    @chat_history.setter
    def chat_history(self, value: List[Tuple[str, str, bool, str, list]]) -> None:
        self.data.chat_history = value

    @property
    def log_messages(self) -> List[str]:
        return self.data.log_messages

    @log_messages.setter
    def log_messages(self, value: List[str]) -> None:
        self.data.log_messages = value

    # --- ST5-S7: audio cluster compat shim (delegates to self.audio) ---
    # Same pattern as the data shim above: ownership of the audio knobs moves to
    # AudioState while the legacy ``app_state.<field>`` access paths keep working
    # via these properties (no annotation, so @dataclass ignores them as fields).
    @property
    def beep_enabled(self) -> bool:
        return self.audio.beep_enabled

    @beep_enabled.setter
    def beep_enabled(self, value: bool) -> None:
        self.audio.beep_enabled = value

    @property
    def beep_volume(self) -> float:
        return self.audio.beep_volume

    @beep_volume.setter
    def beep_volume(self, value: float) -> None:
        self.audio.beep_volume = value

    @property
    def tts_volume(self) -> float:
        return self.audio.tts_volume

    @tts_volume.setter
    def tts_volume(self, value: float) -> None:
        self.audio.tts_volume = value

    @property
    def start_beep(self) -> Optional[np.ndarray]:
        return self.audio.start_beep

    @start_beep.setter
    def start_beep(self, value: Optional[np.ndarray]) -> None:
        self.audio.start_beep = value

    @property
    def stop_beep(self) -> Optional[np.ndarray]:
        return self.audio.stop_beep

    @stop_beep.setter
    def stop_beep(self, value: Optional[np.ndarray]) -> None:
        self.audio.stop_beep = value

    @property
    def recording_start_time(self) -> Optional[float]:
        return self.audio.recording_start_time

    @recording_start_time.setter
    def recording_start_time(self, value: Optional[float]) -> None:
        self.audio.recording_start_time = value

    # --- ST5-S8: auto-prompt cluster compat shim (delegates to self.auto_prompt) ---
    # Same pattern as the data/audio shims above: ownership of the auto-prompt
    # knobs moves to AutoPromptState while the legacy ``app_state.auto_prompt_<field>``
    # access paths keep working via these properties (no annotation, so @dataclass
    # ignores them as fields). The owning attribute is ``self.auto_prompt`` and the
    # legacy field names drop the ``auto_prompt_`` prefix on the owner.
    @property
    def auto_prompt_enabled(self) -> bool:
        return self.auto_prompt.enabled

    @auto_prompt_enabled.setter
    def auto_prompt_enabled(self, value: bool) -> None:
        self.auto_prompt.enabled = value

    @property
    def auto_prompt_timer_duration(self) -> int:
        return self.auto_prompt.timer_duration

    @auto_prompt_timer_duration.setter
    def auto_prompt_timer_duration(self, value: int) -> None:
        self.auto_prompt.timer_duration = value

    @property
    def auto_prompt_timer_active(self) -> bool:
        return self.auto_prompt.timer_active

    @auto_prompt_timer_active.setter
    def auto_prompt_timer_active(self, value: bool) -> None:
        self.auto_prompt.timer_active = value

    @property
    def auto_prompt_ja(self) -> str:
        return self.auto_prompt.ja

    @auto_prompt_ja.setter
    def auto_prompt_ja(self, value: str) -> None:
        self.auto_prompt.ja = value

    @property
    def auto_prompt_en(self) -> str:
        return self.auto_prompt.en

    @auto_prompt_en.setter
    def auto_prompt_en(self, value: str) -> None:
        self.auto_prompt.en = value

    # --- ST5-S9: talk-theme cluster compat shim (delegates to self.talk_theme_state) ---
    # Same pattern as the data/audio/auto-prompt shims above: ownership of the
    # talk-theme knobs moves to TalkThemeState while the legacy ``app_state._<field>``
    # access paths keep working via these properties (no annotation, so @dataclass
    # ignores them as fields). The owning attribute is ``self.talk_theme_state`` and
    # the legacy ``_talk_theme_``/``_theme_panel_`` private names map onto its
    # ``version`` / ``cached_theme`` / ``panel_enabled`` fields.
    @property
    def _talk_theme_version(self) -> int:
        return self.talk_theme_state.version

    @_talk_theme_version.setter
    def _talk_theme_version(self, value: int) -> None:
        self.talk_theme_state.version = value

    @property
    def _cached_talk_theme(self) -> str:
        return self.talk_theme_state.cached_theme

    @_cached_talk_theme.setter
    def _cached_talk_theme(self, value: str) -> None:
        self.talk_theme_state.cached_theme = value

    @property
    def _theme_panel_enabled(self) -> bool:
        return self.talk_theme_state.panel_enabled

    @_theme_panel_enabled.setter
    def _theme_panel_enabled(self, value: bool) -> None:
        self.talk_theme_state.panel_enabled = value

    # --- ST5-S17: feature-toggle compat shim REMOVED ---
    # The 11 ``app_state.<feature>_enabled`` / ``app_state.camera_device_index``
    # backward-compat properties (which delegated to the now-removed self.features
    # mirror) were deleted. The UI no longer holds a feature-flag copy; runtime
    # values come from backend.get_feature_status() and the backend owns/persists
    # the flags via the settings file. Do not re-add these here.

    # --- ST5-S12: display cluster compat shim (delegates to self.display) ---
    # Same pattern as the data/audio/auto-prompt/talk-theme/feature shims above:
    # ownership of the chat font size moves to DisplayState while the legacy
    # ``app_state.chat_font_size`` access path keeps working via this property
    # (no annotation, so @dataclass ignores it as a field).
    @property
    def chat_font_size(self) -> int:
        return self.display.font_size

    @chat_font_size.setter
    def chat_font_size(self, value: int) -> None:
        self.display.font_size = value

    # --- ST5-S12: character cluster compat shim (delegates to self.character) ---
    # Same pattern as above: ownership of the active-character knobs moves to
    # CharacterState while the legacy ``app_state.active_character_<id|icon|name>``
    # access paths keep working via these properties (no annotation, so @dataclass
    # ignores them as fields). The owning attribute is ``self.character`` and the
    # legacy field names drop the ``active_character_`` prefix on the owner.
    @property
    def active_character_id(self) -> Optional[str]:
        return self.character.id

    @active_character_id.setter
    def active_character_id(self, value: Optional[str]) -> None:
        self.character.id = value

    @property
    def active_character_icon(self) -> Optional[str]:
        return self.character.icon

    @active_character_icon.setter
    def active_character_icon(self, value: Optional[str]) -> None:
        self.character.icon = value

    @property
    def active_character_name(self) -> Optional[str]:
        return self.character.name

    @active_character_name.setter
    def active_character_name(self, value: Optional[str]) -> None:
        self.character.name = value

    # --- ST5-S12: prompt-log cluster compat shim (delegates to self.prompt_log) ---
    # Same pattern as above: ownership of the last-prompt display knobs moves to
    # PromptLogState while the legacy ``app_state.last_prompt_<text|timestamp|character>``
    # access paths keep working via these properties (no annotation, so @dataclass
    # ignores them as fields). The owning attribute is ``self.prompt_log`` and the
    # legacy field names drop the ``last_prompt_`` prefix on the owner.
    @property
    def last_prompt_text(self) -> str:
        return self.prompt_log.text

    @last_prompt_text.setter
    def last_prompt_text(self, value: str) -> None:
        self.prompt_log.text = value

    @property
    def last_prompt_timestamp(self) -> Optional[str]:
        return self.prompt_log.timestamp

    @last_prompt_timestamp.setter
    def last_prompt_timestamp(self, value: Optional[str]) -> None:
        self.prompt_log.timestamp = value

    @property
    def last_prompt_character(self) -> Optional[str]:
        return self.prompt_log.character

    @last_prompt_character.setter
    def last_prompt_character(self, value: Optional[str]) -> None:
        self.prompt_log.character = value

    # --- ST5-S12: UI-runtime cluster compat shim (delegates to self.ui_runtime) ---
    # Same pattern as the other shims above: ownership of the transient UI runtime
    # flags moves to UIRuntimeState while the legacy ``app_state.<field>`` access
    # paths keep working via these properties (no annotation, so @dataclass ignores
    # them as fields). The owning attribute is ``self.ui_runtime``; the legacy
    # ``_needs_ui_update`` private name maps onto the owner's ``needs_ui_update``.
    @property
    def conversation_started(self) -> bool:
        return self.ui_runtime.conversation_started

    @conversation_started.setter
    def conversation_started(self, value: bool) -> None:
        self.ui_runtime.conversation_started = value

    @property
    def response_generating(self) -> bool:
        return self.ui_runtime.response_generating

    @response_generating.setter
    def response_generating(self, value: bool) -> None:
        self.ui_runtime.response_generating = value

    @property
    def current_request_id(self) -> Optional[str]:
        return self.ui_runtime.current_request_id

    @current_request_id.setter
    def current_request_id(self, value: Optional[str]) -> None:
        self.ui_runtime.current_request_id = value

    @property
    def _needs_ui_update(self) -> bool:
        return self.ui_runtime.needs_ui_update

    @_needs_ui_update.setter
    def _needs_ui_update(self, value: bool) -> None:
        self.ui_runtime.needs_ui_update = value

    # --- ST5-S12: chat-HTML-cache cluster compat shim (delegates to self.chat_cache) ---
    # Same pattern as above: ownership of the chat-HTML render cache moves to
    # ChatCacheState while the legacy ``app_state._cached_chat_html`` /
    # ``app_state._chat_history_version`` access paths keep working via these
    # properties. The ``_chat_history_version += 1`` compound assignment used by the
    # chat-mutation methods (and call sites) routes through the getter+setter, so the
    # increment lands on the owner transparently.
    @property
    def _cached_chat_html(self) -> str:
        return self.chat_cache.cached_html

    @_cached_chat_html.setter
    def _cached_chat_html(self, value: str) -> None:
        self.chat_cache.cached_html = value

    @property
    def _chat_history_version(self) -> int:
        return self.chat_cache.history_version

    @_chat_history_version.setter
    def _chat_history_version(self, value: int) -> None:
        self.chat_cache.history_version = value

    # --- ST5-S12: status cluster compat shim (delegates to self.status) ---
    # Same pattern as above: ownership of the subsystem-status / history-load flags
    # moves to StatusState while the legacy ``app_state.<field>`` access paths keep
    # working via these properties. The owning attribute is ``self.status``.
    @property
    def backend_available(self) -> bool:
        return self.status.backend_available

    @backend_available.setter
    def backend_available(self, value: bool) -> None:
        self.status.backend_available = value

    @property
    def audio_input_available(self) -> bool:
        return self.status.audio_input_available

    @audio_input_available.setter
    def audio_input_available(self, value: bool) -> None:
        self.status.audio_input_available = value

    @property
    def audio_output_available(self) -> bool:
        return self.status.audio_output_available

    @audio_output_available.setter
    def audio_output_available(self, value: bool) -> None:
        self.status.audio_output_available = value

    @property
    def is_loading_history(self) -> bool:
        return self.status.is_loading_history

    @is_loading_history.setter
    def is_loading_history(self, value: bool) -> None:
        self.status.is_loading_history = value

    @property
    def history_load_error(self) -> Optional[str]:
        return self.status.history_load_error

    @history_load_error.setter
    def history_load_error(self, value: Optional[str]) -> None:
        self.status.history_load_error = value

    def add_log_message(self, level: str, message: str, ws_forward: bool = True) -> None:
        """
        Add a log message to the in-memory log list for display in the UI.
        Also writes to the python logger with the appropriate level.

        Args:
            level: Log level ('info', 'warning', 'error', 'debug')
            message: The message text to log
            ws_forward: False にすると WebSocketErrorHandler のトースト転送
                対象外になる(記録は残る)。ポップアップ等、別チャンネルで
                既にユーザーへ届く内容の二重トースト防止用(稜裁定 2026-08-02)
        """
        # Validate inputs
        if not isinstance(level, str):
            level = str(level)
        if not isinstance(message, str):
            message = str(message)
            
        # Validate log level
        valid_levels = {'info', 'warning', 'error', 'debug'}
        if level.lower() not in valid_levels:
            logger.warning(f"Invalid log level '{level}', defaulting to 'debug'")
            level = 'debug'
            
        log_entry = f"[{level.upper()}] {message}"

        # Add new message and trim to max size
        self.log_messages.append(log_entry)
        if len(self.log_messages) > MAX_LOG_MESSAGES:
            self.log_messages = self.log_messages[-MAX_LOG_MESSAGES:]

        # Map to standard logger methods using dict lookup
        log_methods = {
            'info': logger.info,
            'warning': logger.warning,
            'error': logger.error,
            'debug': logger.debug
        }
        log_method = log_methods.get(level.lower(), logger.debug)
        
        # Set flag to prevent circular logging when we call the logger
        _thread_local.in_add_log_message = True
        try:
            if ws_forward:
                log_method(message)
            else:
                log_method(message, extra={'ag_no_ws_toast': True})
        finally:
            _thread_local.in_add_log_message = False


    def append_chat_message(self, speaker: str, text: str, is_ai: bool, images: list = None, documents: list = None) -> None:
        """
        Append a new message to the in-memory chat_history with size limit.
        Implements a sliding window to prevent unbounded memory growth.

        Args:
            speaker: The name of the speaker (e.g., "User", "AI")
            text: The message text
            is_ai: Whether this is an AI message (for styling)
            images: Optional list of image file paths attached to this message
            documents: Optional list of document filenames attached to this message
        """
        from datetime import datetime, timezone

        # Validate inputs
        if not isinstance(speaker, str):
            speaker = str(speaker)
        if not isinstance(text, str):
            text = str(text)
        if not isinstance(is_ai, bool):
            is_ai = bool(is_ai)

        # Ensure non-empty speaker and text
        if not speaker.strip():
            speaker = "Unknown"
        if not text.strip():
            logger.warning("Attempted to add empty message to chat history")
            return  # Don't add empty messages

        # Add message with current timestamp in ISO format (UTC for consistency with backend)
        timestamp = datetime.now(timezone.utc).isoformat()
        self.chat_history.append((speaker, text, is_ai, timestamp, images or [], documents or []))
        
        # Increment version to invalidate cache
        self._chat_history_version += 1
        
        # Implement sliding window to prevent memory issues
        if len(self.chat_history) > MAX_CHAT_HISTORY_SIZE:
            # Keep only the most recent messages
            self.chat_history = self.chat_history[-MAX_CHAT_HISTORY_SIZE:]
            self.add_log_message("info", f"Chat history trimmed to {MAX_CHAT_HISTORY_SIZE} messages")

    def clear_chat_history(self) -> None:
        """Clear the chat history."""
        self.chat_history = []
        self._chat_history_version += 1
        self._cached_chat_html = ""


# Initialize global state
app_state = AppState()


# Helper Functions for Error Handling
def show_popup(title: str, message: str, level: str = "error") -> None:
    """
    Generic popup function for all levels with input validation.
    
    Args:
        title: Popup title
        message: Detailed message
        level: Popup level ('error', 'warning', 'info')
        
    Raises:
        ValueError: If level is not valid
    """
    # Validate and convert inputs
    if not isinstance(title, str):
        title = str(title)
    if not isinstance(message, str):
        message = str(message)
    if not isinstance(level, str):
        level = str(level)
        
    # Ensure non-empty title and message
    if not title.strip():
        title = "Notification"
    if not message.strip():
        message = "An event occurred"
        
    # Validate level
    valid_levels = {"error", "warning", "info"}
    if level.lower() not in valid_levels:
        raise ValueError(f"Invalid popup level: {level}. Must be one of: {valid_levels}")

    level = level.lower()
    # Always log first — the popup path must never lose the record.
    # ws_forward=False: ポップアップ自体が popup_notification でトーストになる
    # ため、この記録ログまで WS 転送すると同内容トーストが2枚出る(稜裁定
    # 2026-08-02: 1障害=生ログ1+ユーザー向け表示1)。
    app_state.add_log_message(level, f"{title}: {message}", ws_forward=False)

    # Live delivery: WS broadcast -> ws_client_js 'popup_notification' ->
    # showNotification toast (replaces the gr.Timer-polled error_display,
    # whose periodic tick flickered during the 5s display). When no desktop
    # client is connected (browser not open yet / tray-resident), the popup
    # is queued in backend.shared.popup_state and flushed by the WS server
    # when a desktop client next identifies. Call-time import = approved
    # seam (never a module-load-time upward import).
    delivered = False
    try:
        from backend.server.websocket_server import get_websocket_manager
        delivered = get_websocket_manager().send_popup_notification_sync(
            title, message, level
        )
    except Exception:
        delivered = False
    if not delivered:
        from backend.shared.popup_state import enqueue_popup
        enqueue_popup(title, message, level)


def show_error_popup(title: str, message: str) -> None:
    """
    Helper function to show error popup to the user.
    
    Args:
        title: Error title
        message: Detailed error message
    """
    show_popup(title, message, "error")


def show_warning_popup(title: str, message: str) -> None:
    """
    Helper function to show warning popup to the user.
    
    Args:
        title: Warning title
        message: Detailed warning message
    """
    show_popup(title, message, "warning")


def show_info_popup(title: str, message: str) -> None:
    """
    Helper function to show info popup to the user.
    
    Args:
        title: Info title
        message: Detailed info message
    """
    show_popup(title, message, "info")


# Export public API
__all__ = [
    'AppState',
    'app_state',
    'UILogHandler',
    'show_popup',
    'show_error_popup',
    'show_warning_popup',
    'show_info_popup'
]