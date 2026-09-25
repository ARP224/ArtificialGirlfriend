"""
settings_manager.py

UI-layer settings glue for Artificial Girlfriend.

The settings-persistence *core* (directory resolution, defaults, load/save/
get/update) was moved to the shared layer ``backend/shared/settings_store.py``
to remove the backend->ui upward import / hard cycle. It is re-exported
below so existing UI callers
(``from ui.settings_manager import get_setting`` etc.) keep working unchanged.

What stays here is the UI-layer glue that binds settings to ``app_state``:
``apply_settings_to_app_state`` / ``save_app_state_settings``.
"""

import logging
from typing import Any

# Settings-persistence core now lives in the shared layer. Re-exported so the
# public surface of this module is unchanged for UI callers.
from backend.shared.settings_store import (
    DEFAULT_SETTINGS,
    SETTINGS_FILE,
    delete_setting,
    get_setting,
    get_settings_dir,
    load_settings,
    save_settings,
    update_setting,
)

logger = logging.getLogger(__name__)

__all__ = [
    'DEFAULT_SETTINGS',
    'SETTINGS_FILE',
    'delete_setting',
    'get_setting',
    'get_settings_dir',
    'load_settings',
    'save_settings',
    'update_setting',
    'apply_settings_to_app_state',
    'save_app_state_settings',
]


def apply_settings_to_app_state(app_state: Any) -> None:
    """
    Apply loaded settings to the application state.
    
    Args:
        app_state: The AppState instance to update
    """
    try:
        settings = load_settings()
        
        # Apply auto prompt settings
        auto_prompt = settings.get('auto_prompt', {})
        app_state.auto_prompt_enabled = auto_prompt.get('enabled', False)
        app_state.auto_prompt_timer_duration = auto_prompt.get('timer_duration', 60)
        app_state.auto_prompt_ja = auto_prompt.get('prompt_ja', DEFAULT_SETTINGS['auto_prompt']['prompt_ja'])
        app_state.auto_prompt_en = auto_prompt.get('prompt_en', DEFAULT_SETTINGS['auto_prompt']['prompt_en'])
        
        # Apply audio settings
        audio = settings.get('audio', {})
        app_state.beep_enabled = audio.get('beep_enabled', True)
        app_state.beep_volume = audio.get('beep_volume', 0.5)
        app_state.tts_volume = audio.get('tts_volume', 0.7)
        
        # Apply display settings
        display = settings.get('display', {})
        app_state.chat_font_size = display.get('chat_font_size', 14)

        # NOTE: feature toggles ('features') and device settings
        # ('devices') are no longer mirrored on app_state. The backend owns them
        # as the single source of truth and loads them itself from this same file
        # in init_backend(); the UI reads runtime values via get_feature_status().

        logger.info("Applied saved settings to application state")
    except Exception as e:
        logger.error(f"Failed to apply settings to app state: {e}")


def save_app_state_settings(app_state: Any) -> bool:
    """
    Save current app state settings to file.
    
    Args:
        app_state: The AppState instance to save from
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Load existing settings first to preserve categories not managed by app_state
        # (e.g., 'elyth' settings saved via ELYTH Control tab)
        settings = load_settings()

        # Update only the categories managed by app_state
        settings['auto_prompt'] = {
            'enabled': app_state.auto_prompt_enabled,
            'timer_duration': app_state.auto_prompt_timer_duration,
            'prompt_ja': app_state.auto_prompt_ja,
            'prompt_en': app_state.auto_prompt_en
        }
        # 'audio' also carries keys NOT managed by app_state (stt_engine /
        # stt_api_model, written directly via settings_store by the STT
        # engine selector). Update only the app_state-owned keys — replacing
        # the whole category would revert the STT engine on every shutdown.
        audio = settings.setdefault('audio', {})
        audio['beep_enabled'] = app_state.beep_enabled
        audio['beep_volume'] = app_state.beep_volume
        audio['tts_volume'] = app_state.tts_volume
        # 'display' also carries keys NOT managed by app_state (ui 'language',
        # written directly via settings_store by the System page dropdown).
        # Update only the app_state-owned key — replacing the whole category
        # here would silently revert the UI language on every shutdown.
        settings.setdefault('display', {})['chat_font_size'] = app_state.chat_font_size
        # NOTE: the 'features' and 'devices' sections are intentionally
        # NOT written from app_state here. The backend owns those flags and writes
        # them to this file itself whenever they change (feature_toggle_service /
        # device_settings -> update_setting). load_settings() above preserves the
        # existing 'features'/'devices' sections so this save never clobbers them.

        return save_settings(settings)
    except Exception as e:
        logger.error(f"Failed to save app state settings: {e}")
        return False