"""
ui/conversation/text_input.py

テキスト入力の会話フロー責務。入力検証・添付ステータス整形・テキスト生成の
開始/実行・入力欄クリアを持つ。
"""

from typing import Tuple

from ..state import app_state
from ..error_handler import error_status_text
from backend.shared.i18n import t

from .auto_prompt import reset_auto_prompt_timer
from .generation import handle_generate_reply, is_error_response, validate_active_character
from .notify import notify_ui_update


def handle_text_input(text_input: str, images: list = None,
                      documents: list = None) -> Tuple[str, str, list, list, str]:
    """
    Handle text input from the user via keyboard.
    Validates conversation state and adds user message to chat.

    Args:
        text_input: The text entered by the user
        images: Optional list of attached image file paths
        documents: Optional list of attached document dicts

    Returns:
        Tuple[str, str, list, list, str]: (cleared text, status, cleared images, cleared docs, attach status)
    """
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().touch_activity()
    except Exception:
        pass
    # Reset auto prompt timer when user sends text
    reset_auto_prompt_timer()

    # Check if conversation is active (unreachable in normal UI: inputs are
    # locked before start — kept as defense in depth, status text only)
    if not app_state.conversation_started:
        app_state.add_log_message("info", "Attempted to send text before starting conversation")
        return text_input, t('conv.stat_start_first'), images or [], documents or [], _format_attach_status(images)

    # Soft guard: block if another client is generating (best-effort, no lock)
    try:
        from backend.backend import _backend_state as _bs
        if _bs and _bs.is_generating:
            app_state.add_log_message("info", "Text input blocked: another client is generating")
            return text_input, t('conv.stat_generating'), images or [], documents or [], _format_attach_status(images)
    except Exception:
        pass

    # Validate text input
    if not text_input or text_input.strip() == "":
        app_state.add_log_message("info", "Empty text input attempted")
        return "", t('conv.stat_enter_message'), images or [], documents or [], _format_attach_status(images)

    # Validate character selection
    char_id = validate_active_character()
    if not char_id:
        app_state.add_log_message("warning", "No character selected for text input")
        return text_input, t('conv.stat_no_char'), images or [], documents or [], _format_attach_status(images)

    # Live Camera: 送信確定=撮影要求を発火(デスクトップ/モバイル両方が
    # この関数を通る。提供者なしならno-op・待ちは生成直前バリア)
    try:
        from backend.shared.ambient_camera_state import fire_capture_request
        fire_capture_request()
    except Exception:
        pass

    # Resolve image paths from Gradio temp files
    image_paths = []
    if images:
        for img in images:
            path = img if isinstance(img, str) else getattr(img, 'name', str(img))
            image_paths.append(path)

    # Merge with image buffer (companion images) for chat display
    try:
        from backend.backend import _backend_state
        if _backend_state:
            buffer_paths = _backend_state.image_buffer.peek_all()
            # Deduplicate by path string (desktop images are in both lists)
            seen = set(image_paths)
            for bp in buffer_paths:
                if bp not in seen:
                    image_paths.append(bp)
                    seen.add(bp)
    except Exception:
        pass

    # Collect document filenames for chat display
    doc_filenames = []
    if documents:
        for doc in documents:
            name = doc.get("filename", "") if isinstance(doc, dict) else str(doc)
            if name:
                doc_filenames.append(name)

    # Check if location was recently sent (not yet consumed by LLM)
    try:
        from backend.tools.location_manager import has_unrecorded_location
        if has_unrecorded_location():
            doc_filenames.append("__location_sent__")
    except Exception:
        pass

    # Add user message to chat (with images and document filenames for display)
    app_state.append_chat_message("User", text_input, is_ai=False, images=image_paths, documents=doc_filenames)
    img_info = f", {len(image_paths)} images" if image_paths else ""
    doc_info = f", {len(doc_filenames)} documents" if doc_filenames else ""
    app_state.add_log_message("info", f"Text input received: {len(text_input)} characters{img_info}{doc_info}")

    # Notify all WS clients (companion sees user message immediately)
    notify_ui_update(reason="user_message_sent")

    # Set interruption flag if a command approval is pending
    from backend.backend import _backend_state
    if _backend_state and _backend_state.command_approval_pending:
        _backend_state._pending_interruption = True
        _backend_state.command_approval_result = "interrupted"
        if _backend_state.command_approval_event:
            _backend_state.command_approval_event.set()

    # Store the text for processing (images come from buffer now, documents from doc buffer)
    app_state._pending_user_text = text_input
    app_state._pending_user_images = image_paths
    app_state._pending_user_documents = documents

    # Return immediately to show user message (clear text, clear images, clear docs).
    # Status is the same generating text the following chain steps and the WS
    # is_generating notice write, so the line never flickers between send and
    # the final result (stable text until done, ruling 2026-09-05).
    return "", t('conv.stat_generating'), [], [], ""


