"""
ui/conversation/auto_prompt.py

自動プロンプト機能。無応答タイマー(threading.Timer+スリープ耐性のawakeゲート)の
エンジンと、その設定ハンドラを持つ。
"""

import logging
import threading
import time

from ..state import app_state
from backend.shared.awake_clock import awake_seconds
from backend.shared.ui_events import publish_ui_update
from backend.shared.wake_gate import get_wake_gate

import backend

logger = logging.getLogger(__name__)


# Global variables for auto prompt timer management
_auto_prompt_timer = None
_auto_prompt_lock = threading.Lock()
# スリープ耐性: AutoPromptのthreading.TimerはWindowsのbiasedタイムアウトのため
# スリープ復帰時に即発火しうる。awake時計で実経過をゲートし、不足なら再アームする。
_auto_prompt_armed_awake = None  # タイマー武装時の awake_seconds()
_auto_prompt_generation = 0      # ステイルなタイマー発火を無視する世代カウンタ


def _on_auto_prompt_timer_expired(generation=None):
    """Called when auto prompt timer expires (from threading.Timer thread).

    スリープ耐性: threading.TimerはWindowsで「起きている時間」ではなく biased
    タイムアウトで発火するため、スリープ復帰時に即発火しうる。awake時計で実経過を
    ゲートし、武装からの awake経過が不足していれば発火せず残り時間で再アームする
    （=スリープ中に経過した分は数えない）。世代カウンタでステイルな発火を無視する。
    """
    global _auto_prompt_timer, _auto_prompt_armed_awake

    should_fire = False
    resync_remaining = None
    with _auto_prompt_lock:
        # ステイルネス・ガード: reset/再startで世代が進んだ古いタイマー発火は無視
        if generation is not None and generation != _auto_prompt_generation:
            return
        if not app_state.auto_prompt_timer_active or _auto_prompt_armed_awake is None:
            return

        duration = app_state.auto_prompt_timer_duration

        gate = get_wake_gate()
        if not gate.is_open():
            # 覚醒ゲート閉(スリープ後の無人覚醒=DarkWake等)→発火せず、起点を現在へ
            # 進めてフルdurationで再アーム(復帰後にあらためて沈黙N秒を要求する)。
            # UI再同期はしない(画面消灯中・遷移スパム防止)。
            _auto_prompt_armed_awake = awake_seconds()
            _auto_prompt_timer = threading.Timer(
                duration, _on_auto_prompt_timer_expired, args=[generation]
            )
            _auto_prompt_timer.daemon = True
            _auto_prompt_timer.start()
            app_state.add_log_message(
                "debug", "[Auto Prompt] 覚醒ゲート閉(無人覚醒)→ 発火せずフル再アーム"
            )
            return

        # 復帰後は「復帰時点からの沈黙」を要求する(スリープ前+無人覚醒中の蓄積で
        # 復帰直後に発火させない)
        start_ref = _auto_prompt_armed_awake
        if gate.last_resume_awake is not None and gate.last_resume_awake > start_ref:
            start_ref = gate.last_resume_awake
        elapsed = awake_seconds() - start_ref
        if elapsed + 1.0 < duration:
            # awake経過が不足（スリープ等）→ 発火せず残りで再アーム（武装基準は維持）
            remaining = duration - elapsed
            _auto_prompt_timer = threading.Timer(
                remaining, _on_auto_prompt_timer_expired, args=[generation]
            )
            _auto_prompt_timer.daemon = True
            _auto_prompt_timer.start()
            resync_remaining = remaining
            app_state.add_log_message(
                "debug",
                f"[Auto Prompt] awake経過不足({elapsed:.0f}/{duration:.0f}s) → "
                f"残り{remaining:.0f}sで再アーム（スリープ補正）"
            )
        else:
            # 通常発火
            app_state.auto_prompt_timer_active = False
            should_fire = True

    if resync_remaining is not None:
        # JS側カウントダウン表示はDate.now()(壁時計)基準でスリープ中に進んでしまうため、
        # 再アーム後の残り時間(awake基準)で表示を再同期する（「Sending...」固着を防ぐ）。
        publish_ui_update(
            "auto_prompt_countdown_start",
            reason="sleep_resync",
            data={"duration": resync_remaining}
        )
        return

    if should_fire:
        app_state.add_log_message("debug", "[Auto Prompt] Timer expired, triggering via WebSocket")
        # WebSocket通知: チャット更新 + auto prompt処理開始
        publish_ui_update(
            "auto_prompt_chat_update",
            reason="timer_expired",
            data={"stage": "generating"}
        )


