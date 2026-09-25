"""
ui/app.py

Main application entry point and UI orchestration for the Artificial Girlfriend UI.
This module brings together all UI components and handles the Gradio app construction.
"""

import os
import sys
import json
import logging
import traceback
import time
import atexit
import signal
import platform
import threading
import asyncio
from collections import OrderedDict
from pathlib import Path
import gradio as gr
from typing import Tuple, Dict, Any, Optional

# Import from local UI modules
from .gradio_patches import (
    apply_gradio_client_patches,
    apply_gradio_index_patch,
    apply_gradio_local_font_patch,
    apply_gradio_tabs_overflow_patch,
)
from .state import app_state, UILogHandler
from .error_handler import handle_module_operation
from .ws_client_js import create_websocket_init_js
from .ws_error_handler import WebSocketErrorHandler
from .utility_panel import _build_utility_panel_html, build_camera_device_row_html
from .handlers.hotkey import setup_hotkey_callbacks
from .handlers.addon_hotkey import setup_addon_hotkey_handler
from .handlers.system_status import check_extraction_status
from .handlers.history import (
    handle_add_memory,
    handle_memory_action,
    handle_refresh_history,
    on_history_character_select,
)
from .handlers.feature_toggles import (
    handle_command_execution_toggle,
    handle_notes_toggle,
    handle_speechless_toggle,
)
from .handlers.audio_controls import (
    handle_tts_volume_change,
    handle_volume_change,
    test_both_beeps,
    test_voice,
)
from .handlers.api_settings import (
    _refresh_blacklist,
    _refresh_elevenlabs_models,
    _refresh_elevenlabs_voices,
    _refresh_google_models_full,
    _refresh_image_blacklist,
    _refresh_models_and_character_dropdowns,
    _refresh_openai_models_and_stt,
    _remove_from_blacklist,
    _remove_from_image_blacklist,
    _save_api_key_handler,
    _save_elevenlabs_key_and_refresh,
    _save_elevenlabs_model,
    _save_key_and_refresh,
    _save_embedding_handler,
    _save_google_maps_key,
    _save_image_gen_handler,
    _save_ollama_ctx_handler,
    _search_toggle_handler,
)
from .handlers.stt_engine import (
    _change_stt_api_model,
    _change_stt_engine,
    _change_stt_local_model,
)
from .handlers.audio_device import (
    change_mic_device,
    refresh_mic_devices,
)
from .handlers.hotkey_config import save_hotkeys
from .handlers.tuning import (
    create_tuning_change_handler,
    handle_check_click,
    handle_load_click,
    update_tuning_on_character_change,
)
from .handlers.defaults import (
    cancel_apply_all,
    create_defaults_change_handler,
    execute_apply_all,
    handle_defaults_check_click,
    handle_defaults_load_click,
    show_apply_all_confirm,
)
from .handlers.navigation import (
    load_character_page_data,
    refresh_both_char_dropdowns,
    refresh_char_dropdowns_with_youtube,
    switch_page,
)
from .handlers.character_voice import update_voice_choices_for_language
from .handlers.status_log import (
    log_hotkey_status,
    refresh_command_log,
)
from .handlers.attachments import (
    _clear_text_and_attachments,
    _handle_attach,
)
from .handlers.lifecycle import (
    check_ui_updates,
    handle_auto_prompt_generating,
    handle_remote_disconnect,
    initial_load_with_cleanup,
)
from .constants import (
    LOGS_DIR, LOG_REFRESH_INTERVAL,
    MAX_BACKEND_INIT_RETRIES, RETRY_DELAY,
    APP_ROOT, ICONS_DIR, DEFAULT_ICON_PATH,
)
from .components import (
    CHAT_CSS_BODY,
    create_character_creation_ui,
    create_character_edit_ui, get_chat_history, update_log_view, update_prompt_view,
    rebroadcast_prompt_tokens
)
from .conversation import (
    toggle_start_end, toggle_beep, refresh_character_info_on_start,
    restore_character_info_on_load,
    sync_char_dropdown_lock, sync_text_controls_lock,
    sync_conversation_controls_on_load,
    create_beep_sounds, update_recording_time,
    handle_text_input, start_text_generation, process_text_generation,
    wait_for_history_load, voice_btn_chain,
    toggle_auto_prompt, update_auto_prompt_timer_duration,
    process_electron_text_prompt,
)
from backend.shared.text_prompt_command import register_text_prompt_handler
from .character_ui import (
    switch_character, refresh_char_list, load_char_dropdown,
    open_character_management, create_character,
    load_character_for_edit, edit_character,
    confirm_character_deletion, remove_character,
    resize_icon_preview, mark_edit_icon_changed,
    refresh_motion_folder_choices,
)
from .conversation_functions import (
    poll_talk_theme, update_talk_theme_click, clear_talk_theme_click,
    enable_theme_panel, disable_theme_panel
)
from .status_checker import get_status_html
from .status_js import status_auto_hide_js as _status_auto_hide_js
from .status_js import status_show_js as _status_show_js
from .local_fonts import LOCAL_FONTS_CSS
from .license_notice import license_notice_html
from backend.shared.i18n import t
from .pages import (
    Pages, FOOTER_HIDE_CSS, get_sidebar_css,
    create_conversation_page, create_character_settings_page,
    create_conversation_history_page, create_system_logs_page,
    create_system_controls_page
)

# Import backend modules
import backend
import audio_input
import audio_output
from backend.shared.hotkey_handler import get_hotkey_handler, HotkeyAction
from .hotkey_service import get_hotkey_service, cleanup_hotkey_service

# gradio_client 1.7.0 crashes on boolean sub-schemas (see ui/gradio_patches.py);
# must be applied before the first get_api_info() call, i.e. before launch.
apply_gradio_client_patches()
# 配信index.htmlのAG向け加工: Gradio組込文言のUI言語固定(head=では間に合わない)
# +組込外部参照(Google Fonts preconnect/cdnjs iframe-resizer)の除去=外部通信ゼロ方針。
# 詳細は ui/gradio_patches.py。全マウント(desktop/mobile/admin)共通。
apply_gradio_index_patch()
# 実在しないwoff2を指すGradio自動生成@font-faceの間引き(同名familyの
# 汚染でSource Sans 3や実在システムフォントが潰れる)。Blocks生成前に必須。
apply_gradio_local_font_patch()
# 非表示タブが「…」オーバーフローメニューに幽霊表示されるGradio 5.15の
# Tabsバグ修正(配信JSをvisibleフィルタ済みコピーへ差し替え)。History
# ページのYouTube返信タブ非表示化で顕在(稜実機 2026-07-20)。
apply_gradio_tabs_overflow_patch()

# 配線: WS(text_prompt)→会話フローの依存逆転レジストリ(継ぎ目③・feature_commands同型)
register_text_prompt_handler(process_electron_text_prompt)

# 配線: WS(hotkey_*_recording・AG Client Addon)→録音トリガの依存逆転レジストリ(継ぎ目③)
setup_addon_hotkey_handler()

# Global logger
logger = logging.getLogger(__name__)


# === Server-mode static icon serving (chat HTML bandwidth optimization) ===
# Replaces inline base64 icon embedding (~40KB × N AI messages) with a single
# WebP fetch served from /static/icons/<character_id>?v=<mtime>. Browser caches
# the response, so chat HTML refreshes carry only the URL string per message.
ICON_SIZE = 120  # CSS .char-icon is 60px, doubled for retina
ICON_CACHE_MAX = 100
_icon_bytes_cache: "OrderedDict[Tuple[str, int, int], bytes]" = OrderedDict()
_icon_cache_lock = threading.Lock()
SVG_FALLBACK_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" viewBox="0 0 50 50">'
    b'<circle cx="25" cy="25" r="20" fill="#3a3a3a"/>'
    b'<text x="25" y="25" text-anchor="middle" dominant-baseline="middle" '
    b'font-size="20" fill="#aaa">?</text></svg>'
)


def _icon_cache_get(key):
    with _icon_cache_lock:
        v = _icon_bytes_cache.get(key)
        if v is not None:
            _icon_bytes_cache.move_to_end(key)
        return v


def _icon_cache_set(key, value):
    with _icon_cache_lock:
        _icon_bytes_cache[key] = value
        _icon_bytes_cache.move_to_end(key)
        while len(_icon_bytes_cache) > ICON_CACHE_MAX:
            _icon_bytes_cache.popitem(last=False)


def _resolve_character_icon(character_id: str) -> Optional[Path]:
    """Resolve a character_id to a validated icon Path, or None if unresolvable.

    Defense-in-depth path-traversal guard: load_character_config() rejects
    character_ids outside [a-zA-Z0-9_-]+, then we resolve(strict=True) to force
    canonical case on Windows and verify the result is inside character_icons/
    via relative_to.
    """
    from backend.conversation.character_manager import load_character_config
    try:
        cfg = load_character_config(character_id)
    except (ValueError, FileNotFoundError, IOError):
        cfg = None
    icon_str = (cfg or {}).get("icon_path")
    if icon_str:
        try:
            p = Path(icon_str).resolve(strict=True)
            icons_dir = ICONS_DIR.resolve(strict=True)
            p.relative_to(icons_dir)
            return p
        except (ValueError, FileNotFoundError, OSError):
            pass
    if DEFAULT_ICON_PATH.exists():
        return DEFAULT_ICON_PATH
    return None


def _encode_icon_webp(icon_path: Path, size: int) -> Optional[bytes]:
    """Resize and WebP-encode an icon. Synchronous; call via asyncio.to_thread."""
    try:
        from PIL import Image
        import io
        with Image.open(icon_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=85, method=6)
            return buf.getvalue()
    except Exception as e:
        logger.warning(f"icon WebP encode failed for {icon_path}: {e}")
        return None


# JavaScript function to scroll chat to bottom (supplementary to MutationObserver)
SCROLL_TO_BOTTOM_JS = """
() => {
    const chatDisplay = document.getElementById('chat-display');
    if (chatDisplay) {
        setTimeout(() => {
            chatDisplay.scrollTop = chatDisplay.scrollHeight;
        }, 100);
    }
}
"""

# サーバーモードのブラウザマイク🔄: 表示と実録音デバイスを同時に更新する。
# 会話中(micStream保持中)はストリームを掴み直す=Chromeのマイク設定変更が
# リロード無しで実際の録音にも反映される(保持ストリームは古いデバイスを
# 掴み続けるため、掴み直しなしの表示更新は嘘になる)。開始前はストリームを
# 持たない(タブの録音インジケータを点けっぱなしにしない)ので enumerateDevices
# の既定エントリで表示のみ更新。文言は #browser-mic-name の data 属性
# (ビルド時に t() 解決済み)から読む。
BROWSER_MIC_REFRESH_JS = """
async () => {
    const el = document.getElementById('browser-mic-name');
    if (!el || !window.wsManager || !window.wsManager.serverMode || window.isMobileUI) return;
    try {
        if (window._browserMicRecording) return;  // 録音中は掴み直さない
        // 常に getUserMedia で解決する: enumerateDevices の既定エントリは
        // OS既定を映すだけで、Chromeのサイト設定で選んだマイクを反映しない
        // (稜実機 2026-07-25: 設定変更後に🔄が無反応に見えた原因)。
        const had = !!window.micStream;
        if (window.micStream) {
            try { window.micStream.getTracks().forEach(t => t.stop()); } catch (e) {}
            window.micStream = null;
        }
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const track = stream.getAudioTracks()[0];
        el.textContent = (el.dataset.prefix || '') + ((track && track.label) || '');
        if (had) {
            // 会話中: 新ストリームを保持=以後の録音も新デバイスで行う
            window.micStream = stream;
            window.micAvailable = true;
            if (window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({
                    action: 'set_browser_mic_status', available: true
                }));
            }
        } else {
            // 開始前: 表示だけ更新してすぐ手放す(タブの録音インジケータを
            // 点けっぱなしにしない。一瞬の点灯はユーザー自身の🔄操作なので許容)
            try { stream.getTracks().forEach(t => t.stop()); } catch (e) {}
        }
    } catch (e) {
        console.warn('[Mic] Browser mic refresh failed:', e);
        el.textContent = el.dataset.denied || '';
        window.micAvailable = false;
    }
}
"""


# 通知の自動消去JS(_status_auto_hide_js / _status_show_js)はファイル冒頭で
# ui/status_js.py（葉モジュール・admin_app と共有）から import している。


# MutationObserver-based auto-scroll setup (runs once on page load)
CHAT_AUTOSCROLL_OBSERVER_JS = """
() => {
    if (window._chatScrollObserver) return;
    const setupScroll = () => {
        const chatDisplay = document.getElementById('chat-display');
        if (!chatDisplay) {
            setTimeout(setupScroll, 200);
            return;
        }
        if (window._chatScrollObserver) return;

        let scrollTimer = null;
        const observer = new MutationObserver(() => {
            clearTimeout(scrollTimer);
            scrollTimer = setTimeout(() => {
                chatDisplay.scrollTop = chatDisplay.scrollHeight;
            }, 50);
        });
        observer.observe(chatDisplay, { childList: true, subtree: true });
        window._chatScrollObserver = observer;

        setTimeout(() => {
            chatDisplay.scrollTop = chatDisplay.scrollHeight;
        }, 100);

        console.log('[PC] Chat auto-scroll observer installed');
    };
    setupScroll();
}
"""


# Wrapper functions for module initialization with consistent error handling
# show_popup=False: リトライ毎にポップアップ+ERRORログが3連射されていた。
# 最終失敗時のポップアップは initialize_backend_with_retry 側で1回出す
# (稜裁定 2026-08-02)
@handle_module_operation("Backend initialization", show_popup=False)
def _initialize_backend():
    """Initialize the backend module with error handling."""
    backend.init_backend()
    logger.info("Backend initialized successfully")
    
    # Check if backend has required functions
    if not hasattr(backend, 'generate_reply'):
        raise RuntimeError("Backend module missing critical function: generate_reply")
    
    return True


@handle_module_operation("Audio input initialization", show_popup=False)
def _initialize_audio_input():
    """Initialize the audio input module with error handling."""
    from backend.shared.settings_store import get_setting
    model_size = get_setting('audio', 'stt_local_model', 'turbo')
    if model_size not in audio_input.VALID_MODEL_SIZES:
        # A corrupt settings value must not kill mic initialization outright
        logger.warning(f"Invalid stt_local_model '{model_size}' in settings - falling back to 'turbo'")
        model_size = 'turbo'
    audio_input.init_audio_input(model_size=model_size)
    logger.info("Audio input initialized successfully")

    # Check for available microphone devices
    try:
        if hasattr(audio_input, 'get_available_devices'):
            devices = audio_input.get_available_devices()
            logger.info(f"get_available_devices() returned: {devices}")
            if not devices or len(devices) == 0:
                logger.warning("No microphone devices detected")
                from .state import show_info_popup
                show_info_popup(t('appinit.no_mic_title'),
                              t('appinit.no_mic_msg'))
                # Don't raise error - allow text-only mode
            else:
                logger.info(f"Found {len(devices)} audio input device(s)")
    except Exception as e:
        logger.warning(f"Could not check for microphone devices: {e}")
    
    return True


@handle_module_operation("Audio output initialization", show_popup=False)
def _initialize_audio_output():
    """Initialize the audio output module with error handling."""
    audio_output.init_audio_output()
    logger.info("Audio output initialized successfully")
    
    # Check if audio output has required functions
    if not hasattr(audio_output, 'text_to_speech'):
        raise RuntimeError("Audio output module missing critical function: text_to_speech")
    
    return True


def setup_directories() -> None:
    """
    Ensure necessary directories exist.
    
    Raises:
        RuntimeError: If directories cannot be created
    """
    # Create all required directories
    directories = [
        ("logs", LOGS_DIR),
        ("icons", ICONS_DIR)
    ]
    
    for dir_name, dir_path in directories:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            error_msg = f"Permission denied when creating {dir_name} directory at {dir_path.resolve()}. Check folder permissions."
            logger.critical(error_msg)
            raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = f"Failed to create {dir_name} directory at {dir_path.resolve()}: {e}"
            logger.critical(error_msg)
            raise RuntimeError(error_msg)