def _format_attach_status(images: list = None) -> str:
    """Format attachment status text."""
    if not images:
        return ""
    return t('attach.images', count=len(images))


def process_text_generation() -> str:
    """
    Process AI generation for the pending user text.
    This is called after the user message has been displayed.
    
    Returns:
        str: Status message about generation result
    """
    app_state.add_log_message("info", "[SLEEP-DEBUG] process_text_generation entered")
    # Check if there's pending text to process
    if not hasattr(app_state, '_pending_user_text') or not app_state._pending_user_text:
        return ""

    text_input = app_state._pending_user_text
    app_state._pending_user_text = None  # Clear pending text

    # Cross-client generating lock (atomic check-and-set, BEFORE image consumption)
    from backend.backend import _backend_state
    if _backend_state:
        with _backend_state.is_generating_lock:
            if _backend_state.is_generating:
                # Another client is already generating — abort without consuming buffers
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
            # Broadcast cleared state to all clients
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_image_slot_update_sync(0)
    except Exception:
        pass
    if not pending_images:
        pending_images = getattr(app_state, '_pending_user_images', None)
    app_state._pending_user_images = None  # Clear pending images

    # Get documents: prefer document_buffer, fallback to pending
    pending_documents = None
    try:
        if _backend_state and _backend_state.document_buffer.get_count() > 0:
            pending_documents = _backend_state.document_buffer.consume_all()
    except Exception:
        pass
    if not pending_documents:
        pending_documents = getattr(app_state, '_pending_user_documents', None)
    app_state._pending_user_documents = None  # Clear pending documents

    # Set generating state to show indicator
    app_state.response_generating = True
    app_state._chat_history_version += 1  # Force update to show generating indicator

    try:
        ai_reply = handle_generate_reply(text_input, images=pending_images, documents=pending_documents)

        # Check for errors in the reply
        if is_error_response(ai_reply):
            app_state.add_log_message("warning", f"AI reply contained error: {ai_reply}")
            # 汎用文でなく原因入りの具体メッセージを入力欄下に表示する
            # (翻訳済みエラーの唯一のユーザー向け表示・稜裁定 2026-08-02)
            return error_status_text(ai_reply)
        else:
            app_state.add_log_message("info", f"AI reply generated successfully ({len(ai_reply)} chars)")
            return t('gen.done')

    except Exception as e:
        app_state.add_log_message("error", f"Error in text generation: {e}")
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
        notify_ui_update(reason="text_generation_complete")


def start_text_generation() -> str:
    """
    Start the text generation process by setting the generating state.
    This shows the "Generating response..." indicator in the chat. The status
    string is the same one handle_text_input already returned (no flicker).
    
    Returns:
        str: Status message
    """
    app_state.add_log_message("info", "[SLEEP-DEBUG] start_text_generation entered")
    # Check if there's pending text to process
    if hasattr(app_state, '_pending_user_text') and app_state._pending_user_text:
        app_state.response_generating = True
        app_state._chat_history_version += 1  # Force update to show generating indicator
        return t('conv.stat_generating')
    return ""


def clear_text_input() -> Tuple[str, str]:
    """
    Clear the text input field.
    
    Returns:
        Tuple[str, str]: (empty text input, status message)
    """
    app_state.add_log_message("debug", "Text input cleared")
    return "", t('conv.stat_cleared')
