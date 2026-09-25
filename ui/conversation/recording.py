"""
ui/conversation/recording.py

音声入力の責務。録音の開始/停止・文字起こし・録音時間監視・音声会話チェーン・
外部(hotkey/リモート)入口を持つ。
"""

import logging
import threading
import time
import traceback
from typing import Tuple, Optional

import gradio as gr

from ..state import app_state, show_error_popup, show_warning_popup
from ..components import get_chat_history
from ..error_handler import handle_module_operation, error_status_text
from ..constants import MAX_RECORDING_DURATION, RECORDING_WARNING_TIME
from backend.shared.i18n import t

import audio_input

from .auto_prompt import reset_auto_prompt_timer
from .beep import play_beep_sound
from .generation import handle_generate_reply, is_error_response
from .notify import notify_ui_update

logger = logging.getLogger(__name__)


# Thread lock for recording operations to prevent race conditions
_recording_lock = threading.RLock()

# recording_time 表示のタイマー内デデュープキー(hidden/counting/autostopped)。
# 可視インジケータ/ボタンは gr.Timer の出力に載せない: Gradio のタイマー更新は
# 可視要素だと毎tickちらつく(status/auto_prompt が WS 化した既知事象と同根)。
# hotkey 経路のUI同期は _broadcast_recording_display (WS→JSのDOM直接更新)が担う。
_time_display = "hidden"


def _stt_engine() -> str:
    """設定中のSTTエンジン('faster_whisper' | 'openai')。取得失敗はローカル扱い。"""
    try:
        from backend.shared.settings_store import get_setting
        return get_setting('audio', 'stt_engine', 'faster_whisper')
    except Exception:
        return 'faster_whisper'


def _stt_engine_suffix() -> str:
    """micインジケータ用のエンジン識別 " (Faster-whisper)" / " (OpenAI API)"。"""
    try:
        key = 'micstat.engine_api' if _stt_engine() == 'openai' else 'micstat.engine_local'
        return " " + t(key)
    except Exception:
        return ""


def _broadcast_recording_display(state: str, text: str,
                                 button: Optional[str] = None,
                                 button_class: Optional[str] = None) -> None:
    """WS経由でmicインジケータ/録音ボタン表示を更新する(Auto Prompt方式)。

    Gradioイベントを通らない経路(グローバルhotkey・5分自動停止・非同期生成
    完了)のUI反映専用。UIクリック経路は voice_btn_chain のGradio出力が担う。
    JS側ハンドラは ws_client_js.py の 'recording_display' アクション。
    """
    if state == "ready":
        # WS経路はDOMのテキストを直接差し替えるため、_mic_status_html を
        # 通らない=サフィックスはここでも付与する(2書き込み経路の同期)。
        text = text + _stt_engine_suffix()
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("recording_display", state, {
            "state": state,          # inactive | ready | recording | processing
            "text": text,
            "button": button,        # None = ボタンは触らない
            "button_class": button_class,
        })
    except Exception as e:
        logger.debug(f"recording_display broadcast failed: {e}")


_status_reset_timer: Optional[threading.Timer] = None


def _schedule_status_reset(delay: float = 10.0) -> None:
    """micインジケータの一時メッセージを delay 秒後に Ready 表示へ戻す。

    稜依頼 2026-08-02: 無音案内などが出っぱなしにならないように。発火時に
    録音中/生成中なら何もしない(進行中の表示を壊さない)。書き込みは
    WS→JS の既存経路(_broadcast_recording_display)=hotkey 同期と同型。
    """
    global _status_reset_timer
    if _status_reset_timer is not None:
        _status_reset_timer.cancel()

    def _reset():
        if app_state.recording_start_time is not None or app_state.response_generating:
            return
        _broadcast_recording_display("ready", t('micstat.ready'))

    timer = threading.Timer(delay, _reset)
    timer.daemon = True
    timer.start()
    _status_reset_timer = timer


def _mic_status_html(state: str, text: str) -> str:
    """Build the mic-status indicator HTML (single source for every writer).

    state: CSS state class — inactive(灰) / ready(青) / recording(赤点滅) /
    processing(黄点滅). Drives the status-dot colour.
    """
    if state == "ready":
        text = text + _stt_engine_suffix()
    return f"""<div class="mic-status-container">
        <div class="mic-status-indicator {state}">
            <span class="status-dot"></span>
            <span class="status-text">{text}</span>
        </div>
    </div>"""


def initial_mic_status_html() -> str:
    """The mic indicator's initial render (pages.py / engine-switch refresh)."""
    return _mic_status_html("inactive", t('micstat.ready') + _stt_engine_suffix())


# Wrapper functions for audio operations with consistent error handling
@handle_module_operation("Start recording", show_popup=False)
def _start_recording_wrapper():
    """Start microphone recording with error handling."""
    app_state.add_log_message("debug", "Starting recording with default device")
    # 音声層の自動停止上限をUIの約束(Recording... (5 min max))に一致させる。
    # audio_input の既定は600s=UI文言と乖離しており、旧UI側5分監視
    # (check_recording_duration)はタイマー配線撤去で死んでいたため、
    # ローカルモードの5分上限は音声層の自動停止が唯一の実施箇所。
    audio_input.set_max_recording_duration(MAX_RECORDING_DURATION)
    audio_input.start_recording()
    return True


@handle_module_operation("Stop recording", show_popup=False)
def _stop_recording_wrapper():
    """Stop microphone recording with error handling."""
    audio_input.stop_recording()
    return True


