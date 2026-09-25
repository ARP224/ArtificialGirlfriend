"""Lifecycle / runtime handlers for the desktop UI.

Extracted from ui/app.py (B11o, B11p). Self-contained leaves wired to
demo.load / gr.Timer / WebSocket triggers whose bodies reference only
downward module-level names + call-time imports (no local Gradio component
capture). The .click/.tick/.load wiring stays in app.py. B11p added
initial_load_with_cleanup, freed by lowering _build_utility_panel_html to
ui/utility_panel.py (downward import). handle_switch_to_server_mode remains
deferred: it closes over app.py module-level _execute_shutdown and belongs
with the system-control cluster (app-root responsibility).
"""
import logging

import gradio as gr

import backend
from backend.shared.i18n import t
from ..character_ui import load_char_dropdown
from ..components import get_chat_history
from ..conversation import execute_auto_prompt_generation, process_auto_prompt
from ..state import app_state
from ..status_checker import get_status_html
from ..utility_panel import _build_utility_panel_html

logger = logging.getLogger(__name__)


def handle_auto_prompt_generating():
    """Handle auto prompt generating trigger - process auto prompt and update chat"""
    import threading
    # Stage 1: Show "Generating Response" immediately
    process_auto_prompt()
    # Schedule execution in a background thread
    if hasattr(app_state, '_pending_auto_prompt_text'):
        def execute_in_background():
            import time
            time.sleep(0.1)  # Small delay to ensure UI updates first
            execute_auto_prompt_generation()
        threading.Thread(target=execute_in_background, daemon=True).start()
    return get_chat_history()


def check_ui_updates():
    """Check if UI needs updating (fallback for when WebSocket is not available)"""
    if hasattr(app_state, '_needs_ui_update') and app_state._needs_ui_update:
        app_state._needs_ui_update = False
        return get_chat_history()
    return gr.skip()  # No update needed


def handle_remote_disconnect():
    try:
        from backend.server.session_manager import (
            get_session_manager, CLOSE_CODE_SELF_DISCONNECT,
        )
        sm = get_session_manager()
        # 自己切断: admin kick既定のままだと自分の画面に「管理者により
        # 切断されました」が出る(稜実機 2026-07-30)
        result = sm.force_disconnect(
            close_code=CLOSE_CODE_SELF_DISCONNECT, message="接続を解除しました"
        )
        if result:
            return f"<div style='color:#4caf50;padding:15px;border-radius:8px;background:#1a2a1a;'>{t('hdl.lifecycle.disconnected')}</div>"
        else:
            return f"<div style='color:#ff9800;padding:15px;border-radius:8px;background:#2a2515;'>{t('hdl.lifecycle.no_active_session')}</div>"
    except Exception as e:
        return f"<div style='color:#f44336;padding:15px;border-radius:8px;background:#2a1515;'>{t('common.error_with', error=e)}</div>"


_icon_cleanup_done = False


def initial_load_with_cleanup():
    global _icon_cleanup_done
    dropdown = load_char_dropdown()

    # Run icon cleanup ONLY once per process (this is wired to demo.load, which
    # fires on every page load/reconnect — scanning+deleting the whole icons dir
    # on every reload was both wasteful and risky).
    if not _icon_cleanup_done:
        try:
            if dropdown.get('choices') and len(dropdown.get('choices', [])) > 0:
                from ..character_ui import cleanup_orphaned_icons, cleanup_duplicate_icons
                logger.info("Running icon cleanup after successful character load")
                cleanup_orphaned_icons()
                cleanup_duplicate_icons()
                _icon_cleanup_done = True
            else:
                logger.warning("Skipping icon cleanup - no characters loaded")
        except Exception as e:
            logger.error(f"Icon cleanup error: {e}")

    # Get connection status as HTML
    status_html = get_status_html()

    # Also prepare the history dropdown with the same choices
    history_dropdown = gr.update(choices=dropdown.get('choices', []))

    # Build utility panel HTML with actual backend state
    _server_mode = bool(getattr(app_state, "server_mode_enabled", False))
    try:
        from backend.shared.feature_availability import get_block_reasons
        fs = backend.get_feature_status()
        utility_html = _build_utility_panel_html(
            server_mode=_server_mode,
            availability=get_block_reasons(),
            pc_status=fs.get("pc_status_enabled", False),
            screen_capture=fs.get("screen_capture_enabled", False),
            talk_theme=fs.get("talk_theme_enabled", True),
            speechless=fs.get("speechless_enabled", False),
            command_execution=fs.get("command_execution_enabled", False),
            notes=fs.get("notes_enabled", False),
            image_generation=fs.get("image_generation_enabled", False),
            camera_capture=fs.get("camera_capture_enabled", False),
            ambient_camera=fs.get("ambient_camera_enabled", False),
            deep_search=fs.get("deep_search_enabled", False),
            elyth=fs.get("elyth_enabled", False),
        )
    except Exception:
        utility_html = _build_utility_panel_html(server_mode=_server_mode)

    return dropdown, status_html, history_dropdown, utility_html
