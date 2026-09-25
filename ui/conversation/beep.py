"""
ui/conversation/beep.py

録音開始/停止ビープ音の責務。波形生成・再生・テスト・音量設定を持つ。
"""

import logging
import time

import numpy as np

from ..state import app_state
from ..constants import (
    AUDIO_SAMPLE_RATE, BEEP_DURATION, START_BEEP_FREQUENCY,
    STOP_BEEP_FREQUENCY,
)
# create_beep_sounds() 内のローカル変数 t (np.linspace) と紛れないよう _t で import
from backend.shared.i18n import t as _t

from .tts_playback import _send_audio_to_browser

logger = logging.getLogger(__name__)


def toggle_beep(beep_state: bool) -> bool:
    """
    Toggle beep sound setting when user changes the checkbox.
    
    Args:
        beep_state: The new state of the beep checkbox
        
    Returns:
        bool: The updated beep state
    """
    app_state.beep_enabled = beep_state
    app_state.add_log_message("info", f"Beep sounds for mic start/stop set to {beep_state}.")
    
    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)
    
    return app_state.beep_enabled


def create_beep_sounds() -> None:
    """
    Create beep sound effects for microphone start/stop indicators.
    Creates normalized beeps at full amplitude - volume is applied during playback.
    
    Returns:
        None
    """
    try:
        # Create synthetic beep sounds - simple sine waves at full amplitude
        # Start beep: higher pitch
        t = np.linspace(0, BEEP_DURATION, int(AUDIO_SAMPLE_RATE * BEEP_DURATION), False)
        app_state.start_beep = np.sin(2 * np.pi * START_BEEP_FREQUENCY * t)
        
        # Stop beep: lower pitch
        t = np.linspace(0, BEEP_DURATION, int(AUDIO_SAMPLE_RATE * BEEP_DURATION), False)
        app_state.stop_beep = np.sin(2 * np.pi * STOP_BEEP_FREQUENCY * t)
        
        logger.info("Beep sound effects loaded successfully")
    except Exception as e:
        logger.warning(f"Failed to create beep sounds: {e}")
        # Create empty arrays as fallbacks
        app_state.start_beep = np.zeros(8000)
        app_state.stop_beep = np.zeros(8000)


def play_beep_sound(is_start: bool) -> None:
    """
    Play a beep sound when recording starts or stops.
    
    Args:
        is_start: True for start beep, False for stop beep
        
    Returns:
        None
    """
    if not app_state.beep_enabled:
        return
        
    try:
        # Use the appropriate beep sound based on whether we're starting or stopping
        beep_data = app_state.start_beep if is_start else app_state.stop_beep

        if beep_data is not None:
            # Ensure volume is within valid range
            volume = max(0.0, min(1.0, app_state.beep_volume))
            # Browser playback - send via WebSocket (non-blocking)
            _send_audio_to_browser(beep_data, AUDIO_SAMPLE_RATE, volume,
                                   include_lipsync=False, block=False)
            app_state.add_log_message("debug", f"Played {'start' if is_start else 'stop'} beep at {volume*100:.0f}% volume")
        else:
            app_state.add_log_message("warning", "Beep sound data not initialized")
    except Exception as e:
        app_state.add_log_message("warning", f"Failed to play beep sound: {e}")


def test_beep_sound(beep_type: str = "both") -> str:
    """
    Test the beep sound functionality without starting/stopping recording.
    
    Args:
        beep_type: Which beep to test - "start", "stop", or "both"
        
    Returns:
        str: Status message for the UI
    """
    if not app_state.beep_enabled:
        return _t('beep.disabled')
    
    if not app_state.audio_output_available:
        return _t('tts.audio_unavailable')
    
    try:
        # Ensure beep sounds are created
        if app_state.start_beep is None or app_state.stop_beep is None:
            create_beep_sounds()
        
        # Play the requested beep(s)
        if beep_type == "start":
            play_beep_sound(is_start=True)
            return _t('beep.played_start', percent=f"{app_state.beep_volume*100:.0f}")
        elif beep_type == "stop":
            play_beep_sound(is_start=False)
            return _t('beep.played_stop', percent=f"{app_state.beep_volume*100:.0f}")
        else:  # "both"
            # Play start beep, wait, then stop beep
            play_beep_sound(is_start=True)
            # Small delay between beeps
            time.sleep(0.3)
            play_beep_sound(is_start=False)
            return _t('beep.played_both', percent=f"{app_state.beep_volume*100:.0f}")
            
    except Exception as e:
        app_state.add_log_message("error", f"Error testing beep sound: {e}")
        return _t('beep.test_error', error=str(e))


def update_beep_volume(volume_percent: float) -> str:
    """
    Update the beep volume setting.
    
    Args:
        volume_percent: Volume as percentage (0-100)
        
    Returns:
        str: Status message
    """
    # Convert percentage to 0.0-1.0 range
    volume = volume_percent / 100.0
    volume = max(0.0, min(1.0, volume))
    
    app_state.beep_volume = volume
    app_state.add_log_message("info", f"Beep volume set to {volume_percent:.0f}%")
    
    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)
    
    return _t('beep.volume_set', percent=f"{volume_percent:.0f}")
