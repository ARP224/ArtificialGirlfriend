"""
ui/character_ui.py

Character management functionality for the Artificial Girlfriend UI.
This module contains functions for character CRUD operations and UI management.
"""

import os
import logging
import uuid
import traceback
import gradio as gr
from pathlib import Path
from typing import List, Tuple, Optional, Dict

from .state import app_state, show_error_popup, show_warning_popup
from .error_handler import handle_module_operation
from .status_checker import get_status_html
from .conversation import load_conversation_history
from .constants import (
    MAX_CHARACTER_NAME_LENGTH, VALID_IMAGE_EXTENSIONS,
    ICON_OUTPUT_SIZE, MAX_ICON_PIXELS,
    ICONS_DIR, DEFAULT_ICON_PATH, TTS_MODELS_DIR, TTS_MODEL_FILE,
    TTS_CONFIG_FILE, TTS_STYLE_VECTORS_FILE
)
from backend.shared.i18n import t
from backend.llm.ollama_capabilities import UNSUPPORTED_MODEL_CODE
from backend.conversation.character_manager import (
    DELETE_ELYTH_ACTIVE_CODE, DELETE_MEMORY_TASK_CODE,
)
import backend

logger = logging.getLogger(__name__)


def _verify_elyth_api_key(api_key: str) -> Tuple[bool, str]:
    """ELYTH APIキーの疎通確認 (GET /me/profile — spec v6 §3)。

    保存後の付加情報としてのみ使う=失敗しても保存はブロックしない
    （保存は常にローカル完結・ネットワークを保存の人質に取らない）。
    Returns (ok, detail): ok時はhandle、失敗時は短い理由。
    """
    from backend.elyth.elyth_api import ElythAPIClient
    try:
        profile = (ElythAPIClient(api_key, timeout=5.0)
                   .get_me_profile().get("profile")) or {}
        return True, str(profile.get("handle") or "?")
    except Exception as e:
        return False, str(e)


def _elyth_key_toast(api_key: str, status_text: str) -> str:
    """キー入力があれば疎通確認し、結果をトースト文へ反映して返す。"""
    if not api_key:
        return status_text
    ok, detail = _verify_elyth_api_key(api_key)
    if ok:
        return status_text + " " + t("charui.elyth_key_ok", handle=detail)
    gr.Warning(t("charui.elyth_key_invalid", reason=detail))
    return status_text


def validate_character_data(
    name: str, tts_model: str, model_selection: str,
    stt_language: Optional[str] = None,
) -> Optional[str]:
    """
    Validate character data fields.

    Args:
        name: Character name
        tts_model: TTS model identifier
        model_selection: Model selection in "provider::model_name" format
        stt_language: Character language ("ja"/"en"); when given, the local
            TTS engine must match it (ja=SBV2 / en=Kokoro, 2026-07-26 ruling).
            The dropdown filtering already prevents the mismatch; this is the
            deterministic backstop at save time.

    Returns:
        Optional[str]: Error message if validation fails, None if valid
    """
    if not name or not name.strip():
        return t('charui.name_empty')

    name = name.strip()
    if len(name) > MAX_CHARACTER_NAME_LENGTH:
        return t('charui.name_too_long', max=MAX_CHARACTER_NAME_LENGTH)

    if not tts_model or not model_selection:
        return t('charui.required_fields')

    # Validate model selection format
    from backend.shared.api_settings import decode_model_value, PROVIDER_CONFIG
    provider, model_name = decode_model_value(model_selection)
    if provider not in PROVIDER_CONFIG:
        return t('charui.unknown_provider', provider=provider)
    if not model_name:
        return t('charui.model_name_empty')

    if stt_language:
        from backend.shared.api_settings import decode_tts_value
        tts_provider, _ = decode_tts_value(tts_model)
        if tts_provider == "sbv2" and stt_language != "ja":
            return t('charui.tts_language_mismatch_sbv2')
        if tts_provider == "kokoro" and stt_language != "en":
            return t('charui.tts_language_mismatch_kokoro')

    return None  # No errors


def tts_display_name(tts_config: Optional[Dict]) -> str:
    """Human-readable TTS name for info displays.

    ElevenLabs -> "voice_name (ElevenLabs)"; Kokoro -> "voice (KokoroTTS)";
    SBV2 -> the model folder name (same path derivation as before). Used by
    switch_character here and by refresh_character_info_on_start
    (ui/conversation/lifecycle.py).
    """
    tts_config = tts_config or {}
    if tts_config.get("provider") == "elevenlabs":
        label = tts_config.get("voice_name") or tts_config.get("voice_id") or t('charui.unknown')
        return f"{label} (ElevenLabs)"
    if tts_config.get("provider") == "kokoro":
        label = tts_config.get("voice_name") or t('charui.unknown')
        return f"{label} (KokoroTTS)"
    tts_model_path = tts_config.get("model_path", "")
    if tts_model_path:
        path_parts = Path(tts_model_path).parts
        if len(path_parts) >= 2:
            return path_parts[-2]  # Folder name is the model name
    return t('charui.unknown')


def llm_display_name(char_config: Optional[Dict]) -> str:
    """Human-readable LLM label for info displays: "model (Provider)", or the
    translated "unknown" when no model is set. A bundled model character
    (LLM left unset on purpose) used to show " (Ollama)" here because the
    provider default is "ollama" even with an empty model name (稜 Mac 実機
    2026-09-25). Shared with refresh_character_info_on_start
    (ui/conversation/lifecycle.py), like tts_display_name.
    """
    char_config = char_config or {}
    model_name = char_config.get("model_name") or char_config.get("ollama_model_name") or ""
    if not model_name:
        return t('charui.unknown')
    from backend.shared.api_settings import get_provider_display_name
    provider_display = get_provider_display_name(char_config.get("model_provider", "ollama"))
    return f"{model_name} ({provider_display})"


def _build_tts_model_config(tts_model: str) -> Dict:
    """Build the character's tts_model_config from the dropdown's encoded value.

    "sbv2::<folder>" (or a legacy bare folder name) -> the 3-path SBV2 dict;
    "kokoro::<voice>" -> {provider, voice_name};
    "elevenlabs::<voice_id>" -> {provider, voice_id, voice_name}.
    """
    from backend.shared.api_settings import decode_tts_value, get_elevenlabs_voices
    provider, identifier = decode_tts_value(tts_model)
    if provider == "kokoro":
        return {
            "provider": "kokoro",
            "voice_name": identifier,
        }
    if provider == "elevenlabs":
        voice_name = next(
            (v.get("name", "") for v in get_elevenlabs_voices()
             if v.get("voice_id") == identifier),
            "",
        ) or identifier
        return {
            "provider": "elevenlabs",
            "voice_id": identifier,
            "voice_name": voice_name,
        }
    return {
        "provider": "sbv2",
        "model_path": str(TTS_MODELS_DIR / identifier / TTS_MODEL_FILE),
        "config_path": str(TTS_MODELS_DIR / identifier / TTS_CONFIG_FILE),
        "style_vectors_path": str(TTS_MODELS_DIR / identifier / TTS_STYLE_VECTORS_FILE),
    }


def _require_success(response, operation: str):
    """@standardize_response の失敗dictを例外化して既存の except 経路に載せる。

    backend のキャラ操作は例外を投げず {success: False} を返すが、UI側が
    success を検査しておらず失敗が黙って成功扱いになっていた(稜裁定
    2026-08-02)。raise することで各ハンドラ既存のロールバック/ポップアップ/
    ステータス表示がそのまま働く。
    """
    if isinstance(response, dict) and not response.get('success', True):
        raise RuntimeError(f"{operation} failed: {response.get('error', 'Unknown error')}")
    return response


def _error_code(response) -> Optional[str]:
    """standardize_response の失敗 dict から error_code(ag_code)を取り出す。

    コード付きの拒否(例: 非対応モデル=思考を無効化できない Ollama モデル、
    2026-08-16 稜裁定)は _require_success に流す前にここで拾い、翻訳済み
    トースト t("err.<code>") を出す(英語原文の汎用トーストにしない)。
    """
    if isinstance(response, dict) and not response.get('success', True):
        return response.get('error_code')
    return None