def setup_logging() -> None:
    """
    Configure application logging with unified output to UI, console, and file.

    Uses explicit handler configuration instead of basicConfig to avoid conflicts
    with third-party libraries (torch, huggingface_hub, etc.) that add their own
    handlers during import.

    Output destinations (all receive the same content):
    - File: logs/app.log (with rotation, max 10MB, 3 backups)
    - Console: stdout (for Powershell visibility)
    - UI: System Logs page (max 100 messages)
    """
    from logging.handlers import RotatingFileHandler

    # Get root logger and set to accept all levels
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Remove existing handlers added by third-party libraries during import
    # (torch, huggingface_hub, etc. add StreamHandlers on import)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Configure timing_logger to use unified logging
    # (remove its custom handler and enable propagation to root logger)
    timing_logger = logging.getLogger("AG_TIMING")
    for handler in timing_logger.handlers[:]:
        timing_logger.removeHandler(handler)
    timing_logger.propagate = True

    # Suppress noisy third-party library loggers
    noisy_loggers = [
        "torch", "huggingface_hub", "urllib3", "httpx", "httpcore",
        "matplotlib", "PIL", "numba", "transformers", "filelock",
        "fsspec", "asyncio", "websockets"
    ]
    for lib in noisy_loggers:
        logging.getLogger(lib).setLevel(logging.WARNING)

    # Unified log format with timestamp
    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # 1. Rotating file handler
    try:
        file_handler = RotatingFileHandler(
            filename=str(LOGS_DIR / "app.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=3,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception as e:
        # Print to stderr as logging isn't set up yet
        print(f"Warning: Failed to set up file logging: {e}", file=sys.stderr)

    # 2. Console handler (stdout for Powershell visibility)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 3. UI handler for System Logs page
    ui_handler = UILogHandler(app_state)
    ui_handler.setLevel(logging.INFO)
    ui_handler.setFormatter(formatter)
    root_logger.addHandler(ui_handler)

    # Enable DEBUG level for specific modules that need detailed logging
    logging.getLogger("backend.shared.hotkey_handler").setLevel(logging.DEBUG)
    logging.getLogger("ui.hotkey_service").setLevel(logging.DEBUG)

    logger.info("Logging system configured successfully")


def initialize_backend_with_retry(max_retries: int = MAX_BACKEND_INIT_RETRIES) -> bool:
    """
    Initialize backend with retry mechanism.
    
    Args:
        max_retries: Maximum number of retry attempts
        
    Returns:
        bool: True if backend initialized successfully
        
    Raises:
        RuntimeError: If backend fails to initialize after all retries
    """
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            return _initialize_backend()
        except Exception as e:
            retry_count += 1
            if retry_count < max_retries:
                logger.warning(f"Backend initialization attempt {retry_count} failed: {e}. Retrying...")
                time.sleep(RETRY_DELAY)  # Wait before retry
                continue
            logger.critical(f"Backend initialization failed after {retry_count} attempts: {e}")
            # ポップアップは最終失敗時の1回のみ(リトライ毎はデコレータ側で停止済み)
            from .state import show_error_popup
            show_error_popup(t('popup.op_error_title', op="Backend initialization"), str(e))
            raise RuntimeError(f"Failed to initialize backend after {retry_count} attempts: {e}")
    
    return False


def initialize_audio_systems(backend_initialized: bool) -> Tuple[bool, bool]:
    """
    Initialize audio input and output systems.
    
    Args:
        backend_initialized: Whether backend is initialized (affects error handling)
        
    Returns:
        Tuple[bool, bool]: (audio_input_initialized, audio_output_initialized)
    """
    audio_input_initialized = False
    audio_output_initialized = False
    
    # Initialize audio input
    try:
        logger.info("Starting audio input initialization...")
        audio_input_initialized = _initialize_audio_input()
        logger.info(f"Audio input initialization result: {audio_input_initialized}")
    except Exception as e:
        # デコレータ(_initialize_audio_input)が ERROR ログ済み=warning 止まり
        logger.warning(f"Audio input initialization failed: {e}")
        # Continue with warnings rather than fatal error if backend is working
        if backend_initialized:
            from .state import show_warning_popup
            show_warning_popup(t('appinit.partial_init_title'),
                             t('appinit.audio_input_failed', error=e))
        else:
            raise RuntimeError(f"Failed to initialize audio input: {e}")
    
    # Initialize audio output
    try:
        audio_output_initialized = _initialize_audio_output()
    except Exception as e:
        # デコレータ(_initialize_audio_output)が ERROR ログ済み=warning 止まり
        logger.warning(f"Audio output initialization failed: {e}")
        # Continue with warnings rather than fatal error if backend is working
        if backend_initialized:
            from .state import show_warning_popup
            show_warning_popup(t('appinit.partial_init_title'),
                             t('appinit.audio_output_failed', error=e))
        else:
            raise RuntimeError(f"Failed to initialize audio output: {e}")
    
    return audio_input_initialized, audio_output_initialized


def check_system_resources() -> Dict[str, Any]:
    """
    Check system resources and provide warnings if insufficient.
    
    Returns:
        Dict with resource information and any warnings
    """
    resource_info = {
        'memory_gb': 0,
        'memory_available_gb': 0,
        'has_audio': False,
        'warnings': [],
        'errors': []
    }
    
    # Try to import psutil for better resource checking
    try:
        import psutil
        
        # Check memory
        memory = psutil.virtual_memory()
        resource_info['memory_gb'] = memory.total / (1024**3)
        resource_info['memory_available_gb'] = memory.available / (1024**3)
        
        # Minimum requirements
        MIN_MEMORY_GB = 4
        MIN_AVAILABLE_GB = 2
        
        if resource_info['memory_gb'] < MIN_MEMORY_GB:
            resource_info['warnings'].append(
                t('appinit.res_low_memory',
                  memory=f"{resource_info['memory_gb']:.1f}", min=MIN_MEMORY_GB)
            )
        
        if resource_info['memory_available_gb'] < MIN_AVAILABLE_GB:
            resource_info['warnings'].append(
                t('appinit.res_low_available',
                  available=f"{resource_info['memory_available_gb']:.1f}")
            )
            
    except ImportError:
        logger.info("psutil not available, skipping detailed resource checks")
        resource_info['warnings'].append(
            t('appinit.res_no_psutil')
        )
    except Exception as e:
        logger.warning(f"Error checking system resources: {e}")
    
    # Check for audio devices (platform-specific)
    try:
        system = platform.system().lower()
        
        if system == 'linux':
            # Check for ALSA/PulseAudio
            if os.path.exists('/proc/asound/cards'):
                with open('/proc/asound/cards', 'r') as f:
                    content = f.read()
                    resource_info['has_audio'] = bool(content.strip())
        elif system == 'windows':
            # Windows typically has audio if we get this far
            resource_info['has_audio'] = True
        elif system == 'darwin':  # macOS
            # macOS typically has audio
            resource_info['has_audio'] = True
        else:
            resource_info['has_audio'] = True  # Assume audio exists
            
        if not resource_info['has_audio']:
            resource_info['warnings'].append(
                t('appinit.res_no_audio')
            )
            
    except Exception as e:
        logger.debug(f"Could not check audio devices: {e}")
        resource_info['has_audio'] = True  # Assume audio exists
    
    return resource_info


def update_state_flags(backend_ok: bool, audio_in_ok: bool, audio_out_ok: bool) -> None:
    """
    Update application state flags based on initialization results.
    
    Args:
        backend_ok: Whether backend initialized successfully
        audio_in_ok: Whether audio input initialized successfully
        audio_out_ok: Whether audio output initialized successfully
    """
    app_state.backend_available = backend_ok
    app_state.audio_input_available = audio_in_ok
    app_state.audio_output_available = audio_out_ok
    
    # Clear Ollama status cache when backend availability changes
    # This ensures the status checker immediately reflects the new state
    if backend_ok:
        try:
            from .status_checker import clear_ollama_cache
            clear_ollama_cache()
            logger.debug("Cleared Ollama cache after backend initialization")
        except Exception as e:
            logger.debug(f"Could not clear Ollama cache: {e}")
    
    # Log initialization status summary
    if backend_ok and audio_in_ok and audio_out_ok:
        logger.info("Application initialization complete - all systems operational")
    else:
        components = []
        if not backend_ok:
            components.append("backend")
        if not audio_in_ok:
            components.append("audio input")
        if not audio_out_ok:
            components.append("audio output")

        if components:
            logger.warning(f"Application initialization partial - issues with: {', '.join(components)}")


def initialize_application(max_retries: int = 3) -> None:
    """
    One-time setup for the entire app:
      - Initialize logging
      - Initialize backend, audio modules
      - Load beep sound effects
      - Optionally load or remember the last-used character

    Args:
        max_retries: Maximum number of retry attempts for transient failures

    Returns:
        None

    Raises:
        RuntimeError: If critical components fail to initialize
    """
    # Set up directories and logging
    setup_directories()
    setup_logging()

    logger.info("Initializing Artificial Girlfriend application...")

    # macOS: TCC permission preflight to the startup log (Mac 3-3).
    # All four denials are otherwise silent (empty recording / wallpaper
    # screenshot / dead hotkeys / dead window control).
    from backend.shared.platform_caps import IS_MAC
    if IS_MAC:
        from backend.shared.mac_permissions import log_permission_status
        log_permission_status()

    # Clean up stale restart flag from previous session (e.g., crash after flag creation)
    stale_flag = APP_ROOT / '.restart_flag'
    if stale_flag.exists():
        try:
            stale_flag.unlink()
            logger.info("Removed stale restart flag from previous session")
        except Exception as e:
            logger.warning(f"Failed to remove stale restart flag: {e}")

    # Load user settings
    from .settings_manager import apply_settings_to_app_state
    apply_settings_to_app_state(app_state)
    logger.info("Loaded user settings")
    
    # Log important paths for debugging
    logger.info(f"Application root directory: {APP_ROOT}")
    logger.info(f"Icons directory: {ICONS_DIR.resolve()}")
    logger.info(f"Logs directory: {LOGS_DIR.resolve()}")
    
    # Check system resources
    logger.info("Checking system resources...")
    resources = check_system_resources()
    
    # Log resource information
    if resources.get('memory_gb', 0) > 0:
        logger.info(f"System memory: {resources['memory_gb']:.1f}GB total, "
                   f"{resources['memory_available_gb']:.1f}GB available")
    
    # Show warnings if any
    for warning in resources.get('warnings', []):
        logger.warning(f"Resource warning: {warning}")
        from .state import show_warning_popup
        show_warning_popup(t('appinit.resource_warning_title'), warning)
    
    # Show errors if any (these are critical)
    for error in resources.get('errors', []):
        logger.error(f"Resource error: {error}")
        from .state import show_error_popup
        show_error_popup(t('appinit.resource_error_title'), error)
        # Don't raise here, let the app try to continue
    
    # If no audio detected, update expectations
    if not resources.get('has_audio', True):
        logger.warning("No audio devices detected, voice features will be limited")
        app_state.audio_input_available = False
        app_state.audio_output_available = False
    
    # Initialize backend with retry mechanism
    backend_ok = initialize_backend_with_retry(max_retries)

    # NOTE (ST5-S17): the feature-toggle startup push (app_state -> _backend_state)
    # was removed. The backend now loads its persisted feature toggles itself from
    # the settings file inside init_backend() (single source of truth). The UI no
    # longer keeps a mirror copy; it reads runtime values via get_feature_status().

    # NOTE: Icon cleanup moved to after UI loads to ensure backend is fully initialized
    # Running cleanup during startup was causing all icons to be deleted if backend wasn't ready
    
    # Initialize audio systems
    audio_in_ok, audio_out_ok = initialize_audio_systems(backend_ok)
    
    # Load beep sound effects if audio output is available
    if audio_out_ok:
        create_beep_sounds()
    else:
        logger.warning("Skipping beep sound initialization as audio output is not available")
    
    # Update state flags
    update_state_flags(backend_ok, audio_in_ok, audio_out_ok)
    
    # Initialize hotkey service for global keyboard shortcuts
    try:
        logger.info("Initializing global hotkey service...")
        hotkey_service = get_hotkey_service()
        
        # We'll set up the callbacks after the UI is created
        # For now, just log that the service is ready
        logger.info("Hotkey service initialized (callbacks will be set after UI creation)")
    except Exception as e:
        logger.warning(f"Failed to initialize hotkey service: {e}")
        logger.warning("Global hotkeys will not be available")
    
    # Register cleanup handlers
    atexit.register(shutdown_application)
    
    # Register signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        shutdown_application()
        exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
    if hasattr(signal, 'SIGHUP'):  # POSIX only: terminal/session hangup
        signal.signal(signal.SIGHUP, signal_handler)
    
    logger.info("Cleanup handlers registered")


# Global variable to track demo object for cleanup
_demo_instance = None
_uvicorn_server = None  # Track uvicorn server for programmatic shutdown

def shutdown_application() -> None:
    """
    Cleanly terminate the application:
      - Stop any ongoing microphone capture
      - Possibly finalize resources in the backend
      - Free up model resources, etc.
      
    Returns:
        None
    """
    # Prevent multiple shutdown calls
    if getattr(shutdown_application, '_shutdown_called', False):
        return
    shutdown_application._shutdown_called = True
    
    logger.info("Shutting down application...")

    # Save user settings (auto_prompt / audio / display owned by app_state).
    # NOTE (ST5-S17): the BackendState -> AppState feature pull was removed. Feature
    # toggles are owned solely by _backend_state and persisted to the settings file
    # at the moment they change (feature_toggle_service / device_settings call
    # update_setting), so they are already current on disk at shutdown — no pull or
    # re-save of features is needed here.
    try:
        from .settings_manager import save_app_state_settings
        save_app_state_settings(app_state)
        logger.info("User settings saved during shutdown")
    except Exception as e:
        logger.warning(f"Failed to save settings during shutdown: {e}")

    # Stop any active recording first
    if app_state.recording_start_time is not None:
        try:
            app_state.recording_start_time = None  # Clear recording flag
            audio_input.stop_recording()
            logger.info("Active recording stopped during shutdown")
        except Exception as e:
            logger.warning(f"Error stopping active recording: {e}")

    # Gracefully stop mic capture (if active)
    try:
        audio_input.stop_recording()
        logger.info("Microphone capture stopped")
    except Exception as e:
        logger.warning(f"Error stopping microphone: {e}")

    # Release audio output resources (TTS model, BERT, GPU memory)
    try:
        audio_output.cleanup()
        logger.info("Audio output resources released")
    except Exception as e:
        logger.warning(f"Error cleaning up audio output: {e}")

    # Release audio input resources (Whisper model, GPU memory)
    try:
        audio_input.cleanup()
        logger.info("Audio input resources released")
    except Exception as e:
        logger.warning(f"Error cleaning up audio input: {e}")

    # Stop WebSocket server
    try:
        from backend.server.websocket_server import stop_websocket_server
        stop_websocket_server()
        logger.info("WebSocket server stopped")
    except Exception as e:
        logger.warning(f"Error stopping WebSocket server: {e}")
    
    # Stop hotkey service
    try:
        cleanup_hotkey_service()
        logger.info("Hotkey service stopped")
    except Exception as e:
        logger.warning(f"Error stopping hotkey service: {e}")

    # Try to finalize backend resources
    try:
        result = backend.stop_conversation()
        if result["success"]:
            logger.info(f"Backend conversation stopped: {result.get('message', 'Stopped successfully')}")
        else:
            logger.warning(f"Failed to stop backend conversation: {result.get('error', 'Unknown error')}")
    except Exception as e:
        logger.warning(f"Unexpected error stopping backend conversation: {e}")

    # Shut down backend to ensure memory is saved
    try:
        backend.shutdown_backend()
        logger.info("Backend shutdown complete")
    except Exception as e:
        logger.warning(f"Error during backend shutdown: {e}")

    # Unload Ollama models to release VRAM immediately.
    # provider=="ollama" のキャラに限定する: 従来はAPIキャラのモデル名
    # (claude-*等)をそのままOllamaへ投げ、毎終了時にHTTP 404がログを汚して
    # いた(curl実測 2026-08-01)。埋め込みモデル(ollama時のみ)も対象に追加
    # — こちらは従来どこからも解放されていなかった。/api/generate +
    # keep_alive:0 は embedding専用モデルにも効く(curl実測・done_reason=unload)。
    try:
        from backend.llm.ollama_integration import unload_ollama_model
        from backend.backend import _backend_state
        from backend.conversation.character_manager import load_character_config

        targets = []
        if _backend_state and _backend_state.active_character_id:
            try:
                config = load_character_config(_backend_state.active_character_id)
                if config.get("model_provider") == "ollama":
                    model_name = config.get("model_name") or config.get("ollama_model_name")
                    if model_name:
                        targets.append(model_name)
            except Exception as config_err:
                logger.warning(f"Could not load character config: {config_err}")

        try:
            from backend.shared.api_settings import get_embedding_model
            emb = get_embedding_model()
            if emb and emb[0] == "ollama" and emb[1]:
                targets.append(emb[1])
        except Exception as emb_err:
            logger.warning(f"Could not resolve embedding model for unload: {emb_err}")

        if targets:
            for model_name in dict.fromkeys(targets):
                logger.info(f"Unloading Ollama model '{model_name}'...")
                result = unload_ollama_model(model_name)
                if result.get("success"):
                    logger.info(f"Ollama model '{model_name}' unloaded to release VRAM")
                else:
                    logger.warning(f"Failed to unload Ollama model: {result.get('error', 'Unknown error')}")
        else:
            logger.info("No Ollama models to unload")
    except Exception as e:
        logger.warning(f"Error unloading Ollama model: {e}")

    # Final GPU cleanup to ensure all VRAM is released
    # torch 未ロードなら空にすべき CUDA キャッシュも存在しない — 遅延import化
    # 後に「会話せず終了/再起動」した場合、ここで初 torch import(~5秒)を
    # 払わないための sys.modules ガード(意味は従来と同一)
    try:
        import gc
        gc.collect()
        if 'torch' in sys.modules:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        logger.info("Final GPU memory cleanup complete")
    except Exception as e:
        logger.warning(f"Error during final GPU cleanup: {e}")

    # Note: Gradio demo will be closed by the calling function (complete_shutdown)
    # to ensure proper sequencing of shutdown operations
    logger.info("Shutdown preparation complete.")


def _execute_shutdown(create_restart_flag: bool = False) -> None:
    """
    Shared shutdown execution for both exit and restart.

    Performs clean shutdown, optionally creates restart flag, closes Gradio,
    and terminates the process.

    Args:
        create_restart_flag: If True, creates .restart_flag file before exiting
                            so the PS1 launcher can detect and restart the app.
    """
    action = "restart" if create_restart_flag else "shutdown"
    try:
        logger.info(f"Starting graceful {action} process...")

        # Give time for UI to show message
        time.sleep(1.0)

        # Execute shutdown preparation (cleanup all resources)
        logger.info("Executing shutdown preparation...")
        shutdown_application()
        logger.info("Shutdown preparation complete")

        # Brief delay for final cleanup
        time.sleep(2.0)

        # Create restart flag if requested (PS1 will detect this and restart)
        if create_restart_flag:
            flag_path = APP_ROOT / '.restart_flag'
            try:
                flag_path.write_text('restart')
                logger.info(f"Restart flag created at {flag_path}")
            except Exception as e:
                logger.warning(f"Failed to create restart flag: {e}")
                # Flag creation failure → treated as normal exit (user can manually restart)

        # Close Gradio demo to release port 7860
        global _demo_instance, _uvicorn_server
        if _demo_instance is not None:
            try:
                logger.info("Closing Gradio demo...")
                _demo_instance.close()
                logger.info("Gradio demo closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Gradio demo: {e}")

        # Signal uvicorn to stop (server mode fallback)
        if _uvicorn_server is not None:
            try:
                logger.info("Signaling uvicorn to shut down...")
                _uvicorn_server.should_exit = True
            except Exception as e:
                logger.warning(f"Error signaling uvicorn shutdown: {e}")

        # Force process termination to ensure all resources are released
        # os._exit() bypasses cleanup handlers but shutdown_application()
        # has already been called, so this is safe
        logger.info("Terminating process...")
        os._exit(0)

    except Exception as e:
        logger.error(f"Error during {action}: {e}", exc_info=True)
        # Try to close Gradio and uvicorn as fallback
        try:
            if _demo_instance is not None:
                _demo_instance.close()
        except:
            pass
        try:
            if _uvicorn_server is not None:
                _uvicorn_server.should_exit = True
        except:
            pass
        # Force termination even on error
        logger.info("Force terminating process after error...")
        os._exit(1)


def handle_exit_request() -> str:
    """
    Handle exit button click. Returns a message to display before exit.

    Returns:
        str: Exit confirmation message
    """
    logger.info("Exit requested via UI")

    # Double-check memory-task status (button should be disabled, but safety check)
    if backend.is_memory_task_running():
        logger.warning("Exit requested while memory task in progress - blocking")
        return f"""<div style='text-align: center; padding: 20px; background-color: #ff9800; color: white; border-radius: 10px;'>
            <strong>{t('overlay.extracting_title')}</strong><br>
            {t('overlay.extracting_body')}
        </div>"""

    # Check if there are any active tasks
    has_active_tasks = False
    task_info = ""
    extraction_in_progress = False
    try:
        # Check queue manager status
        queue_status = backend.get_queue_status()
        if queue_status and queue_status.get('queue_size', 0) > 0:
            has_active_tasks = True
            task_info = t('overlay.tasks_waiting', count=queue_status['queue_size'])

        # Check if a memory task is in progress (redundant check, but kept for the
        # message). ガードと同じ述語(is_memory_task_running)を見る=台帳の直接走査を
        # しない(is_alive 判定を含め真実源は backend 側の1本。2026-07-25 稜実機の
        # 「完了済み残骸で抽出中誤表示」も述語側で is_alive 済み)
        extraction_in_progress = backend.is_memory_task_running()
    except Exception as e:
        logger.warning(f"Could not check queue status: {e}")

    # Execute shutdown in non-daemon thread for proper completion
    shutdown_thread = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": False})
    shutdown_thread.daemon = False
    shutdown_thread.start()

    # Return appropriate message based on system state
    if extraction_in_progress:
        return f"""<div style='text-align: center; padding: 20px; background-color: #ff9800; color: white; border-radius: 10px; font-size: 18px;'>
            <strong>{t('overlay.exit_title')}</strong><br><br>
            <span style='font-size: 16px;'>{t('overlay.exit_extract_body', tasks=task_info)}</span><br>
            <span style='font-size: 14px; opacity: 0.9;'>{t('overlay.exit_extract_wait')}</span><br><br>
            <span style='font-size: 16px; font-weight: bold; background-color: rgba(255,255,255,0.2); padding: 10px; border-radius: 5px;'>
                {t('overlay.exit_extract_note')}
            </span><br><br>
            <span style='font-size: 12px; font-style: italic; opacity: 0.8;'>
                {t('overlay.tray_progress')}
            </span>
        </div>"""
    elif has_active_tasks:
        return f"""<div style='text-align: center; padding: 20px; background-color: #ff9800; color: white; border-radius: 10px; font-size: 18px;'>
            <strong>{t('overlay.exit_title')}</strong><br><br>
            <span style='font-size: 16px;'>{t('overlay.exit_tasks_body', tasks=task_info)}</span><br>
            <span style='font-size: 14px; opacity: 0.9;'>{t('overlay.exit_tasks_wait')}</span><br><br>
            <span style='font-size: 16px; font-weight: bold; background-color: rgba(255,255,255,0.2); padding: 10px; border-radius: 5px;'>
                {t('overlay.close_tab_note')}
            </span><br><br>
            <span style='font-size: 12px; font-style: italic; opacity: 0.8;'>
                {t('overlay.tray_done')}
            </span>
        </div>"""
    else:
        return f"""<div style='text-align: center; padding: 20px; background-color: #4CAF50; color: white; border-radius: 10px; font-size: 18px;'>
            <strong>{t('overlay.exit_clean_title')}</strong><br><br>
            <span style='font-size: 16px;'>{t('overlay.exit_clean_body')}</span><br>
            <span style='font-size: 14px; opacity: 0.9;'>{t('overlay.exit_clean_wait')}</span><br><br>
            <span style='font-size: 16px; font-weight: bold; background-color: rgba(255,255,255,0.2); padding: 10px; border-radius: 5px;'>
                {t('overlay.close_tab_note_short')}
            </span><br><br>
            <span style='font-size: 12px; font-style: italic; opacity: 0.8;'>
                {t('overlay.tray_gray')}
            </span>
        </div>"""


def handle_restart_request() -> str:
    """
    Handle restart button click. Performs the same clean shutdown as exit,
    but creates a .restart_flag file so the PS1 launcher can restart the app.

    Returns:
        str: Restart status message HTML
    """
    logger.info("Restart requested via UI")

    # Double-check memory-task status (button should be disabled, but safety check)
    if backend.is_memory_task_running():
        logger.warning("Restart requested while memory task in progress - blocking")
        return f"""<div style='text-align: center; padding: 20px; background-color: #ff9800; color: white; border-radius: 10px;'>
            <strong>{t('overlay.extracting_title')}</strong><br>
            {t('overlay.extracting_body')}
        </div>"""

    # Execute restart in non-daemon thread for proper completion
    restart_thread = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": True})
    restart_thread.daemon = False
    restart_thread.start()

    return f"""<div style='text-align: center; padding: 20px; background-color: #2196F3; color: white; border-radius: 10px; font-size: 18px;'>
        <strong>{t('overlay.restart_title')}</strong><br><br>
        <span style='font-size: 16px;'>{t('overlay.restart_body')}</span><br>
        <span style='font-size: 14px; opacity: 0.9;'>{t('overlay.restart_wait')}</span><br><br>
        <span style='font-size: 16px; font-weight: bold; background-color: rgba(255,255,255,0.2); padding: 10px; border-radius: 5px;'>
            {t('overlay.restart_note')}
        </span>
    </div>"""


def create_gradio_interface(server_mode_enabled: bool = False) -> gr.Blocks:
    """
    Create the Gradio interface components.

    Args:
        server_mode_enabled: Whether server mode is active

    Returns:
        gr.Blocks: Configured Gradio interface
    """
    # Store server mode flag in app_state for conversation.py to check
    app_state.server_mode_enabled = server_mode_enabled

    # Sync server_mode to BackendState so conversation_manager can suppress commands
    try:
        from backend.backend import _backend_state
        if _backend_state:
            _backend_state.server_mode = server_mode_enabled
            if server_mode_enabled:
                logger.info("[ServerMode] Command execution suppressed via BackendState.server_mode=True")
    except Exception as e:
        logger.warning(f"Failed to sync server_mode to BackendState: {e}")

    # Notification toast helper. This <script> block once carried the
    # localStorage-based Primary/Secondary tab coordination; that concept was
    # replaced by the server-side "last one wins" takeover (ST-F, close code
    # 4004), so only showNotification (used by ws_client_js error toasts and
    # mobile) remains.
    notification_script = """
        <script>
        // Show notification
        // Phase 4D: textContent (not innerHTML) for XSS safety — error
        // messages from logger may contain user/model output. durationMs
        // defaulted to 4000 to preserve existing callers' behavior;
        // error_notification passes 5000 explicitly.
        function showNotification(title, message, type = 'info', durationMs = 4000) {
            // Toasts stack in a shared fixed container: simultaneous popups
            // (e.g. the queued-popup flush right after WS connect) must not
            // overlap at one fixed position.
            let stack = document.getElementById('ag-toast-stack');
            if (!stack) {
                stack = document.createElement('div');
                stack.id = 'ag-toast-stack';
                stack.style.cssText = 'position:fixed;top:60px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;align-items:flex-end;';
                document.body.appendChild(stack);
            }
            const notification = document.createElement('div');
            notification.className = `popup-container popup-${type}`;
            // position:static (inline beats the class's position:fixed) so the
            // stack container controls placement.
            notification.style.cssText = 'position:static;min-width:300px;';
            const h3 = document.createElement('h3');
            h3.textContent = title;
            const p = document.createElement('p');
            p.textContent = message;
            notification.appendChild(h3);
            notification.appendChild(p);
            stack.appendChild(notification);

            setTimeout(() => notification.remove(), durationMs);
        }
        </script>
        """
    
    # History page CSS
    history_css = """
    /* History page styles */
    #history-short-term, #history-long-term {
        min-height: 400px;
        max-height: 500px;
        overflow-y: auto;
        background: #181818;
        border-radius: 8px;
        padding: 16px;
        border: 1px solid #3a3a3a;
    }

    .countdown-container {
        margin-bottom: 20px;
        padding: 15px;
        background: #232323;
        border-radius: 8px;
        position: relative;
        overflow: hidden;
    }
    
    .countdown-progress {
        position: absolute;
        top: 0;
        left: 0;
        height: 100%;
        background: linear-gradient(90deg, #4CAF50, #FFC107);
        opacity: 0.2;
        border-radius: 8px;
        transition: width 0.3s ease;
    }
    
    .countdown-text {
        position: relative;
        z-index: 1;
        font-size: 16px;
    }
    
    .history-message {
        margin: 10px 0;
        padding: 12px;
        border-radius: 8px;
        border: 1px solid #3a3a3a;
    }

    .history-message.user-msg {
        background: #1a2a3e;
        margin-left: 10%;
    }

    .history-message.ai-msg {
        background: #2a2a2a;
        margin-right: 10%;
    }

    .msg-header {
        display: flex;
        justify-content: space-between;
        margin-bottom: 8px;
        font-size: 14px;
        color: #999;
    }

    .msg-content {
        color: #d0d0d0;
        line-height: 1.5;
    }

    .history-summary {
        margin: 15px 0;
        padding: 15px;
        border: 1px solid #3a3a3a;
        border-radius: 8px;
        background: #232323;
    }

    .history-summary.high-usage {
        border-color: #ff9800;
        background: #2e2a1a;
    }

    .summary-header {
        display: flex;
        justify-content: space-between;
        margin-bottom: 10px;
        font-weight: bold;
        color: #bbb;
    }

    .summary-content {
        color: #d0d0d0;
        line-height: 1.6;
    }

    .history-empty {
        text-align: center;
        padding: 50px;
        color: #999;
        font-style: italic;
    }

    .chunk-id {
        color: #64b5f6;
    }
    
    .usage {
        color: #ff9800;
        font-size: 14px;
    }
    """

    # Tuning tab CSS
    tuning_css = """
    /* Tuning input - make outer container transparent */
    .tuning-input {
        background-color: transparent !important;
    }

    /* Tuning tab - changed value (green) */
    .tuning-changed input {
        background-color: #1a2e1a !important;
        border-color: #4caf50 !important;
    }

    /* Tuning tab - error value (red) */
    .tuning-error input {
        background-color: #2e1a1a !important;
        border-color: #f44336 !important;
    }

    /* Tuning tab - disabled state */
    .tuning-disabled input {
        background-color: #2a2a2a !important;
        color: #777 !important;
    }

    /* Tuning status message */
    .tuning-status-success {
        color: #4caf50;
        font-weight: bold;
    }

    .tuning-status-error {
        color: #f44336;
        font-weight: bold;
    }

    /* Tuning parameter group - bordered container */
    .tuning-param-group {
        border: 1px solid #3a3a3a !important;
        border-radius: 8px !important;
        padding: 12px !important;
        margin-bottom: 8px !important;
        background-color: #232323 !important;
    }

    /* Tuning description text */
    .tuning-description {
        font-size: 0.85em !important;
        color: #999 !important;
        margin-top: 4px !important;
        line-height: 1.4 !important;
    }

    .tuning-description p {
        margin: 0 !important;
    }
    """

    # Characters page CSS (ELYTHセッションタブ: キャラクター再取得ボタンを
    # 「セッション開始」と同じコンパクト幅+中央寄せに — 稜指摘 2026-07-19)
    characters_css = """
    #elyth-refresh-chars-btn {
        flex: none !important;
        width: 220px !important;
        margin: 0 auto !important;
    }
    """

    # Voice Output tab CSS (稜依頼 2026-07-25): 音声テストボタンを音量調整
    # パネルに対して縦中央に(グレー帯除去後の位置ズレ解消)。
    voice_output_css = """
    /* gap: 実アプリ実測(CDP 2026-07-25)で行gapが1pxしかなくボタンが音量
       パネルに密着して見えた(稜スクショの「ずれ」の正体)。16pxで分離 */
    .tts-volume-row { align-items: stretch !important; gap: 16px !important; }
    #tts-test-col {
        display: flex;
        flex-direction: column;
        /* 稜裁定 2026-07-25: ボタンの上辺を音量パネルの上辺に揃える
           (中央揃えではない)。列上端=行上端=パネル上端 */
        justify-content: flex-start;
    }
    /* equal_height列内でボタンが縦に伸びる(flex-grow)のを止め、既定の
       margin-top 8pxも殺して上辺をパネルに一致させる(ライブ注入で実測済み) */
    #test-tts-button {
        flex-grow: 0 !important;
        height: auto !important;
        margin-top: 0 !important;
    }
    """

    # Auto Prompt tab CSS (稜依頼 2026-07-25): 3領域の高さ揃え+カウント表示の
    # タイマーボックス化。JSの書込み先セレクタ #auto-prompt-countdown p は不変。
    auto_prompt_css = """
    .auto-prompt-row { align-items: stretch !important; }
    .auto-prompt-row .form { height: 100%; }
    #auto-timer-box {
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 6px;
        padding: 12px 14px;
        border: 1px solid var(--border-color-primary, #444);
        border-radius: 10px;
        background: var(--block-background-fill, rgba(255,255,255,0.03));
    }
    #auto-timer-box-title p {
        margin: 0 !important;
        font-size: 12px;
        color: #9ca3af;
        text-align: center;
    }
    #auto-prompt-countdown {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 44px;
        border-radius: 8px;
        background: rgba(0, 0, 0, 0.25);
    }
    #auto-prompt-countdown p {
        margin: 0 !important;
        font-size: 15px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        text-align: center;
    }
    """

    # System page CSS (稜指摘 2026-08-01): リモート切替のステータス行は
    # リスナー停止中は空文字だが、Gradioブロック自体は残りColumnのgapを
    # 1つ食う(チェックボックス下の不自然な空白の正体)。中身が空の間だけ
    # ブロックごと畳む。JS(WS)がtextContentを入れれば:emptyが外れて自動で
    # 再表示・空文字書込みで自動で消える=書き手一人原則は不変。
    system_css = """
    #remote-switch-status:has(#remote-switch-status-text:empty) {
        display: none;
    }
    /* サーバーモード切替のストリーム表示(2026-08-05): StatusTracker は
       show_progress="hidden" が効く前に一瞬 full バリアントでマウントされ、
       コールドスタート時 "queue: 1/1 | 0.3s" がステータス欄に漏れる(CDP実測)。
       このコンポーネントの本文は .html-container 側・.wrap はトラッカー専用
       のため、丸ごと非表示にして進捗表示は fn の yield だけに一本化する。 */
    #system-status-text .wrap {
        display: none !important;
    }
    /* リトライ待ちのカウントダウン(2026-08-05 稜依頼): DOM を毎秒差し替えると
       ちらつくため、CSS カウンターのアニメーションで数字だけを減らす。
       --ag-cd は page_load_js の CSS.registerProperty で <integer> 登録
       (@property の at-rule は Gradio の css= 経路で効かない=CDP実測)。
       5s/from 5 は backend/server/tailscale.py の _CERT_RETRY_WAIT_SEC=5 と対。 */
    /* Gradio の .prose が span に自前の文字色を当て、親divの黄色を数字だけ
       上書きして白くする(稜指摘2026-08-05)→継承を強制して周囲と同色に。 */
    .ag-countdown, .ag-countdown::before {
        color: inherit !important;
    }
    .ag-countdown::before {
        content: counter(ag-cd-counter);
        counter-reset: ag-cd-counter var(--ag-cd, 5);
        animation: ag-cd-anim 5s linear forwards;
    }
    @keyframes ag-cd-anim {
        from { --ag-cd: 5; }
        to   { --ag-cd: 1; }
    }
    """

    # Combine non-chat CSS for gr.Blocks(css=...). CHAT_CSS_BODY is injected
    # via head= instead, so its rules land at the end of <head> — after
    # Gradio's own stylesheets — which preserves the cascade priority that
    # the prior inline-in-chat-HTML embedding (Plan F predecessor) had.
    # notification_script は <script> ブロック。css= に連結すると Gradio が
    # <style>.textContent として注入するため HTML として解析されず一切実行されない。
    # head= 経由で実行させる。
    # LOCAL_FONTS_CSS: 同梱Source Sans 3の@font-face(data URI)。テーマの
    # font=指定(下のgr.Blocks)と対で、Google Fonts参照なしに全UI共通の書体を出す。
    combined_css = LOCAL_FONTS_CSS + get_sidebar_css() + history_css + tuning_css + characters_css + voice_output_css + auto_prompt_css + system_css + FOOTER_HIDE_CSS
    chat_css_head = f'<style>{CHAT_CSS_BODY}</style>' + notification_script
    
    # JavaScript that runs on page load (Gradio 5.x sanitizes onclick/script in gr.HTML)
    page_load_js = """() => {
        document.body.classList.add('dark');

        // リトライ待ちカウントダウン(.ag-countdown)用の整数プロパティ登録。
        // 未登録だと離散補間になり数字が5→1へ一括ジャンプする(CDP実測)。
        // 再登録エラーと非対応ブラウザは握る=その場合も表示は壊れない。
        try {
            CSS.registerProperty({ name: '--ag-cd', syntax: '<integer>',
                                   initialValue: '5', inherits: false });
        } catch (e) {}

        // Auto-stop recording on tab hide
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                const micStatus = document.querySelector('#mic-status p');
                if (micStatus && micStatus.textContent.includes('\\u{1f534}')) {
                    const stopBtn = document.querySelector('[id$="_stop_btn"]');
                    if (stopBtn && !stopBtn.disabled) stopBtn.click();
                }
            }
        });

        // Lightbox setup
        function ensureLightbox() {
            if (document.getElementById('image-lightbox')) return;
            const lb = document.createElement('div');
            lb.id = 'image-lightbox';
            lb.innerHTML = '<img id="lightbox-img" src="" />'
                + '<div class="lightbox-controls">'
                + '<button id="lightbox-download-btn">Download</button>'
                + '<button id="lightbox-close-btn">Close</button>'
                + '</div>';
            document.body.appendChild(lb);
            lb.addEventListener('click', function(e) {
                if (e.target === lb) lb.classList.remove('active');
            });
            document.getElementById('lightbox-close-btn').addEventListener('click', function() {
                lb.classList.remove('active');
            });
            document.getElementById('lightbox-download-btn').addEventListener('click', function() {
                const img = document.getElementById('lightbox-img');
                if (!img.src) return;
                const a = document.createElement('a');
                a.href = img.src; a.download = 'image.png';
                document.body.appendChild(a); a.click(); document.body.removeChild(a);
            });
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    const lb = document.getElementById('image-lightbox');
                    if (lb) lb.classList.remove('active');
                }
            });
        }
        ensureLightbox();

        // Event delegation for lightbox-image clicks (Gradio strips onclick attrs)
        document.addEventListener('click', function(e) {
            const img = e.target.closest('.lightbox-image');
            if (!img) return;
            ensureLightbox();
            const fullSrc = img.getAttribute('data-full-src') || img.src;
            document.getElementById('lightbox-img').src = fullSrc;
            document.getElementById('image-lightbox').classList.add('active');
        });

        // Phase 2C: ws-overlay (reconnecting / error states)
        (function() {
            if (document.getElementById('ws-overlay-style')) return;
            const style = document.createElement('style');
            style.id = 'ws-overlay-style';
            style.textContent = ''
                + '.ws-overlay { position: fixed; inset: 0; z-index: 100000;'
                + '   background: rgba(0,0,0,0.65); display: none;'
                + '   align-items: center; justify-content: center;'
                + '   font-family: inherit; }'
                + '.ws-overlay.visible { display: flex; }'
                + '.ws-overlay-card { background: #1a1a1a; color: #fff;'
                + '   padding: 32px 40px; border-radius: 12px;'
                + '   max-width: min(420px, calc(100vw - 32px)); text-align: center;'
                + '   box-shadow: 0 8px 32px rgba(0,0,0,0.4); }'
                + '.ws-overlay-icon { font-size: 48px; margin-bottom: 12px;'
                + '   line-height: 1; display: inline-block; }'
                + '.ws-overlay-icon.spinning { animation: ws-spin 1s linear infinite; }'
                + '@keyframes ws-spin { to { transform: rotate(360deg); } }'
                + '.ws-overlay-title { font-size: 20px; margin: 0 0 12px; font-weight: 600; }'
                + '.ws-overlay-message { font-size: 14px; margin: 0 0 16px;'
                + '   opacity: 0.85; line-height: 1.5; }'
                + '.ws-overlay-detail { font-size: 12px; opacity: 0.6;'
                + '   margin: 0 0 16px; min-height: 1em; }'
                + '.ws-overlay-action { background: #2563eb; color: #fff; border: none;'
                + '   padding: 10px 24px; border-radius: 6px; cursor: pointer;'
                + '   font-size: 14px; font-weight: 500; }'
                + '.ws-overlay-action:hover { background: #1d4ed8; }'
                + '@media (max-width: 480px) {'
                + '   .ws-overlay-card { padding: 24px 28px; }'
                + '   .ws-overlay-icon { font-size: 40px; }'
                + '   .ws-overlay-title { font-size: 18px; } }';
            document.head.appendChild(style);
        })();
        (function() {
            if (document.getElementById('ws-overlay')) return;
            const overlay = document.createElement('div');
            overlay.id = 'ws-overlay';
            overlay.className = 'ws-overlay';
            overlay.innerHTML = ''
                + '<div class="ws-overlay-card">'
                + '<div class="ws-overlay-icon" id="ws-overlay-icon"></div>'
                + '<h2 class="ws-overlay-title" id="ws-overlay-title"></h2>'
                + '<p class="ws-overlay-message" id="ws-overlay-message"></p>'
                + '<p class="ws-overlay-detail" id="ws-overlay-detail"></p>'
                + '<button class="ws-overlay-action" id="ws-overlay-action"'
                + ' style="display:none;" onclick="location.reload()">リロード</button>'
                + '</div>';
            document.body.appendChild(overlay);
        })();
    }"""

    # JS 内のユーザー可視文言を注入。単純な文字列後置換なので、対象キーの t() 値に
    # シングルクォート/バッククォートを含めないこと(JS 文字列リテラル内挿)。
    page_load_js = (page_load_js
                    .replace('>リロード</button>', '>' + t('js.ovl.reload') + '</button>')
                    .replace('>Download</button>', '>' + t('js.lightbox.download') + '</button>')
                    .replace('>Close</button>', '>' + t('js.lightbox.close') + '</button>'))

    # theme: Default相当のまま、フォントだけ同梱Source Sans 3(LocalFont=文字列指定)に。
    # 組込GoogleFontのfonts.googleapis.comリンクを外すのが目的(外部通信ゼロ方針)。
    with gr.Blocks(
        title="Artificial Girlfriend",
        theme=gr.themes.Default(
            font=["Source Sans 3", "ui-sans-serif", "system-ui", "sans-serif"],
        ),
        css=combined_css,
        head=chat_css_head,
        js=page_load_js,
    ) as demo:
        # (Popup notifications are delivered via WS 'popup_notification' →
        # showNotification toasts — no Gradio-side display component needed.)

        # Main layout with sidebar and content area
        with gr.Row():
            # Sidebar column
            with gr.Column(scale=1, elem_id="sidebar"):
                gr.Markdown("## Artificial Girlfriend", elem_classes="sidebar-title")
                
                # Navigation buttons
                nav_conversation = gr.Button("💬 Conversation", elem_classes="nav-btn active", elem_id="nav-conversation")
                nav_characters = gr.Button("👥 Characters", elem_classes="nav-btn", elem_id="nav-characters")
                nav_history = gr.Button("📜 History", elem_classes="nav-btn", elem_id="nav-history")
                nav_logs = gr.Button("📋 System Logs", elem_classes="nav-btn", elem_id="nav-logs")
                nav_system = gr.Button("⚙️ System", elem_classes="nav-btn", elem_id="nav-system")

                # Audio Status Panel (for TTS browser playback)
                gr.Markdown("---")  # Separator
                with gr.Group(elem_id="audio-status-panel"):
                    utility_panel_html = gr.HTML(
                        value=_build_utility_panel_html(server_mode=server_mode_enabled),
                        elem_id="audio-enable-container"
                    )

                # Hidden fallback for Speechless toggle (always available, even without WS)
                with gr.Row(visible=False):
                    speechless_toggle_value = gr.Textbox(elem_id="speechless-toggle-value", visible=False)
                    speechless_toggle_trigger = gr.Button(elem_id="speechless-toggle-trigger", visible=False)

                # Hidden fallback for Command Execution toggle
                with gr.Row(visible=False):
                    command_execution_toggle_value = gr.Textbox(elem_id="command-execution-toggle-value", visible=False)
                    command_execution_toggle_trigger = gr.Button(elem_id="command-execution-toggle-trigger", visible=False)

                # Hidden fallback for Notes toggle
                with gr.Row(visible=False):
                    notes_toggle_value = gr.Textbox(elem_id="notes-toggle-value", visible=False)
                    notes_toggle_trigger = gr.Button(elem_id="notes-toggle-trigger", visible=False)

                # Camera device row (稜裁定 2026-07-16: Function Calling と
                # Talk Theme の間・白線で区分け。挙動は ws_client_js)
                gr.Markdown("---")  # Separator
                with gr.Group(elem_id="camera-device-panel"):
                    gr.HTML(value=build_camera_device_row_html(),
                            elem_id="camera-device-row-wrap")

                # Talk Theme Settings Panel
                gr.Markdown("---")  # Separator
                with gr.Group(elem_id="talk-theme-panel"):
                    gr.Markdown(f"<h3 style='text-align: center; margin: 5px 0;'>{t('theme.title')}</h3>", elem_classes="theme-panel-title")

                    # Current theme display
                    current_theme_display = gr.Textbox(
                        label=t('theme.current'),
                        value=t('gen.no_talk_theme'),
                        interactive=False,
                        lines=2,
                        max_lines=3,
                        elem_id="current-theme-display",
                        elem_classes="theme-display"
                    )

                    # New theme input
                    new_theme_input = gr.Textbox(
                        label=t('theme.new'),
                        placeholder=t('theme.placeholder'),
                        interactive=False,  # Initially disabled
                        lines=2,
                        max_lines=3,
                        elem_id="new-theme-input",
                        elem_classes="theme-input",
                        max_length=300  # 300 character limit
                    )

                    # Error message display
                    theme_error_display = gr.HTML(
                        value="",
                        visible=False,
                        elem_id="theme-error-display",
                        elem_classes="error-message"
                    )

                    # Buttons
                    with gr.Row():
                        update_theme_btn = gr.Button(
                            t('theme.update'),
                            size="sm",
                            interactive=False,  # Initially disabled
                            elem_id="update-theme-btn",
                            elem_classes="theme-btn"
                        )
                        clear_theme_btn = gr.Button(
                            t('theme.clear'),
                            size="sm",
                            interactive=False,  # Initially disabled
                            elem_id="clear-theme-btn",
                            elem_classes="theme-btn"
                        )

            # Main content area
            with gr.Column(scale=4, elem_id="main-content"):
                # Hidden state to track current page
                current_page = gr.State(value=Pages.CONVERSATION)
                
                # Page 1: Conversation (new structured layout)
                with gr.Column(visible=True, elem_id="page-conversation") as page_conversation:
                        # Create the conversation page
                        conversation_components = create_conversation_page(app_state)
                        
                        # Status display for character management
                        char_mgmt_status = gr.HTML(
                            value="",
                            visible=False,
                            elem_id="char-mgmt-status"
                        )
                
                # Page 2: Character Settings
                with gr.Column(visible=False, elem_id="page-characters") as page_characters:
                    character_components = create_character_settings_page(app_state)
                
                # Page 3: Conversation History (new feature)
                with gr.Column(visible=False, elem_id="page-history") as page_history:
                    history_components = create_conversation_history_page(app_state)
                
                # Page 4: System Logs
                with gr.Column(visible=False, elem_id="page-logs") as page_logs:
                    log_components = create_system_logs_page(app_state, server_mode_enabled)
                
                # Page 5: System Controls (new feature)
                with gr.Column(visible=False, elem_id="page-system") as page_system:
                    system_controls_components = create_system_controls_page(app_state, server_mode_enabled)
        
        # ===== WEBSOCKET INTEGRATION =====
        # Initialize WebSocket for real-time updates
        ws_port = None
        ws_init_js = None
        feature_status = {}
        try:
            feature_status = backend.get_feature_status()
        except Exception:
            pass

        try:
            from backend.server.websocket_server import start_websocket_server
            ws_port = start_websocket_server()
            if ws_port:
                logger.info(f"[WebSocket] Server started on port {ws_port}")
                ws_init_js = create_websocket_init_js(
                    ws_port, server_mode=server_mode_enabled,
                    feature_status=feature_status
                )
                
                # Create hidden update trigger buttons
                with gr.Row(visible=False):
                    ws_update_trigger = gr.Button(
                        "WebSocket Update Trigger",
                        elem_id="ws-update-trigger",
                        visible=False
                    )
                    ws_status_update_trigger = gr.Button(
                        "WebSocket Status Update Trigger",
                        elem_id="ws-status-update-trigger",
                        visible=False
                    )
                    extraction_status_trigger = gr.Button(
                        "Extraction Status Trigger",
                        elem_id="extraction-status-trigger",
                        visible=False
                    )
                    # Auto Prompt用の隠しボタン
                    auto_prompt_generating_trigger = gr.Button(
                        "Auto Prompt Generating Trigger",
                        elem_id="auto-prompt-generating-trigger",
                        visible=False
                    )
                    auto_prompt_complete_trigger = gr.Button(
                        "Auto Prompt Complete Trigger",
                        elem_id="auto-prompt-complete-trigger",
                        visible=False
                    )
            else:
                logger.warning("[WebSocket] Server failed to start, using fallback mode")
        except ImportError:
            logger.warning("[WebSocket] Module not available, using fallback mode")
        except Exception as e:
            logger.error(f"[WebSocket] Failed to initialize: {e}")

        # ===== EVENT HANDLERS SECTION =====
        # All event handlers must be defined after all components are created
        
        # Speechless / Command Execution / Notes toggle fallback handlers (work
        # with or without WS). Bodies live in ui/handlers/feature_toggles.py and
        # publish via backend.shared.feature_commands.dispatch_feature_toggle (B3 / V3).
        speechless_toggle_trigger.click(
            fn=handle_speechless_toggle,
            inputs=[speechless_toggle_value],
            outputs=[speechless_toggle_value]
        )

        command_execution_toggle_trigger.click(
            fn=handle_command_execution_toggle,
            inputs=[command_execution_toggle_value],
            outputs=[command_execution_toggle_value]
        )

        notes_toggle_trigger.click(
            fn=handle_notes_toggle,
            inputs=[notes_toggle_value],
            outputs=[notes_toggle_value]
        )

        # Start/End conversation
        # chat_displayはoutputsに含めない: ステータス文の点滅撤去
        # (後段.thenのget_chat_historyがチャット欄更新の単独の書き手)
        conversation_components["start_end_btn"].click(
            fn=toggle_start_end,
            inputs=[],
            outputs=[
                conversation_components["start_end_btn"],
                conversation_components["voice_btn"]
            ],
            js="""
            () => {
                try {
                    if (!window.ttsAudioContext) {
                        window.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                    }
                    window.ttsAudioContext.resume().then(() => {
                        window.ttsAudioEnabled = true;
                        console.log('[TTS] Audio enabled via Start Conversation click');
                    }).catch(err => {
                        console.error('[TTS] Failed to enable audio:', err);
                    });
                } catch (e) {
                    console.error('[TTS] Error initializing audio:', e);
                }

                // Server mode desktop: request browser mic permission
                if (window.wsManager && window.wsManager.serverMode && !window.isMobileUI) {
                    // Browser mic name display (#browser-mic-name is JS-owned:
                    // sole writer is this block; texts come from data attrs
                    // resolved by t() at build time)
                    const micNameEl = document.getElementById('browser-mic-name');
                    const showMicName = () => {
                        if (!micNameEl) return;
                        try {
                            const track = window.micStream && window.micStream.getAudioTracks()[0];
                            if (track && track.label) {
                                micNameEl.textContent = (micNameEl.dataset.prefix || '') + track.label;
                            }
                        } catch (e) { /* display-only */ }
                    };
                    if (!window.micStream) {
                        navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
                            window.micStream = stream;
                            window.micAvailable = true;
                            if (window.wsManager && window.wsManager.ws &&
                                window.wsManager.ws.readyState === WebSocket.OPEN) {
                                window.wsManager.enqueueSend(JSON.stringify({
                                    action: 'set_browser_mic_status',
                                    available: true
                                }));
                            }
                            console.log('[Mic] Permission granted');
                            showMicName();
                        }).catch(err => {
                            window.micAvailable = false;
                            if (window.wsManager && window.wsManager.ws &&
                                window.wsManager.ws.readyState === WebSocket.OPEN) {
                                window.wsManager.enqueueSend(JSON.stringify({
                                    action: 'set_browser_mic_status',
                                    available: false
                                }));
                            }
                            if (micNameEl) micNameEl.textContent = micNameEl.dataset.denied || '';
                            console.warn('[Mic] Permission denied, text-only mode:', err);
                        });
                    } else {
                        showMicName();
                    }
                }
            }
            """
        ).then(
            # Lock the character dropdown while a conversation is active
            # (unlocked again when the conversation ends). Prevents the
            # dropdown from displaying a character the blocked switch
            # never actually applied.
            fn=sync_char_dropdown_lock,
            inputs=[],
            outputs=[conversation_components["character_dropdown"]],
            queue=False
        ).then(
            # Start前はテキスト入力系を無効化・開始で解放(状態からの導出)
            fn=sync_text_controls_lock,
            inputs=[],
            outputs=[
                conversation_components["text_input"],
                conversation_components["send_btn"],
                conversation_components["clear_btn"],
                conversation_components["attach_btn"],
            ],
            queue=False
        ).then(
            # Refresh character info (name, description, model, icon) on start
            fn=refresh_character_info_on_start,
            inputs=[],
            outputs=[
                conversation_components["char_icon"],
                conversation_components["char_name"],
                conversation_components["char_description"],
                conversation_components["model_info"],
            ],
        ).then(
            # Enable/disable theme panel based on conversation state
            fn=lambda: enable_theme_panel(app_state) if app_state.conversation_started else disable_theme_panel(app_state),
            inputs=[],
            outputs=[new_theme_input, update_theme_btn, clear_theme_btn],
            queue=False
        ).then(
            fn=wait_for_history_load,
            inputs=[],
            outputs=[],
        ).then(
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            fn=get_status_html,
            inputs=[],
            outputs=[conversation_components["status_display"]],
            queue=False
        )

        # Command Deny/Accept buttons
        # Note: Command Deny/Accept buttons removed — approval is handled
        # via inline WS buttons in chat
        
        # System Controls Event Handlers
        # Restart Application
        # js parameter: Same as exit - set shutdown flag and close the window
        # right away (1s). The tray launcher reopens the front after restart.
        system_controls_components["restart_btn"].click(
            fn=handle_restart_request,
            inputs=[],
            outputs=[system_controls_components["status_text"]],
            js="() => { if(window.wsManager) window.wsManager.shutdownInitiated = true; setTimeout(() => window.close(), 1000); }"
        )

        # Exit Application
        # js parameter: Set shutdown flag and close the window right away (1s)
        # The shutdown flag prevents extraction updates from overwriting the shutdown message
        # This ensures proper TCP connection closure (client sends FIN)
        # preventing FIN_WAIT_2 state that delays restart
        system_controls_components["exit_btn"].click(
            fn=handle_exit_request,
            inputs=[],
            outputs=[system_controls_components["status_text"]],
            js="() => { if(window.wsManager) window.wsManager.shutdownInitiated = true; setTimeout(() => window.close(), 1000); }"
        )

        # Switch to Server Mode button (local mode only)
        from ui.handlers.server_mode import make_switch_to_server_handler
        handle_switch_to_server_mode = make_switch_to_server_handler(_execute_shutdown)

        # fn はジェネレータ(ui/handlers/server_mode.py): 準備中→証明書取得の
        # 試行 n/3 をステータス欄へ順次ストリーム描画する(2026-08-05)。
        # クリック時 js はボタン自身を即時「切り替え中...」+disabled にして
        # 押下直後の無反応をなくす(ストリーム開始前の一拍を埋める)。
        # ステータス欄の書き手は Gradio(fn yield)専有のまま=JSでは触らない。
        # inputs=[] なので fn+js 同居は安全(restart/exit ボタンと同型)。
        _sw_label_busy = json.dumps(t('system.switch_to_server_starting'), ensure_ascii=False)
        _sw_label_idle = json.dumps(t('system.server_mode'), ensure_ascii=False)
        _sw_btn_lookup = (
            "const el = document.getElementById('switch-to-server-btn');"
            " const b = el && (el.tagName === 'BUTTON' ? el : el.querySelector('button'));"
        )
        system_controls_components["switch_to_server_btn"].click(
            fn=handle_switch_to_server_mode,
            inputs=[],
            outputs=[system_controls_components["status_text"]],
            # ジェネレータの進捗 yield にローディング表示を被せない
            # (ボタン自身の「切り替え中...」が操作中フィードバックを担う)
            show_progress="hidden",
            js=f"() => {{ {_sw_btn_lookup} if (b) {{ b.disabled = true; b.textContent = {_sw_label_busy}; }} }}",
        ).then(
            fn=lambda: None,
            inputs=[],
            outputs=[],
            # 成功(=shutdown-initiated 描画)なら従来どおり1秒後にクローズ。
            # 失敗(エラー表示)ならボタンを復元して再試行可能にする。
            js=f"() => {{ if(document.querySelector('.shutdown-initiated')) {{ if(window.wsManager) window.wsManager.shutdownInitiated = true; setTimeout(() => window.close(), 1000); }} else {{ {_sw_btn_lookup} if (b) {{ b.disabled = false; b.textContent = {_sw_label_idle}; }} }} }}",
        )

        # Disconnect button (server mode remote desktop only)
        system_controls_components["disconnect_btn"].click(
            fn=handle_remote_disconnect,
            inputs=[],
            outputs=[system_controls_components["status_text"]],
        )

        # Startup registration toggle (ST-D)
        # .input (not .change): fires only on user interaction, so the
        # registry-readback update below cannot re-trigger the handler
        from ui.handlers.startup_toggle import handle_startup_toggle
        system_controls_components["startup_toggle"].input(
            fn=handle_startup_toggle,
            inputs=[system_controls_components["startup_toggle"]],
            outputs=[system_controls_components["startup_toggle"]],
        )

        # 状態行(remote_switch_status)はJS専有=outputsに載せない
        from ui.handlers.remote_switch import handle_remote_switch_toggle
        system_controls_components["remote_switch_toggle"].input(
            fn=handle_remote_switch_toggle,
            inputs=[system_controls_components["remote_switch_toggle"]],
            outputs=[system_controls_components["remote_switch_toggle"]],
        )

        # Chrome profile for the app window (Mac Phase 2)
        from ui.handlers.chrome_profile import handle_chrome_profile_change
        system_controls_components["chrome_profile_dropdown"].input(
            fn=handle_chrome_profile_change,
            inputs=[system_controls_components["chrome_profile_dropdown"]],
            outputs=[system_controls_components["chrome_profile_dropdown"]],
        )

        from ui.handlers.ui_language import handle_language_change
        system_controls_components["language_dropdown"].input(
            fn=handle_language_change,
            inputs=[system_controls_components["language_dropdown"]],
            outputs=[system_controls_components["language_dropdown"]],
        )

        # Note: Extraction status timer has been removed in favor of WebSocket-based updates
        # This eliminates UI flicker caused by periodic timer callbacks
        # Extraction status is now updated via:
        # 1. Start notification: sent when extraction task is added (conversation_manager.py)
        # 2. End notification: sent when extraction completes (status_monitor.py)
        # 3. Both trigger the hidden button #extraction-status-trigger via WebSocket

        # Beep toggle
        conversation_components["beep_toggle"].change(
            fn=toggle_beep,
            inputs=[conversation_components["beep_toggle"]],
            outputs=[conversation_components["beep_toggle"]],
        )
        
        # Beep volume control (通知は10秒で自動消去)
        conversation_components["beep_volume_slider"].change(
            fn=handle_volume_change,
            inputs=[conversation_components["beep_volume_slider"]],
            outputs=[conversation_components["volume_label"], conversation_components["test_result"]]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('test-result'), queue=False
        )

        # Test beep button (通知は10秒で自動消去)
        conversation_components["test_beep_btn"].click(
            fn=test_both_beeps,
            inputs=[],
            outputs=[conversation_components["test_result"]]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('test-result'), queue=False
        )

        # TTS volume control (通知は10秒で自動消去)
        conversation_components["tts_volume_slider"].change(
            fn=handle_tts_volume_change,
            inputs=[conversation_components["tts_volume_slider"]],
            outputs=[conversation_components["tts_volume_label"], conversation_components["tts_test_result"]]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('tts-test-result'), queue=False
        )
        
        # Test TTS voice button
        # js: 押下=ユーザージェスチャでAudioContextを生成/resumeしておく
        # (Start/End押下前でもテスト再生が自動再生ポリシーに阻まれないように。
        # モバイルUIの同ボタンと同じ対策)。
        conversation_components["test_tts_btn"].click(
            fn=test_voice,
            inputs=[],
            outputs=[conversation_components["tts_test_result"]],
            js="""
            () => {
                try {
                    if (!window.ttsAudioContext) {
                        window.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                    }
                    window.ttsAudioContext.resume().then(() => {
                        window.ttsAudioEnabled = true;
                        console.log('[TTS] Audio enabled via Test Voice click');
                    }).catch(() => {});
                } catch (e) {}
            }
            """
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('tts-test-result'), queue=False
        )

        # STT engine selector (Faster-whisper local / OpenAI API)
        # ステータス行(stt_engine_status)は WS の stt_model_status が単独の
        # 書き手(handlers が publish)なので Gradio outputs には載せない。
        conversation_components["stt_engine_radio"].change(
            fn=_change_stt_engine,
            inputs=[conversation_components["stt_engine_radio"]],
            outputs=[
                conversation_components["stt_api_model_dd"],
                conversation_components["stt_local_model_dd"],
                conversation_components["mic_status"],
            ]
        )

        conversation_components["stt_api_model_dd"].change(
            fn=_change_stt_api_model,
            inputs=[conversation_components["stt_api_model_dd"]],
            outputs=[]
        )

        conversation_components["stt_local_model_dd"].change(
            fn=_change_stt_local_model,
            inputs=[conversation_components["stt_local_model_dd"]],
            outputs=[]
        )

        # Mic device selector (standalone sounddevice recording only —
        # server mode greys it out at build time in pages.py). 結果表示は
        # WS の stt_model_status 行(単一書き手)なので outputs は自分に
        # 戻さない(自己書き戻しピンポン回避)。
        conversation_components["mic_device_dd"].change(
            fn=change_mic_device,
            inputs=[conversation_components["mic_device_dd"]],
            outputs=[]
        )

        conversation_components["mic_device_refresh_btn"].click(
            fn=refresh_mic_devices,
            inputs=[],
            outputs=[conversation_components["mic_device_dd"]]
        )

        # Browser mic display refresh (server mode row) — client-side only
        conversation_components["browser_mic_refresh_btn"].click(
            fn=lambda: None,
            inputs=[],
            outputs=[],
            queue=False,
            js=BROWSER_MIC_REFRESH_JS
        )

        # Recording hotkey editor: 保存で検証→永続化→リスナー再構築
        # (再起動不要)。結果表示は10秒で自動消去 — js は必ず .then に分離
        # (同一イベントに fn+js を載せると js の返り値が inputs を
        # 置き換え、無返り値だと全引数 None になる)。
        conversation_components["hotkey_save_btn"].click(
            fn=save_hotkeys,
            inputs=[
                conversation_components["hotkey_start_ctrl"],
                conversation_components["hotkey_start_alt"],
                conversation_components["hotkey_start_shift"],
                conversation_components["hotkey_start_key"],
                conversation_components["hotkey_stop_ctrl"],
                conversation_components["hotkey_stop_alt"],
                conversation_components["hotkey_stop_shift"],
                conversation_components["hotkey_stop_key"],
            ],
            outputs=[conversation_components["hotkey_save_result"]]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('hotkey-save-result'), queue=False
        )

        # Auto Prompt event handlers
        conversation_components["auto_prompt_enabled"].change(
            fn=toggle_auto_prompt,
            inputs=[conversation_components["auto_prompt_enabled"]],
            outputs=[conversation_components["auto_prompt_enabled"]]
        )
        
        conversation_components["auto_timer_duration"].change(
            fn=update_auto_prompt_timer_duration,
            inputs=[conversation_components["auto_timer_duration"]],
            outputs=[]  # 自身への書き戻しは高速ドラッグで.changeを再発火させ続けるため削除
        )
        
        # Font size slider change event
        if "chat_font_slider" in conversation_components:
            from .conversation import update_chat_font_size
            conversation_components["chat_font_slider"].change(
                fn=update_chat_font_size,
                inputs=[conversation_components["chat_font_slider"]],
                outputs=[conversation_components["font_status"]],
                js="""
                (fontSize) => {
                    // Apply font size immediately on client side with !important for priority
                    let style = document.getElementById('client-font-style');
                    if (!style) {
                        style = document.createElement('style');
                        style.id = 'client-font-style';
                        document.head.appendChild(style);
                    }
                    style.textContent = `
                        .ai-bubble, .user-bubble { 
                            font-size: ${fontSize}px !important; 
                        }
                        .message-timestamp { 
                            font-size: ${Math.max(10, fontSize - 3)}px !important; 
                        }
                    `;
                    
                    // Update the label
                    const label = document.querySelector('#font-size-label p');
                    if (label) {
                        label.innerHTML = `<strong>Current: ${fontSize}px</strong>`;
                    }
                    
                    return fontSize;
                }
                """
            )

        # Initial load - populate character dropdown and check status
        demo.load(
            fn=initial_load_with_cleanup,
            inputs=[],
            outputs=[
                conversation_components["character_dropdown"],
                conversation_components["status_display"],
                history_components["character_dropdown"],
                utility_panel_html
            ]
        )

        # Chat auto-scroll observer (unconditional — works in both local and server mode)
        demo.load(fn=lambda: None, inputs=[], outputs=[], js=CHAT_AUTOSCROLL_OBSERVER_JS)

        # 会話状態依存コントロールのリロード同期: build時デフォルトは
        # 「開始前(無効)」なので、会話中のページリロードでボタン/入力欄が
        # 死んだままになるのを実状態から復元する
        demo.load(
            fn=sync_conversation_controls_on_load,
            inputs=[],
            outputs=[
                conversation_components["start_end_btn"],
                conversation_components["voice_btn"],
                conversation_components["text_input"],
                conversation_components["send_btn"],
                conversation_components["clear_btn"],
                conversation_components["attach_btn"],
            ],
            queue=False
        )

        # キャラクター情報カードのリロード復元: build時デフォルトは「未選択」
        # なので、リロードすると選択中のキャラが画面から消えて見える
        # (会話中はドロップダウンがロックされ選び直せない・稜報告 2026-08-08)。
        # queue=False にはしない: Gradio はイベントごとに独立の並行グループ
        # (concurrency_id = id(fn)) を持つので生成中でも待たされず、キュー外
        # 実行にするとUIキャッシュのclearと履歴描画の競合窓を増やすだけになる。
        demo.load(
            fn=restore_character_info_on_load,
            inputs=[],
            outputs=[
                conversation_components["char_icon"],
                conversation_components["char_name"],
                conversation_components["char_description"],
                conversation_components["model_info"],
                conversation_components["mic_status_info"],
            ],
        )

        # Initialize WebSocket client and set initial button states if available
        if ws_init_js:
            demo.load(
                fn=check_extraction_status,
                inputs=[],
                outputs=[
                    system_controls_components["exit_btn"],
                    system_controls_components["restart_btn"],
                    system_controls_components["status_text"],
                    conversation_components["refresh_history_btn"],
                    system_controls_components["switch_to_server_btn"]
                ],
                js=ws_init_js
            )
            
            # Set up WebSocket update trigger handler
            if 'ws_update_trigger' in locals():
                ws_update_trigger.click(
                    fn=get_chat_history,
                    inputs=[],
                    outputs=conversation_components["chat_display"],
                    js=SCROLL_TO_BOTTOM_JS
                )
                logger.info("[WebSocket] Update trigger handler configured")
            
            # Set up WebSocket status update trigger handler
            if 'ws_status_update_trigger' in locals():
                ws_status_update_trigger.click(
                    fn=get_status_html,
                    inputs=[],
                    outputs=conversation_components["status_display"]
                )
                logger.info("[WebSocket] Status update trigger handler configured")

            # Set up extraction status trigger handler
            if 'extraction_status_trigger' in locals():
                extraction_status_trigger.click(
                    fn=check_extraction_status,
                    inputs=[],
                    outputs=[
                        system_controls_components["exit_btn"],
                        system_controls_components["restart_btn"],
                        system_controls_components["status_text"],
                        conversation_components["refresh_history_btn"],
                        system_controls_components["switch_to_server_btn"]
                    ]
                )
                logger.info("[WebSocket] Extraction status trigger handler configured")

            # Set up Auto Prompt trigger handlers
            if 'auto_prompt_generating_trigger' in locals():
                auto_prompt_generating_trigger.click(
                    fn=handle_auto_prompt_generating,
                    inputs=[],
                    outputs=conversation_components["chat_display"],
                    js=SCROLL_TO_BOTTOM_JS
                )
                logger.info("[WebSocket] Auto prompt generating trigger handler configured")

            if 'auto_prompt_complete_trigger' in locals():
                auto_prompt_complete_trigger.click(
                    fn=get_chat_history,
                    inputs=[],
                    outputs=conversation_components["chat_display"],
                    js=SCROLL_TO_BOTTOM_JS
                )
                logger.info("[WebSocket] Auto prompt complete trigger handler configured")
        else:
            # Fallback: Timer-based updates when WebSocket is not available
            logger.info("[WebSocket] Not available, setting up timer fallback")
            
            # Check for updates periodically
            try:
                if hasattr(gr, 'Timer'):
                    # Use Timer API if available (Gradio 4.x+)
                    update_timer = gr.Timer(3.0, active=True)  # Check every 3 seconds
                    update_timer.tick(
                        fn=check_ui_updates,
                        inputs=[],
                        outputs=conversation_components["chat_display"]
                    )
                    logger.info("[Fallback] Timer-based update checking enabled (3s interval)")
                else:
                    logger.warning("[Fallback] Timer API not available, manual refresh required")
            except Exception as e:
                logger.error(f"[Fallback] Failed to set up timer: {e}")

            # Define baseline toggles using Gradio fallback (WS-init-failure path).
            # Only speechless / command_execution / notes have Gradio trigger+value
            # elements; the other toggles are WS-only by design. Without these three
            # fallback definitions their buttons silently no-op (M10).
            initial_speechless = 'true' if feature_status.get('speechless_enabled', False) else 'false'
            initial_command = 'true' if feature_status.get('command_execution_enabled', False) else 'false'
            initial_notes = 'true' if feature_status.get('notes_enabled', False) else 'false'
            speechless_fallback_js = """() => {
    const makeToggle = (fnName, slug, stateVar, label, onColor) => {
        if (window[fnName]) return;
        window[fnName] = function() {
            const newState = !window[stateVar];
            const tb = document.querySelector('#' + slug + '-toggle-value textarea');
            if (tb) {
                const ns = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value').set;
                ns.call(tb, newState ? 'true' : 'false');
                tb.dispatchEvent(new Event('input', { bubbles: true }));
            }
            const btn = document.querySelector('#' + slug + '-toggle-trigger button');
            if (btn) btn.click();
            window[stateVar] = newState;
            const dispBtn = document.getElementById(slug + '-toggle-btn');
            if (dispBtn) {
                dispBtn.textContent = label + ': ' + (newState ? 'ON' : 'OFF');
                dispBtn.style.backgroundColor = newState ? onColor : '#607d8b';
            }
        };
    };
    window.speechlessEnabled = """ + initial_speechless + """;
    window.commandExecutionEnabled = """ + initial_command + """;
    window.notesEnabled = """ + initial_notes + """;
    makeToggle('toggleSpeechless', 'speechless', 'speechlessEnabled', 'Speechless', '#4caf50');
    makeToggle('toggleCommandExecution', 'command-execution', 'commandExecutionEnabled', 'Command', '#4caf50');
    makeToggle('toggleNotes', 'notes', 'notesEnabled', 'Notes', '#4caf50');
    return [];
}"""
            demo.load(fn=lambda: None, inputs=[], outputs=[], js=speechless_fallback_js)

        # Switch active character - update all character info displays and status
        conversation_components["character_dropdown"].change(
            fn=switch_character,
            inputs=[conversation_components["character_dropdown"]],
            outputs=[
                conversation_components["char_icon"],
                conversation_components["char_name"],
                conversation_components["char_description"],
                conversation_components["model_info"],
                conversation_components["mic_status_info"],
                conversation_components["status_display"]
            ]
        ).then(
            # Update chat display immediately to show loading state
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Wait for history to finish loading
            fn=wait_for_history_load,
            inputs=[],
            outputs=[]
        ).then(
            # Update chat display after history is loaded
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Update talk theme display for new character
            fn=lambda: poll_talk_theme(app_state, backend),
            inputs=[],
            outputs=[current_theme_display, theme_error_display]
        ).then(
            # Update tuning inputs for new character
            fn=update_tuning_on_character_change,
            inputs=[conversation_components["character_dropdown"]],
            outputs=[
                conversation_components['tuning_inputs']['temperature'],
                conversation_components['tuning_inputs']['top_k'],
                conversation_components['tuning_inputs']['top_p'],
                conversation_components['tuning_inputs']['min_p'],
                conversation_components['tuning_inputs']['repeat_last_n'],
                conversation_components['tuning_inputs']['repeat_penalty'],
                conversation_components['tuning_inputs']['presence_penalty'],
                conversation_components['tuning_inputs']['frequency_penalty'],
                conversation_components['tuning_inputs']['num_predict'],
                conversation_components['tuning_check_btn'],
                conversation_components['tuning_load_btn'],
                conversation_components['tuning_status'],
                conversation_components['tuning_original']
            ]
        ).then(
            # Enable refresh history button when character is selected
            fn=lambda: gr.update(interactive=True) if app_state.active_character_id else gr.update(interactive=False),
            inputs=[],
            outputs=[conversation_components["refresh_history_btn"]]
        ).then(
            # キャラ選択時の「パラメータを読み込みました」通知も自動消去
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('tuning-status'), queue=False
        )

        # ==============================
        # Refresh History Button Event
        # ==============================
        conversation_components["refresh_history_btn"].click(
            fn=handle_refresh_history,
            inputs=[],
            outputs=[
                conversation_components["refresh_history_feedback"],
                conversation_components["chat_display"]
            ]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('refresh-history-feedback'), queue=False
        )

        # ==============================
        # Talk Theme Management Events
        # ==============================

        # Phase 4B: theme_polling_timer removed. AI tool path
        # (talk_theme_tools._save_theme), user button path
        # (backend.update_talk_theme), and char-switch path
        # (character_ui.py) all broadcast `talk_theme_updated`
        # (snapshot s90), and the JS WS handler updates
        # `#current-theme-display` directly.

        # Update theme button click
        update_theme_btn.click(
            fn=lambda theme: update_talk_theme_click(theme, app_state, backend),
            inputs=[new_theme_input],
            outputs=[current_theme_display, new_theme_input, theme_error_display, new_theme_input]
        ).then(
            # Update chat history to show feedback message immediately.
            # Theme display is synced via WS push (talk_theme_updated).
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        )

        # Clear theme button click
        clear_theme_btn.click(
            fn=lambda: clear_talk_theme_click(app_state, backend),
            inputs=[],
            outputs=[current_theme_display, new_theme_input, theme_error_display]
        ).then(
            # Update chat history to show feedback message immediately.
            # Theme display is synced via WS push (talk_theme_updated).
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        )

        # ==============================
        # Tuning Tab Events
        # ==============================

        # Parameter names for tuning
        TUNING_PARAM_NAMES = ['temperature', 'top_k', 'top_p', 'min_p', 'repeat_last_n',
                              'repeat_penalty', 'presence_penalty', 'frequency_penalty',
                              'num_predict']

        # Tuning Check button click
        conversation_components['tuning_check_btn'].click(
            fn=handle_check_click,
            inputs=[],
            outputs=[
                conversation_components['tuning_inputs']['temperature'],
                conversation_components['tuning_inputs']['top_k'],
                conversation_components['tuning_inputs']['top_p'],
                conversation_components['tuning_inputs']['min_p'],
                conversation_components['tuning_inputs']['repeat_last_n'],
                conversation_components['tuning_inputs']['repeat_penalty'],
                conversation_components['tuning_inputs']['presence_penalty'],
                conversation_components['tuning_inputs']['frequency_penalty'],
                conversation_components['tuning_inputs']['num_predict'],
                conversation_components['tuning_status'],
                conversation_components['tuning_original']
            ]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('tuning-status'), queue=False
        )

        # Tuning Load button click
        conversation_components['tuning_load_btn'].click(
            fn=handle_load_click,
            inputs=[
                conversation_components['tuning_inputs']['temperature'],
                conversation_components['tuning_inputs']['top_k'],
                conversation_components['tuning_inputs']['top_p'],
                conversation_components['tuning_inputs']['min_p'],
                conversation_components['tuning_inputs']['repeat_last_n'],
                conversation_components['tuning_inputs']['repeat_penalty'],
                conversation_components['tuning_inputs']['presence_penalty'],
                conversation_components['tuning_inputs']['frequency_penalty'],
                conversation_components['tuning_inputs']['num_predict']
            ],
            outputs=[
                conversation_components['tuning_status'],
                conversation_components['tuning_original'],
                conversation_components['tuning_inputs']['temperature'],
                conversation_components['tuning_inputs']['top_k'],
                conversation_components['tuning_inputs']['top_p'],
                conversation_components['tuning_inputs']['min_p'],
                conversation_components['tuning_inputs']['repeat_last_n'],
                conversation_components['tuning_inputs']['repeat_penalty'],
                conversation_components['tuning_inputs']['presence_penalty'],
                conversation_components['tuning_inputs']['frequency_penalty'],
                conversation_components['tuning_inputs']['num_predict']
            ]
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('tuning-status'), queue=False
        )

        # Tuning input change events - validate and update color
        for param_name in TUNING_PARAM_NAMES:
            handler = create_tuning_change_handler(param_name)
            conversation_components['tuning_inputs'][param_name].change(
                fn=handler,
                inputs=[
                    conversation_components['tuning_inputs'][param_name],
                    conversation_components['tuning_original']
                ],
                outputs=[conversation_components['tuning_inputs'][param_name]]
            )

        # Voice recording button (unified start/stop)
        # Phase 5 Day 2: single-event generator chain. Replaces the prior
        # 7-stage .then() chain that was vulnerable to Gradio queue
        # interleaving (HAR analysis 2026-04-26 14:14-14:21 UTC found late
        # stages from chain N firing during chain N+1, sometimes dropping
        # stages entirely). concurrency_limit=1 + trigger_mode="once"
        # guarantees full chain serialization and discards clicks dispatched
        # while a chain is pending. MutationObserver auto-scrolls chat on
        # DOM change, so the per-stage SCROLL_TO_BOTTOM_JS is no longer needed.
        conversation_components["voice_btn"].click(
            fn=voice_btn_chain,
            inputs=[conversation_components["is_recording"]],
            outputs=[
                conversation_components["voice_btn"],
                conversation_components["mic_status"],
                conversation_components["recording_time"],
                conversation_components["recording_time"],
                conversation_components["is_recording"],
                conversation_components["recording_start_time"],
                conversation_components["chat_display"],
            ],
            js="""
            () => {
                // Server mode desktop only: manage MediaRecorder for browser mic
                if (!window.wsManager || !window.wsManager.serverMode || window.isMobileUI) {
                    return;
                }

                // Phase 5 Day 1: state machine prevents burst-click from spawning
                // multiple MediaRecorders or sending stale blobs while a previous
                // transcription is still in flight.
                if (typeof window._browserMicState === 'undefined') {
                    window._browserMicState = 'idle';  // 'idle' | 'recording' | 'transcribing'
                }

                if (window._isGenerating) {
                    console.log('[BrowserMic] Generation in progress, ignoring click');
                    return;
                }
                if (window._browserMicState === 'transcribing') {
                    console.log('[BrowserMic] Transcribing previous, ignoring click');
                    return;
                }

                if (!window._browserMicRecording) {
                    // === Start recording ===
                    // Phase 2C: block recording while WS is disconnected
                    if (!window.wsManager.ws || window.wsManager.ws.readyState !== WebSocket.OPEN) {
                        alert('再接続中は録音できません');
                        return;
                    }
                    if (!window.micAvailable || !window.micStream) {
                        console.warn('[BrowserMic] Mic not available');
                        return;  // Python side will check bridge.mic_available
                    }
                    try {
                        window.mediaRecorder = new MediaRecorder(window.micStream);
                        window.recordedChunks = [];
                        window.mediaRecorder.ondataavailable = (e) => {
                            if (e.data.size > 0) window.recordedChunks.push(e.data);
                        };
                        window.mediaRecorder.start();
                        window._browserMicRecording = true;
                        window._browserMicState = 'recording';

                        // 5-minute auto-stop timer
                        window._browserMicTimeout = setTimeout(() => {
                            if (window.mediaRecorder && window.mediaRecorder.state === 'recording') {
                                console.log('[BrowserMic] Auto-stop: 5 min limit');
                                const btn = document.querySelector('#voice-record-btn button') ||
                                            document.querySelector('.voice-btn-recording');
                                if (btn) btn.click();
                            }
                        }, 5 * 60 * 1000);

                        console.log('[BrowserMic] Recording started');
                    } catch(e) {
                        console.error('[BrowserMic] Start failed:', e);
                    }
                } else {
                    // === Stop recording → send audio via WebSocket ===
                    window._browserMicRecording = false;
                    window._browserMicState = 'transcribing';
                    clearTimeout(window._browserMicTimeout);

                    // Phase 5 Day 1: JS-side safety net. Server's wait_for_result
                    // times out at 15s and broadcasts mic_state_reset; this 20s
                    // timer is the fallback for the case where that broadcast
                    // never reaches us (WS down, etc).
                    clearTimeout(window._transcribeJsTimeout);
                    window._transcribeJsTimeout = setTimeout(() => {
                        if (window._browserMicState === 'transcribing') {
                            console.warn('[BrowserMic] JS transcribe timeout — forcing idle');
                            window._browserMicState = 'idle';
                            if (window.mediaRecorder) {
                                try { window.mediaRecorder.onstop = null; } catch (e) {}
                                try { window.mediaRecorder.ondataavailable = null; } catch (e) {}
                                window.mediaRecorder = null;
                            }
                            window.recordedChunks = [];
                        }
                    }, 20000);

                    if (window.mediaRecorder && window.mediaRecorder.state === 'recording') {
                        window.mediaRecorder.onstop = async () => {
                            try {
                                const blob = new Blob(window.recordedChunks, {type: 'audio/webm'});
                                const buffer = await blob.arrayBuffer();
                                if (window.wsManager && window.wsManager.ws &&
                                    window.wsManager.ws.readyState === WebSocket.OPEN) {
                                    window.wsManager.ws.send(buffer);
                                    console.log('[BrowserMic] Audio sent:', buffer.byteLength, 'bytes');
                                } else {
                                    console.error('[BrowserMic] WebSocket not open, audio lost');
                                }
                            } catch(e) {
                                console.error('[BrowserMic] Failed to send audio:', e);
                            }
                        };
                        window.mediaRecorder.stop();
                        console.log('[BrowserMic] Recording stopped, sending audio...');
                    }
                }
            }
            """.replace("再接続中は録音できません", t('js.rec.blocked_reconnect')),
            concurrency_limit=1,
            # "once" が本来の設計(上記コメント)。導入時(6b96618)から誤って
            # "multiple" になっており、チェーン実行中(文字起こし+生成)の
            # クリックが全てキューに積まれ、完了後に幽霊発火して勝手に
            # 録音を再開していた(遅いSTT環境=Macで顕在化)。
            trigger_mode="once",
            show_progress="hidden",
        )

        conversation_components["attach_btn"].upload(
            fn=_handle_attach,
            inputs=[
                conversation_components["attach_btn"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
            ],
            outputs=[
                conversation_components["images_state"],
                conversation_components["documents_state"],
                conversation_components["attach_status"],
            ]
        )

        # Text input event handlers
        # Send button click
        conversation_components["send_btn"].click(
            fn=handle_text_input,
            inputs=[
                conversation_components["text_input"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
            ],
            outputs=[
                conversation_components["text_input"],
                conversation_components["text_status"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
                conversation_components["attach_status"],
            ]
        ).then(
            # 前回の自動隠しを解除して送信ステータスを見せる(js専用then)
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_show_js('text-status'), queue=False
        ).then(
            # Update chat display to show user message
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Start generation (shows generating indicator)
            fn=start_text_generation,
            inputs=[],
            outputs=conversation_components["text_status"]
        ).then(
            # Update chat display to show generating indicator
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Process AI generation
            fn=process_text_generation,
            inputs=[],
            outputs=conversation_components["text_status"]
        ).then(
            # Update chat display to show final response
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # 生成結果ステータス(✅/❌)は10秒で自動消去(稜依頼 2026-08-02)
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('text-status'), queue=False
        )

        # Text input submit (Enter key)
        conversation_components["text_input"].submit(
            fn=handle_text_input,
            inputs=[
                conversation_components["text_input"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
            ],
            outputs=[
                conversation_components["text_input"],
                conversation_components["text_status"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
                conversation_components["attach_status"],
            ]
        ).then(
            # 前回の自動隠しを解除して送信ステータスを見せる(js専用then)
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_show_js('text-status'), queue=False
        ).then(
            # Update chat display to show user message
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Start generation (shows generating indicator)
            fn=start_text_generation,
            inputs=[],
            outputs=conversation_components["text_status"]
        ).then(
            # Update chat display to show generating indicator
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # Process AI generation
            fn=process_text_generation,
            inputs=[],
            outputs=conversation_components["text_status"]
        ).then(
            # Update chat display to show final response
            fn=get_chat_history,
            inputs=[],
            outputs=conversation_components["chat_display"],
            js=SCROLL_TO_BOTTOM_JS
        ).then(
            # 生成結果ステータス(✅/❌)は10秒で自動消去(稜依頼 2026-08-02)
            fn=lambda: None, inputs=[], outputs=[],
            js=_status_auto_hide_js('text-status'), queue=False
        )

        # Clear button click (also clears attached images and documents)
        conversation_components["clear_btn"].click(
            fn=_clear_text_and_attachments,
            inputs=[],
            outputs=[
                conversation_components["text_input"],
                conversation_components["text_status"],
                conversation_components["images_state"],
                conversation_components["documents_state"],
                conversation_components["attach_status"],
            ]
        )

        # Chat display uses event-driven updates instead of polling
        # This prevents flickering and improves performance
        logger.info("Chat display configured for event-driven updates")

        # Try to use Timer if available (Gradio 4.x+)
        try:
            if hasattr(gr, 'Timer'):
                # Icon cleanup runs at startup only (in initial_load_with_cleanup)
                # No periodic timer needed since orphaned icons are rare
                logger.info("Icon cleanup runs at startup only (no periodic timer)")

                # Status update timer - DISABLED to use WebSocket event-driven updates instead
                # This eliminates the periodic flicker caused by timer-based updates
                logger.info("Status timer DISABLED - using WebSocket event-driven updates to eliminate flicker")
                
                # Recording time update timer - runs every second when recording.
                # 出力は recording_time 1本のみ。可視のインジケータ/ボタンを
                # タイマー出力に載せると gr.skip でも毎tickちらつくため
                # (status/auto_prompt がWS化した既知事象)、hotkey同期は
                # WS 'recording_display'(ws_client_js)が担う。
                recording_timer = gr.Timer(1, active=True)  # 1 second
                recording_timer.tick(
                    fn=update_recording_time,
                    inputs=[],
                    outputs=[conversation_components["recording_time"]],
                    show_progress="hidden"
                )
                logger.info("Recording time timer initialized (updates every second)")

                # Error popup timer削除 - show_*_popup はWS 'popup_notification'
                # →showNotification直接描画に移行(Timer tickが表示中の
                # error_displayを毎回再描画してちらつく既知事象の根治)。
                # 未接続時のキューはbackend/shared/popup_state.pyが保持し、
                # デスクトップ接続時にWSサーバーがflushする。

                # Auto Prompt timer削除 - WebSocket方式に移行
                # カウントダウン表示はJavaScript側で処理するため、gr.Timerは不要
                logger.info("Auto prompt now uses WebSocket-based updates (no timer flicker)")

        except Exception as timer_error:
            logger.warning(f"Could not initialize timers (Gradio version compatibility): {timer_error}")
            
        
        # Set up global hotkey callbacks now that UI components are created
        setup_hotkey_callbacks()
        
        # Log initial hotkey status at startup (debug level only)
        try:
            log_hotkey_status()
        except Exception as e:
            logger.debug(f"Could not log initial hotkey status: {e}")
            
        # Get the character management components early for navigation handlers
        create_components = character_components['create']
        edit_components = character_components['edit']

        # Connect navigation buttons to page switching
        nav_conversation.click(
            fn=lambda: switch_page(Pages.CONVERSATION, "nav-conversation"),
            inputs=[],
            outputs=[current_page, page_conversation, page_characters, page_history, page_logs, page_system,
                    nav_conversation, nav_characters, nav_history, nav_logs, nav_system]
        )
            
        nav_characters.click(
            fn=lambda: switch_page(Pages.CHARACTER_SETTINGS, "nav-characters"),
            inputs=[],
            outputs=[current_page, page_conversation, page_characters, page_history, page_logs, page_system,
                    nav_conversation, nav_characters, nav_history, nav_logs, nav_system]
        ).then(
            # フォーム現在値を渡す: 選択肢撒き直しによる保持値の検証エラーと
            # 編集キャラ選択の消失を防ぐ(稜報告 2026-08-15)
            fn=load_character_page_data,
            inputs=[
                create_components["stt_lang_dropdown"],
                create_components["tts_dropdown"],
                create_components["ollama_models_dropdown"],
                edit_components["edit_stt_dropdown"],
                edit_components["edit_tts_dropdown"],
                edit_components["edit_ollama_dropdown"],
                edit_components["existing_char_dropdown"],
            ],
            outputs=[
                create_components["tts_dropdown"],
                edit_components["edit_tts_dropdown"],
                create_components["ollama_models_dropdown"],
                edit_components["edit_ollama_dropdown"],
                edit_components["existing_char_dropdown"],
                char_mgmt_status
            ]
        )
            
        nav_history.click(
            fn=lambda: switch_page(Pages.CONVERSATION_HISTORY, "nav-history"),
            inputs=[],
            outputs=[current_page, page_conversation, page_characters, page_history, page_logs, page_system,
                    nav_conversation, nav_characters, nav_history, nav_logs, nav_system]
        ).then(
            fn=lambda: gr.update(choices=refresh_char_list()),
            inputs=[],
            outputs=[history_components['character_dropdown']]
        )
            
        # Phase 4A: in server mode, do NOT auto-fetch logs on tab navigation.
        # Refresh button is the only update path (bandwidth control).
        nav_logs_chain = nav_logs.click(
            fn=lambda: switch_page(Pages.SYSTEM_LOGS, "nav-logs"),
            inputs=[],
            outputs=[current_page, page_conversation, page_characters, page_history, page_logs, page_system,
                    nav_conversation, nav_characters, nav_history, nav_logs, nav_system]
        )
        if not server_mode_enabled:
            nav_logs_chain.then(
                fn=update_log_view,
                inputs=[],
                outputs=[log_components["log_textbox"]]
            )
            
        nav_system.click(
            fn=lambda: switch_page(Pages.SYSTEM_CONTROLS, "nav-system"),
            inputs=[],
            outputs=[current_page, page_conversation, page_characters, page_history, page_logs, page_system,
                    nav_conversation, nav_characters, nav_history, nav_logs, nav_system]
        )
            
        # Character Management Event Handlers

        # キャラの作成/編集/削除後のドロップダウン更新先: YouTube返信タブの
        # 担当キャラDDも build 時に choices を焼き込むため F5 まで古いままだった
        # (APIキー保存時にモデルDDを撒き直すのと同型の欠陥・稜報告 2026-08-11)。
        # サーバーモードではYouTubeタブ自体が生成されない=従来の2つ組に落とす。
        _yt_char_dd = character_components.get('youtube_control', {}).get('character')
        if _yt_char_dd is not None:
            _refresh_char_dds = refresh_char_dropdowns_with_youtube
            _refresh_char_dd_outputs = [
                conversation_components["character_dropdown"],
                edit_components["existing_char_dropdown"],
                _yt_char_dd,
            ]
        else:
            _refresh_char_dds = refresh_both_char_dropdowns
            _refresh_char_dd_outputs = [
                conversation_components["character_dropdown"],
                edit_components["existing_char_dropdown"],
            ]

        # CREATE character with auto-clear status

        # Rescan MotionPNGPlayer/Asset/ choices (Create form)
        create_components["motion_pngtuber_refresh"].click(
            fn=refresh_motion_folder_choices,
            inputs=[],
            outputs=[create_components["motion_pngtuber_folder"]],
        )

        # Rescan MotionPNGPlayer/Asset/ choices (Edit form)
        edit_components["edit_motion_pngtuber_refresh"].click(
            fn=refresh_motion_folder_choices,
            inputs=[],
            outputs=[edit_components["edit_motion_pngtuber_folder"]],
        )

        # 作成/保存ボタンの押下中表示(稜裁定 2026-08-16): 思考無効化プローブの
        # 初回はモデルロード込みで数秒〜数十秒かかり、保存は outputs=[] で無反応に
        # 見えるため、ボタン自身を disabled+「処理中...」にする。
        # 配線は「busy js → 本体 fn → 既存の後処理 → restore js」の順。inputs を持つ
        # 本体イベントに js を同居させない(js の返り値が inputs を置き換える罠)。
        # .then は前段の成否に関わらず走る=拒否トースト/例外/検証NGでも復元される。
        _char_btn_label_busy = json.dumps(t('charform.processing'), ensure_ascii=False)

        def _char_btn_js(elem_id: str, label_json, disabled: bool) -> str:
            return (
                f"() => {{ const el = document.getElementById('{elem_id}');"
                " const b = el && (el.tagName === 'BUTTON' ? el : el.querySelector('button'));"
                f" if (b) {{ b.disabled = {'true' if disabled else 'false'};"
                f" b.textContent = {label_json}; }} }}"
            )

        _create_btn_busy_js = _char_btn_js("char-create-btn", _char_btn_label_busy, True)
        _create_btn_restore_js = _char_btn_js(
            "char-create-btn", json.dumps(t('charform.create_btn'), ensure_ascii=False), False)
        _save_btn_busy_js = _char_btn_js("char-save-edit-btn", _char_btn_label_busy, True)
        _save_btn_restore_js = _char_btn_js(
            "char-save-edit-btn", json.dumps(t('charform.save_changes'), ensure_ascii=False), False)

        # outputsは入力10コンポーネント: 作成成功時のみビルド時初期値へ
        # リセットされる(失敗時はno-op=入力保持)。結果はgr.Info/gr.Warningの
        # トースト(稜裁定 2026-08-15)。
        # 順序はcreate_characterのreset_inputs/keep_inputsと一致必須。
        create_components["create_btn"].click(
            fn=lambda: None, inputs=[], outputs=[],
            js=_create_btn_busy_js, queue=False
        ).then(
            fn=create_character,
            inputs=[
                create_components["name_input"],
                create_components["summary_input"],
                create_components["icon_upload"],
                create_components["tts_dropdown"],
                create_components["stt_lang_dropdown"],
                create_components["system_prompt_box"],
                create_components["ollama_models_dropdown"],
                create_components["motion_pngtuber_folder"],
                create_components["elyth_system_prompt"],
                create_components["elyth_api_key"],
            ],
            outputs=[
                create_components["name_input"],
                create_components["summary_input"],
                create_components["icon_upload"],
                create_components["tts_dropdown"],
                create_components["stt_lang_dropdown"],
                create_components["system_prompt_box"],
                create_components["ollama_models_dropdown"],
                create_components["motion_pngtuber_folder"],
                create_components["elyth_system_prompt"],
                create_components["elyth_api_key"],
            ],
        ).then(
            fn=_refresh_char_dds,
            inputs=[edit_components["existing_char_dropdown"]],
            outputs=_refresh_char_dd_outputs
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_create_btn_restore_js, queue=False
        )

        # 言語→ローカルTTSエンジンは1対1(ja=SBV2/en=Kokoro): 言語DDのユーザー
        # 操作(.input=プログラム的セットでは発火しない)でボイスDDを絞り込む
        create_components["stt_lang_dropdown"].input(
            fn=update_voice_choices_for_language,
            inputs=[create_components["stt_lang_dropdown"], create_components["tts_dropdown"]],
            outputs=[create_components["tts_dropdown"]],
        )
        edit_components["edit_stt_dropdown"].input(
            fn=update_voice_choices_for_language,
            inputs=[edit_components["edit_stt_dropdown"], edit_components["edit_tts_dropdown"]],
            outputs=[edit_components["edit_tts_dropdown"]],
        )

        # LOAD character for edit
        edit_components["load_edit_btn"].click(
            fn=load_character_for_edit,
            inputs=[edit_components["existing_char_dropdown"]],
            outputs=[
                edit_components["edit_name"],
                edit_components["edit_summary"],
                edit_components["edit_icon_original"],
                edit_components["edit_icon_upload"],
                edit_components["edit_tts_dropdown"],
                edit_components["edit_stt_dropdown"],
                edit_components["edit_system_prompt"],
                edit_components["edit_ollama_dropdown"],
                edit_components["edit_motion_pngtuber_folder"],
                edit_components["edit_elyth_system_prompt"],
                edit_components["edit_elyth_api_key"],
                edit_components["edit_icon_changed"],
            ],
        )

        # Resize uploaded icons for preview display
        # Uses .upload (not .change) to avoid infinite loop when setting the value back
        create_components["icon_upload"].upload(
            fn=resize_icon_preview,
            inputs=[create_components["icon_upload"]],
            outputs=[create_components["icon_upload"]]
        )
        edit_components["edit_icon_upload"].upload(
            fn=resize_icon_preview,
            inputs=[edit_components["edit_icon_upload"]],
            outputs=[edit_components["edit_icon_upload"]]
        )
        # Mark icon as changed on any user interaction (upload/paste/clear).
        # .input fires only on user action, not on programmatic set by Load Config,
        # so the flag reliably distinguishes "touched" from "loaded" (H8).
        edit_components["edit_icon_upload"].input(
            fn=mark_edit_icon_changed,
            inputs=[],
            outputs=[edit_components["edit_icon_changed"]]
        )

        # SAVE character edits: 結果はgr.Info/gr.Warningのトースト。
        # フォーム値は保持され、選択キャラも_refresh_char_ddsが「存在すれば
        # 保持」するので続けて編集→再保存できる(稜裁定 2026-08-15)。
        edit_components["save_edit_btn"].click(
            fn=lambda: None, inputs=[], outputs=[],
            js=_save_btn_busy_js, queue=False
        ).then(
            fn=edit_character,
            inputs=[
                edit_components["existing_char_dropdown"],
                edit_components["edit_name"],
                edit_components["edit_summary"],
                edit_components["edit_icon_upload"],
                edit_components["edit_icon_original"],
                edit_components["edit_icon_changed"],
                edit_components["edit_tts_dropdown"],
                edit_components["edit_stt_dropdown"],
                edit_components["edit_system_prompt"],
                edit_components["edit_ollama_dropdown"],
                edit_components["edit_motion_pngtuber_folder"],
                edit_components["edit_elyth_system_prompt"],
                edit_components["edit_elyth_api_key"],
            ],
            outputs=[],
        ).then(
            fn=_refresh_char_dds,
            inputs=[edit_components["existing_char_dropdown"]],
            outputs=_refresh_char_dd_outputs
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=_save_btn_restore_js, queue=False
        )

        # DELETE - show confirmation dialog
        edit_components["delete_char_btn"].click(
            fn=confirm_character_deletion,
            inputs=[edit_components["existing_char_dropdown"]],
            outputs=[
                edit_components["delete_confirm_box"],
                edit_components["confirm_text"],
                edit_components["confirm_char_id"]
            ],
        )

        # Confirm deletion: 結果はトースト。成功時はremove_characterが
        # 編集フォーム12項目+confirm_char_idをクリアする(削除したキャラの
        # 内容がフォームに残る不整合の根治=稜裁定 2026-08-15)。
        # outputsの順序はremove_characterのreset_form/keep_formと一致必須。
        edit_components["confirm_yes"].click(
            fn=remove_character,
            inputs=[edit_components["confirm_char_id"]],
            outputs=[
                edit_components["delete_confirm_box"],
                edit_components["edit_name"],
                edit_components["edit_summary"],
                edit_components["edit_icon_upload"],
                edit_components["edit_icon_original"],
                edit_components["edit_icon_changed"],
                edit_components["edit_tts_dropdown"],
                edit_components["edit_stt_dropdown"],
                edit_components["edit_system_prompt"],
                edit_components["edit_ollama_dropdown"],
                edit_components["edit_motion_pngtuber_folder"],
                edit_components["edit_elyth_system_prompt"],
                edit_components["edit_elyth_api_key"],
                edit_components["confirm_char_id"],
            ],
        ).then(
            fn=_refresh_char_dds,
            inputs=[edit_components["existing_char_dropdown"]],
            outputs=_refresh_char_dd_outputs
        )

        # Cancel deletion: 確認ボックスを閉じるだけ(それ自体が十分な
        # フィードバック。旧実装の英語ハードコード表示は撤去)
        edit_components["confirm_no"].click(
            fn=lambda: gr.update(visible=False),
            inputs=[],
            outputs=[edit_components["delete_confirm_box"]],
        )

        # ==============================
        # Tuning Defaults Tab Event Handlers
        # ==============================

        # Get tuning defaults components
        tuning_defaults = character_components.get('tuning_defaults', {})
        if tuning_defaults:
            defaults_inputs = tuning_defaults.get('tuning_inputs', {})
            defaults_status = tuning_defaults.get('status')
            defaults_check_btn = tuning_defaults.get('check_btn')
            defaults_load_btn = tuning_defaults.get('load_btn')
            defaults_original = tuning_defaults.get('original')

            # Parameter names
            DEFAULTS_PARAM_NAMES = ['temperature', 'top_k', 'top_p', 'min_p', 'repeat_last_n',
                                    'repeat_penalty', 'presence_penalty', 'frequency_penalty',
                                    'num_predict']

            # Check button click
            if defaults_check_btn and defaults_inputs:
                defaults_check_btn.click(
                    fn=handle_defaults_check_click,
                    inputs=[],
                    outputs=[
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict'],
                        defaults_status,
                        defaults_original
                    ]
                ).then(
                    fn=lambda: None, inputs=[], outputs=[],
                    js=_status_auto_hide_js('defaults-tuning-status'), queue=False
                )

            # Load button click
            if defaults_load_btn and defaults_inputs:
                defaults_load_btn.click(
                    fn=handle_defaults_load_click,
                    inputs=[
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict']
                    ],
                    outputs=[
                        defaults_status,
                        defaults_original,
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict']
                    ]
                ).then(
                    fn=lambda: None, inputs=[], outputs=[],
                    js=_status_auto_hide_js('defaults-tuning-status'), queue=False
                )

            # Input change events for validation and color
            if defaults_inputs and defaults_original:
                for param_name in DEFAULTS_PARAM_NAMES:
                    if param_name in defaults_inputs:
                        handler = create_defaults_change_handler(param_name)
                        defaults_inputs[param_name].change(
                            fn=handler,
                            inputs=[
                                defaults_inputs[param_name],
                                defaults_original
                            ],
                            outputs=[defaults_inputs[param_name]]
                        )

            # ==============================
            # Apply to All Characters Events
            # ==============================

            apply_all_btn = tuning_defaults.get('apply_all_btn')
            apply_all_confirm_box = tuning_defaults.get('apply_all_confirm_box')
            apply_all_confirm_text = tuning_defaults.get('apply_all_confirm_text')
            apply_all_confirm_yes = tuning_defaults.get('apply_all_confirm_yes')
            apply_all_confirm_no = tuning_defaults.get('apply_all_confirm_no')
            apply_all_status = tuning_defaults.get('apply_all_status')

            if apply_all_btn and apply_all_confirm_box:
                # Apply All button -> show confirmation
                apply_all_btn.click(
                    fn=show_apply_all_confirm,
                    inputs=[
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict']
                    ],
                    outputs=[
                        apply_all_confirm_box,
                        apply_all_confirm_text,
                        apply_all_status
                    ]
                )

                # Confirm Yes -> execute
                apply_all_confirm_yes.click(
                    fn=execute_apply_all,
                    inputs=[
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict']
                    ],
                    outputs=[
                        apply_all_confirm_box,
                        apply_all_status,
                        defaults_original,
                        defaults_inputs['temperature'],
                        defaults_inputs['top_k'],
                        defaults_inputs['top_p'],
                        defaults_inputs['min_p'],
                        defaults_inputs['repeat_last_n'],
                        defaults_inputs['repeat_penalty'],
                        defaults_inputs['presence_penalty'],
                        defaults_inputs['frequency_penalty'],
                        defaults_inputs['num_predict']
                    ]
                ).then(
                    fn=lambda: None, inputs=[], outputs=[],
                    js=_status_auto_hide_js('defaults-apply-all-status'), queue=False
                )

                # Confirm No -> cancel
                apply_all_confirm_no.click(
                    fn=cancel_apply_all,
                    inputs=[],
                    outputs=[
                        apply_all_confirm_box,
                        apply_all_status
                    ]
                )

        # System Logs Page Event Handlers
        # Manual refresh button for system logs
        log_components["refresh_logs_btn"].click(
            fn=update_log_view,
            outputs=log_components["log_textbox"]
        )
        
        # Manual refresh button for prompt log
        if "prompt_refresh_btn" in log_components:
            log_components["prompt_refresh_btn"].click(
                fn=update_prompt_view,
                outputs=log_components["prompt_textbox"]
            )
            # トークン行はWS再配信のみ（JS専有div=Gradio出力に載せると
            # ちらつく既知問題。書き手はws_client_jsの一人）
            log_components["prompt_refresh_btn"].click(
                fn=rebroadcast_prompt_tokens,
                outputs=None
            )

        # Manual refresh button for command log
        if "cmd_log_refresh_btn" in log_components:
            log_components["cmd_log_refresh_btn"].click(
                fn=refresh_command_log,
                outputs=log_components["cmd_log_textbox"]
            )

        # ==============================
        # API Settings Tab Event Handlers
        # ==============================

        api_settings_components = character_components.get('api_settings', {})
        if api_settings_components:
            # --- OpenAI ---
            # Key save also fetches the model list and re-populates every
            # derived dropdown (STT / character LLM / embedding) — the
            # build-time frozen choices otherwise needed an app restart.
            api_settings_components['openai_save_btn'].click(
                fn=_save_key_and_refresh("openai"),
                inputs=[api_settings_components['openai_key']],
                outputs=[
                    api_settings_components['openai_status'],
                    api_settings_components['openai_models'],
                    conversation_components['stt_api_model_dd'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                    api_settings_components['embedding_model_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('openai-status'), queue=False
            )
            # STT model dropdown derives from the OpenAI model list -> update together.
            # Character create/edit model dropdowns live on the same page -> update too
            # (they would otherwise stay stale until the next page navigation).
            api_settings_components['openai_refresh_btn'].click(
                fn=_refresh_openai_models_and_stt,
                inputs=[],
                outputs=[
                    api_settings_components['openai_models'],
                    api_settings_components['openai_status'],
                    conversation_components['stt_api_model_dd'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                    api_settings_components['embedding_model_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('openai-status'), queue=False
            )
            api_settings_components['openai_web_search'].change(
                fn=_search_toggle_handler("openai", "web_search_enabled"),
                inputs=[api_settings_components['openai_web_search']],
                outputs=[],
            )

            # --- Anthropic ---
            api_settings_components['anthropic_save_btn'].click(
                fn=_save_key_and_refresh("anthropic"),
                inputs=[api_settings_components['anthropic_key']],
                outputs=[
                    api_settings_components['anthropic_status'],
                    api_settings_components['anthropic_models'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('anthropic-status'), queue=False
            )
            api_settings_components['anthropic_refresh_btn'].click(
                fn=_refresh_models_and_character_dropdowns("anthropic"),
                inputs=[],
                outputs=[
                    api_settings_components['anthropic_models'],
                    api_settings_components['anthropic_status'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('anthropic-status'), queue=False
            )
            api_settings_components['anthropic_web_search'].change(
                fn=_search_toggle_handler("anthropic", "web_search_enabled"),
                inputs=[api_settings_components['anthropic_web_search']],
                outputs=[],
            )

            # --- xAI ---
            api_settings_components['xai_save_btn'].click(
                fn=_save_key_and_refresh("xai"),
                inputs=[api_settings_components['xai_key']],
                outputs=[
                    api_settings_components['xai_status'],
                    api_settings_components['xai_models'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('xai-status'), queue=False
            )
            api_settings_components['xai_refresh_btn'].click(
                fn=_refresh_models_and_character_dropdowns("xai"),
                inputs=[],
                outputs=[
                    api_settings_components['xai_models'],
                    api_settings_components['xai_status'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('xai-status'), queue=False
            )
            api_settings_components['xai_web_search'].change(
                fn=_search_toggle_handler("xai", "web_search_enabled"),
                inputs=[api_settings_components['xai_web_search']],
                outputs=[],
            )
            api_settings_components['xai_x_search'].change(
                fn=_search_toggle_handler("xai", "x_search_enabled"),
                inputs=[api_settings_components['xai_x_search']],
                outputs=[],
            )

            # --- Google ---
            # Google key save / refresh also re-populate the embedding and
            # image-generation dropdowns (both filter the Google model list).
            api_settings_components['google_save_btn'].click(
                fn=_save_key_and_refresh("google"),
                inputs=[api_settings_components['google_key']],
                outputs=[
                    api_settings_components['google_status'],
                    api_settings_components['google_models'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                    api_settings_components['embedding_model_dropdown'],
                    api_settings_components['image_gen_model_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('google-status'), queue=False
            )
            api_settings_components['google_refresh_btn'].click(
                fn=_refresh_google_models_full,
                inputs=[],
                outputs=[
                    api_settings_components['google_models'],
                    api_settings_components['google_status'],
                    create_components['ollama_models_dropdown'],
                    edit_components['edit_ollama_dropdown'],
                    api_settings_components['embedding_model_dropdown'],
                    api_settings_components['image_gen_model_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('google-status'), queue=False
            )
            api_settings_components['google_web_search'].change(
                fn=_search_toggle_handler("google", "web_search_enabled"),
                inputs=[api_settings_components['google_web_search']],
                outputs=[],
            )

            # --- ElevenLabs (TTS) ---
            # Key save also fetches voices + TTS models (same one-event flow
            # as the LLM providers' _save_key_and_refresh) and re-populates
            # every dropdown that derives from them.
            api_settings_components['eleven_save_btn'].click(
                fn=_save_elevenlabs_key_and_refresh,
                inputs=[
                    api_settings_components['eleven_key'],
                    create_components['stt_lang_dropdown'],
                    edit_components['edit_stt_dropdown'],
                ],
                outputs=[
                    api_settings_components['eleven_status'],
                    api_settings_components['eleven_voices_dd'],
                    api_settings_components['eleven_model_dd'],
                    create_components['tts_dropdown'],
                    edit_components['edit_tts_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('eleven-status'), queue=False
            )
            # Voices refresh also re-populates the character TTS dropdowns
            # (same page — they would otherwise stay stale until re-navigation)
            api_settings_components['eleven_voices_refresh_btn'].click(
                fn=_refresh_elevenlabs_voices,
                inputs=[
                    create_components['stt_lang_dropdown'],
                    edit_components['edit_stt_dropdown'],
                ],
                outputs=[
                    api_settings_components['eleven_voices_dd'],
                    api_settings_components['eleven_status'],
                    create_components['tts_dropdown'],
                    edit_components['edit_tts_dropdown'],
                ],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('eleven-status'), queue=False
            )
            api_settings_components['eleven_models_refresh_btn'].click(
                fn=_refresh_elevenlabs_models,
                inputs=[],
                outputs=[api_settings_components['eleven_model_dd'], api_settings_components['eleven_status']],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('eleven-status'), queue=False
            )
            api_settings_components['eleven_model_save_btn'].click(
                fn=_save_elevenlabs_model,
                inputs=[api_settings_components['eleven_model_dd']],
                outputs=[api_settings_components['eleven_status']],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('eleven-status'), queue=False
            )

            # --- Embedding Model ---
            api_settings_components['embedding_save_btn'].click(
                fn=_save_embedding_handler,
                inputs=[api_settings_components['embedding_model_dropdown']],
                outputs=[api_settings_components['embedding_status']],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('embedding-status'), queue=False
            )

            # --- Image Generation Model ---
            api_settings_components['image_gen_save_btn'].click(
                fn=_save_image_gen_handler,
                inputs=[api_settings_components['image_gen_model_dropdown']],
                outputs=[api_settings_components['image_gen_status']],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('image-gen-status'), queue=False
            )

            # --- Ollama Context Size ---
            api_settings_components['ollama_ctx_save_btn'].click(
                fn=_save_ollama_ctx_handler,
                inputs=[api_settings_components['ollama_ctx_dropdown']],
                outputs=[api_settings_components['ollama_ctx_status']],
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('ollama-ctx-status'), queue=False
            )

            # --- Web Search Blacklist ---
            api_settings_components['blacklist_refresh_btn'].click(
                fn=_refresh_blacklist,
                inputs=[],
                outputs=[api_settings_components['blacklist_display']],
            )
            api_settings_components['blacklist_remove_btn'].click(
                fn=_remove_from_blacklist,
                inputs=[api_settings_components['blacklist_display']],
                outputs=[api_settings_components['blacklist_display']],
            )

            # --- Image Input Blacklist ---
            api_settings_components['img_blacklist_refresh_btn'].click(
                fn=_refresh_image_blacklist,
                inputs=[],
                outputs=[api_settings_components['img_blacklist_display']],
            )
            api_settings_components['img_blacklist_remove_btn'].click(
                fn=_remove_from_image_blacklist,
                inputs=[api_settings_components['img_blacklist_display']],
                outputs=[api_settings_components['img_blacklist_display']],
            )

            # --- Google Maps API Key ---
            if 'google_maps_save_btn' in api_settings_components:
                api_settings_components['google_maps_save_btn'].click(
                    fn=_save_google_maps_key,
                    inputs=[api_settings_components['google_maps_key']],
                    outputs=[api_settings_components['google_maps_status']],
                ).then(
                    fn=lambda: None, inputs=[], outputs=[],
                    js=_status_auto_hide_js('google-maps-status'), queue=False
                )

        # History Page Event Handlers
        _history_outputs = [
            history_components['memory_stats'],
            history_components['countdown_display'],
            history_components['short_term_display'],
            history_components['long_term_display'],
            history_components['notes_display'],
            history_components['elyth_notes_display'],
            history_components['elyth_sessions_display'],
            history_components['youtube_tab'],
            history_components['youtube_sessions_display'],
            history_components['current_character'],
        ]

        # Wire up character dropdown change event
        history_components['character_dropdown'].change(
            fn=on_history_character_select,
            inputs=[history_components['character_dropdown']],
            outputs=_history_outputs,
        )

        # Refresh button
        history_components['refresh_btn'].click(
            fn=on_history_character_select,
            inputs=[history_components['current_character']],
            outputs=_history_outputs,
        )
        
        # Memory CRUD event handlers (Phase 4)
        if 'memory_add_btn' in history_components:
            history_components['memory_add_btn'].click(
                fn=handle_add_memory,
                inputs=[
                    history_components['memory_category'],
                    history_components['memory_content_input'],
                    history_components['current_character'],
                ],
                outputs=[
                    history_components['memory_content_input'],
                    history_components['memory_action_status'],
                    history_components['long_term_display'],
                ]
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('memory-action-status'), queue=False
            )

        if 'memory_action_btn' in history_components:
            history_components['memory_action_btn'].click(
                fn=handle_memory_action,
                inputs=[
                    history_components['memory_action_trigger'],
                    history_components['current_character'],
                ],
                outputs=[
                    history_components['long_term_display'],
                    history_components['memory_action_status'],
                ]
            ).then(
                fn=lambda: None, inputs=[], outputs=[],
                js=_status_auto_hide_js('memory-action-status'), queue=False
            )

        # Automatic refresh timers for logs
        # Phase 4A: in server mode, timers are disabled (active=False) to cut
        # background bandwidth. Refresh buttons (already wired above) become
        # the manual update path. Local mode keeps the polling for instant logs.
        try:
            if hasattr(gr, 'Timer'):
                timers_active = not server_mode_enabled

                # Change-detection wrapper: local-mode Logs timers tick every
                # 1-2s and used to re-send the full log text unconditionally even
                # when nothing changed (and the Logs page is hidden). Return
                # gr.skip() when the content is identical to the last tick (M28).
                def _skip_if_unchanged(fn):
                    last = {"v": object()}

                    def wrapped():
                        v = fn()
                        if v == last["v"]:
                            return gr.skip()
                        last["v"] = v
                        return v
                    return wrapped

                # System logs timer
                log_refresh_timer = gr.Timer(LOG_REFRESH_INTERVAL, active=timers_active)
                log_refresh_timer.tick(
                    fn=_skip_if_unchanged(update_log_view),
                    outputs=log_components["log_textbox"]
                )

                # Prompt log timer (refresh less frequently - every 2 seconds)
                # トークン行はここに載せない（WS→JSのDOM直接更新が担う）
                if "prompt_textbox" in log_components:
                    prompt_refresh_timer = gr.Timer(2.0, active=timers_active)
                    prompt_refresh_timer.tick(
                        fn=_skip_if_unchanged(update_prompt_view),
                        outputs=log_components["prompt_textbox"]
                    )

                # Command log timer (refresh every 2 seconds)
                if "cmd_log_textbox" in log_components:
                    cmd_log_refresh_timer = gr.Timer(2.0, active=timers_active)
                    cmd_log_refresh_timer.tick(
                        fn=_skip_if_unchanged(refresh_command_log),
                        outputs=log_components["cmd_log_textbox"]
                    )

                logger.info(
                    f"Log refresh timers initialized (active={timers_active}, "
                    f"server_mode={server_mode_enabled})"
                )
        except Exception as timer_error:
            logger.warning(f"Could not initialize log refresh timer: {timer_error}")
            # Try alternative timer approach
            try:
                # Alternative: use demo.load with every parameter
                demo.load(
                    fn=update_log_view,
                    outputs=log_components["log_textbox"],
                    every=LOG_REFRESH_INTERVAL
                )
                logger.info("System logs refresh using demo.load with 'every' parameter")
            except Exception as alt_error:
                logger.warning(f"Alternative timer approach also failed: {alt_error}")

        # AGPL-3.0 section 13: offer the Corresponding Source to every user who
        # reaches this program over a network. Mounted at "/" (catch-all) in server mode.
        gr.HTML(license_notice_html(), elem_id="ag-license-notice")

    return demo


def _preload_ml_stacks() -> None:
    """
    開店前仕込み: 重量MLライブラリを launch(=開店)前に同期ロードする。

    faster_whisper / Style-Bert-VITS2(→torch/transformers 等)の import は
    モデルロード時まで遅延されており、旧構成はUI表示を最速にするため背景
    スレッドでプリウォームしていた。だが開店直後のキャラ切替が import 完了
    待ちで無言で固まる(温間~8秒/冷間~16秒・2026-07-18実測)。
    稜裁定(2026-07-19): UIが開くまでは遅くてよい・開店後は常にスムーズを優先
    =仕込みは開店前に直列で済ませる。失敗しても起動は止めない(呼出時
    import 経路が生きているため初回利用時に取得される=従来と同じ劣化)。
    """
    t0 = time.perf_counter()
    # SBV2(torch系)とwhisperは独立に仕込む: 片方のimport失敗が他方の
    # 仕込みを巻き添えにしないため(SBV2はMacでも同梱=2026-07-26解禁)。
    # 失敗しても起動は止めない(呼出時import経路が生きているため初回
    # 利用時に取得される=従来と同じ劣化)。
    try:
        import audio_output.audio_output as _ao_impl
        _ao_impl._ensure_sbv2()
    except Exception as e:
        logger.warning(f"Pre-launch SBV2 preload failed (first use will import instead): {e}")
    try:
        import audio_input.audio_input as _ai_impl
        _ai_impl._ensure_whisper_import()
    except Exception as e:
        logger.warning(f"Pre-launch whisper preload failed (first use will import instead): {e}")
    # Kokoro(英語TTS)はキャラが誰も使っていなければ仕込まない(ja専用運用に
    # ~3秒の import 代を払わせない。初回activate時の呼出時importで賄われる)。
    try:
        from backend.conversation.character_manager import any_character_uses_tts_provider
        if any_character_uses_tts_provider("kokoro"):
            from audio_output import kokoro_engine as _kokoro
            _kokoro._ensure_kokoro()
    except Exception as e:
        logger.warning(f"Pre-launch Kokoro preload failed (first use will import instead): {e}")
    logger.info(
        f"Pre-launch ML preload finished in {time.perf_counter() - t0:.1f}s")


def run_ui() -> None:
    """
    Build and launch the Gradio UI with comprehensive error handling.
    """
    global _demo_instance, _uvicorn_server
    
    try:
        # === 1. Determine server mode BEFORE creating the Gradio interface ===
        # This must happen first because WebSocketManager needs server config
        # before it is started inside create_gradio_interface().
        from backend.shared.launch_config import load_launch_config
        config = load_launch_config()
        server_mode_enabled = config.get("server_mode", {}).get("enabled", False)
        web_port = int(config.get("launcher", {}).get("web_port", 7860))

        tailscale_ip = None
        certfile = None
        keyfile = None

        # === 2. Server mode: Tailscale + SSL preparation ===
        if server_mode_enabled:
            from backend.server.tailscale import (
                check_tailscale_available, get_tailscale_ip, ensure_valid_cert
            )

            if not check_tailscale_available():
                raise RuntimeError(t('startup.tailscale_missing'))

            tailscale_ip = get_tailscale_ip()
            certfile, keyfile = ensure_valid_cert()

            # Pre-initialize WebSocketManager singleton with server mode flag.
            # When create_gradio_interface() later calls start_websocket_server(),
            # it will get this already-configured instance.  In server mode,
            # the internal WS server is NOT started — connections come through
            # the FastAPI /ws endpoint on port 7860 instead.
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager(server_mode=True)
            logger.info(f"Server mode initialized: Tailscale IP={tailscale_ip}")

            # macOS (Mac 3-8): hold a caffeinate child for the life of this
            # process so the Mac does not system-sleep while serving remote
            # clients (-i idle sleep, -m disk sleep; display may still
            # sleep). The child dies with us (caffeinate exits when its
            # parent goes away is NOT guaranteed — terminate via atexit).
            from backend.shared.platform_caps import IS_MAC
            if IS_MAC:
                try:
                    import atexit
                    import subprocess
                    caffeinate = subprocess.Popen(['/usr/bin/caffeinate', '-im'])
                    atexit.register(caffeinate.terminate)
                    logger.info(f"caffeinate -im started (pid={caffeinate.pid}) — sleep suppressed for server mode")
                except Exception as e:
                    logger.warning(f"caffeinate start failed ({e}) — Mac may sleep in server mode")

            # Phase 4D: attach WebSocketErrorHandler so ERROR/CRITICAL logs
            # are pushed to clients as toast notifications. Done here (after
            # WebSocketManager init, server-mode-only) to avoid spamming
            # local-mode users and to ensure the singleton exists.
            try:
                ws_error_handler = WebSocketErrorHandler()
                logging.getLogger().addHandler(ws_error_handler)
                logger.info("Phase 4D: WebSocketErrorHandler attached")
            except Exception as e:
                logger.warning(f"Failed to attach WebSocketErrorHandler: {e}")

            # Initialize SessionManager
            from backend.server.session_manager import get_session_manager
            from backend.shared.launch_config import get_launch_config_value
            timeout_min = get_launch_config_value(
                "server_mode", "session_timeout_minutes", 60
            )
            sm = get_session_manager()
            sm.configure(server_mode=True, timeout_minutes=timeout_min)

            def _session_end_callback():
                """Auto-cleanup when remote session ends."""
                # 1. Stop active conversation
                try:
                    from backend.backend import _backend_state
                    if _backend_state and _backend_state.conversation_active:
                        import backend.backend as backend_mod
                        backend_mod.stop_conversation()
                except Exception as e:
                    logger.error(f"[Session] stop_conversation error: {e}")
                # 2. Stop Auto Prompt timer
                try:
                    from ui.conversation import reset_auto_prompt_timer
                    reset_auto_prompt_timer()
                except Exception as e:
                    logger.error(f"[Session] reset_auto_prompt error: {e}")
                # 3. Reset AppState
                try:
                    app_state.conversation_started = False
                except Exception as e:
                    logger.error(f"[Session] app_state reset error: {e}")
                # 4. Clear image buffer
                try:
                    from backend.backend import _backend_state
                    if _backend_state:
                        _backend_state.image_buffer.clear()
                except Exception as e:
                    logger.error(f"[Session] image_buffer clear error: {e}")

            sm.register_session_end_callback(_session_end_callback)

        # === 3. Create the Gradio interface ===
        demo = create_gradio_interface(server_mode_enabled=server_mode_enabled)

        # Store demo instance for cleanup
        _demo_instance = demo

        # UI構築完了 → 重量MLライブラリを開店(launch)前に同期ロード。
        # ここはローカル/サーバー両モード共通の経路(モード分岐は下の Launch)。
        _preload_ml_stacks()

        # === 4. Launch ===
        try:
            # Set favicon before queue() to ensure it's available when App is created
            _project_root = os.path.dirname(os.path.dirname(__file__))
            favicon_path = os.path.join(_project_root, "app_images", "favicon.png")
            demo.favicon_path = favicon_path

            demo.queue()  # Use Gradio's queue system for concurrency

            if server_mode_enabled:
                # === Server Mode ON: FastAPI + Gradio mount + uvicorn (HTTPS) ===
                from fastapi import FastAPI
                from fastapi.responses import RedirectResponse, FileResponse, Response
                from starlette.websockets import WebSocket as StarletteWebSocket
                import uvicorn
                from ui.admin_app import create_admin_interface
                from backend.server.websocket_server import get_websocket_manager

                ws_manager = get_websocket_manager()

                app = FastAPI()

                # Apple touch icon path (favicon_path already set above)
                icon_path = os.path.join(_project_root, "app_images", "Artificial_Girlfriend_Logo.png")

                @app.get("/apple-touch-icon.png")
                async def apple_touch_icon():
                    return FileResponse(icon_path, media_type="image/png")

                # Web App Manifest + icons: lets client browsers install the
                # desktop (/) and mobile (/mobile/) pages as standalone app windows.
                # Registered before the Gradio mounts so these outer routes take
                # precedence (same as /apple-touch-icon.png above); the template's
                # manifest link is made mount-relative in ui/gradio_patches.py.
                from ui.pwa_manifest import register_pwa_routes
                register_pwa_routes(app, mobile_path="/mobile")

                # Static character icons — WebP-encoded, mtime-versioned. Reduces chat
                # HTML download from ~470KB/refresh to ~50KB by replacing inline base64
                # icon embedding with a single cached fetch per character.
                @app.get("/static/icons/{character_id}")
                async def serve_character_icon(character_id: str, v: Optional[str] = None):
                    resolved = await asyncio.to_thread(_resolve_character_icon, character_id)
                    if resolved is None:
                        return Response(
                            content=SVG_FALLBACK_BYTES,
                            media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400, immutable"},
                        )
                    try:
                        mtime = int(resolved.stat().st_mtime)
                    except OSError:
                        return Response(
                            content=SVG_FALLBACK_BYTES,
                            media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=300"},
                        )
                    cache_key = (str(resolved), ICON_SIZE, mtime)
                    cached = _icon_cache_get(cache_key)
                    if cached is None:
                        cached = await asyncio.to_thread(_encode_icon_webp, resolved, ICON_SIZE)
                        if cached is None:
                            return Response(status_code=500)
                        _icon_cache_set(cache_key, cached)
                    return Response(
                        content=cached,
                        media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=86400, immutable"},
                    )

                # WebSocket endpoint — all WS traffic goes through port 7860
                @app.websocket("/ws")
                async def websocket_endpoint(ws: StarletteWebSocket):
                    await ws.accept()
                    await ws_manager.handle_starlette_ws(ws)

                # Redirect /admin -> /admin/ (Starlette Mount requires trailing slash)
                @app.get("/admin")
                async def admin_redirect():
                    return RedirectResponse(url="/admin/")

                # Mount admin panel at /admin FIRST (before catch-all "/")
                admin_demo = create_admin_interface(
                    timeout_minutes=timeout_min
                )
                admin_demo.queue()
                app = gr.mount_gradio_app(app, admin_demo, path="/admin", favicon_path=favicon_path)

                # Redirect /mobile -> /mobile/
                @app.get("/mobile")
                async def mobile_redirect():
                    return RedirectResponse(url="/mobile/")

                # Mount mobile UI at /mobile
                from ui.mobile_app import create_mobile_interface
                mobile_demo = create_mobile_interface(
                    server_mode_enabled=server_mode_enabled
                )
                mobile_demo.queue()
                app = gr.mount_gradio_app(app, mobile_demo, path="/mobile", favicon_path=favicon_path)

                # Mount main UI at "/" (catch-all, must be last)
                app = gr.mount_gradio_app(app, demo, path="/", favicon_path=favicon_path)

                logger.info(
                    f"Starting in server mode (FastAPI + uvicorn) on "
                    f"{tailscale_ip}:{web_port} (HTTPS) — admin at /admin, mobile at /mobile, WS at /ws"
                )
                # Phase 2E: explicit WS keepalive (matches local mode 20s/10s).
                # Without these, Starlette/uvicorn defaults may not send pings,
                # which leaves dead TCP connections undetected on mobile networks.
                _uvicorn_server = uvicorn.Server(uvicorn.Config(
                    app, host=tailscale_ip, port=web_port,
                    ssl_certfile=certfile, ssl_keyfile=keyfile,
                    ws_ping_interval=20,
                    ws_ping_timeout=10,
                ))
                _uvicorn_server.run()
            else:
                # === Server Mode OFF: 現行動作（変更なし） ===
                demo.launch(
                    server_name="127.0.0.1",  # Bind to localhost only
                    server_port=web_port,      # launch_config launcher.web_port (default 7860)
                    inbrowser=False,           # Don't auto-open browser (Chrome will be opened by launcher)
                    share=False,               # Don't create public URL
                    favicon_path=favicon_path
                )
        except Exception as launch_error:
            error_details = traceback.format_exc()
            logger.critical(f"Failed to launch UI: {launch_error}")
            logger.debug(f"UI launch error details: {error_details}")

            # Attempt to show a basic error message if possible
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                port_hint = ""
                if "port" in str(launch_error).lower():
                    # Real resolved path, not a %APPDATA% pattern — the env
                    # var syntax means nothing on macOS (Mac 3-9).
                    from backend.shared.launch_config import LAUNCH_CONFIG_FILE
                    port_hint = t('startup.port_conflict', port=web_port,
                                  config_path=str(LAUNCH_CONFIG_FILE))
                messagebox.showerror("Critical Error", f"Failed to start Artificial Girlfriend UI: {str(launch_error)}\n\nCheck logs for details.{port_hint}")
                root.destroy()
            except:
                # If even tkinter fails, use logger as last resort
                logger.critical(f"Failed to launch UI (check the logs for details): {launch_error}")

            raise RuntimeError(f"Failed to launch UI: {launch_error}")

    except Exception as ui_init_error:
        error_details = traceback.format_exc()
        logger.critical(f"Critical error setting up UI: {ui_init_error}")
        logger.debug(f"UI initialization error details: {error_details}")

        # Try to show a system dialog if UI creation fails
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Critical Error", f"Failed to initialize UI: {str(ui_init_error)}\n\nCheck logs for details.")
            root.destroy()
        except:
            # If even tkinter fails, use logger
            logger.critical(f"Failed to initialize UI (check the logs for details): {ui_init_error}")

        raise RuntimeError(f"Failed to initialize UI: {ui_init_error}")


def _start_control_api() -> None:
    """
    Start the localhost-only control API for the tray launcher (ST-A).

    System control (exit/restart/shutdown) is a composition-root
    responsibility; the transport module (backend/server/control_api.py)
    gets all behavior injected here. Shutdown/restart mirror the UI
    buttons exactly (same _execute_shutdown thread pattern, same
    memory-task guard = is_memory_task_running).
    """
    from backend.server.control_api import start_control_api
    from backend.shared.launch_config import get_launch_config_value
    # ローカル変数 t(Thread) がモジュールの t() を隠すため別名で取り直す
    from backend.shared.i18n import t as _t
    from .state import show_warning_popup

    def _reject_toast(message: str) -> None:
        # アプリ側がトレイの要求を断ったことを AG 画面にも出す(稜裁定 2026-08-16:
        # OS 通知が握り潰される環境があった)。既存の popup_notification 経路=
        # 右上5秒トースト。文言は OS 通知(トレイ側)と同じ理由文。デスクトップ
        # クライアント未接続なら捨てる(起動フェーズのみキュー=popup_state)。
        # サーバーモードの Admin 画面にはトースト機構が無いので出ない(バナー常時表示済み)。
        show_warning_popup(_t('system.tray_action_blocked_title'), message)

    def _shutdown_cb() -> dict:
        if backend.is_memory_task_running():
            logger.warning("Shutdown requested via control API while memory task in progress - blocking")
            _reject_toast(_t('tray.stop_blocked_extracting'))
            return {"accepted": False, "reason": "extracting"}
        logger.info("Shutdown requested via control API (tray)")
        t = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": False})
        t.daemon = False
        t.start()
        return {"accepted": True}

    def _restart_cb() -> dict:
        if backend.is_memory_task_running():
            logger.warning("Restart requested via control API while memory task in progress - blocking")
            _reject_toast(_t('tray.restart_blocked_extracting'))
            return {"accepted": False, "reason": "extracting"}
        logger.info("Restart requested via control API (tray)")
        t = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": True})
        t.daemon = False
        t.start()
        return {"accepted": True}

    def _front_status_cb() -> dict:
        from backend.server.websocket_server import get_client_counts
        return get_client_counts()

    def _switch_mode_cb(body: dict) -> dict:
        if backend.is_memory_task_running():
            logger.warning("Mode switch requested via control API while memory task in progress - blocking")
            reason = _t('system.switch_blocked_extracting')
            _reject_toast(reason)
            return {"accepted": False, "reason": reason}
        to_server = bool(body.get('server_mode'))
        from backend.server.mode_switch import (
            prepare_switch_to_local,
            prepare_switch_to_server,
        )
        ok, err = prepare_switch_to_server() if to_server else prepare_switch_to_local()
        if not ok:
            _reject_toast(err)
            return {"accepted": False, "reason": err}
        logger.info(f"Mode switch requested via control API (tray): -> {'server' if to_server else 'local'}")
        t = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": True})
        t.daemon = False
        t.start()
        return {"accepted": True}

    port = get_launch_config_value('launcher', 'control_port', 7865)
    try:
        bound = start_control_api(
            port=port,
            shutdown_cb=_shutdown_cb,
            restart_cb=_restart_cb,
            front_status_cb=_front_status_cb,
            switch_mode_cb=_switch_mode_cb,
        )
        if bound is not None and bound != port:
            # Self-heal a port collision (M1実測: NTKDaemonが7865を占有):
            # persist the port actually bound — the tray re-reads
            # launch_config on every request, so one write re-links it.
            from backend.shared.launch_config import update_launch_config_value
            if update_launch_config_value('launcher', 'control_port', bound):
                logger.info(f"launcher.control_port updated: {port} -> {bound}")
            else:
                logger.warning(
                    f"launcher.control_port write-back failed — tray will keep "
                    f"trying {port} while the control API listens on {bound}")
    except Exception as e:
        # Background convenience service — never fatal for the app itself
        logger.error(f"Failed to start control API: {e}")


def _start_remote_switch_listener() -> None:
    """
    Configure (and, when opted in, start) the remote mode-switch listener.

    Local mode only — in server mode the real UI already listens on the
    Tailscale IP. Like the control API, the transport module
    (backend/server/remote_switch_listener.py) gets all behavior injected
    here; configure() always runs so the System-page checkbox
    (ui/handlers/remote_switch.py) can start/stop it without re-wiring.
    """
    from backend.server.remote_switch_listener import get_remote_switch_listener
    from backend.shared.launch_config import get_launch_config_value

    if get_launch_config_value('server_mode', 'enabled', False):
        return

    def _remote_switch_cb() -> dict:
        from backend.shared.i18n import t as _t
        if backend.is_memory_task_running():
            logger.warning("Remote mode switch requested while memory task in progress - blocking")
            return {"accepted": False, "reason": _t('system.switch_blocked_extracting')}
        from backend.server.mode_switch import prepare_switch_to_server
        ok, err = prepare_switch_to_server()
        if not ok:
            return {"accepted": False, "reason": err}
        logger.info("Mode switch requested via remote switch page: -> server")
        th = threading.Thread(target=_execute_shutdown, kwargs={"create_restart_flag": True})
        th.daemon = False
        th.start()
        return {"accepted": True, "reason": ""}

    def _remote_switch_status_cb() -> None:
        # Push the translated status line to the JS-owned div on the System
        # page (fires on every transition + each watchdog tick; delivery is
        # a no-op until the WS server is up and a client is connected).
        from backend.shared.ui_events import publish_ui_update
        from ui.handlers.remote_switch import remote_switch_status_text
        publish_ui_update('remote_switch_status',
                          data={'message': remote_switch_status_text()})

    def _remote_switch_busy_cb():
        # 記憶タスク中(抽出/relationship)は切替ページを開いた時点でボタンを
        # グレー+理由表示にする(2026-08-16 稜裁定)。押下時の拒否は
        # _remote_switch_cb 側に残る=二重の守り。判定は同じ述語1本
        from backend.shared.i18n import t as _t
        if backend.is_memory_task_running():
            return _t('remote_switch.busy_reopen')
        return None

    listener = get_remote_switch_listener()
    listener.configure(
        _remote_switch_cb,
        int(get_launch_config_value('launcher', 'web_port', 7860)),
        on_status_change=_remote_switch_status_cb,
        busy_cb=_remote_switch_busy_cb,
    )
    if get_launch_config_value('server_mode', 'remote_switch_enabled', False):
        listener.start()


def main():
    """
    Main entry point for the Artificial Girlfriend application.
    Handles initialization, UI launch, and cleanup.
    """
    initialize_application()
    _start_control_api()
    _start_remote_switch_listener()
    try:
        run_ui()
    except Exception as e:
        logger.critical(f"Application crashed: {e}", exc_info=True)
        raise
    finally:
        if not getattr(shutdown_application, '_shutdown_called', False):
            shutdown_application()


# Main Entry Point
if __name__ == "__main__":
    main()