def process_auto_prompt():
    """
    Process pending auto prompt in main thread.
    This function handles the complete auto prompt flow.
    Called from WebSocket trigger (auto-prompt-generating-trigger button click).
    """
    # WebSocket方式では、この関数はトリガーされた時にのみ呼ばれるため、
    # auto_prompt_pendingフラグのチェックは不要

    app_state.add_log_message("debug", "[Auto Prompt] process_auto_prompt() called")

    try:
        # Get language settings
        stt_language = 'en'
        char_name = 'AI'
        if app_state.active_character_id:
            config_response = backend.load_character_config(app_state.active_character_id)
            if isinstance(config_response, dict) and 'result' in config_response:
                char_config = config_response.get('result', {})
            else:
                char_config = config_response
            stt_language = char_config.get('faster_whisper_config', {}).get('language', 'en').lower()
            char_name = char_config.get('name', 'AI')
        
        # Select prompt based on language
        auto_prompt_text = app_state.auto_prompt_ja if stt_language == 'ja' else app_state.auto_prompt_en
        
        app_state.add_log_message("info", f"[Auto Prompt] Sending after {app_state.auto_prompt_timer_duration}s silence")
        
        # Set generating state to show "Generating Response" in UI
        app_state.response_generating = True
        app_state._chat_history_version += 1  # Force UI update
        
        # Store the auto prompt request
        app_state._pending_auto_prompt_text = auto_prompt_text
        app_state._pending_auto_prompt_char = char_name
        
    except Exception as e:
        app_state.add_log_message("error", f"[Auto Prompt] Error: {e}")
        reset_auto_prompt_timer()
        app_state.response_generating = False
        app_state._chat_history_version += 1