def switch_character(new_char_id: str) -> Tuple[gr.update, gr.update, gr.update, gr.update, str, str]:
    """
    Switch the active character when user selects from dropdown.
    This calls the backend to activate the new character,
    which changes STT language, TTS model, memory DB, etc.

    Args:
        new_char_id: ID of the character to activate

    Returns:
        Tuple containing updates for:
        - char_icon: Character icon image
        - char_name: Character name display
        - char_description: Character description
        - model_info: TTS/LLM model information
        - mic_status: Updated microphone language label
        - status_display: Combined HTML status display (all services)
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    if not new_char_id:
        # ドロップダウンが空＝「キャラが無効になった」ではない。ページ再読み込みでは
        # load_char_dropdown が value=None を書き込み、その .change がここへ来るため、
        # 有効なキャラが残っているのに表示だけ「未選択」へ落ちて、同じ読み込みで
        # 走る restore_character_info_on_load の復元を上書きしていた(稜実機 2026-08-08)。
        # キャラ削除時は active_character_id が None になる(:1513)ので、
        # そのときだけ従来どおり未選択表示へ落ちる。
        if app_state.active_character_id:
            return (
                gr.update(),  # Keep current icon
                gr.update(),  # Keep current name
                gr.update(),  # Keep current description
                gr.update(),  # Keep current model info
                gr.update(),  # Keep current mic status
                gr.update()   # Keep current status_display
            )
        # Return empty/default values when no character selected
        # Get current status as HTML
        status_html = get_status_html()
        return (
            gr.update(value=None),  # char_icon
            gr.update(value=t('conv.char_not_selected')),  # char_name
            gr.update(value=t('conv.char_select_hint')),  # char_description
            gr.update(value=t('charui.model_info_na')),  # model_info
            t('charui.mic_inactive'),  # mic_status
            status_html  # status_display
        )
    
    # Prevent character switching during active conversation (unreachable in
    # normal UI: the dropdown is locked while a conversation is active —
    # kept as defense in depth)
    if app_state.conversation_started:
        app_state.add_log_message("warning", "Cannot switch character during active conversation")
        # Return current state without changes
        # (配線は6出力。旧3分割ステータス whisper/ollama/tts の3要素が残骸として
        #  残っており、会話中の切替が警告でなく Gradio の出力数不一致エラーになっていた)
        return (
            gr.update(),  # Keep current icon
            gr.update(),  # Keep current name
            gr.update(),  # Keep current description
            gr.update(),  # Keep current model info
            gr.update(),  # Keep current mic status
            gr.update()   # Keep current status_display
        )
        
    # Store previous state for rollback
    previous_char_id = app_state.active_character_id
    previous_char_icon = app_state.active_character_icon
    previous_language = t('charui.unknown')
    
    # Try to get current language for potential rollback display
    if previous_char_id:
        try:
            prev_response = backend.load_character_config(previous_char_id)
            # Handle structured response
            if isinstance(prev_response, dict) and 'result' in prev_response:
                prev_config = prev_response.get('result', {})
            else:
                prev_config = prev_response
            previous_language = prev_config.get("faster_whisper_config", {}).get("language", "en")
        except:
            pass
    
    try:
        # First, attempt to load the new character config to validate it exists
        config_response = backend.load_character_config(new_char_id)
        
        # Handle structured response format
        if isinstance(config_response, dict) and 'result' in config_response:
            char_config = config_response.get('result', {})
            if not config_response.get('success', False):
                raise ValueError(f"Failed to load character config: {config_response.get('error', 'Unknown error')}")
        else:
            char_config = config_response
        
        # Validate config structure
        if not isinstance(char_config, dict):
            raise ValueError("Invalid character configuration format")
            
        whisper_config = char_config.get("faster_whisper_config", {})
        stt_lang = whisper_config.get("language", "en")
        
        # Only activate after validation passes
        activate_response = backend.activate_character(new_char_id)
        if _error_code(activate_response) == UNSUPPORTED_MODEL_CODE:
            # 非対応モデルは activate_character が状態変更前に拒否する=前キャラは
            # 無傷なので、except 経路のロールバック再 activation(TTS 再ロード)は
            # 走らせず前の表示を返すだけにする。トーストで理由を伝える
            app_state.add_log_message(
                "warning", f"Character switch refused (unsupported model): {new_char_id}")
            gr.Warning(t("err.model_thinking_unsupported"))
            return (
                gr.update(),  # Keep current icon
                gr.update(),  # Keep current name
                gr.update(),  # Keep current description
                gr.update(),  # Keep current model info
                t('charui.mic_language', lang=previous_language),  # mic_status
                gr.update()   # Keep current status_display
            )
        _require_success(activate_response, "Character activation")

        # LLM未接続(遅延再生成待ち)はキャラ読み込み時点で知らせる
        # (稜依頼 2026-08-03: 初回送信まで無言だとユーザーが気づけない)。
        # error(赤)で出す: モバイルの❌表示と色を揃える(稜指摘 2026-08-03)
        try:
            llm_check = backend.is_llm_ready(new_char_id)
            if isinstance(llm_check, dict) and llm_check.get('result') is False:
                show_error_popup(t('charui.llm_not_ready_title'),
                                 t('err.service_unavailable'))
        except Exception as llm_check_err:
            logger.debug(f"LLM readiness check failed (ignored): {llm_check_err}")

        # Update app state only after successful activation
        app_state.active_character_id = new_char_id
        app_state.active_character_name = char_config.get("name", None)

        # Store the character's icon path for chat display
        app_state.active_character_icon = char_config.get("icon_path", None)
        if app_state.active_character_icon:
            icon_path = Path(app_state.active_character_icon)
            if not icon_path.exists():
                app_state.active_character_icon = None
        
        if not app_state.active_character_icon:
            # Check if default icon exists
            if DEFAULT_ICON_PATH.exists():
                app_state.active_character_icon = str(DEFAULT_ICON_PATH)
            else:
                app_state.active_character_icon = None
                # Only log at debug level to avoid cluttering test output
                logger.debug(f"Default icon not found at {DEFAULT_ICON_PATH}")
            
        app_state.add_log_message("info", f"Switched to character: {char_config.get('name', new_char_id)}")

        # 稜依頼 2026-07-25: STTモデルはキャラ選択時点で温め始める(Start押下時に
        # 右上インジケータが黄色=読み込み中になるのを避ける)。冪等・背景スレッド
        # なので切替の体感時間には影響しない。モバイルも本関数経由のため共通。
        from ui.handlers.stt_engine import warm_local_stt_model
        warm_local_stt_model()

        # Notify the appeared MotionPNGPlayer of the character change
        # (server mode: remote client / local mode: the player AG launched)
        try:
            from backend.server.websocket_server import get_websocket_manager
            ws_mgr = get_websocket_manager()
            motion_folder = char_config.get("motion_pngtuber_folder", "")
            folder_name = os.path.basename(motion_folder) if motion_folder else ""
            ws_mgr.notify_character_changed(
                character_name=char_config.get("name", ""),
                folder_name=folder_name,
                motion_folder=motion_folder
            )
            # Broadcast to all clients (companion mode sync)
            ws_mgr.broadcast_active_character_sync(
                character_id=new_char_id,
                character_name=char_config.get("name", ""),
            )
            # Phase 4B: also broadcast the new character's talk_theme so other
            # clients update their theme display without a polling timer.
            ws_mgr.broadcast_talk_theme_updated_sync(
                theme=char_config.get("talk_theme", "") or "",
                character_id=new_char_id,
            )
        except Exception:
            pass  # Notification failure must not block character switch

        # Clear icon cache to ensure new character's icon is processed fresh
        if hasattr(app_state, '_icon_cache'):
            app_state._icon_cache.clear()
        
        # Clear character config cache if it exists
        if hasattr(app_state, '_character_config_cache'):
            app_state._character_config_cache.clear()
        
        # Load conversation history for the new character (async)
        # This will replace any existing chat history with the loaded messages
        load_conversation_history(new_char_id)
        
        # Clear last prompt when switching characters
        if hasattr(app_state, 'last_prompt_text'):
            app_state.last_prompt_text = t('charui.no_prompts')
            app_state.last_prompt_timestamp = None
            app_state.last_prompt_character = None
        
        # Reset audio device selection to default when switching characters
        app_state.selected_microphone_device = None
        
        # Extract model information from config
        tts_config = char_config.get("tts_model_config", {})
        tts_model_name = tts_display_name(tts_config)

        llm_model = llm_display_name(char_config)
        char_name = char_config.get('name', new_char_id)
        char_summary = char_config.get('summary_text', t('charui.no_description'))
        
        # Prepare icon update
        icon_update = gr.update(value=None)
        if app_state.active_character_icon:
            icon_path = Path(app_state.active_character_icon)
            if icon_path.exists():
                icon_update = gr.update(value=str(icon_path))
        
        # Get updated connection status as HTML
        status_html = get_status_html()
        
        # Return all updates
        return (
            icon_update,  # char_icon
            gr.update(value=f"**{char_name}**"),  # char_name
            gr.update(value=char_summary),  # char_description
            gr.update(value=t('charui.model_info', tts=tts_model_name, llm=llm_model)),  # model_info
            t('charui.stt_language', lang=stt_lang),  # mic_status_info (accordion) language label
            status_html  # status_display
        )
        
    except KeyError as e:
        # Rollback to previous character
        app_state.active_character_id = previous_char_id
        app_state.active_character_icon = previous_char_icon
        
        # Try to reactivate previous character
        if previous_char_id:
            try:
                backend.activate_character(previous_char_id)
                app_state.add_log_message("warning", f"Rolled back to previous character due to config error: {e}")
            except:
                app_state.add_log_message("error", "Failed to rollback to previous character")
        
        app_state.add_log_message("error", f"Missing required field in character config: {e}")
        show_error_popup(t('charui.config_error_title'),
                        t('charui.config_error_msg', error=e))
        # Return previous state (配線は6出力=成功パスと同形)
        return (
            gr.update(),  # Keep current icon
            gr.update(),  # Keep current name
            gr.update(),  # Keep current description
            gr.update(),  # Keep current model info
            t('charui.mic_language', lang=previous_language),  # mic_status
            gr.update()   # Keep current status_display
        )
        
    except Exception as e:
        # Rollback to previous character
        app_state.active_character_id = previous_char_id
        app_state.active_character_icon = previous_char_icon
        
        # Try to reactivate previous character
        if previous_char_id:
            try:
                backend.activate_character(previous_char_id)
                app_state.add_log_message("warning", f"Rolled back to previous character due to error: {e}")
            except:
                app_state.add_log_message("error", "Failed to rollback to previous character")
        
        app_state.add_log_message("error", f"Error switching character: {e}")
        show_error_popup(t('charui.switch_error_title'),
                        t('charui.switch_error_msg', error=str(e)))
        # Return previous state (配線は6出力=成功パスと同形)
        return (
            gr.update(),  # Keep current icon
            gr.update(),  # Keep current name
            gr.update(),  # Keep current description
            gr.update(),  # Keep current model info
            t('charui.mic_language', lang=previous_language),  # mic_status
            gr.update()   # Keep current status_display
        )


def manual_load_characters() -> List[Dict[str, str]]:
    """
    Manually load character files from directory as a fallback.
    This is a workaround for when the backend fails to load characters.
    
    Returns:
        List of character dictionaries
    """
    import json
    from backend.shared.constants import CHARACTER_CONFIGS_DIR, resolve_data_path

    characters = []
    config_dir = CHARACTER_CONFIGS_DIR
    
    if not config_dir.exists():
        return characters
    
    for json_file in config_dir.glob("*.json"):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                char_data = json.load(f)
            
            # Ensure required fields exist - check both 'id' and 'character_id'
            char_id = char_data.get('id') or char_data.get('character_id')
            char_name = char_data.get('name')
            
            if char_id and char_name:
                characters.append({
                    'id': char_id,
                    'name': char_name,
                    'icon_path': resolve_data_path(char_data.get('icon_path', ''))
                })
                
        except Exception as e:
            app_state.add_log_message("error", f"Failed to manually load {json_file.name}: {e}")
    
    return characters


def refresh_char_list() -> List[Tuple[str, str]]:
    """
    Refresh the character dropdown data from the backend.
    
    Returns:
        List[Tuple[str, str]]: List of (label, value) for Gradio dropdown
    """
    try:
        chars_response = backend.load_character_list()
        
        # Handle None response
        if chars_response is None:
            logger.warning("Backend returned None for character list")
            return []
        
        # Handle structured response format
        if isinstance(chars_response, dict) and 'result' in chars_response:
            chars = chars_response.get('result', [])
            if not chars_response.get('success', False):
                logger.warning("Character list query returned unsuccessful status")
        elif isinstance(chars_response, list):
            # Direct list format (backward compatibility)
            chars = chars_response
        else:
            # Unexpected format
            logger.warning(f"Unexpected character list response format: {type(chars_response)}")
            return []
        
        # Each item in chars is like {"id": "...", "name": "...", "icon_path": "..."}
        options = []
        for c in chars:
            if isinstance(c, dict) and "name" in c and "id" in c:
                options.append((c["name"], c["id"]))
        
        # Fallback: Try manual loading if backend returns empty
        if not options:
            manual_chars = manual_load_characters()
            
            if manual_chars:
                options = [(c["name"], c["id"]) for c in manual_chars]
            
        return options
    except Exception as e:
        app_state.add_log_message("error", f"Failed to load character list: {e}")
        return [(t('charui.list_load_error'), "")]


def load_char_dropdown() -> gr.update:
    """
    Load character dropdown for UI initialization.
    
    Returns:
        gr.update: Gradio update for dropdown component
    """
    opts = refresh_char_list()
    # Filter out empty entries but keep full tuples for proper display
    valid_opts = [o for o in opts if o[1]]  # Filter out empty IDs

    # Keep the dropdown locked if a conversation is already active
    # (e.g. page reload mid-conversation; unlocked by ending the conversation)
    return gr.update(choices=valid_opts, value=None,
                     interactive=not app_state.conversation_started)


def _empty_management_ui(error_msg=""):
    """Return empty character management UI state"""
    return (
        gr.update(choices=[]),                      # tts_models
        gr.update(choices=[]),                      # tts_models (edit)
        gr.update(choices=[]),                      # ollama_models
        gr.update(choices=[]),                      # ollama_models (edit)
        gr.update(choices=[], value=None),          # char list
        gr.update(value=error_msg, visible=bool(error_msg))  # status
    )


@handle_module_operation("Character Management Loading", show_popup=False)
def open_character_management(create_lang="ja", create_tts=None, create_model=None,
                              edit_lang="en", edit_tts=None, edit_model=None,
                              current_edit_char=None) -> Tuple[gr.update, gr.update, gr.update, gr.update, gr.update, gr.update]:
    """
    Return data needed for character management UI with comprehensive error handling.

    引数はページ再訪時のフォーム現在値(稜報告 2026-08-15 の2バグ対策):
    - 各フォームの言語でボイス選択肢を絞る(旧実装は create=ja / edit=en の
      ビルド時既定で撒き直し、編集フォームにsbv2ボイスが残っているとGradioの
      「Value ... is not in the list of choices」検証エラーになっていた)
    - 保持中の値が新choicesに有効なら残す・無効ならNoneへ明示的に落とす
    - 編集キャラ選択(current_edit_char)は存在すれば保持(旧実装は毎回None=
      ページを開き直すたびに選択が消えていた)

    Returns:
        Tuple containing gradio updates for various dropdown components and status message
    """
    status_text = gr.update(value="", visible=False)  # Add status text component to return
    
    # Check if backend is available
    if not app_state.backend_available:
        error_msg = t('charui.backend_unavailable')
        return _empty_management_ui(error_msg)
    

    try:
        # First check backend connectivity
        try:
            # Test backend connectivity with a simple call
            char_list_test = backend.load_character_list()
            if char_list_test is None:
                raise RuntimeError("Backend returned None for character list")
        except Exception as conn_error:
            error_details = traceback.format_exc()
            logger.debug(f"Backend connectivity error details: {error_details}")
            # ERROR は1本(1障害1トースト・稜裁定 2026-08-02)
            app_state.add_log_message("error", f"Backend connection error: {conn_error}")

            return _empty_management_ui(t('charui.backend_connect_error'))

        # Now try to load resources with specific error handling for each component
        tts_models = []
        ollama_models = []
        icons = []
        char_list = []

        # Load TTS models with error handling
        try:
            tts_response = backend.list_tts_models()
            
            # Handle structured response format
            if isinstance(tts_response, dict) and 'result' in tts_response:
                tts_models = tts_response.get('result', [])
                if not tts_response.get('success', False):
                    app_state.add_log_message("warning", "TTS models query returned unsuccessful status")
            elif isinstance(tts_response, list):
                # Direct list format (backward compatibility)
                tts_models = tts_response
            else:
                # Unexpected format
                app_state.add_log_message("warning", f"Unexpected TTS models response format: {type(tts_response)}")
                tts_models = []
            
            if not tts_models:
                app_state.add_log_message("warning", "No TTS models found. Character voices will not work.")
        except Exception as tts_error:
            error_details = traceback.format_exc()
            app_state.add_log_message("error", f"Failed to load TTS models: {tts_error}")
            logger.debug(f"TTS models load error details: {error_details}")
            # Continue with empty list

        # Load unified model list (Ollama + API providers)
        ollama_models = []
        try:
            ollama_response = backend.list_ollama_models()

            # Handle structured response format
            if isinstance(ollama_response, dict) and 'result' in ollama_response:
                ollama_models = ollama_response.get('result', [])
                if not ollama_response.get('success', False):
                    app_state.add_log_message("warning", "Ollama models query returned unsuccessful status")
            elif isinstance(ollama_response, list):
                # Direct list format (backward compatibility)
                ollama_models = ollama_response
            else:
                # Unexpected format
                app_state.add_log_message("warning", f"Unexpected Ollama models response format: {type(ollama_response)}")
                ollama_models = []
        except Exception as ollama_error:
            error_details = traceback.format_exc()
            app_state.add_log_message("error", f"Failed to load Ollama models: {ollama_error}")
            logger.debug(f"Ollama models load error details: {error_details}")

        # Build unified model list with API models
        try:
            from backend.shared.api_settings import get_all_available_models
            unified_models = get_all_available_models(ollama_models)
        except Exception as api_error:
            app_state.add_log_message("error", f"Failed to load API models: {api_error}")
            # Fallback: Ollama models only
            unified_models = [(f"{m} (Ollama)", f"ollama::{m}") for m in ollama_models]

        # Build unified TTS lists (SBV2 folders + Kokoro voices + ElevenLabs).
        # Each form starts filtered to its language dropdown's build-time
        # default (create=ja / edit=en); the language dropdown's .input
        # re-filters on user change, and loading a character into the edit
        # form supplies choices filtered to that character's language.
        try:
            from backend.shared.api_settings import get_all_tts_choices
            tts_choices_ja = get_all_tts_choices(tts_models, language="ja")
            tts_choices_en = get_all_tts_choices(tts_models, language="en")
            # Unfiltered union: only the "no TTS models" warning keys off it
            tts_choices = get_all_tts_choices(tts_models)
        except Exception as tts_choices_error:
            app_state.add_log_message("error", f"Failed to build TTS choices: {tts_choices_error}")
            # Fallback: SBV2 folders only
            tts_choices = [(f"{m} (Style-bert-vits2)", f"sbv2::{m}") for m in tts_models]
            tts_choices_ja = tts_choices
            tts_choices_en = tts_choices

        # Load character icons with error handling
        try:
            icons_response = backend.list_character_icons()
            
            # Handle structured response format
            if isinstance(icons_response, dict) and 'result' in icons_response:
                icons = icons_response.get('result', [])
                if not icons_response.get('success', False):
                    app_state.add_log_message("warning", "Character icons query returned unsuccessful status")
            elif isinstance(icons_response, list):
                # Direct list format (backward compatibility)
                icons = icons_response
            else:
                # Unexpected format
                app_state.add_log_message("warning", f"Unexpected character icons response format: {type(icons_response)}")
                icons = []
        except Exception as icon_error:
            error_details = traceback.format_exc()
            app_state.add_log_message("error", f"Failed to load character icons: {icon_error}")
            logger.debug(f"Character icons load error details: {error_details}")
            # Continue with empty list

        # Load character list with error handling
        try:
            char_list = refresh_char_list()
        except Exception as char_error:
            error_details = traceback.format_exc()
            app_state.add_log_message("error", f"Failed to load character list: {char_error}")
            logger.debug(f"Character list load error details: {error_details}")
            char_list = [(t('charui.list_load_error'), "")]

        # Determine overall status
        status_message = ""
        status_visible = False

        if not tts_choices:
            status_message += t('charui.no_tts_models') + " "
            status_visible = True

        if not unified_models:
            status_message += t('charui.no_models') + " "
            status_visible = True

        # No need to warn about empty icons - this is normal for fresh installations
        # The default icon will be used automatically when needed

        if len(char_list) == 1 and char_list[0][1] == "":
            status_message += t('charui.char_list_failed') + " "
            status_visible = True


        # Return all updates including status
        def _keep_if_valid(value, choices):
            return value if value in {v for _label, v in choices} else None

        create_choices = tts_choices_en if create_lang == "en" else tts_choices_ja
        edit_choices = tts_choices_ja if edit_lang == "ja" else tts_choices_en
        char_ids = {char_id for _name, char_id in char_list}
        edit_dd_value = current_edit_char if current_edit_char in char_ids else None
        return (
            gr.update(choices=create_choices,
                      value=_keep_if_valid(create_tts, create_choices)),
            gr.update(choices=edit_choices,
                      value=_keep_if_valid(edit_tts, edit_choices)),
            gr.update(choices=unified_models,
                      value=_keep_if_valid(create_model, unified_models)),
            gr.update(choices=unified_models,
                      value=_keep_if_valid(edit_model, unified_models)),
            gr.update(choices=char_list, value=edit_dd_value),  # Pass full tuples for proper name display
            gr.update(value=status_message, visible=status_visible)
        )
    except Exception as e:
        error_details = traceback.format_exc()
        app_state.add_log_message("error", f"Error loading character management data: {e}")
        # トレース全文は debug(WSトーストに断片が出るのを防ぐ)
        logger.debug(f"Character management load error details: {error_details}")

        return _empty_management_ui(t('charui.mgmt_error_status', error=str(e)))




def cleanup_orphaned_icons() -> None:
    """
    Clean up orphaned icon files that are not referenced by any character.
    Should be called periodically or during maintenance.
    """
    try:
        # Get all character configs to find referenced icons
        referenced_icons = set()
        character_count = 0
        
        try:
            chars_response = backend.load_character_list()
            
            # Handle structured response format
            if isinstance(chars_response, dict) and 'result' in chars_response:
                characters = chars_response.get('result', [])
            elif isinstance(chars_response, list):
                characters = chars_response
            elif chars_response is None:
                characters = []
            else:
                logger.warning(f"Unexpected character list format in cleanup: {type(chars_response)}")
                characters = []
            
            # CRITICAL SAFETY CHECK: If we have no characters, don't delete anything
            if not characters:
                logger.warning("No characters found - skipping icon cleanup to prevent data loss")
                app_state.add_log_message("warning", "Icon cleanup skipped - no characters found")
                return
            
            for char in characters:
                if isinstance(char, dict) and 'id' in char:
                    character_count += 1
                    try:
                        config_response = backend.load_character_config(char['id'])
                        # Handle structured response
                        if isinstance(config_response, dict) and 'result' in config_response:
                            config = config_response.get('result', {})
                        else:
                            config = config_response
                        icon_path = config.get('icon_path')
                        if icon_path:
                            referenced_icons.add(Path(icon_path).name)
                    except Exception as e:
                        logger.debug(f"Could not load config for character {char['id']}: {e}")
                        # Don't delete icons if we can't load configs
                        
            # SAFETY CHECK: Ensure we actually loaded some characters
            if character_count == 0:
                logger.warning("No character configs could be loaded - skipping cleanup")
                app_state.add_log_message("warning", "Icon cleanup skipped - no character configs loaded")
                return
                
        except Exception as e:
            # 同一失敗を warning と error で二重記録しない: クリーンアップ
            # 中断は続行可能な事象なので warning に統一(稜裁定 2026-08-02)
            app_state.add_log_message("warning", f"Icon cleanup aborted - backend error: {e}")
            return
        
        # Check all files in icons directory
        icon_dir = Path(ICONS_DIR)
        if not icon_dir.exists():
            return
        
        # Count total icons before cleanup
        total_icons = sum(1 for f in icon_dir.iterdir() if f.is_file() and f.name != 'default.png')
        
        # SAFETY CHECK: Don't delete if we would delete ALL icons
        if total_icons > 0 and len(referenced_icons) == 0:
            # ERROR は1本に集約(詳細はこの1行に併記・稜裁定 2026-08-02)
            app_state.add_log_message("error", "Icon cleanup aborted - SAFETY: would delete ALL icons")
            return
            
        # Log what we're about to do
        orphaned_files = []
        for icon_file in icon_dir.iterdir():
            if icon_file.is_file() and icon_file.name != 'default.png':
                if icon_file.name not in referenced_icons:
                    orphaned_files.append(icon_file)
        
        if orphaned_files:
            logger.info(f"Found {len(orphaned_files)} orphaned icons out of {total_icons} total")
            app_state.add_log_message("info", f"Cleaning {len(orphaned_files)} orphaned icons (keeping {len(referenced_icons)})")
        
        # Now do the actual cleanup
        cleaned_count = 0
        for icon_file in orphaned_files:
            try:
                icon_file.unlink()
                cleaned_count += 1
                logger.info(f"Removed orphaned icon: {icon_file.name}")
            except Exception as e:
                logger.warning(f"Could not remove orphaned icon {icon_file.name}: {e}")
        
        if cleaned_count > 0:
            app_state.add_log_message("info", f"Cleaned up {cleaned_count} orphaned icon(s)")
            
    except Exception as e:
        logger.error(f"Error during icon cleanup: {e}")


def cleanup_duplicate_icons() -> None:
    """
    Clean up duplicate icon files by comparing file content.
    Keeps only icons that are referenced by characters.
    For safety, this function is conservative and only removes clear duplicates.
    """
    try:
        import hashlib
        
        icon_dir = Path(ICONS_DIR)
        if not icon_dir.exists():
            return
        
        # First, get all referenced icon paths from characters
        referenced_paths = {}  # filename -> full path
        character_count = 0
        
        try:
            chars_response = backend.load_character_list()
            
            # Handle structured response format
            if isinstance(chars_response, dict) and 'result' in chars_response:
                characters = chars_response.get('result', [])
            elif isinstance(chars_response, list):
                characters = chars_response
            elif chars_response is None:
                characters = []
            else:
                characters = []
                
            # CRITICAL SAFETY CHECK: If we have no characters, don't delete anything
            if not characters:
                logger.warning("No characters found - skipping duplicate icon cleanup")
                app_state.add_log_message("warning", "Duplicate cleanup skipped - no characters found")
                return
            
            for char in characters:
                if isinstance(char, dict) and 'id' in char:
                    character_count += 1
                    try:
                        config_response = backend.load_character_config(char['id'])
                        # Handle structured response
                        if isinstance(config_response, dict) and 'result' in config_response:
                            config = config_response.get('result', {})
                        else:
                            config = config_response
                        icon_path = config.get('icon_path')
                        if icon_path:
                            icon_name = Path(icon_path).name
                            referenced_paths[icon_name] = icon_path
                    except Exception:
                        pass
                        
            # SAFETY CHECK: Ensure we actually loaded some characters
            if character_count == 0:
                logger.warning("No character configs could be loaded - skipping duplicate cleanup")
                app_state.add_log_message("warning", "Duplicate cleanup skipped - no configs loaded")
                return
                
        except Exception as e:
            logger.warning(f"Could not load character list for duplicate cleanup: {e}")
            return
        
        # Calculate hashes for all icon files
        icon_hashes = {}  # hash -> list of file paths
        
        for icon_file in icon_dir.iterdir():
            if icon_file.is_file() and icon_file.name != 'default.png':
                try:
                    # Calculate file hash
                    with open(icon_file, 'rb') as f:
                        file_hash = hashlib.md5(f.read()).hexdigest()
                    
                    if file_hash not in icon_hashes:
                        icon_hashes[file_hash] = []
                    icon_hashes[file_hash].append(icon_file)
                except Exception as e:
                    logger.debug(f"Could not hash icon file {icon_file.name}: {e}")
        
        # Count total icons before cleanup
        total_icons = sum(1 for f in icon_dir.iterdir() if f.is_file() and f.name != 'default.png')
        
        # SAFETY CHECK: Don't run if we have no referenced paths
        if len(referenced_paths) == 0 and total_icons > 0:
            # ERROR は1本に集約(稜裁定 2026-08-02)
            app_state.add_log_message("error", "Duplicate cleanup aborted - SAFETY: no referenced icons found")
            return
        
        # Find and remove duplicates
        cleaned_count = 0
        files_to_delete = []
        
        for file_hash, file_list in icon_hashes.items():
            if len(file_list) > 1:
                # We have duplicates
                # Keep only the referenced ones
                referenced_in_group = [f for f in file_list if f.name in referenced_paths]
                
                if referenced_in_group:
                    # Keep the first referenced file, delete others
                    keep_file = referenced_in_group[0]
                    for f in file_list:
                        if f != keep_file:
                            files_to_delete.append(f)
                else:
                    # No referenced files in this group, keep the oldest and delete others
                    # Sort by creation time (or modification time as fallback)
                    file_list.sort(key=lambda f: f.stat().st_ctime)
                    keep_file = file_list[0]
                    
                    for f in file_list[1:]:
                        files_to_delete.append(f)
        
        # Log what we're about to do
        if files_to_delete:
            logger.info(f"Found {len(files_to_delete)} duplicate icons to remove")
            app_state.add_log_message("info", f"Removing {len(files_to_delete)} duplicate icons")
        
        # Now do the actual deletion
        for f in files_to_delete:
            try:
                f.unlink()
                cleaned_count += 1
                logger.info(f"Removed duplicate icon: {f.name}")
            except Exception as e:
                logger.warning(f"Could not remove duplicate icon {f.name}: {e}")
        
        if cleaned_count > 0:
            app_state.add_log_message("info", f"Cleaned up {cleaned_count} duplicate icon(s)")
            
    except ImportError:
        logger.warning("hashlib not available, skipping duplicate icon cleanup")
    except Exception as e:
        logger.error(f"Error during duplicate icon cleanup: {e}")



def resize_icon_preview(pil_image):
    """Resize uploaded image for preview display.
    Called on Image.upload to prevent ERR_CONTENT_LENGTH_MISMATCH
    when Gradio tries to serve large uploaded files to the browser.
    """
    if pil_image is None:
        return None
    from PIL import Image, ImageOps
    img = pil_image.copy()
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA')
    # Crop to square and resize to icon output size
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((ICON_OUTPUT_SIZE, ICON_OUTPUT_SIZE), Image.Resampling.LANCZOS)
    return img


def mark_edit_icon_changed() -> str:
    """Set the hidden icon-changed flag when the user touches the edit Image
    component (wired to .input, which does not fire on programmatic set)."""
    return "1"


def refresh_motion_folder_choices():
    """Rescan MotionPNGPlayer/Asset/ and update the motion folder dropdown
    choices (wired to the refresh button on both character forms)."""
    from backend.tools.motion_pngtuber_launcher import list_asset_folders
    return gr.Dropdown(choices=[""] + list_asset_folders())


def _resolve_edit_icon(image_input, original_path: str, icon_changed: str) -> Tuple[str, str]:
    """
    Determine the final icon path when saving an edited character.
    Called at save time to distinguish between unchanged, cleared, and new icons.

    Trusts the hidden icon-changed flag, not image comparison. Comparing the
    component value to the on-disk icon cannot work here: dimensions always
    match (uploads are force-resized to ICON_OUTPUT_SIZE), pixel comparison via
    getbbox() ignores RGB on Pillow>=10 (alpha_only=True default), and the
    gr.Image webp round-trip is lossy so even an untouched icon differs (H8).

    Args:
        image_input: Current value of the gr.Image component (PIL Image or None)
        original_path: The icon_path from the loaded config (edit_icon_original)
        icon_changed: "" if the icon was not touched since Load Config

    Returns:
        Tuple[str, str]: (final icon path, user-facing warning or "" — see
        process_and_save_icon)
    """
    if image_input is None:
        return "", ""

    if not icon_changed and original_path and Path(original_path).exists():
        # Icon untouched since load — keep the on-disk original
        return original_path, ""

    # New/changed icon (or original file missing) — process and save
    return process_and_save_icon(image_input)


def process_and_save_icon(image_input) -> Tuple[str, str]:
    """
    Process a character icon image: resize, crop to square, and save as PNG.
    Called at save time. Accepts a PIL Image (from gr.Image type='pil') or a file path string.

    Args:
        image_input: PIL Image object or path to the image file

    Returns:
        Tuple[str, str]: (icon path, warning). On success the path points into
        character_icons/ and warning is "". On failure the path is "" and
        warning carries the user-facing reason — the caller merges it into the
        save-status display (the character itself is still saved without icon).
    """
    if image_input is None:
        return "", ""

    from PIL import Image, ImageOps

    target_path = None

    try:
        # Open the image — accept both PIL Image and file path
        if isinstance(image_input, Image.Image):
            img = image_input.copy()
        else:
            # File path string
            image_path = str(image_input)
            source_path = Path(image_path)

            # Check if already in character_icons/ (skip re-processing)
            icon_dir = Path(ICONS_DIR)
            try:
                source_abs = source_path.resolve()
                icon_dir_abs = icon_dir.resolve()
                try:
                    if source_abs.parent.samefile(icon_dir_abs):
                        return str(source_abs), ""
                except (OSError, AttributeError):
                    if str(source_abs.parent).lower() == str(icon_dir_abs).lower():
                        return str(source_abs), ""
            except Exception:
                pass

            if not source_path.exists():
                app_state.add_log_message("error", f"Source file does not exist: {image_path}")
                return "", t('charui.upload_error_msg')

            img = Image.open(image_path)

        # Pixel count check (memory protection)
        w, h = img.size
        if w * h > MAX_ICON_PIXELS:
            error_msg = f"Image too large: {w}x{h} pixels (max {MAX_ICON_PIXELS:,} pixels)"
            app_state.add_log_message("error", error_msg)
            img.close()
            return "", t('charui.image_too_large_msg', width=w, height=h)

        # Apply EXIF rotation (e.g. smartphone photos)
        img = ImageOps.exif_transpose(img)

        # Normalize mode (RGB/RGBA only for PNG output)
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')

        # Center crop to square (based on shorter side)
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

        # Resize to standard icon size
        img = img.resize((ICON_OUTPUT_SIZE, ICON_OUTPUT_SIZE), Image.Resampling.LANCZOS)

        # Ensure directory exists
        icon_dir = Path(ICONS_DIR)
        try:
            icon_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            app_state.add_log_message("error", f"Failed to create icon directory: {e}")
            img.close()
            return "", t('charui.dir_error_msg', error=e)

        # Save as PNG
        target_path = icon_dir / f"{uuid.uuid4()}.png"
        img.save(target_path, 'PNG', optimize=True)
        img.close()

        # Verify
        if not target_path.exists() or target_path.stat().st_size == 0:
            app_state.add_log_message("error", "Saved icon file is empty or missing")
            if target_path.exists():
                target_path.unlink()
            return "", t('charui.save_error_msg')

        app_state.add_log_message("info", f"Successfully saved icon ({ICON_OUTPUT_SIZE}x{ICON_OUTPUT_SIZE}px) to {target_path.resolve()}")
        return str(target_path.resolve()), ""

    except Exception as e:
        app_state.add_log_message("error", f"Error processing icon: {e}")
        if target_path and target_path.exists():
            try:
                target_path.unlink()
            except Exception:
                pass
        return "", t('charui.img_process_error_msg')


def create_character(
    name: str,
    summary_text: str,
    icon_upload_path: Optional[str],
    tts_model: str,
    stt_language: str,
    system_prompt: str,
    model_selection: str,
    motion_pngtuber_folder: str = "",
    elyth_system_prompt: str = "",
    elyth_api_key: str = "",
) -> tuple:
    """
    Create a new character configuration.
    Icon is processed at save time (not at upload time).
    youtube_system_prompt はこのフォームでは扱わない(編集口はYouTube返信タブ
    に一本化=稜裁定 2026-07-22)。backend側がconfigに""で初期化する。

    Args:
        name: Character name
        summary_text: Character description/summary
        icon_upload_path: Raw image path from gr.Image component (temp path or None)
        tts_model: TTS model identifier
        stt_language: Speech-to-text language code
        system_prompt: System prompt for LLM
        model_selection: Model selection in "provider::model_name" format
        motion_pngtuber_folder: Path to Motion PNG Tuber folder (optional)

    Returns:
        tuple: 入力10コンポーネント分のgr.update。
        成功時のみ入力をビルド時初期値へリセットし、バリデーション失敗・
        例外時はno-op(入力保持=修正して再送できる)。連続作成で同じキャラを
        誤って二重作成する事故の対策(稜裁定 2026-07-22)。
        結果通知は最下部Markdownではなく gr.Info / gr.Warning のトースト
        (稜裁定 2026-08-15: 最下部だとボタンから遠く見落とす)。
    """
    # 入力10コンポーネント分のno-op(失敗パス用: 入力値を一切動かさない)
    keep_inputs = tuple(gr.update() for _ in range(10))
    # ビルド時初期値へのリセット(成功パス用)。DDはgr.update(value=...)で
    # choicesを保持したままvalueのみ戻す(stt言語の初期値は"ja"=components.py)
    reset_inputs = (
        gr.update(value=""),    # name_input
        gr.update(value=""),    # summary_input
        gr.update(value=None),  # icon_upload
        gr.update(value=None),  # tts_dropdown
        gr.update(value="ja"),  # stt_lang_dropdown
        gr.update(value=""),    # system_prompt_box
        gr.update(value=None),  # ollama_models_dropdown
        gr.update(value=""),    # motion_pngtuber_folder
        gr.update(value=""),    # elyth_system_prompt
        gr.update(value=""),    # elyth_api_key
    )

    # Use shared validation function
    validation_error = validate_character_data(name, tts_model, model_selection, stt_language)
    if validation_error:
        gr.Warning(validation_error)
        return keep_inputs

    # Parse model selection
    from backend.shared.api_settings import decode_model_value
    model_provider, model_name = decode_model_value(model_selection)

    # Name is already validated and trimmed by validate_character_data
    name = name.strip()

    # Process icon at save time (icon failure does not block the save; the
    # warning is merged into the status display below)
    icon_path, icon_warning = process_and_save_icon(icon_upload_path) if icon_upload_path else ("", "")

    try:
        info = {
            "name": name,  # Use trimmed name
            "summary_text": summary_text.strip() if summary_text else "",
            "icon_path": icon_path,
            "tts_model_config": _build_tts_model_config(tts_model),
            "faster_whisper_config": {
                "language": stt_language
            },
            "system_prompt": system_prompt.strip() if system_prompt else "",
            "model_provider": model_provider,
            "model_name": model_name,
            "motion_pngtuber_folder": motion_pngtuber_folder.strip() if motion_pngtuber_folder else "",
            "elyth_system_prompt": elyth_system_prompt.strip() if elyth_system_prompt else "",
            "elyth_api_key": elyth_api_key.strip() if elyth_api_key else "",
        }

        create_response = backend.create_character(info)
        if _error_code(create_response) == UNSUPPORTED_MODEL_CODE:
            # 非対応モデル(思考を無効化できない Ollama モデル)=保存されない。
            # 入力は残す(モデルだけ選び直せる)
            app_state.add_log_message(
                "warning", f"Character creation refused (unsupported model): {model_name}")
            gr.Warning(t("err.model_thinking_unsupported"))
            return keep_inputs
        create_response = _require_success(create_response, "Character creation")
        # standardize_response は非dict戻り値を {success, result} に包む=IDはresult側
        new_id = create_response.get('result', '') if isinstance(create_response, dict) else create_response
        app_state.add_log_message("info", f"Character '{name}' created successfully")
        
        # Fix 6: Refresh character list after creation (for UI update)
        # This ensures the dropdown is updated with the new character
        status_text = t("charui.created_status", name=name, id=new_id)
        if icon_warning:
            status_text += " " + t("charui.icon_save_failed", reason=icon_warning)
        status_text = _elyth_key_toast(info["elyth_api_key"], status_text)
        gr.Info(status_text, duration=5)
        return reset_inputs
    except Exception as e:
        app_state.add_log_message("error", f"Error creating character: {e}")
        gr.Warning(t("charui.create_error_status", error=e))
        return keep_inputs


def _dropdown_update(choices, value):
    """gr.update for a Dropdown whose value must be one of its choices.

    Returns (update, is_set). ``choices`` is a list of (label, value) tuples
    or plain values; None means "leave the component's choices as they are"
    (then only a non-empty value is passed through). An empty value, or one
    that is not offered, becomes None (= unset) instead of tripping Gradio's
    "Value ... is not in the list of choices" error, which fails the whole
    event and shows "Error" on every output.
    """
    if choices is None:
        is_set = bool(value)
        return gr.update(value=value if is_set else None), is_set
    offered = {c[1] if isinstance(c, (tuple, list)) else c for c in choices}
    is_set = bool(value) and value in offered
    return gr.update(choices=choices, value=value if is_set else None), is_set


def _unified_model_choices():
    """Unified LLM choices (Ollama + API providers with a key) for the model
    dropdowns — the same list open_character_management builds on page open.
    Never raises: falls back to Ollama-only, then to None (= keep choices)."""
    ollama_models = []
    try:
        ollama_response = backend.list_ollama_models()
        if isinstance(ollama_response, dict) and 'result' in ollama_response:
            ollama_models = ollama_response.get('result', []) or []
        elif isinstance(ollama_response, list):
            ollama_models = ollama_response
    except Exception as e:
        app_state.add_log_message("warning", f"Failed to load Ollama models: {e}")
    try:
        from backend.shared.api_settings import get_all_available_models
        return get_all_available_models(ollama_models)
    except Exception as e:
        app_state.add_log_message("warning", f"Failed to load API models: {e}")
        return [(f"{m} (Ollama)", f"ollama::{m}") for m in ollama_models] or None


def load_character_for_edit(char_id: str) -> Tuple[str, str, str, Optional[str], str, str, str, str, str, str, str, str]:
    """
    Load character configuration for editing.

    Args:
        char_id: ID of character to load

    Returns:
        Tuple with character configuration fields including icon for display.
        The final element resets the hidden icon-changed flag (H8).
    """
    if not char_id:
        # 配線は12出力(ELYTH 2項目+iconフラグ含む)。要素数は配線outputsと
        # 常に一致させること — YouTube欄時代にここだけ12のまま取り残され
        # 「未選択でLoad Config」が出力数不一致になる潜伏バグがあった
        # (YouTube欄撤去=配線12出力化で解消・2026-07-22)。
        return ("", "", "", gr.update(value=None), gr.update(value=None), "en", "",
                gr.update(value=None), "", "", "", "")
    
    try:
        cfg_response = backend.load_character_config(char_id)
        
        # Handle structured response format
        if isinstance(cfg_response, dict) and 'result' in cfg_response:
            cfg = cfg_response.get('result', {})
            if not cfg_response.get('success', False):
                app_state.add_log_message("warning", f"Character config query returned unsuccessful status for {char_id}")
        else:
            # Direct config format (backward compatibility)
            cfg = cfg_response
        
        # Get icon path
        icon_path = cfg.get("icon_path", "")
        icon_display = None

        # Check if icon file exists and prepare it for display
        # Use PIL Image so Gradio caches it properly for browser display
        if icon_path:
            icon_path_obj = Path(icon_path)
            if icon_path_obj.exists():
                try:
                    from PIL import Image as PILImage
                    icon_display = PILImage.open(icon_path_obj).copy()
                except Exception as img_err:
                    app_state.add_log_message("warning", f"Could not load icon image: {img_err}")
                    icon_display = None
            else:
                app_state.add_log_message("warning", f"Icon file not found at: {icon_path}")
                icon_path = ""  # Clear the path if file doesn't exist
        
        # Build the encoded TTS dropdown value ("provider::identifier")
        tts_cfg = cfg.get("tts_model_config", {})
        if tts_cfg.get("provider") == "elevenlabs":
            tts_value = f"elevenlabs::{tts_cfg.get('voice_id', '')}"
        elif tts_cfg.get("provider") == "kokoro":
            tts_value = f"kokoro::{tts_cfg.get('voice_name', '')}"
        else:
            # Parse the model folder name from a path like
            # "sbv2_models/model_name/model.safetensors"
            tts_path = tts_cfg.get("model_path", "")
            tts_folder = ""
            if tts_path:
                path_parts = Path(tts_path).parts
                if len(path_parts) >= 2:
                    tts_folder = path_parts[-2]  # Get the second-to-last part
            tts_value = f"sbv2::{tts_folder}" if tts_folder else ""

        # Choices follow the character's language (ja=SBV2/en=Kokoro) so the
        # edit form is consistent on load; the language dropdown's .input
        # re-filters when the user changes it afterwards.
        char_language = cfg.get("faster_whisper_config", {}).get("language", "en")
        # Dropdown values must be one of the current choices (or None): Gradio
        # rejects anything else ("Value ... is not in the list of choices") and
        # the whole load event errors out with "Error" on every output
        # (稜 Mac 実機 2026-09-25: 声/LLM 未設定で出荷される同梱モデルキャラクター
        # が読み込めなかった). A saved value that is not offered any more
        # (voice model removed, LLM unset) loads as "unset" plus a toast;
        # save-time validation (validate_character_data) still requires both.
        try:
            from backend.shared.api_settings import get_all_tts_choices
            from .handlers.api_settings import _list_sbv2_models
            tts_choices = get_all_tts_choices(_list_sbv2_models(), language=char_language)
        except Exception:
            tts_choices = None  # keep the component's current choices
        tts_dd_update, tts_set = _dropdown_update(tts_choices, tts_value)

        # Build encoded model value for dropdown; choices = the unified list
        # (Ollama + keyed API providers), the same source as the page-open refresh
        from backend.shared.api_settings import encode_model_value
        model_provider = cfg.get("model_provider", "ollama")
        model_name_val = cfg.get("model_name", "")
        encoded_model = encode_model_value(model_provider, model_name_val) if model_name_val else ""
        model_dd_update, model_set = _dropdown_update(_unified_model_choices(), encoded_model)
        if not tts_set and not model_set:
            gr.Warning(t('charui.edit_missing_both'))
        elif not tts_set:
            gr.Warning(t('charui.edit_missing_voice'))
        elif not model_set:
            gr.Warning(t('charui.edit_missing_model'))

        # Use gr.update(value=None) to properly clear Image component
        # (passing raw None can cause broken image display in some Gradio versions)
        icon_update = gr.update(value=icon_display)

        return (
            cfg.get("name", ""),
            cfg.get("summary_text", ""),
            icon_path,  # Hidden textbox still gets the path string
            icon_update,  # Image component: gr.update with file path or None
            tts_dd_update,  # choices filtered by language + encoded value
            char_language,
            cfg.get("system_prompt", ""),
            model_dd_update,  # unified choices + "provider::model_name" value (None = unset)
            cfg.get("motion_pngtuber_folder", ""),
            cfg.get("elyth_system_prompt", ""),
            cfg.get("elyth_api_key", ""),
            "",  # reset icon-changed flag on (re)load
        )
    except Exception as e:
        app_state.add_log_message("error", f"Error loading character config: {e}")
        return ("", "", "", gr.update(value=None), gr.update(value=None), "en", "",
                gr.update(value=None), "", "", "", "")


def edit_character(
    char_id: str,
    name: str,
    summary_text: str,
    icon_upload_path: Optional[str],
    icon_original_path: str,
    icon_changed: str,
    tts_model: str,
    stt_language: str,
    system_prompt: str,
    model_selection: str,
    motion_pngtuber_folder: str = "",
    elyth_system_prompt: str = "",
    elyth_api_key: str = "",
) -> None:
    """
    Update an existing character configuration.
    Icon is resolved at save time from the hidden icon-changed flag (H8).
    youtube_system_prompt はこのフォームでは扱わない(編集口はYouTube返信タブ
    に一本化=稜裁定 2026-07-22)。edit_characterは部分更新マージなので、
    updated_infoに含めなければタブで保存済みの値がそのまま保持される。
    結果通知は gr.Info / gr.Warning のトースト(稜裁定 2026-08-15)。
    フォーム値と選択キャラは保持され、続けて編集→再保存できる。

    Args:
        char_id: Character ID to edit
        name: Updated character name
        summary_text: Updated character description
        icon_upload_path: Current value of gr.Image component (temp path, character_icons path, or None)
        icon_original_path: Original icon_path from the loaded config
        icon_changed: Hidden flag — "" unless the user touched the Image component
        tts_model: Updated TTS model
        stt_language: Updated STT language
        system_prompt: Updated system prompt
        model_selection: Updated model in "provider::model_name" format
        motion_pngtuber_folder: Updated Motion PNG Tuber folder path
    """
    if not char_id:
        gr.Warning(t("charui.char_id_required"))
        return

    # Prevent editing active character during conversation
    if app_state.active_character_id == char_id and app_state.conversation_started:
        gr.Warning(t("charui.edit_blocked"))
        return

    # Use shared validation function
    validation_error = validate_character_data(name, tts_model, model_selection, stt_language)
    if validation_error:
        gr.Warning(validation_error)
        return

    # Parse model selection
    from backend.shared.api_settings import decode_model_value
    model_provider, model_name_parsed = decode_model_value(model_selection)

    # Name is already validated and trimmed by validate_character_data
    name = name.strip()

    # Resolve icon at save time (icon failure does not block the save; the
    # warning is merged into the status display below)
    icon_path, icon_warning = _resolve_edit_icon(icon_upload_path, icon_original_path, icon_changed)

    # Use the original config path for old icon cleanup (no need to reload config)
    old_icon_path = icon_original_path if icon_original_path else None

    try:
        updated_info = {
            "name": name,
            "summary_text": summary_text.strip() if summary_text else "",
            "icon_path": icon_path,
            "tts_model_config": _build_tts_model_config(tts_model),
            "faster_whisper_config": {
                "language": stt_language
            },
            "system_prompt": system_prompt.strip() if system_prompt else "",
            "model_provider": model_provider,
            "model_name": model_name_parsed,
            "motion_pngtuber_folder": motion_pngtuber_folder.strip() if motion_pngtuber_folder else "",
            "elyth_system_prompt": elyth_system_prompt.strip() if elyth_system_prompt else "",
            "elyth_api_key": elyth_api_key.strip() if elyth_api_key else "",
        }

        edit_response = backend.edit_character(char_id, updated_info)
        if _error_code(edit_response) == UNSUPPORTED_MODEL_CODE:
            # モデル変更が非対応モデル=保存されない(他の編集も未保存のまま)
            app_state.add_log_message(
                "warning", f"Character update refused (unsupported model): {model_name_parsed}")
            gr.Warning(t("err.model_thinking_unsupported"))
            return
        _require_success(edit_response, "Character update")
        app_state.add_log_message("info", f"Character '{name}' updated successfully")
        
        # Clean up old icon if it changed
        if old_icon_path and icon_path and old_icon_path != icon_path:
            try:
                old_icon = Path(old_icon_path)
                # Only delete if it's not the default icon and exists
                if old_icon.exists() and old_icon.name != 'default.png':
                    old_icon.unlink()
                    app_state.add_log_message("info", f"Removed old icon: {old_icon.name}")
            except Exception as icon_cleanup_error:
                app_state.add_log_message("warning", f"Could not remove old icon: {icon_cleanup_error}")
                # Don't fail the operation if icon cleanup fails
        
        # アクティブキャラの再ロードは backend.edit_character 内で1回だけ行われる
        # (preserve_conversation=True・関連フィールド変更時のみ)。ここでの2回目の
        # activate_character 呼び出しは冗長なフル再ロードで、しかも
        # preserve_conversation=False のため会話状態を落とす経路だった(2026-08-15 撤去)。

        status_text = t("charui.updated_status", name=name, id=char_id)
        if icon_warning:
            status_text += " " + t("charui.icon_save_failed", reason=icon_warning)
        status_text = _elyth_key_toast(updated_info["elyth_api_key"], status_text)
        gr.Info(status_text, duration=5)
    except Exception as e:
        app_state.add_log_message("error", f"Error editing character: {e}")
        gr.Warning(t("charui.edit_error_status", error=e))


def confirm_character_deletion(char_id: str) -> Tuple[gr.update, str, str]:
    """
    Show confirmation dialog for character deletion.
    
    Args:
        char_id: ID of the character to delete
        
    Returns:
        Tuple containing dialog visibility, confirmation text, and character ID
    """
    if not char_id:
        # 確認ボックスは閉じたまま=中の文言は見えないため、トーストで通知
        gr.Warning(t('charui.no_char_selected'))
        return (gr.update(visible=False), "", "")

    try:
        config_response = backend.load_character_config(char_id)
        # Handle structured response
        if isinstance(config_response, dict) and 'result' in config_response:
            char_config = config_response.get('result', {})
        else:
            char_config = config_response
        char_name = char_config.get("name", t('charui.unknown_character'))
        return (
            gr.update(visible=True),
            t('charui.delete_confirm', name=char_name, id=char_id),
            char_id
        )
    except Exception as e:
        app_state.add_log_message("error", f"Error preparing confirmation: {e}")
        gr.Warning(t('charui.char_data_load_error'))
        return (
            gr.update(visible=False),
            "",
            ""
        )


def remove_character(char_id: str) -> tuple:
    """
    Remove a character from the system with safety checks.

    Args:
        char_id: ID of the character to remove

    Returns:
        tuple: (確認ボックスのgr.update, 編集フォーム12項目+confirm_char_idの
        gr.update)。削除成功時のみフォームをクリアし、失敗時はno-op(保持)。
        順序は app.py の confirm_yes 配線の outputs と一致必須。
        結果通知は gr.Info / gr.Warning のトースト(稜裁定 2026-08-15)。
    """
    # フォーム12項目+confirm_char_id のno-op(失敗パス用)
    keep_form = tuple(gr.update() for _ in range(13))
    # 削除成功時のフォームクリア(新規作成の reset_inputs と同じ既定値)
    reset_form = (
        gr.update(value=""),    # edit_name
        gr.update(value=""),    # edit_summary
        gr.update(value=None),  # edit_icon_upload
        gr.update(value=""),    # edit_icon_original
        gr.update(value=""),    # edit_icon_changed
        gr.update(value=None),  # edit_tts_dropdown
        gr.update(value="ja"),  # edit_stt_dropdown
        gr.update(value=""),    # edit_system_prompt
        gr.update(value=None),  # edit_ollama_dropdown
        gr.update(value=""),    # edit_motion_pngtuber_folder
        gr.update(value=""),    # edit_elyth_system_prompt
        gr.update(value=""),    # edit_elyth_api_key
        gr.update(value=""),    # confirm_char_id
    )

    if not char_id:
        gr.Warning(t("charui.no_char_selected"))
        return (gr.update(visible=False),) + keep_form

    try:
        remove_response = backend.remove_character(char_id)

        # コード付きの拒否(記憶タスク実行中 / ELYTHセッション実行中)は翻訳済み
        # トーストで理由を返し、フォームは no-op で保持する。
        # ここで先に拾うのは _require_success が英語原文の汎用例外に潰すため。
        refused_code = _error_code(remove_response)
        if refused_code in (DELETE_ELYTH_ACTIVE_CODE, DELETE_MEMORY_TASK_CODE):
            app_state.add_log_message(
                "warning", f"Character removal refused ({refused_code}): {char_id}")
            gr.Warning(t(f"err.{refused_code}"))
            return (gr.update(visible=False),) + keep_form

        _require_success(remove_response, "Character removal")

        # アクティブキャラの解除は「削除が成功してから」。呼び出し前に解除して
        # いた旧実装だと、上のガードで拒否されたときに「解除しました」の
        # ポップアップと「削除に失敗」のトーストが同時に出て、キャラは残って
        # いるのにUIだけアクティブを失う矛盾状態になっていた。
        if app_state.active_character_id == char_id:
            app_state.active_character_id = None
            app_state.active_character_icon = None
            show_warning_popup(t('charui.active_removed_title'),
                             t('charui.active_removed_msg'))

        # 失敗時は上で raise 済み=このログが嘘になることはない(旧実装は
        # 失敗しても removed successfully と記録していた)
        app_state.add_log_message("info", "Character removed successfully")

        # NOTE: Automatic cleanup disabled due to safety concerns
        # Icon cleanup will run periodically with safety checks instead
        # This prevents accidental deletion of all icons

        gr.Info(t("charui.removed_status", id=char_id), duration=5)
        return (gr.update(visible=False),) + reset_form
    except Exception as e:
        app_state.add_log_message("error", f"Error removing character: {e}")
        gr.Warning(t("charui.remove_error_status", error=e))
        return (gr.update(visible=False),) + keep_form


# Export public API
__all__ = [
    'validate_character_data',
    'switch_character',
    'refresh_char_list',
    'load_char_dropdown',
    'open_character_management',
    'process_and_save_icon',
    'resize_icon_preview',
    'mark_edit_icon_changed',
    'create_character',
    'load_character_for_edit',
    'edit_character',
    'confirm_character_deletion',
    'remove_character',
    'cleanup_orphaned_icons',
    'cleanup_duplicate_icons'
]