def _notify_mic_fallback() -> None:
    """選択マイクが使えず既定へ劣化した録音開始をポップアップで可視化する。

    状態行(WS)の通知は音声入力設定アコーディオンを開いていないと見えず、
    「選んだマイクと違うデバイスで録れている/無音」に気づけない(実障害
    2026-07-25)。同じ理由が続く間は1回だけ表示し、選択マイクで開けたら
    リセット(状態遷移ごとに1回)。通知失敗が録音を壊さないよう swallow。
    """
    try:
        reason = audio_input.last_recording_fallback()
        state = app_state.audio
        if reason is None:
            state.mic_fallback_notified = None
            return
        if reason == state.mic_fallback_notified:
            return
        state.mic_fallback_notified = reason
        from backend.shared.settings_store import get_setting
        name = get_setting('audio', 'input_device', '')
        from ..state import show_info_popup
        show_info_popup(t('rec.popup_mic_fallback_title'),
                        t('rec.popup_mic_fallback_msg', name=name))
    except Exception as e:
        logger.warning(f"Mic fallback notification failed: {e}")


@handle_module_operation("Audio transcription")
def _transcribe_audio_wrapper():
    """Transcribe audio with error handling."""
    # Use transcribe_audio() since recording is already stopped
    user_text = audio_input.transcribe_audio()
    
    # Handle empty transcription result
    if not user_text or user_text.strip() == "":
        from audio_input.errors import STTError, STT_NO_SPEECH
        err = STTError(STT_NO_SPEECH, "No speech detected in recording")
        # 無音は正常系イベント: デコレータのエラー処理(ERRORログ+
        # エラーポップアップ)を素通しさせ、呼出元の専用分岐(ボタンラベル
        # 案内+warning)だけに扱わせる(稜裁定 2026-08-02)
        err.ag_expected = True
        raise err
    
    return user_text


def toggle_voice_recording(is_recording: bool) -> Tuple[gr.update, gr.update, gr.update, gr.update, bool, str]:
    """
    Handle the unified voice recording button toggle.
    Now returns transcribed text for subsequent processing.
    
    Args:
        is_recording: Current recording state
        
    Returns:
        Tuple containing:
        - Button update (text and style)
        - Microphone status HTML update
        - Recording time display update
        - Recording time visibility update
        - New recording state
        - Transcribed text (empty string if starting recording)
    """
    if not is_recording:
        # Start recording
        result = record_speech()
        
        # Check for specific error conditions
        if t('voicebtn.start_first') in str(result[0].get('value', '')):
            # Conversation not started - keep button interactive
            return (
                result[0],  # Button update from record_speech
                gr.update(value=_mic_status_html("inactive", t('rec.status_start_first'))),
                gr.update(value=""),
                gr.update(visible=False),
                False,
                ""  # No transcribed text
            )
        elif t('voicebtn.select_char') in str(result[0].get('value', '')):
            # No character selected
            return (
                result[0],  # Button update from record_speech
                gr.update(value=_mic_status_html("inactive", t('rec.status_select_char'))),
                gr.update(value=""),
                gr.update(visible=False),
                False,
                ""  # No transcribed text
            )
        elif t('voicebtn.mic_unavailable') in str(result[0].get('value', '')):
            # Browser mic not available (server mode)
            return (
                result[0],
                gr.update(value=_mic_status_html("inactive", t('rec.status_mic_text_only'))),
                gr.update(value=""),
                gr.update(visible=False),
                False,
                ""
            )
        elif str(result[1]) == t('rec.stat_model_preparing'):
            # STTモデルのダウンロード/ロード中(record_speech がブロック):
            # 録音は始まっていない=状態は False のまま
            return (
                gr.update(value=t('voicebtn.press_to_record'), elem_classes="voice-btn-ready"),
                gr.update(value=_mic_status_html("inactive", str(result[1]))),
                gr.update(value=""),
                gr.update(visible=False),
                False,
                ""  # No transcribed text
            )
        elif "error" in str(result[1]).lower() or "⚫" in str(result[1]):
            # Recording failed
            return (
                gr.update(value=t('voicebtn.press_to_record'), elem_classes="voice-btn-ready"),
                gr.update(value=_mic_status_html("inactive", str(result[1]))),
                gr.update(value=""),
                gr.update(visible=False),
                False,
                ""  # No transcribed text
            )
        else:
            # Recording started successfully
            return (
                gr.update(value=t('voicebtn.stop_recording'), elem_classes="voice-btn-recording"),
                gr.update(value=_mic_status_html("recording", t('rec.recording'))),
                gr.update(value=f"""<div class="recording-time">
                    <span class="time-text">{t('rec.time', time='0:00')}</span>
                </div>"""),
                gr.update(visible=True),
                True,
                ""  # No transcribed text when starting
            )
    else:
        # Stop recording
        return stop_and_process_recording()


def stop_and_process_recording() -> Tuple[gr.update, gr.update, gr.update, gr.update, bool, str]:
    """
    Stop recording and process the audio, updating UI appropriately.
    Now returns transcribed text for subsequent processing.
    
    Returns:
        Tuple containing UI updates, new recording state, and transcribed text
    """
    # Stop and transcribe (Phase 1 only)
    button_update, status, transcribed_text = stop_and_transcribe_phase1()

    # Determine ready status based on transcription result
    if transcribed_text:  # Success
        ready_status = t('rec.ready_next')
    else:  # Error or no speech
        ready_status = status  # Use the status from phase1

    # SSE(Gradioイベント応答)が切れてもボタン/インジケータが録音中表示のまま
    # 固まらないよう、WSでも同内容を同報する(JS側は同値ならno-op)
    _broadcast_recording_display(
        "ready", str(ready_status),
        button=t('voicebtn.press_to_record'), button_class="voice-btn-ready")

    # 無音/失敗の一時メッセージは約10秒でReadyへ戻す(稜依頼 2026-08-02)
    if not transcribed_text:
        _schedule_status_reset()

    return (
        gr.update(value=t('voicebtn.press_to_record'), elem_classes="voice-btn-ready", interactive=True),
        gr.update(value=_mic_status_html("ready", ready_status)),
        gr.update(value=""),
        gr.update(visible=False),
        False,
        transcribed_text  # Return the transcribed text
    )