def execute_auto_prompt_generation():
    """
    Execute the actual auto prompt generation (called after UI shows generating state).
    """
    from backend.shared.ui_events import publish_ui_update

    app_state.add_log_message("debug", "[Auto Prompt] execute_auto_prompt_generation() called")

    # Check if there's a pending auto prompt to generate
    if not hasattr(app_state, '_pending_auto_prompt_text'):
        app_state.add_log_message("warning", "[Auto Prompt] No pending auto prompt text found")
        return

    auto_prompt_text = app_state._pending_auto_prompt_text
    char_name = getattr(app_state, '_pending_auto_prompt_char', 'AI')

    # Clear the pending auto prompt
    delattr(app_state, '_pending_auto_prompt_text')
    if hasattr(app_state, '_pending_auto_prompt_char'):
        delattr(app_state, '_pending_auto_prompt_char')

    # 会話が既に終了していたら中止(稜裁定 2026-08-21)。タイマー発火→実行の
    # 間にEndが通った場合の受け皿(stop_auto_prompt_on_conversation_endは
    # タイマーは掃除するがpending済みの実行チェーンまでは止められない)。
    # pendingは上で消費済み=残すと次回ターンで再生される(L9と同型の罠)。
    if not app_state.conversation_started:
        app_state.response_generating = False
        app_state._chat_history_version += 1
        publish_ui_update(
            "auto_prompt_chat_update",
            reason="generation_blocked",
            data={"stage": "complete"}
        )
        app_state.add_log_message("info", "[Auto Prompt] Blocked: conversation ended before generation")
        return

    # Cross-client generating lock (atomic check-and-set, BEFORE try)
    from backend.backend import _backend_state
    if _backend_state:
        with _backend_state.is_generating_lock:
            if _backend_state.is_generating:
                app_state.response_generating = False
                app_state._chat_history_version += 1
                publish_ui_update(
                    "auto_prompt_chat_update",
                    reason="generation_blocked",
                    data={"stage": "complete"}
                )
                app_state.add_log_message("info", "[Auto Prompt] Blocked: another client is generating")
                return
            _backend_state.is_generating = True
        try:
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_generating_state_sync(True)
        except Exception:
            pass

    def _clear_generating_lock():
        """Helper to clear is_generating lock and broadcast."""
        if _backend_state:
            with _backend_state.is_generating_lock:
                _backend_state.is_generating = False
            try:
                from backend.server.websocket_server import get_websocket_manager
                get_websocket_manager().broadcast_generating_state_sync(False)
            except Exception:
                pass

    try:
        # Send to backend (includes system prompt + conversation history + long-term memory + auto prompt)
        # is_auto_prompt=True: 固定文のuserメッセージ保存にUI非表示印を付ける
        # (会話ページの履歴ロードがスキップ=稜裁定 2026-08-21)
        result = backend.generate_reply(auto_prompt_text, app_state.active_character_id,
                                        is_auto_prompt=True)

        if result.get("success"):
            ai_reply = result.get("response")

            # The backend already spoke this reply (single-speaker design,
            # ST6-§7⑤): generate_reply blocks until TTS playback finishes,
            # so speaking here again would double the audio (the old
            # auto-prompt double-TTS bug). Just re-arm the auto prompt timer.
            if app_state.audio_output_available:
                start_auto_prompt_timer()

            # AFTER TTS completes, add to chat history and update UI
            app_state.append_chat_message(char_name, ai_reply, is_ai=True)

            # Clear generating state
            app_state.response_generating = False
            app_state._chat_history_version += 1
            _clear_generating_lock()

            # WebSocket通知: TTS完了後にチャット更新
            publish_ui_update(
                "auto_prompt_chat_update",
                reason="generation_complete",
                data={"stage": "complete"}
            )

            # If TTS was not available, start timer here
            if not app_state.audio_output_available:
                start_auto_prompt_timer()

            app_state.add_log_message("info", "[Auto Prompt] Response generated successfully")
        else:
            app_state.response_generating = False
            app_state._chat_history_version += 1
            _clear_generating_lock()
            # WebSocket通知: エラー時もチャット更新（generating状態をクリア）
            publish_ui_update(
                "auto_prompt_chat_update",
                reason="generation_failed",
                data={"stage": "complete"}
            )
            app_state.add_log_message("warning", f"[Auto Prompt] Failed: {result.get('error')}")
            reset_auto_prompt_timer()

    except Exception as e:
        app_state.response_generating = False
        app_state._chat_history_version += 1
        _clear_generating_lock()
        # WebSocket通知: エラー時もチャット更新（generating状態をクリア）
        publish_ui_update(
            "auto_prompt_chat_update",
            reason="generation_error",
            data={"stage": "complete"}
        )
        app_state.add_log_message("error", f"[Auto Prompt] Generation error: {e}")
        reset_auto_prompt_timer()


def start_auto_prompt_timer():
    """Start the auto prompt timer"""
    global _auto_prompt_timer, _auto_prompt_armed_awake, _auto_prompt_generation
    from backend.shared.ui_events import publish_ui_update

    if not app_state.auto_prompt_enabled or not app_state.conversation_started:
        return

    with _auto_prompt_lock:
        if _auto_prompt_timer:
            _auto_prompt_timer.cancel()

        # スリープ耐性: 世代を進めて古いタイマー発火を無効化し、武装時のawake時刻を記録
        _auto_prompt_generation += 1
        _auto_prompt_armed_awake = awake_seconds()

        app_state.auto_prompt_timer_active = True

        _auto_prompt_timer = threading.Timer(
            app_state.auto_prompt_timer_duration,
            _on_auto_prompt_timer_expired,  # WebSocket通知でUI更新をトリガー
            args=[_auto_prompt_generation]
        )
        _auto_prompt_timer.daemon = True
        _auto_prompt_timer.start()

        app_state.add_log_message("debug", f"[Auto Prompt] Timer started ({app_state.auto_prompt_timer_duration}s)")

        # WebSocket通知: カウントダウン開始（JS側でsetIntervalを開始）
        publish_ui_update(
            "auto_prompt_countdown_start",
            reason="timer_started",
            data={"duration": app_state.auto_prompt_timer_duration}
        )


