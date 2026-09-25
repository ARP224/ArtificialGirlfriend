"""
ui/status_checker.py

Connection status checking for various services used by Artificial Girlfriend.
This module provides functions to check the connectivity of:
- STT: Faster-Whisper (local) / OpenAI transcription (API)
- LLM: Ollama (local) / Claude・ChatGPT・Grok・Gemini (API, per character)
- TTS: Style-BERT-VITS2 (local) / ElevenLabs (API)

API services are checked with a live probe via ui/status_probe.py (async
cache — rendering never blocks on the network).
"""

import logging
from typing import Tuple
from datetime import datetime, timedelta

from .state import app_state
from backend.shared.i18n import t

logger = logging.getLogger(__name__)

# Module-level variable to store previous status
_previous_status_html = None  # Cache for HTML status

# Cache for Ollama status to prevent frequent API calls
_ollama_cache = {
    'status': None,
    'timestamp': None,
    'cache_duration': timedelta(seconds=30)  # Cache for 30 seconds
}


def clear_ollama_cache() -> None:
    """
    Clear the Ollama status cache.
    This is useful when backend_available state changes,
    ensuring the next status check reflects the current state.
    """
    global _ollama_cache
    _ollama_cache['status'] = None
    _ollama_cache['timestamp'] = None
    logger.debug("Cleared Ollama status cache")


def check_whisper_status() -> str:
    """
    Check the status of Faster-Whisper (Speech-to-Text) service.
    
    Returns:
        str: Status indicator with emoji and service name
    """
    try:
        # Check if audio input is available (mic is needed in both engines)
        if not app_state.audio_input_available:
            return t('status.whisper_no_mic')

        # API engine: readiness is live OpenAI connectivity, not the local
        # model. Display name "Whisper API" (稜裁定 2026-07-19) — the provider
        # default "ChatGPT" would mislead on this line.
        from backend.shared.settings_store import get_setting
        if get_setting('audio', 'stt_engine', 'faster_whisper') == 'openai':
            return _api_probe_status("openai", display_name="Whisper API")

        # キャラ未選択の表記はLLM/TTS行と統一(稜裁定 2026-07-22)。
        # ※キャラ選択自体はSTTモデルをロードしない(言語文字列を設定するだけ)
        if not app_state.active_character_id:
            return t('status.whisper_no_char')

        # 実モデル状態で表示(状態インジケーター=稜裁定 2026-07-22)。モデルは
        # Start Conversation時に背景ロードされる(lifecycle.py)。マイク
        # インジケーターと同じ情報源(is_model_ready/loading)で整合を取る。
        # 遷移の再描画はロードworkerの_publish_model_statusがupdate_statusを
        # 相乗りpublishすることで届く。
        import audio_input
        if audio_input.is_model_ready():
            return t('status.whisper_connected')
        if audio_input.is_model_loading():
            return t('status.whisper_loading')
        return t('status.whisper_not_loaded')

    except Exception as e:
        logger.error(f"Error checking Whisper status: {e}")
        return t('status.whisper_error')


def _api_probe_status(provider: str, display_name: str = None) -> str:
    """Render one indicator line from the async probe cache (never blocks)."""
    from .status_probe import get_probe_state
    from backend.shared.api_settings import get_provider_display_name

    name = display_name or get_provider_display_name(provider)
    result = get_probe_state(provider)
    state = result["state"]
    if state == "ok":
        return t('status.api_connected', name=name)
    if state == "auth_error":
        return t('status.api_auth_error', name=name)
    if state == "no_key":
        return t('status.api_no_key', name=name)
    if state == "checking":
        return t('status.api_checking', name=name)
    return t('status.api_unreachable', name=name)


def _active_llm_provider():
    """model_provider of the active character; None when no character is active.

    Config-read failures degrade to "ollama" — the pre-provider-aware
    behavior of this line.
    """
    if not app_state.active_character_id:
        return None
    try:
        import backend
        response = backend.load_character_config(app_state.active_character_id)
        if isinstance(response, dict) and 'result' in response:
            config = response.get('result') or {}
        else:
            config = response or {}
        return config.get("model_provider", "ollama")
    except Exception as e:
        logger.debug(f"LLM provider lookup failed: {e}")
        return "ollama"


def check_llm_status() -> str:
    """
    Check the status of the active character's LLM.

    Local (Ollama) characters keep the live Ollama check; API characters get
    a live probe of their provider. An Ollama red lamp must never appear for
    a user who runs API characters without Ollama installed.

    Returns:
        str: Status indicator with emoji and provider name
    """
    try:
        provider = _active_llm_provider()
        if provider is None:
            return t('status.llm_no_char')
        if provider == "ollama":
            return check_ollama_status()
        return _api_probe_status(provider)
    except Exception as e:
        logger.error(f"Error checking LLM status: {e}")
        return check_ollama_status()


# Ollama死活の前回値（down→up遷移検知用。None=未観測）
_ollama_was_alive = None