def update_recording_time() -> gr.update:
    """1秒タイマー本体: 録音時間カウンタ(recording_time)のみを更新する。

    出力は recording_time 1本だけに限定する。可視のインジケータ/ボタンを
    gr.Timer の出力に載せると毎tickちらつくため(本リポジトリで status 表示・
    Auto Prompt が WS 化した既知事象)、それらの hotkey 同期は
    _broadcast_recording_display(WS→JS)が担う。アイドル時は非表示化を
    一度だけ送り、以後は gr.skip() で無送信。

    Returns:
        recording_time の gr.update (変化がない tick は gr.skip())
    """
    global _time_display
    recording = app_state.recording_start_time is not None

    if not recording:
        if _time_display == "hidden":
            return gr.skip()
        _time_display = "hidden"
        return gr.update(visible=False)

    # === Recording (started via UI click or hotkey) ===
    elapsed = time.time() - app_state.recording_start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    # ローカルモード: 音声層が上限(MAX_RECORDING_DURATION)で自動停止した後は
    # 「停止ボタン待ち」を明示する。バッファは保持されており、停止ボタンで
    # そのまま文字起こしされる。(サーバーモードはJS側の5分タイマーが担当)
    if (not app_state.server_mode_enabled
            and elapsed >= MAX_RECORDING_DURATION
            and not audio_input.is_currently_recording()):
        if _time_display == "autostopped":
            return gr.skip()
        _time_display = "autostopped"
        # インジケータの文言差し替えは WS 経由(一度だけ)
        _broadcast_recording_display(
            "recording", t('rec.autostopped_press_stop'))
        return gr.update(
            value=f"""<div class="recording-time-warning">
                <span class="time-text">{t('rec.autostopped_time')}</span>
            </div>""",
            visible=True
        )

    # Warning colour based on time (per-second counter update is by design)
    _time_display = "counting"
    time_class = "recording-time-warning" if elapsed > RECORDING_WARNING_TIME else "recording-time"
    time_str = f"{minutes}:{seconds:02d}"
    return gr.update(
        value=f"""<div class="{time_class}">
            <span class="time-text">{t('rec.time', time=time_str)}</span>
        </div>""",
        visible=True
    )


def reset_recording_state() -> None:
    """
    Helper function to ensure recording state is properly reset.
    Clears all recording-related flags and states.
    """
    app_state.recording_start_time = None

    # Try to stop any active recording (only if currently recording)
    try:
        if audio_input.is_currently_recording():
            audio_input.stop_recording()
    except Exception:
        pass  # Ignore errors when force-stopping