def reset_auto_prompt_timer():
    """Reset the auto prompt timer"""
    global _auto_prompt_timer, _auto_prompt_armed_awake, _auto_prompt_generation
    from backend.shared.ui_events import publish_ui_update

    with _auto_prompt_lock:
        was_active = app_state.auto_prompt_timer_active

        if _auto_prompt_timer:
            _auto_prompt_timer.cancel()
            _auto_prompt_timer = None

        # スリープ耐性: 世代を進めて、保留中/発火待ちの古いタイマーを無効化
        _auto_prompt_generation += 1
        _auto_prompt_armed_awake = None

        app_state.auto_prompt_timer_active = False
        app_state.add_log_message("debug", "[Auto Prompt] Timer reset")

        # WebSocket通知: カウントダウン停止（アクティブだった場合のみ）
        if was_active:
            publish_ui_update(
                "auto_prompt_countdown_stop",
                reason="timer_reset",
                data={"enabled": app_state.auto_prompt_enabled}
            )


def stop_auto_prompt_on_conversation_end():
    """End Conversation時のAuto Promptタイマー完全掃除。

    reset_auto_prompt_timer()はwas_activeの時しかstop通知をpublishしないため、
    タイマー発火後(=JS表示がSending...のまま)のEndでは通知が出ず表示が固着する。
    トグルOFF時(toggle_auto_prompt)と同じく、resetに加えて無条件でstop通知を
    送る(JS側がinterval clearとInactive/Disabled表示まで行う)。
    手動End経路だけこの掃除が抜けていた(Mac実機 2026-07-22: End後もタイマー
    表示が残留・古いthreading.Timerが会話終了後に発火しうる)。
    """
    from backend.shared.ui_events import publish_ui_update

    reset_auto_prompt_timer()
    publish_ui_update(
        "auto_prompt_countdown_stop",
        reason="conversation_ended",
        data={"enabled": app_state.auto_prompt_enabled}
    )


def toggle_auto_prompt(enabled: bool) -> bool:
    """Toggle auto prompt feature on/off"""
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    from backend.shared.ui_events import publish_ui_update

    app_state.auto_prompt_enabled = enabled
    app_state.add_log_message("info", f"Auto prompt {'enabled' if enabled else 'disabled'}")

    if not enabled:
        reset_auto_prompt_timer()
        # タイマーが非アクティブでも、無効化時は常に通知を送信
        # （reset_auto_prompt_timer内ではwas_activeの場合のみ通知するため）
        publish_ui_update(
            "auto_prompt_countdown_stop",
            reason="disabled",
            data={"enabled": False}
        )
    elif app_state.conversation_started:
        # Start timer if conversation is active
        start_auto_prompt_timer()
    else:
        # 有効化されたが会話未開始の場合、Inactive表示に更新
        publish_ui_update(
            "auto_prompt_countdown_stop",
            reason="enabled_but_inactive",
            data={"enabled": True}
        )

    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)

    return enabled


def update_auto_prompt_timer_duration(duration: int) -> None:
    """Update auto prompt timer duration.

    戻り値なし: スライダーへ値を書き戻すと、素早いドラッグ中にステイルな
    書き戻しが .change を再発火させてイベントがピンポンする。書き戻さない。
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    app_state.auto_prompt_timer_duration = duration
    app_state.add_log_message("info", f"Auto prompt timer duration set to {duration}s")

    # Reset timer with new duration if active
    if app_state.auto_prompt_timer_active:
        start_auto_prompt_timer()

    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)


