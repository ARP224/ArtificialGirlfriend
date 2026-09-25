"""
ui/conversation/tts_playback.py

TTS応答の再生責務。TTSデータ検証・ブラウザへの音声送出・音量設定・テスト発話を持つ。
"""

import logging
import traceback
from typing import Any

import numpy as np

from ..state import app_state, show_warning_popup
from backend.shared.timing_logger import timing_block, end_turn
from backend.shared.i18n import t
from backend.shared.prompt_i18n import get_prompt_language
from backend.shared.ui_events import publish_ui_update

import backend
import audio_output

from .auto_prompt import start_auto_prompt_timer

logger = logging.getLogger(__name__)


def update_tts_volume(volume_percent: float) -> str:
    """
    Update the TTS (Text-to-Speech) volume setting.

    Args:
        volume_percent: Volume as percentage (0-100)

    Returns:
        str: Status message
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    # Convert percentage to 0.0-1.0 range
    volume = volume_percent / 100.0
    volume = max(0.0, min(1.0, volume))
    
    app_state.tts_volume = volume
    app_state.add_log_message("info", f"TTS volume set to {volume_percent:.0f}%")
    
    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)
    
    return t('tts.volume_set', percent=f"{volume_percent:.0f}")


# キャラ言語ごとのテスト発話文。locales/prompts/ は「LLM に届く byte 契約」の
# カタログなので、TTS にしか行かないこの文言はここに置く(2026-07-18 稜裁定)。
# 言語キーは get_prompt_language() の返す値(現状 'ja'/'en'・無指定は 'en')。
_TEST_PHRASES = {
    'ja': "これはテストです。聞こえていますか？",
    'en': "This is a test. Can you hear me.",
}


def test_tts_voice() -> str:
    """
    Test the TTS voice functionality with a sample phrase.

    Returns:
        str: Status message for the UI
    """
    # 早期returnより前に必ず1行残す(サーバーモード等で「押下がハンドラまで
    # 届いたか」をログだけで切り分けるため)。
    app_state.add_log_message("info", "TTS voice test requested")

    if not app_state.audio_output_available:
        return t('tts.audio_unavailable')

    if not app_state.active_character_id:
        return t('tts.select_char')

    try:
        # Get character's language setting
        try:
            config_response = backend.load_character_config(app_state.active_character_id)
            if isinstance(config_response, dict) and 'result' in config_response:
                char_config = config_response.get('result', {})
            else:
                char_config = config_response
        except Exception as e:
            app_state.add_log_message("warning", f"Could not load character config, defaulting to English: {e}")
            char_config = None

        # キャラ言語の解決は正準ヘルパに一本化(直読みはドリフトの温床)。
        language = get_prompt_language(char_config)
        test_phrase = _TEST_PHRASES.get(language, _TEST_PHRASES['en'])
        app_state.add_log_message("info", f"Testing TTS voice ({language}): {test_phrase}")

        # Convert to speech
        try:
            # 本番の会話TTS(play_tts_response)と同じ呼び方に統一 — テストは本番と
            # 同じ声・同じスタイルで鳴ってこそ確認になる(旧実装は ja のみ
            # style="通常" を明示する非対称で、モデルに無いと警告+Neutralフォール
            # バックのログノイズ源だった)。
            audio_data, sr = audio_output.text_to_speech(test_phrase)

            # Validate TTS data
            if not validate_tts_data(audio_data, sr):
                return t('tts.test_invalid')
            
            # Additional diagnostics for audio issues
            app_state.add_log_message("info", f"[TEST TTS] Generated audio: {len(audio_data)} samples at {sr}Hz")
            app_state.add_log_message("info", f"[TEST TTS] Audio type: {type(audio_data)}, dtype: {audio_data.dtype if hasattr(audio_data, 'dtype') else 'N/A'}")

            # Browser playback - send via WebSocket
            _play_test_audio_browser(audio_data, sr)

            # Check peak level for diagnostics
            peak_level = np.max(np.abs(audio_data))

            return t('tts.test_done', percent=f"{app_state.tts_volume*100:.0f}",
                     peak=f"{peak_level:.2f}")
            
        except Exception as tts_error:
            app_state.add_log_message("error", f"TTS test error: {tts_error}")
            return t('tts.test_failed', error=str(tts_error))
            
    except Exception as e:
        app_state.add_log_message("error", f"Error during voice test: {e}")
        return t('tts.test_error', error=str(e))


def validate_tts_data(audio_data: Any, sample_rate: Any) -> bool:
    """
    Validate TTS return values.
    
    Args:
        audio_data: The audio data from TTS
        sample_rate: The sample rate from TTS
        
    Returns:
        bool: True if data is valid, False otherwise
    """
    if audio_data is None:
        app_state.add_log_message("warning", "TTS returned None for audio data")
        return False
    
    if len(audio_data) == 0:
        app_state.add_log_message("warning", "TTS generated empty audio data")
        return False
    
    if sample_rate <= 0:
        app_state.add_log_message("warning", f"TTS returned invalid sample rate: {sample_rate}")
        return False
    
    return True


def handle_tts_error(error: Exception) -> None:
    """
    Handle TTS errors with appropriate user messages.
    
    Args:
        error: The TTS exception
    """
    app_state.add_log_message("error", f"TTS error: {error}")
    error_details = traceback.format_exc()
    logger.debug(f"TTS error details: {error_details}")
    
    # Provide specific error messages based on error type
    error_msg = str(error).lower()
    if "model" in error_msg:
        show_warning_popup(t('tts.model_error_title'),
                          t('tts.model_error_msg'))
    elif "memory" in error_msg or "cuda" in error_msg or "gpu" in error_msg:
        show_warning_popup(t('tts.resource_error_title'),
                          t('tts.resource_error_msg'))
    else:
        show_warning_popup(t('tts.error_title'), t('tts.speech_failed', error=str(error)))


def play_tts_response(ai_reply: str) -> None:
    """
    Convert AI reply to speech and play it.
    Supports two playback modes:
    - "browser": Send audio via WebSocket for browser playback (default)
    - "local": Play via sounddevice locally

    Args:
        ai_reply: The AI's text response
    """
    app_state.add_log_message("info", "Converting response to speech...")

    # Check TTS configuration before attempting
    if not hasattr(audio_output, 'text_to_speech'):
        app_state.add_log_message("error", "TTS module not properly initialized")
        show_warning_popup(t('tts.error_title'), t('tts.unavailable_msg'))
        return

    # Log the TTS attempt for debugging
    app_state.add_log_message("debug", f"Attempting TTS for reply of length {len(ai_reply)}")

    try:
        # Call text_to_speech with timing
        with timing_block("tts_generate"):
            audio_data, sr = audio_output.text_to_speech(ai_reply)

        # Validate return values
        if not validate_tts_data(audio_data, sr):
            end_turn()  # End timing even on validation failure
            return

        # Browser playback mode - send via WebSocket
        _play_tts_browser(audio_data, sr, text=ai_reply)

    except Exception as tts_exec_error:
        error_str = str(tts_exec_error)
        app_state.add_log_message("error", f"Error executing TTS: {tts_exec_error}")

        # Check for numpy compatibility error
        if "NumPy compatibility issue" in error_str or "numpy.dtype size changed" in error_str:
            # 1障害1ERRORログ: 上の "Error executing TTS" が既に error 済み。
            # 対処指示はポップアップ本文(tts.numpy_msg)が担う(稜裁定 2026-08-02)
            app_state.add_log_message("warning", "NumPy ABI incompatibility detected - reinstall numpy and torch packages")
            show_warning_popup(
                t('tts.numpy_title'),
                t('tts.numpy_msg')
            )
        else:
            show_warning_popup(t('tts.exec_error_title'), t('tts.exec_error_msg', error=error_str))

        # End timing on exception
        end_turn()


def _play_tts_for_step(text: str) -> None:
    """
    Play TTS for a single command-flow step without turn management.
    Blocks until playback completes so subsequent UI updates appear after audio.
    Does NOT call end_turn() or start_auto_prompt_timer() — caller handles that.
    """
    try:
        audio_data, sr = audio_output.text_to_speech(text)
        if not validate_tts_data(audio_data, sr):
            return

        _send_audio_to_browser(audio_data, sr, app_state.tts_volume,
                               include_lipsync=True, block=True, text=text)
    except Exception as e:
        logger.error(f"TTS step playback error: {e}")


def _send_audio_to_browser(audio_data: np.ndarray, sr: int, volume: float,
                           include_lipsync: bool = True, block: bool = True,
                           text: str = None) -> None:
    """
    Encode audio and send to browser via WebSocket.
    Common helper for all browser audio playback paths.

    Args:
        audio_data: NumPy array containing audio waveform
        sr: Sample rate
        volume: Playback volume (0.0 to 1.0)
        include_lipsync: Whether to calculate and include lipsync frames for MotionPNGTuber
        block: Whether to block until playback completes (Phase 2.5: real
               browser notification with timeout fallback)
        text: The spoken text, for the MotionPNGPlayer speech bubble (omitted
              from the payload when None, e.g. test audio / beep)
    """
    import uuid
    from backend.server.websocket_server import get_websocket_manager

    lipsync_frames = []
    if include_lipsync:
        lipsync_frames = audio_output.calculate_lipsync_frames(audio_data, sr)

    audio_base64 = audio_output.audio_to_aac_mp4_base64(audio_data, sr)
    duration_ms = int(len(audio_data) / sr * 1000)

    payload = {
        "audio_base64": audio_base64,
        "codec": "aac",
        "sample_rate": sr,
        "duration_ms": duration_ms,
        "volume": volume,
        "lipsync_frames": lipsync_frames,
    }
    if text:
        payload["text"] = text

    mgr = None
    playback_id = None
    if block:
        mgr = get_websocket_manager()
        playback_id = uuid.uuid4().hex
        payload["playback_id"] = playback_id
        mgr.register_playback(playback_id)

    try:
        publish_ui_update("tts_audio", data=payload)

        app_state.add_log_message("debug",
            f"Sent audio to browser ({len(audio_data)} samples, {duration_ms}ms, "
            f"lipsync={'yes' if include_lipsync else 'no'}) at {volume*100:.0f}% volume")

        if block:
            timeout = duration_ms / 1000 + 5.0
            mgr.wait_playback(playback_id, timeout)
    except Exception:
        if block and mgr is not None:
            mgr.wait_playback(playback_id, 0.001)  # cleanup orphan event
        raise


def _play_test_audio_browser(audio_data: np.ndarray, sr: int) -> None:
    """
    Play test audio via WebSocket for browser playback.
    Blocks until playback completes.

    Args:
        audio_data: NumPy array containing audio waveform
        sr: Sample rate
    """
    try:
        # 稜指示 2026-09-19: 音声テストの声でも MotionPNGPlayer の口を動かす
        # (会話と同じく口パクのフレームを付ける)。text は渡さない=吹き出しは出ない
        _send_audio_to_browser(audio_data, sr, app_state.tts_volume,
                               include_lipsync=True, block=True)
    except Exception as e:
        app_state.add_log_message("error", f"Error playing test audio in browser: {e}")


def _play_tts_browser(audio_data: np.ndarray, sr: int, text: str = None) -> None:
    """
    Play TTS audio via WebSocket for browser playback.
    Does NOT use sounddevice - audio is played in the browser.

    This function BLOCKS until playback completes, to maintain the same behavior
    as local playback (where the UI shows "Generating Response..." until TTS finishes).

    Args:
        audio_data: NumPy array containing audio waveform
        sr: Sample rate
        text: The spoken text (for the MotionPNGPlayer speech bubble)
    """
    try:
        _send_audio_to_browser(audio_data, sr, app_state.tts_volume,
                               include_lipsync=True, block=True, text=text)
    except Exception as e:
        app_state.add_log_message("error", f"Error sending TTS to browser: {e}")
    finally:
        end_turn()
        start_auto_prompt_timer()