def record_speech() -> Tuple[gr.update, str]:
    """
    Begin recording speech from the microphone with comprehensive error handling.

    Returns:
        Tuple[gr.update, str]: Button update and microphone status text
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    # Reset auto prompt timer when user starts recording
    reset_auto_prompt_timer()

    # Audio input check removed - we'll try to use the default device

    # Check if conversation is active (unreachable in normal UI: the record
    # button is disabled before start — kept as defense in depth)
    if not app_state.conversation_started:
        app_state.add_log_message("info", "Attempted to record before starting conversation")
        return gr.update(value=t('voicebtn.start_first'), variant="secondary", elem_classes=[], interactive=False), t('rec.stat_start_conversation')

    # Phase 5 Day 1: defense in depth. JS-side button.disabled responds to
    # is_generating_update broadcasts but can race against clicks dispatched
    # before the broadcast arrives on a flaky network. Catch those here.
    try:
        from backend.backend import _backend_state as _bs
        if _bs and _bs.is_generating:
            app_state.add_log_message("info", "Recording blocked: generation in progress")
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_generating_wait')
            )
    except Exception:
        pass

    # ローカルSTTモデルのダウンロード/ロード中は録音開始をブロック(稜裁定
    # 2026-07-17)。文字起こしは遅延するだけで消えない設計だが、モデル準備中に
    # 「録音中」になるのは壊れて見える。JS側のボタンdisable(stt_model_status)
    # と対の server-side twin: hotkey/Addon 経由もここを通る。停止経路は
    # この関数を通らない=停止は決してブロックされない。
    try:
        if (_stt_engine() == 'faster_whisper'
                and not audio_input.is_model_ready()
                and audio_input.is_model_loading()):
            app_state.add_log_message("info", "Recording blocked: STT model load in progress")
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_model_preparing')
            )
    except Exception:
        pass

    # Atomic check-and-claim: a single lock region both checks "already
    # recording" AND claims the slot by setting recording_start_time, so a
    # simultaneous UI-click + hotkey can't both pass the check and double-start
    # (M19). Failure paths below call reset_recording_state() which clears it.
    with _recording_lock:
        if app_state.recording_start_time is not None:
            app_state.add_log_message("warning", "Attempted to start recording while already recording")
            return gr.update(value=t('rec.btn_already_recording'), variant="stop", interactive=False), t('rec.stat_already_recording')
        app_state.recording_start_time = time.time()

    # === Server mode: browser mic recording ===
    if app_state.server_mode_enabled:
        try:
            from backend.server.websocket_server import get_browser_mic_bridge
            bridge = get_browser_mic_bridge()
            if not bridge.mic_available:
                # Release the claim taken above, or every later press is
                # rejected as "Already recording" until restart (M19 regression)
                reset_recording_state()
                return (
                    gr.update(value=t('voicebtn.mic_unavailable'), variant="secondary", interactive=True),
                    t('rec.stat_mic_unavailable')
                )

            # Clear stale data from the bridge queue
            bridge.reset()

            # Play start beep (WebSocket → browser playback, already implemented)
            if app_state.beep_enabled:
                play_beep_sound(is_start=True)
        except Exception as e:
            reset_recording_state()  # Use helper for consistent cleanup
            app_state.add_log_message("error", f"Failed to start browser mic recording: {e}")
            return gr.update(value=t('rec.btn_start_failed'), variant="secondary"), t('rec.stat_mic_error')

        # recording_start_time already claimed atomically above
        app_state.add_log_message("info", f"Browser mic recording started (max {MAX_RECORDING_DURATION}s)")
        return gr.update(value=t('rec.btn_recording'), variant="stop"), t('rec.stat_recording')

    # === Local mode: sounddevice recording ===
    # Start recording with comprehensive error handling
    try:
        # Device check removed - we'll use the system default microphone

        # Play start beep if enabled
        if app_state.beep_enabled:
            try:
                play_beep_sound(is_start=True)
            except Exception as beep_error:
                app_state.add_log_message("warning", f"Failed to play start beep: {beep_error}")
                # Continue despite beep failure

        # Start the microphone recording with error handling
        try:
            _start_recording_wrapper()
            _notify_mic_fallback()
        except PermissionError:
            reset_recording_state()  # Use helper for consistent cleanup
            show_error_popup(t('rec.popup_perm_title'),
                           t('rec.popup_perm_msg'))
            return gr.update(value=t('rec.btn_perm_denied'), variant="secondary"), t('rec.stat_mic_unavailable')
        except RuntimeError as rt_error:
            reset_recording_state()  # Use helper for consistent cleanup
            error_msg = str(rt_error).lower()

            # Windows-specific error handling
            if "exclusive" in error_msg:
                show_error_popup(t('rec.popup_exclusive_title'),
                               t('rec.popup_exclusive_msg'))
                return gr.update(value=t('rec.btn_mic_locked'), variant="secondary"), t('rec.stat_mic_locked')
            elif "busy" in error_msg or "use" in error_msg:
                return gr.update(value=t('rec.btn_mic_busy'), variant="secondary"), t('rec.stat_mic_busy')
            elif "wasapi" in error_msg:
                show_error_popup(t('rec.popup_wasapi_title'),
                               t('rec.popup_wasapi_msg'))
                return gr.update(value=t('rec.btn_audio_error'), variant="secondary"), t('rec.stat_audio_error')
            else:
                return gr.update(value=t('rec.btn_recording_error', error=str(rt_error)), variant="secondary"), t('rec.stat_mic_error')
        except Exception as e:
            reset_recording_state()  # Use helper for consistent cleanup
            # デコレータ(handle_module_operation)が ERROR ログ済み=ここは
            # warning 止まりで二重トーストを防ぐ(稜裁定 2026-08-02)
            app_state.add_log_message("warning", f"Failed to start recording: {e}")
            return gr.update(value=t('rec.btn_start_failed'), variant="secondary"), t('rec.stat_mic_error')

        # recording_start_time already claimed atomically above (before the
        # device open), so the start is single-entry without a second lock here.
        app_state.add_log_message("info", f"Recording started (max {MAX_RECORDING_DURATION}s)")

        # Update mic icon to red (recording) and button state
        return gr.update(value=t('rec.btn_recording'), variant="stop"), t('rec.stat_recording_limit')

    except Exception as e:
        reset_recording_state()  # Use helper for consistent cleanup
        error_details = traceback.format_exc()
        app_state.add_log_message("error", f"Unexpected error during recording start: {e}")
        # トレース全文は debug: error だとWSトーストに300字切りの断片が
        # そのまま出る(稜裁定 2026-08-02)
        logger.debug(f"Unexpected recording error details: {error_details}")

        show_error_popup(t('popup.unexpected_title'), t('rec.popup_unexpected_msg', error=str(e)))
        return gr.update(value=t('rec.btn_unexpected_error'), variant="secondary"), t('rec.stat_mic_inactive')


def stop_and_transcribe_phase1() -> Tuple[gr.update, str, str]:
    """
    Phase 1: Stop recording, transcribe speech, and add user message.
    Returns user text for subsequent AI generation.

    Returns:
        Tuple[gr.update, str, str]: Button update, microphone status, and transcribed text
    """
    # Thread-safe recording stop with lock
    with _recording_lock:
        # Check if actually recording
        if app_state.recording_start_time is None:
            app_state.add_log_message("warning", "Tried to stop recording while not recording.")
            return gr.update(value=t('rec.btn_not_recording'), variant="primary"), t('rec.stat_mic_inactive'), ""
    
    if not app_state.conversation_started:
        reset_recording_state()  # Use helper for consistent cleanup
        app_state.add_log_message("warning", "Tried to stop recording while not active.")
        return gr.update(value=t('rec.btn_no_stop'), variant="secondary"), t('rec.stat_mic_inactive'), ""

    # 文字起こし中フラグ(稜裁定 2026-08-21): 本体冒頭で recording_start_time
    # が消える=録音クレームが途切れるため、会話終了ガード(toggle_start_end)
    # が音声ターンを文字起こし完了まで拒否できるようにする。スコープを
    # phase1 本体に限定するのは clear 漏れ固着を防ぐため(UIクリック/hotkey/
    # サーバモードの全経路がこの関数を通る)。
    app_state.audio.transcribing = True
    try:
        return _transcribe_phase1_body()
    finally:
        app_state.audio.transcribing = False


def _transcribe_phase1_body() -> Tuple[gr.update, str, str]:
    """stop_and_transcribe_phase1 の本体(transcribing フラグは呼出元が管理)。"""
    # Live Camera: 録音停止=生成確定なのでここで撮影要求を発火し、以降の
    # 文字起こし(STT)と並行させる(提供者なしならno-op・待ちは生成直前バリア)
    try:
        from backend.shared.ambient_camera_state import fire_capture_request
        fire_capture_request()
    except Exception:
        pass

    # === Server mode: wait for browser mic transcription via WebSocket ===
    if app_state.server_mode_enabled:
        # Play stop beep (WebSocket → browser playback)
        if app_state.beep_enabled:
            play_beep_sound(is_start=False)

        # Clear recording state
        app_state.recording_start_time = None

        # Touch session activity
        try:
            from backend.server.session_manager import get_session_manager
            get_session_manager().touch_activity()
        except Exception:
            pass

        app_state.add_log_message("info", "Waiting for browser mic transcription...")

        # Block until the WebSocket handler delivers a transcription result.
        # API engine: the OpenAI round-trip on a long recording can exceed
        # the local 15s budget — allow 60s before declaring the slot stale.
        bridge_timeout = 60.0 if _stt_engine() == "openai" else 15.0
        from backend.server.websocket_server import get_browser_mic_bridge
        bridge = get_browser_mic_bridge()
        try:
            user_text = bridge.wait_for_result(timeout=bridge_timeout)
            # info=文字数だけ(AI側 "AI reply generated successfully (N chars)" と対称)。
            # 全文は debug に落とす: 画面のシステムログには今までどおり出るが
            # (add_log_message はレベルに関係なく log_messages へ積む)、app.log と
            # コンソールのハンドラは INFO 以上・WSトーストは ERROR 以上なので
            # ディスクには一切残らない。旧実装は音声入力の書き起こしだけ発話全文が
            # app.log に永続化されていた(テキスト入力は元から記録なし)。
            app_state.add_log_message("info", f"Transcribed ({len(user_text or '')} chars)")
            app_state.add_log_message("debug", f"User said: {user_text}")
        except TimeoutError:
            # Phase 5 Day 1: the bridge slot is now stale, and the JS-side
            # MediaRecorder may still hold a pending onstop. Reset both sides
            # so the next record click starts cleanly.
            bridge.reset()
            try:
                from backend.server.websocket_server import get_websocket_manager
                get_websocket_manager().broadcast_mic_state_reset_sync()
            except Exception as broadcast_err:
                logger.warning(f"Failed to broadcast mic_state_reset: {broadcast_err}")
            app_state.add_log_message("warning", "Browser mic timed out — state reset")
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_mic_reset'),
                ""
            )
        except RuntimeError as e:
            error_msg = str(e)
            # コード判定(稜裁定 2026-07-25: 英語文字列の部分一致は全廃)
            from audio_input.errors import STT_NO_SPEECH
            if getattr(e, "ag_code", None) == STT_NO_SPEECH:
                app_state.add_log_message("info", "No speech detected in browser recording")
                return (
                    gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                    t('rec.stat_no_speech'),
                    ""
                )
            app_state.add_log_message("error", f"Browser mic error: {error_msg}")
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_error', error=error_msg),
                ""
            )

        # Success: add user message to chat (same path as local mode)
        if user_text and user_text.strip():
            app_state.append_chat_message("User", user_text, is_ai=False)
            app_state._pending_voice_text = user_text
            notify_ui_update(reason="transcription_complete", data={"text": user_text[:100]})
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_processing_response'),
                user_text
            )
        else:
            return (
                gr.update(value=t('voicebtn.press_to_record'), variant="primary", interactive=True),
                t('rec.stat_no_speech'),
                ""
            )

    # === Local mode: sounddevice recording + transcription ===
    # Store recording state to ensure cleanup
    had_recording = app_state.recording_start_time is not None

    try:
        # Check recording duration
        recording_duration = 0
        if app_state.recording_start_time:
            recording_duration = time.time() - app_state.recording_start_time
            app_state.add_log_message("info", f"Recording duration: {recording_duration:.1f} seconds")

            # Clear the start time
            app_state.recording_start_time = None

        # Stop the microphone recording with error handling
        try:
            _stop_recording_wrapper()
        except Exception as stop_error:
            # デコレータ(_stop_recording_wrapper)が ERROR ログ済み=warning
            app_state.add_log_message("warning", f"Error stopping recording: {stop_error}")
            show_warning_popup(t('rec.popup_stop_error_title'), t('rec.popup_stop_error_msg', error=str(stop_error)))
            # Continue with transcription attempt anyway

        # Play stop beep if enabled
        if app_state.beep_enabled:
            try:
                play_beep_sound(is_start=False)
            except Exception as beep_error:
                app_state.add_log_message("warning", f"Failed to play stop beep: {beep_error}")
                # Continue despite beep failure

        # Show waiting indicator during transcription
        app_state.add_log_message("info", "Processing audio recording...")

        # Transcribe with error handling
        try:
            user_text = _transcribe_audio_wrapper()
            # 全文は debug=画面のシステムログのみ・app.log には残さない
            # (理由はブラウザマイク経路の同じ箇所を参照)
            app_state.add_log_message("info", f"Transcribed ({len(user_text or '')} chars)")
            app_state.add_log_message("debug", f"User said: {user_text}")
        except RuntimeError as rt_error:
            # コード判定(稜裁定 2026-07-25: 英語文字列の部分一致は全廃)
            from audio_input.errors import STT_NO_SPEECH
            if getattr(rt_error, "ag_code", None) == STT_NO_SPEECH:
                app_state.add_log_message("warning", "No speech detected in recording")
                # ステータス行に無音を明示する(ブラウザマイク経路 :604 と同型)。
                # ボタン更新(第1要素)は stop_and_process_recording が常に
                # 「録音を開始」へ戻すため表示されない=案内はステータス行が
                # 唯一のUI面(稜実機 2026-08-02: 旧 stat_mic_inactive だと
                # 無音のフィードバックがゼロだった)
                return gr.update(value=t('rec.btn_no_speech'), variant="primary"), t('rec.stat_no_speech'), ""
            else:
                # デコレータが ERROR ログ+ポップアップ済み=warning 止まり
                # (稜裁定 2026-08-02)
                app_state.add_log_message("warning", f"Transcription error: {rt_error}")
                return gr.update(value=t('rec.btn_transcribe_failed_logs'), variant="secondary"), t('rec.stat_mic_inactive'), ""
        except Exception as e:
            app_state.add_log_message("warning", f"Transcription error: {e}")
            return gr.update(value=t('rec.btn_transcribe_failed_retry'), variant="secondary"), t('rec.stat_mic_inactive'), ""

        # Display user bubble (only if we have transcribed text)
        if user_text and user_text.strip():
            app_state.append_chat_message("User", user_text, is_ai=False)
            # Store for AI generation
            app_state._pending_voice_text = user_text
            app_state.add_log_message("info", "User message added to chat history")
            return gr.update(value=t('rec.btn_transcribed'), variant="primary"), t('rec.stat_processing'), user_text
        else:
            # 文字起こし結果が空(音はあるが言葉がない): 無音と同じく
            # ステータス行で案内(上の STT_NO_SPEECH 分岐と同じ理由)
            return gr.update(value=t('rec.btn_no_speech'), variant="secondary"), t('rec.stat_no_speech'), ""

    except Exception as e:
        error_details = traceback.format_exc()
        app_state.add_log_message("error", f"Unexpected error in transcription flow: {e}")
        # トレース全文は debug(recording start 側と同じ理由)
        logger.debug(f"Unexpected transcription error details: {error_details}")

        show_error_popup(t('popup.unexpected_title'), t('rec.popup_unexpected_msg', error=str(e)))
        return gr.update(value=t('rec.btn_transcribe_process_failed'), variant="secondary"), t('rec.stat_mic_inactive'), ""
    finally:
        # Ensure recording state is always cleaned up
        if had_recording:
            reset_recording_state()
            app_state.add_log_message("debug", "Recording state cleaned up in finally block")


def start_voice_generation() -> str:
    """
    Phase 2a: Start the voice generation process by setting the generating state.
    This shows the "Generating response..." indicator in the chat.
    
    Returns:
        str: Status message
    """
    # Check if there's pending voice text to process
    if hasattr(app_state, '_pending_voice_text') and app_state._pending_voice_text:
        app_state.response_generating = True
        app_state._chat_history_version += 1  # Force update to show generating indicator
        return t('gen.generating')
    return ""


def process_voice_ai_generation() -> str:
    """
    Phase 2b: Generate AI response for the transcribed voice input.
    This is called after the generating indicator has been displayed.
    
    Returns:
        str: Status message about generation result
    """
    # Check if there's pending voice text to process
    if not hasattr(app_state, '_pending_voice_text') or not app_state._pending_voice_text:
        return ""

    text_input = app_state._pending_voice_text
    app_state._pending_voice_text = None  # Clear pending text

    # Cross-client generating lock (atomic check-and-set, BEFORE image consumption)
    from backend.backend import _backend_state
    if _backend_state:
        with _backend_state.is_generating_lock:
            if _backend_state.is_generating:
                app_state.response_generating = False
                app_state._chat_history_version += 1
                return t('gen.another_client')
            _backend_state.is_generating = True
        try:
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_generating_state_sync(True)
        except Exception:
            pass

    # Get images: prefer image_buffer (has companion images), fallback to pending
    pending_images = None
    try:
        if _backend_state and _backend_state.image_buffer.get_count() > 0:
            pending_images = _backend_state.image_buffer.consume_all()
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_image_slot_update_sync(0)
    except Exception:
        pass
    if not pending_images:
        pending_images = getattr(app_state, '_pending_user_images', None)
    app_state._pending_user_images = None  # Clear pending images

    # Get documents: same as the text path — the voice path used to skip this,
    # so an attached document leaked into the next text turn instead (L10).
    pending_documents = None
    try:
        if _backend_state and _backend_state.document_buffer.get_count() > 0:
            pending_documents = _backend_state.document_buffer.consume_all()
    except Exception:
        pass
    if not pending_documents:
        pending_documents = getattr(app_state, '_pending_user_documents', None)
    app_state._pending_user_documents = None

    try:
        ai_reply = handle_generate_reply(text_input, images=pending_images, documents=pending_documents)

        # Check for errors in the reply
        if is_error_response(ai_reply):
            app_state.add_log_message("warning", f"AI reply contained error: {ai_reply}")
            # 汎用文でなく原因入りの具体メッセージを表示(text_input と同型・
            # 稜裁定 2026-08-02)
            return error_status_text(ai_reply)
        else:
            app_state.add_log_message("info", f"AI reply generated successfully ({len(ai_reply)} chars)")
            return t('gen.done')

    except Exception as e:
        app_state.add_log_message("error", f"Error in voice AI generation: {e}")
        return t('gen.error')
    finally:
        # Always clear generating state
        app_state.response_generating = False
        app_state._chat_history_version += 1  # Force update to hide generating indicator
        # Clear cross-client generating lock and broadcast
        if _backend_state:
            with _backend_state.is_generating_lock:
                _backend_state.is_generating = False
            try:
                from backend.server.websocket_server import get_websocket_manager
                get_websocket_manager().broadcast_generating_state_sync(False)
            except Exception:
                pass
        # Notify all WS clients (companion mode sync)
        notify_ui_update(reason="voice_generation_complete")


def voice_btn_chain(is_recording: bool):
    """Phase 5 Day 2: Unified voice button event handler.

    Replaces the prior 7-stage .then() chain with a single generator.
    The chain version was vulnerable to Gradio queue interleaving — late
    stages of chain N could fire during chain N+1, sometimes dropping
    stages entirely (HAR analysis 2026-04-26 14:14-14:21 UTC). Single-event
    design + concurrency_limit=1 + trigger_mode="once" guarantees that
    chain N+1 cannot start until chain N completes, and clicks while a
    chain is pending are discarded.
    """
    # Plan H: drop chat_display from yields where a WS broadcast already covers
    # the chat refresh — eliminates double-fetches. Yield 2 keeps chat because
    # start_voice_generation() does NOT fire notify_ui_update, so the in-chat
    # "Generating..." spinner has no other delivery path.

    # Branch on the ACTUAL claim, not the Gradio State: hotkey start/stop does
    # not update the State, so after a hotkey action the State is stale and a
    # UI click would take the wrong branch ("Already recording" dead end /
    # stop-without-recording). The State stays wired as an output for the
    # click-path bookkeeping but is not trusted as input.
    is_recording = app_state.recording_start_time is not None

    if not is_recording:
        # === Start branch ===
        # Recording-start does not change chat content; rely on existing display.
        result = toggle_voice_recording(False)
        new_start = time.time() if result[4] else None
        # UIクリック開始も停止ブランチと同様にWSへ同報する。hotkey停止のJS直書き
        # 後はGradioフロントのpropsが実DOMより古く、同値のyieldはsvelteのprop差分で
        # 捨てられ再描画されない(2026-07-25ヘッドレス実測)ため、WS側(JS同値no-op)
        # が表示を担保する。条件はGradio戻り値でなく実クレーム:
        # toggle_voice_recording の成功分岐は文字列マッチの else 側で、生成中
        # ブロック等も成功扱いになりうる(external_start_recording と同じ判定)。
        if app_state.recording_start_time is not None:
            _broadcast_recording_display(
                "recording", t('rec.recording'),
                button=t('voicebtn.stop_recording'), button_class="voice-btn-recording")
        yield (
            result[0],   # voice_btn
            result[1],   # mic_status
            result[2],   # recording_time content
            result[3],   # recording_time visibility
            result[4],   # is_recording
            new_start,   # recording_start_time
            gr.update(), # chat_display unchanged
        )
        return

    # === Stop branch ===
    # 即時フィードバック: 停止押下と同時にトグル/インジケータを「文字起こし中」へ
    # 切り替える。従来は最初のyieldが文字起こし完了後で、STTが遅い環境(Mac CPU)
    # では赤い「録音を停止」が数秒〜十数秒残った。WS同報はSSEが切れた場合の
    # 自己回復用ミラー(内容同一・JS側は同値ならno-op)。
    _broadcast_recording_display(
        "processing", t('rec.stat_transcribing'),
        button=t('voicebtn.transcribing'), button_class="voice-btn-ready")
    yield (
        gr.update(value=t('voicebtn.transcribing'), elem_classes="voice-btn-ready"),
        gr.update(value=_mic_status_html("processing", t('rec.stat_transcribing'))),
        gr.update(value=""),
        gr.update(visible=False),
        False,
        None,
        gr.update(),
    )

    # Phase 1: stop + transcribe (~1-15s blocking via bridge.wait_for_result).
    # User message append is broadcast via notify_ui_update("transcription_complete")
    # inside stop_and_transcribe_phase1, which already triggers ws-update-trigger.
    result = toggle_voice_recording(True)
    transcribed = result[5]
    yield (
        result[0], result[1], result[2], result[3],
        result[4],   # is_recording (False)
        None,        # recording_start_time cleared
        gr.update(), # chat_display covered by transcription_complete WS
    )

    if not transcribed:
        return

    # Phase 2: show "Generating..." indicator. start_voice_generation does NOT
    # broadcast — this yield is the only path for the in-chat spinner, so chat
    # MUST be sent here.
    indicator = start_voice_generation()
    _broadcast_recording_display("processing", indicator or t('gen.generating'))
    chat = get_chat_history()
    yield (
        gr.update(),
        gr.update(value=_mic_status_html("processing", indicator or t('gen.generating'))),
        gr.update(), gr.update(),
        gr.update(), gr.update(), chat,
    )

    # Phase 3: AI generation (10-30s); outer try/except guarantees final yield.
    # process_voice_ai_generation's finally block fires
    # notify_ui_update("voice_generation_complete") which triggers ws-update-trigger,
    # so chat_display refresh is already covered.
    try:
        final = process_voice_ai_generation()
    except Exception as e:
        app_state.add_log_message("error", f"voice_btn_chain: AI gen error: {e}")
        final = t('gen.error')
    _broadcast_recording_display("ready", final or t('micstat.ready'))
    # 生成結果メッセージ(✅/❌)も同じスロットに出るため同じく約10秒で
    # Readyへ戻す(稜依頼 2026-08-02)
    _schedule_status_reset()
    yield (
        gr.update(),
        gr.update(value=_mic_status_html("ready", final)),
        gr.update(), gr.update(),
        gr.update(), gr.update(),
        gr.update(), # chat_display covered by voice_generation_complete WS
    )


def external_start_recording() -> Optional[Tuple[gr.update, str]]:
    """
    Called by external hotkey/foot pedal library to start recording.
    
    Returns:
        Optional[Tuple[gr.update, str]]: Recording status or None if not active
    """
    logger.info("[EXTERNAL] external_start_recording called")
    logger.info(f"[EXTERNAL] State - conversation_started: {app_state.conversation_started}, "
               f"recording: {app_state.recording_start_time is not None}")
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    # Reset auto prompt timer when external recording starts
    reset_auto_prompt_timer()

    if app_state.conversation_started:
        logger.info("[EXTERNAL] Conditions met, calling record_speech()")
        result = record_speech()
        logger.info(f"[EXTERNAL] record_speech() returned: {result}")
        app_state.add_log_message("info", "[EXTERNAL] Started recording via hotkey/external trigger")
        # Gradioイベント外からの開始なので、インジケータ/ボタンはWS経由で同期
        if app_state.recording_start_time is not None:
            _broadcast_recording_display(
                "recording", t('rec.recording'),
                button=t('voicebtn.stop_recording'), button_class="voice-btn-recording")
        return result
    else:
        logger.warning(f"[EXTERNAL] Recording trigger ignored - conversation_started: {app_state.conversation_started}")
        app_state.add_log_message("warning", "[EXTERNAL] Recording trigger ignored - conversation not active")
        return None


def external_stop_recording() -> Optional[Tuple[gr.update, str]]:
    """
    Called by external hotkey/foot pedal library to stop recording.
    Processes in two phases: transcription (immediate) and AI generation (async).
    
    Returns:
        Optional[Tuple[gr.update, str]]: Transcription status or None if not active
    """
    logger.info("[EXTERNAL] external_stop_recording called")
    logger.info(f"[EXTERNAL] State - conversation_started: {app_state.conversation_started}, "
               f"recording: {app_state.recording_start_time is not None}")

    if app_state.conversation_started:
        logger.info("[EXTERNAL] Conditions met, processing in phases")

        # 即時フィードバック: 停止トリガーと同時に「文字起こし中」へ切り替える。
        # 従来は phase 1 (文字起こし) 完了後の同報が最初のUI反映で、STTが遅い
        # 環境(Mac CPU)では録音中表示が残った。
        _broadcast_recording_display(
            "processing", t('rec.stat_transcribing'),
            button=t('voicebtn.transcribing'), button_class="voice-btn-ready")

        # Phase 1: Stop and transcribe (adds user message to chat)
        button_update, status, transcribed_text = stop_and_transcribe_phase1()
        logger.info(f"[EXTERNAL] Phase 1 complete - transcribed: {bool(transcribed_text)}")
        app_state.add_log_message("info", "[EXTERNAL] Stopped recording via hotkey/external trigger")

        # Gradioイベント外からの停止なので、インジケータ/ボタンはWS経由で同期
        if transcribed_text:
            _broadcast_recording_display(
                "processing", t('gen.generating'),
                button=t('voicebtn.press_to_record'), button_class="voice-btn-ready")
        else:
            _broadcast_recording_display(
                "ready", str(status) if status else t('micstat.ready'),
                button=t('voicebtn.press_to_record'), button_class="voice-btn-ready")

        if transcribed_text:
            # Send immediate WebSocket notification for user message display
            logger.info("[EXTERNAL] Sending WebSocket notification for transcription")
            ws_sent = notify_ui_update("hotkey_transcription_complete", {"text": transcribed_text[:100]})
            logger.info(f"[EXTERNAL] WebSocket notification sent: {ws_sent}")
            
            # Phase 2: Generate AI response asynchronously
            def async_ai_generation():
                # Cross-client generating lock (atomic check-and-set, BEFORE try)
                from backend.backend import _backend_state
                if _backend_state:
                    with _backend_state.is_generating_lock:
                        if _backend_state.is_generating:
                            logger.info("[EXTERNAL] Blocked: another client is generating")
                            # finally を通らない早期return: インジケータを戻す
                            _broadcast_recording_display("ready", t('micstat.ready'))
                            return
                        _backend_state.is_generating = True
                    try:
                        from backend.server.websocket_server import get_websocket_manager
                        get_websocket_manager().broadcast_generating_state_sync(True)
                    except Exception:
                        pass

                try:
                    logger.info("[EXTERNAL] Starting AI generation phase")
                    # Show generating indicator
                    app_state.response_generating = True
                    app_state._chat_history_version += 1  # Force UI update
                    notify_ui_update("ai_generation_started")

                    # Generate response
                    ai_reply = handle_generate_reply(transcribed_text)
                    logger.info(f"[EXTERNAL] AI generation complete - success: {not is_error_response(ai_reply)}")

                    # Notify completion
                    notify_ui_update("ai_generation_complete", {"success": not is_error_response(ai_reply)})

                except Exception as e:
                    logger.error(f"[EXTERNAL] Error in AI generation: {e}")
                    notify_ui_update("ai_generation_error", {"error": str(e)})
                finally:
                    app_state.response_generating = False
                    app_state._chat_history_version += 1  # Force UI update
                    # hotkey経路の生成完了: インジケータをReadyへ戻す(WS経由)
                    _broadcast_recording_display("ready", t('micstat.ready'))
                    # Clear cross-client generating lock and broadcast
                    if _backend_state:
                        with _backend_state.is_generating_lock:
                            _backend_state.is_generating = False
                        try:
                            from backend.server.websocket_server import get_websocket_manager
                            get_websocket_manager().broadcast_generating_state_sync(False)
                        except Exception:
                            pass
            
            # Start AI generation in background thread
            import threading
            thread = threading.Thread(target=async_ai_generation, daemon=True)
            thread.start()
        else:
            logger.warning("[EXTERNAL] No transcribed text, skipping AI generation")
        
        return button_update, status
    else:
        logger.warning(f"[EXTERNAL] Stop trigger ignored - conversation_started: {app_state.conversation_started}")
        app_state.add_log_message("warning", "[EXTERNAL] Stop trigger ignored - conversation not active")
        return None