def _note_ollama_alive(alive: bool) -> None:
    """Ollama死活のdown→up遷移で機能可用性を再判定・再配信する。

    Ollamaのcapability判定(2軸グレーアウト)は遅延照会+負キャッシュのため、
    アプリより後からOllamaを起動したユーザーはイベントが来るまで
    「判定不能」グレーが陳腐化する。オンデマンドプローブはここにしか
    無い(status_monitorは抽出専用)ので、up遷移でpublishする。enforceでは
    なくpublish: up遷移で機能を勝手にONへ戻さない(再有効化はユーザー操作
    =2026-07-25裁定)一方、OFF方向の強制も不要なため。
    """
    global _ollama_was_alive
    prev = _ollama_was_alive
    _ollama_was_alive = alive
    if alive and prev is False:
        try:
            from backend.llm.ollama_capabilities import notify_server_reachable
            notify_server_reachable()
            from backend.backend import publish_feature_availability
            publish_feature_availability()
            logger.info("Ollama came up - republished feature availability")
        except Exception as e:
            logger.debug(f"Availability republish on Ollama up failed: {e}")


def check_ollama_status() -> str:
    """
    Check the status of Ollama (LLM) service with caching to avoid frequent API calls.

    Returns:
        str: Status indicator with emoji and service name
    """
    global _ollama_cache
    
    # Check cache first
    now = datetime.now()
    if (_ollama_cache['timestamp'] and 
        _ollama_cache['status'] and 
        now - _ollama_cache['timestamp'] < _ollama_cache['cache_duration']):
        logger.debug("Using cached Ollama status")
        return _ollama_cache['status']
    
    try:
        # Check if backend is available
        if not app_state.backend_available:
            status = t('status.ollama_backend_unavailable')
            _ollama_cache['status'] = status
            _ollama_cache['timestamp'] = now
            return status
        
        # Liveness check via the tuple API (models, error_message). The
        # boundary wrapper backend.list_ollama_models gracefully degrades to
        # [] on failure — indistinguishable from "server up, nothing
        # installed", which showed 🟡 Connected（モデルなし）while Ollama was
        # shut down (実機 2026-07-19). The indicator needs the error signal.
        # Single attempt: this runs behind a 30s cache, no retry backoff.
        try:
            from backend.llm.ollama_integration import list_ollama_models
            models, error_message = list_ollama_models(max_retries=1)

            if error_message:
                status = t('status.ollama_disconnected')
                _note_ollama_alive(False)
            elif models:
                status = t('status.ollama_connected')
                _note_ollama_alive(True)
            else:
                status = t('status.ollama_no_models')
                _note_ollama_alive(True)  # サーバー自体は応答している

        except Exception as e:
            logger.debug(f"Ollama check failed: {e}")
            status = t('status.ollama_disconnected')
            _note_ollama_alive(False)
        
        # Update cache
        _ollama_cache['status'] = status
        _ollama_cache['timestamp'] = now
        return status
            
    except Exception as e:
        logger.error(f"Error checking Ollama status: {e}")
        status = t('status.ollama_error')
        _ollama_cache['status'] = status
        _ollama_cache['timestamp'] = now
        return status


def check_tts_status() -> str:
    """
    Check the status of Style-BERT-VITS2 (TTS) service.
    
    Returns:
        str: Status indicator with emoji and service name
    """
    try:
        # Check if audio output is available
        if not app_state.audio_output_available:
            return t('status.tts_no_output')

        # Check if a character is selected (which would have TTS config)
        if not app_state.active_character_id:
            return t('status.tts_no_char')

        # ElevenLabs character active: readiness is live API connectivity
        import audio_output
        provider = audio_output.get_current_tts_provider()
        if provider == "elevenlabs":
            return _api_probe_status("elevenlabs")

        # Local engines (name matches the voice dropdown labels).
        # In a real implementation, you might check if the TTS model is loaded
        # For now, assume it's working if audio output is available
        if provider == "kokoro":
            return t('status.tts_connected', name="KokoroTTS")
        return t('status.tts_connected', name="Style-bert-vits2")

    except Exception as e:
        logger.error(f"Error checking TTS status: {e}")
        return t('status.tts_error')


def check_all_status() -> Tuple[str, str, str]:
    """
    Check the status of all services.
    
    Returns:
        Tuple of status strings for (whisper, llm, tts)
    """
    whisper_status = check_whisper_status()
    llm_status = check_llm_status()
    tts_status = check_tts_status()

    return whisper_status, llm_status, tts_status


def get_status_html() -> str:
    """
    Get all service status indicators as a single HTML string.
    This reduces flicker by updating only one component instead of three.
    
    Returns:
        str: HTML string containing all three status indicators
    """
    global _previous_status_html
    
    # Get current status
    whisper_status, llm_status, tts_status = check_all_status()
    
    # Build HTML with inline styles to prevent layout shifts
    html = f"""
    <div class="status-container" style="display: flex; flex-direction: column; gap: 4px;">
        <div class="status-item" style="font-size: 14px; line-height: 1.5;">{whisper_status}</div>
        <div class="status-item" style="font-size: 14px; line-height: 1.5;">{llm_status}</div>
        <div class="status-item" style="font-size: 14px; line-height: 1.5;">{tts_status}</div>
    </div>
    """
    
    # Only log if status actually changed
    if html != _previous_status_html:
        logger.debug("[Status Update] HTML status display updated - Flicker reduction active")
        logger.debug(f"[Status Update] Whisper: {whisper_status}")
        logger.debug(f"[Status Update] LLM: {llm_status}")
        logger.debug(f"[Status Update] TTS: {tts_status}")
        _previous_status_html = html
    else:
        logger.debug("[Status Update] No change in status - skipping update")
    
    return html


# Export public API
__all__ = [
    'check_whisper_status',
    'check_llm_status',
    'check_ollama_status',
    'check_tts_status',
    'check_all_status',
    'get_status_html',
    'clear_ollama_cache'
]