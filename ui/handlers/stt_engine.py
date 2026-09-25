"""
ui/handlers/stt_engine.py

Gradio handlers for the STT engine selector in the Voice Input tab's Audio
settings accordion (Faster-whisper local vs OpenAI transcription API).

Persistence follows the device_settings pattern (plain update_setting — the
feature_toggle registry is for LLM-exposed capabilities, not device prefs).
The ``.change(fn=...)`` wiring stays in app.py.

Status line policy: the stt-engine-status line is written ONLY via WS
(``stt_model_status`` events → JS DOM update, Auto Prompt方式). Handlers here
publish their immediate messages through the same channel instead of
returning gr.update — a single writer, so Gradio (svelte) re-renders can
never race or duplicate the WS-written DOM (實機で二重表示を実測済み).
"""

import logging

import gradio as gr

from backend.shared.i18n import t
from backend.shared.settings_store import update_setting

logger = logging.getLogger(__name__)


def warm_local_stt_model() -> None:
    """Kick the idempotent background load of the local faster-whisper model.

    Called on character selection (稜依頼 2026-07-25: Start押下時に初めて
    黄色=読み込み中になるのを避ける) and on conversation start (fallback)。
    No-op when the API engine is selected or the model is already
    ready/loading — start_model_load() itself is also idempotent. Never raises.
    """
    try:
        from backend.shared.settings_store import get_setting
        if get_setting('audio', 'stt_engine', 'faster_whisper') != 'faster_whisper':
            return
        import audio_input
        if not audio_input.is_model_ready():
            audio_input.start_model_load()
    except Exception as e:
        logger.warning(f"Could not start STT model warmup: {e}")


def _publish_stt_status(message_key: str, record_disabled: bool = None, **fmt) -> None:
    """Push a status-line message over the WS channel (best-effort).

    record_disabled: True/False toggles the record button in the browser
    (server-side twin: the record_speech guard); None leaves it untouched.
    """
    try:
        from backend.shared.ui_events import publish_ui_update
        data = {"state": "info", "message": t(message_key, **fmt)}
        if record_disabled is not None:
            data["record_disabled"] = record_disabled
        publish_ui_update("stt_model_status", data=data)
    except Exception as e:
        logger.debug(f"stt status publish failed: {e}")


def _record_unblock() -> bool:
    """record_disabled value for the "unblock" outcomes.

    The record button may only re-enable during an active conversation —
    before Start it stays greyed out (interactive=False), and an
    unconditional record_disabled=False over WS would strip that DOM
    disabled state from the browser side.
    """
    from ui.state import app_state
    return not getattr(app_state, "conversation_started", False)


def _mic_status_update() -> gr.update:
    """Refresh the mic indicator with the new engine suffix.

    Skipped while a recording/generation is in flight (an engine toggle must
    not clobber the live indicator); otherwise re-rendered as ready (during a
    conversation) or the initial inactive state. While the local model is
    still loading/downloading the text says 'preparing' instead of 'ready'
    (the load worker's stt_model_status events restore it on completion).
    """
    import audio_input
    from ui.state import app_state
    from ui.conversation.recording import _mic_status_html, _stt_engine_suffix
    from backend.shared.settings_store import get_setting

    if app_state.recording_start_time is not None or getattr(app_state, "is_recording", False):
        return gr.update()

    engine = get_setting('audio', 'stt_engine', 'faster_whisper')
    preparing = (engine == 'faster_whisper' and not audio_input.is_model_ready())
    text_key = 'micstat.preparing' if preparing else 'micstat.ready'

    if getattr(app_state, "conversation_started", False):
        return gr.update(value=_mic_status_html("ready", t(text_key)))
    return gr.update(value=_mic_status_html("inactive", t(text_key) + _stt_engine_suffix()))


