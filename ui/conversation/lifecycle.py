"""
ui/conversation/lifecycle.py

会話ライフサイクルの責務。会話の開始/終了トグル・
会話状態のWS broadcast・開始時のキャラクター情報更新を持つ。
"""

import logging
from pathlib import Path
from typing import Tuple

import gradio as gr

from ..state import app_state, show_error_popup, show_warning_popup
from backend.shared.i18n import t

import backend

from .history import load_conversation_history

logger = logging.getLogger(__name__)


def _broadcast_conversation_state(started: bool):
    """Broadcast conversation state (started/ended) to all WS clients for companion sync."""
    try:
        from backend.server.websocket_server import get_websocket_manager
        get_websocket_manager().broadcast_conversation_state_sync(started)
    except Exception:
        pass


def toggle_start_end() -> Tuple[str, gr.update]:
    """
    Toggle conversation start/end state when user clicks Start or End.

    チャット欄はoutputsに含めない: ここで返すステータス文は直後の
    .then(get_chat_history)が~0.5秒で必ず上書きし読めない点滅にしか
    ならなかった(稜裁定 2026-07-19=撤去)。異常系の告知はポップアップが担う。

    Returns:
        Tuple[str, gr.update]: Button text, voice button update
        (生成中ガードで拒否した場合は両方 gr.update()=無変更)

    Raises:
        No exceptions raised, errors handled internally
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    # Check if backend is available
    if not app_state.backend_available:
        show_error_popup(t('conv.backend_unavailable_title'), t('conv.backend_unavailable_msg'))
        return t('btn.start_conversation'), gr.update(value=t('voicebtn.backend_unavailable'), interactive=False)

    # 音声ターン(録音〜文字起こし)と生成中(TTS再生完了まで)はStart/Endを
    # 受け付けない(稜裁定 2026-08-21)。走行中ターンにキャンセル機構はなく、
    # ここでstopすると終了済み会話へのコミット・終了後のTTS再生・ボタン復活が
    # 起きる(2026-08-17 app.log実測)。録音中の終了はさらに、殺されかけた録音の
    # 部分音声が次の会話へ文字起こしされる事故窓になる。主表示はJS側の
    # グレーアウト(is_generating_update / recording_display)で、ここはWS断・
    # 競合窓・リロード直後の受け止め。gr.update()=無変更を返すのは、ラベルを
    # 返すと再レンダがJSのdisabledを剥がすため。
    try:
        from backend.backend import _backend_state as _bs
        generating = _bs is not None and _bs.is_generating
    except Exception:
        generating = False
    # 録音・文字起こし条件は会話中のみ効かせる: 万一クレームが異常系で
    # 残ってもStart(会話開始)まで死なないように
    voice_busy = app_state.conversation_started and (
        app_state.recording_start_time is not None or app_state.audio.transcribing)
    if generating or voice_busy:
        show_warning_popup(t('conv.end_blocked_title'), t('conv.end_blocked_msg'))
        return gr.update(), gr.update()

    if app_state.conversation_started:
        # End conversation
        try:
            # Properly finalize the conversation in the backend
            result = backend.stop_conversation()
            
            if result["success"]:
                # Only update state if backend operation succeeded
                app_state.conversation_started = False
                app_state.add_log_message("info", f"Conversation ended: {result.get('message', 'Conversation stopped successfully')}")
                
                # Clear chat history after successful stop
                app_state.clear_chat_history()
                # Auto Promptタイマーの完全掃除(残留するとEnd後に古いタイマーが
                # 発火し、JSカウントダウン表示も固着する — 録音/テキスト送信/
                # リモートタイムアウト経路は既にreset済で、手動Endだけ抜けていた)
                try:
                    from .auto_prompt import stop_auto_prompt_on_conversation_end
                    stop_auto_prompt_on_conversation_end()
                except Exception as ap_err:
                    logger.warning(f"Auto prompt cleanup on end failed: {ap_err}")
                # Broadcast conversation state to all WS clients (companion sync)
                _broadcast_conversation_state(False)
                # 終了で返す(稜裁定 2026-08-01): Ollamaのチャット/埋め込み
                # モデルを背景でアンロード。記憶保存・抽出の完了待ちと
                # 再開時の中止はワーカー側が持つ(Never raises)
                from ui.handlers.ollama_vram import release_ollama_models
                release_ollama_models()
                # MotionPNGPlayerも会話終了で閉じる(稜裁定 2026-08-01:
                # Endボタンのみ。Disappearボタンと同一経路・Never raises)
                try:
                    from backend.server.websocket_server import get_websocket_manager
                    get_websocket_manager().close_motion_pngtuber_sync()
                except Exception as mpp_err:
                    logger.warning(f"MotionPNGPlayer close on end failed: {mpp_err}")
                return t('btn.start_conversation'), gr.update(value=t('voicebtn.start_first'), variant="secondary", elem_classes=[], interactive=False)
            else:
                # Backend failed to stop conversation
                error_msg = result.get("error", "Unknown error stopping conversation")
                app_state.add_log_message("error", f"Failed to stop conversation: {error_msg}")
                show_error_popup(t('conv.stop_error_title'), error_msg)
                # Keep conversation active since stop failed
                return t('btn.end_conversation'), gr.update(value=t('voicebtn.error'), interactive=False)
                
        except Exception as e:
            # Handle unexpected exceptions
            app_state.add_log_message("error", f"Unexpected error ending conversation: {e}")
            show_error_popup(t('popup.unexpected_title'), t('conv.status_stop_failed', error=str(e)))
            # Attempt to clean up UI state anyway
            app_state.conversation_started = False
            app_state.clear_chat_history()
            try:
                from .auto_prompt import stop_auto_prompt_on_conversation_end
                stop_auto_prompt_on_conversation_end()
            except Exception as ap_err:
                logger.warning(f"Auto prompt cleanup on end failed: {ap_err}")
            # 例外経路でもEnd相当の後処理と同様にVRAMを返す(Never raises)
            from ui.handlers.ollama_vram import release_ollama_models
            release_ollama_models()
            # 例外経路でもMotionPNGPlayerを閉じる(成功経路と同じ後始末)
            try:
                from backend.server.websocket_server import get_websocket_manager
                get_websocket_manager().close_motion_pngtuber_sync()
            except Exception as mpp_err:
                logger.warning(f"MotionPNGPlayer close on end failed: {mpp_err}")
            return t('btn.start_conversation'), gr.update(value=t('voicebtn.error'), interactive=False)
    else:
        # Check if a character is selected
        if not app_state.active_character_id:
            show_warning_popup(t('conv.no_char_title'), t('conv.no_char_start_msg'))
            return t('btn.start_conversation'), gr.update(value=t('voicebtn.select_char'), interactive=False)
            
        # Start conversation
        try:
            # Initialize a new conversation in the backend
            # Try calling with character_id parameter (stateless approach)
            try:
                result = backend.start_conversation(character_id=app_state.active_character_id)
            except TypeError as e:
                # If that fails, try without parameters (stateful approach)
                logger.warning(f"Backend expects different parameters for start_conversation: {e}")
                result = backend.start_conversation()
            
            if result["success"]:
                # Only update state if backend operation succeeded
                app_state.conversation_started = True
                app_state.add_log_message("info", f"Conversation started with character {result.get('character_id', app_state.active_character_id)}")

                # Load conversation history (same pattern as switch_character in character_ui.py)
                load_conversation_history(app_state.active_character_id)

                # Broadcast conversation state to all WS clients (companion sync)
                _broadcast_conversation_state(True)

                # Auto Prompt有効なら会話開始と同時にタイマー武装。従来は
                # ターン完了時のみ武装で、開始直後の沈黙では永遠に作動せず
                # 「トグルOFF→ONで始まる」状態だった(稜実機 2026-07-22)。
                # start_auto_prompt_timer自身がenabled+会話中ガードと
                # countdown_start通知を持つ(失敗しても会話開始は成立させる)。
                try:
                    if app_state.auto_prompt_enabled:
                        from .auto_prompt import start_auto_prompt_timer
                        start_auto_prompt_timer()
                except Exception as ap_err:
                    logger.warning(f"Auto prompt arm on start failed: {ap_err}")

                # ST-E: warm the Whisper model in the background. Recording
                # works during the load; transcription just waits for it.
                # 準備中の告知はロードworkerのstt_model_status(WS)とmic
                # インジケータが担う(チャット欄への注記は点滅撤去と共に廃止)。
                # 2026-07-25: 主経路はキャラ選択時ウォーム(switch_character)へ
                # 移動。ここは選択なし直Start等の取りこぼし用フォールバック。
                from ui.handlers.stt_engine import warm_local_stt_model
                warm_local_stt_model()

                # 開始で温める(稜裁定 2026-08-01): Ollamaの埋め込み→チャット
                # モデルを背景ロードし、初回送信の直列コールドスタート
                # (qwen3:14b実測22.8秒+埋め込み)を先払いする(Never raises)
                from ui.handlers.ollama_vram import warm_ollama_models
                warm_ollama_models()

                return t('btn.end_conversation'), gr.update(value=t('voicebtn.press_to_record'), elem_classes="voice-btn-ready", interactive=True)
            else:
                # Backend failed to start conversation
                error_msg = result.get("error", "Unknown error starting conversation")
                app_state.add_log_message("error", f"Failed to start conversation: {error_msg}")
                # error_code があれば翻訳本文、無ければ従来の文字列そのまま
                # (稜裁定 2026-07-25: 翻訳できるものは翻訳・できなければ原文)
                error_code = result.get("error_code")
                display_msg = (t(f"err.{error_code}", **result.get("error_params", {}))
                               if error_code else error_msg)
                show_error_popup(t('conv.start_error_title'), display_msg)
                # Don't update conversation state since start failed
                return t('btn.start_conversation'), gr.update(value=t('voicebtn.error'), interactive=False)
                
        except Exception as e:
            # Handle unexpected exceptions
            app_state.add_log_message("error", f"Unexpected error starting conversation: {e}")
            show_error_popup(t('popup.unexpected_title'), t('conv.status_start_failed', error=str(e)))
            # Don't update conversation state since start failed
            return t('btn.start_conversation'), gr.update(value=t('voicebtn.error'), interactive=False)


def sync_char_dropdown_lock() -> gr.update:
    """
    Lock/unlock the character dropdown based on conversation state.

    Called via .then() after toggle_start_end(). Deriving from
    app_state.conversation_started (instead of hardcoding per branch) keeps
    the lock correct even when start/stop fails and the state doesn't flip.
    While locked, the user cannot pick a new character, so the dropdown can
    never display a character that the blocked switch didn't actually apply.
    """
    return gr.update(interactive=not app_state.conversation_started)


def sync_text_controls_lock() -> Tuple[gr.update, gr.update, gr.update, gr.update]:
    """
    Lock/unlock the text-input controls based on conversation state
    (same derived-from-state shape as sync_char_dropdown_lock).

    Called via .then() after toggle_start_end() and from the page-load
    sync. Order: (text_input, send_btn, clear_btn, attach_btn).
    """
    on = app_state.conversation_started
    # 開始前は「まず開始ボタンを」の案内、開始後は通常の入力プロンプト
    text_placeholder = t('conv.text_placeholder') if on else t('conv.text_placeholder_locked')
    return (gr.update(interactive=on, placeholder=text_placeholder),
            gr.update(interactive=on),
            gr.update(interactive=on), gr.update(interactive=on))


def sync_conversation_controls_on_load():
    """
    Re-sync conversation-dependent controls on page (re)load.

    Components render their build-time defaults ("start first" / disabled)
    on every fresh page session, so a reload during an active conversation
    would otherwise leave the voice button and text inputs dead until the
    user toggles end/start. Derives everything from app_state.

    Order: (start_end_btn, voice_btn, text_input, send_btn, clear_btn,
    attach_btn).
    """
    if app_state.conversation_started:
        start_end = gr.update(value=t('btn.end_conversation'))
        voice = gr.update(value=t('voicebtn.press_to_record'),
                          elem_classes="voice-btn-ready", interactive=True)
    else:
        start_end = gr.update(value=t('btn.start_conversation'))
        voice = gr.update(value=t('voicebtn.start_first'), variant="secondary",
                          elem_classes=[], interactive=False)
    return (start_end, voice) + sync_text_controls_lock()


def _character_info_updates():
    """
    キャラクター情報カード(アイコン/名前/説明/モデル情報)と、音声入力設定内の
    音声入力言語ラベルの更新5点を組み立てる。

    呼び出し側で active_character_id が非 None であることを保証すること。
    ガードの条件は入口ごとに違う(会話開始時=会話中のみ / ページ再読み込み時=
    選択済みなら常に)ため、条件を持たない本体だけをここに置く。

    Order: (char_icon, char_name, char_description, model_info, mic_status_info).
    """
    try:
        # Clear caches to ensure fresh data
        if hasattr(app_state, '_icon_cache'):
            app_state._icon_cache.clear()
        if hasattr(app_state, '_character_config_cache'):
            app_state._character_config_cache.clear()

        # Load fresh character config
        config_response = backend.load_character_config(app_state.active_character_id)
        if isinstance(config_response, dict) and 'result' in config_response:
            char_config = config_response.get('result', {})
        else:
            char_config = config_response

        # Update icon in app_state (may have changed during edit)
        icon_update = gr.update()
        new_icon_path = char_config.get("icon_path", None)
        if new_icon_path:
            icon_path = Path(new_icon_path)
            if icon_path.exists():
                app_state.active_character_icon = str(icon_path)
                icon_update = gr.update(value=str(icon_path))

        # Extract character info (same logic as switch_character in character_ui.py)
        char_name = char_config.get('name', app_state.active_character_id)
        char_summary = char_config.get('summary_text', t('conv.no_description'))

        # Call-time import: character_ui imports ui.conversation at module
        # load, so a top-level import here would be a cycle.
        from ui.character_ui import llm_display_name, tts_display_name
        tts_config = char_config.get("tts_model_config", {})
        tts_model_name = tts_display_name(tts_config)

        llm_model = llm_display_name(char_config)

        # 音声入力設定アコーディオン内の音声入力言語ラベル。switch_character の
        # 成功時(character_ui.py:355)と同じ文字列を作る。この1行はビルド既定が
        # 「システム既定のマイクを使用中」でキャラ選択時のみ言語表示に変わるため、
        # 復元しないとリロードで意味の違う文言に戻る(稜裁定 2026-08-08)。
        stt_lang = char_config.get("faster_whisper_config", {}).get("language", "en")

        return (
            icon_update,
            gr.update(value=f"**{char_name}**"),
            gr.update(value=char_summary),
            gr.update(value=t('charui.model_info', tts=tts_model_name, llm=llm_model)),
            t('charui.stt_language', lang=stt_lang),
        )
    except Exception as e:
        logger.warning(f"Failed to refresh character info on start: {e}")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()


def restore_character_info_on_load():
    """
    ページ再読み込み時にキャラクター情報カードを app_state から復元する。

    char_icon / char_name / char_description / model_info / mic_status_info は
    ビルド時の「未選択」既定で描画されるため、リロードすると選択中のキャラクターが
    画面から消えて見える(会話中はドロップダウンがロックされ選び直せない)。
    会話の開始有無に関わらず、キャラクターが選択済みなら復元する
    (選択しただけでリロードした場合も「未選択」に戻らない)。

    Order: (char_icon, char_name, char_description, model_info, mic_status_info).
    """
    if not app_state.active_character_id:
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    return _character_info_updates()


def refresh_character_info_on_start():
    """
    Refresh character info display when conversation starts.
    Called via .then() after toggle_start_end() to update character info
    that may have been edited while conversation was stopped.
    """
    if not app_state.conversation_started or not app_state.active_character_id:
        return gr.update(), gr.update(), gr.update(), gr.update()
    # 会話開始時の配線は4出力(ui/app.py)。音声入力言語ラベルは含めない。
    return _character_info_updates()[:4]
