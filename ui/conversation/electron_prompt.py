"""
ui/conversation/electron_prompt.py

MotionPNGPlayerのテキスト入力からの会話フロー責務。WS(text_prompt)から
backend.shared.text_prompt_command 経由で呼ばれ、ブラウザUIなしで
テキストターンをヘッドレス実行する(隠しボタン中継のAuto Prompt方式は
UIページが開いていないと動かないため使えない)。
"""

import logging
import threading

from ..state import app_state
from backend.shared.i18n import t

from .auto_prompt import reset_auto_prompt_timer
from .generation import handle_generate_reply, is_error_response, validate_active_character
from .notify import notify_ui_update

logger = logging.getLogger(__name__)


def process_electron_text_prompt(text: str) -> dict:
    """
    Validate and start a headless text turn from the MotionPNGPlayer input box.

    Mirrors handle_text_input's validations (without Gradio popups/outputs),
    atomically takes the cross-client generating lock, then runs the turn in a
    daemon thread. Returns immediately with an accept/reject dict:
    {"success": bool, "message": str} — on success, generation is running and
    the result reaches clients through the normal broadcast paths (chat
    notify + tts_audio).
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass

    text = (text or "").strip()
    if not text:
        return {"success": False, "message": t('conv.stat_enter_message')}

    if not app_state.conversation_started:
        logger.info("[ElectronPrompt] Rejected: conversation not started")
        return {"success": False, "message": t('conv.stat_start_first')}

    char_id = validate_active_character()
    if not char_id:
        logger.info("[ElectronPrompt] Rejected: no active character")
        return {"success": False, "message": t('conv.stat_no_char')}

    # Cross-client generating lock (atomic check-and-set, same as
    # process_text_generation / execute_auto_prompt_generation)
    from backend.backend import _backend_state
    if _backend_state:
        with _backend_state.is_generating_lock:
            if _backend_state.is_generating:
                logger.info("[ElectronPrompt] Rejected: another client is generating")
                return {"success": False, "message": t('gen.another_client')}
            _backend_state.is_generating = True
    # From here on we own the generating lock — the worker's finally releases it.

    # Live Camera: 送信確定=撮影要求を発火(提供者なしならno-op)
    try:
        from backend.shared.ambient_camera_state import fire_capture_request
        fire_capture_request()
    except Exception:
        pass

    reset_auto_prompt_timer()
    app_state.append_chat_message("User", text, is_ai=False)
    app_state.add_log_message("info", f"[ElectronPrompt] Text prompt received: {len(text)} characters")
    notify_ui_update(reason="user_message_sent")

    threading.Thread(
        target=_run_generation, args=(text,),
        name="electron-prompt", daemon=True
    ).start()
    return {"success": True, "message": ""}


def _run_generation(text: str) -> None:
    """Run the turn (LLM + chat append + TTS broadcast) off the WS event loop."""
    from backend.backend import _backend_state
    try:
        from backend.server.websocket_server import get_websocket_manager
        get_websocket_manager().broadcast_generating_state_sync(True)
    except Exception:
        pass
    app_state.response_generating = True
    app_state._chat_history_version += 1

    try:
        ai_reply = handle_generate_reply(text)
        if is_error_response(ai_reply):
            app_state.add_log_message("warning", f"[ElectronPrompt] AI reply contained error: {ai_reply}")
        else:
            app_state.add_log_message("info", f"[ElectronPrompt] AI reply generated ({len(ai_reply)} chars)")
    except Exception as e:
        app_state.add_log_message("error", f"[ElectronPrompt] Generation error: {e}")
    finally:
        app_state.response_generating = False
        app_state._chat_history_version += 1
        if _backend_state:
            with _backend_state.is_generating_lock:
                _backend_state.is_generating = False
        try:
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_generating_state_sync(False)
        except Exception:
            pass
        notify_ui_update(reason="text_generation_complete")