def _change_stt_engine(engine: str):
    """Persist the engine choice and load/unload the local model accordingly.

    Returns updates for (stt_api_model_dd, stt_local_model_dd, mic_status).
    Status-line text goes over WS: directly for the no-worker outcomes, via
    the load worker's own events when a load starts.
    """
    import audio_input

    if engine not in ("faster_whisper", "openai"):
        return gr.update(), gr.update(), gr.update()

    # フールプルーフ防御: OpenAIキー未設定ならAPIエンジンへ切り替えない
    # (JS側で選択肢ロック済みだが、古い画面からの操作はここで止める。
    # 不永続+状態行へ理由を可視化。ラジオの見た目だけは選択側に残るが
    # 実設定はローカルのまま=リロードで一致する)
    if engine == "openai":
        from backend.shared.api_settings import load_api_settings
        if not load_api_settings().get("openai", {}).get("api_key"):
            _publish_stt_status('hdl.stt_engine.no_api_key')
            return gr.update(), gr.update(), gr.update()

    if not update_setting('audio', 'stt_engine', engine):
        # 保存失敗は無言で再起動後に巻き戻る(実機 2026-07-17)=必ず可視化
        _publish_stt_status('hdl.stt_engine.save_failed')

    if engine == "openai":
        # Free the local model's VRAM at switch time. record_disabled=False:
        # the API route needs no local model — a download that was blocking
        # the record button must stop blocking it now (the in-flight worker
        # no longer publishes an unblock once the engine is the API).
        try:
            audio_input.unload_model()
        except Exception as e:
            logger.warning(f"Whisper unload on engine switch failed: {e}")
        _publish_stt_status('hdl.stt_engine.switched_api', record_disabled=_record_unblock())
    elif audio_input.is_model_ready():
        # Model survived the round-trip (deferred unload saw the engine come
        # back) — no worker runs, so publish the answer directly.
        _publish_stt_status('hdl.stt_engine.switched_local_ready', record_disabled=_record_unblock())
    else:
        # Warm the local model at switch time (background, non-blocking).
        # The worker's stt_model_status events carry the status from here.
        try:
            audio_input.start_model_load()
        except Exception as e:
            logger.warning(f"Whisper load on engine switch failed: {e}")

    # 右上インジケーターのSTT行を即時追従させる(Start conversation後の
    # 切替でも、次のキャラ切替/ページ遷移を待たずに表示が変わる)
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("update_status", reason="stt_engine_switch")
    except Exception as e:
        logger.debug(f"status update publish failed: {e}")

    return (
        gr.update(visible=(engine == "openai")),
        gr.update(visible=(engine == "faster_whisper")),
        _mic_status_update(),
    )


def _change_stt_api_model(model: str) -> None:
    """Persist the OpenAI STT model choice."""
    if not model:
        return
    if not update_setting('audio', 'stt_api_model', model):
        _publish_stt_status('hdl.stt_engine.save_failed')
        return
    _publish_stt_status('hdl.stt_engine.model_saved', model=model)


def _change_stt_local_model(model_size: str) -> None:
    """Persist the local faster-whisper model choice and hot-swap the model.

    'reloading' means a worker runs and its own events carry the status;
    the no-worker outcomes are answered directly here.
    """
    import audio_input

    if not model_size:
        return

    result = audio_input.set_model_size(model_size)
    if result == 'invalid':
        return

    if not update_setting('audio', 'stt_local_model', model_size):
        # モデル切替自体は成功している=保存だけ失われたことを必ず伝える
        _publish_stt_status('hdl.stt_engine.save_failed')

    if result == 'already_loaded':
        _publish_stt_status('hdl.stt_engine.local_model_already_loaded',
                            record_disabled=_record_unblock(), model=model_size)
    elif result == 'deferred':
        _publish_stt_status('hdl.stt_engine.local_model_saved', model=model_size)
    elif result == 'queued':
        # Another model's load/download is in flight and cannot be cancelled;
        # without this the status keeps naming the OLD model for minutes.
        _publish_stt_status('hdl.stt_engine.local_model_queued',
                            record_disabled=True, model=model_size)
    # 'reloading': the load worker publishes from here on
