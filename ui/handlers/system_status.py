"""
ui/handlers/system_status.py

System/extraction-status polling that drives exit/restart/refresh-history
button states. Extracted verbatim from ui/app.py (B11d) as a self-contained
UI handler leaf (registry-seam home: ui/handlers/<feature>.py).
"""

import logging

import gradio as gr

import backend
from backend.shared.i18n import t
from ..state import app_state

logger = logging.getLogger(__name__)


def check_extraction_status():
    """
    Check if a memory task (extraction / relationship update) is in progress and
    return button state updates. Called via WebSocket trigger
    (extraction_started/completed) and on page load.

    Returns:
        Tuple of (exit_btn_update, restart_btn_update, status_text_update,
        refresh_history_btn_update, switch_to_server_btn_update)
    """
    try:
        is_busy = backend.is_memory_task_running()
        # Check if character is selected for refresh history button
        has_character = app_state.active_character_id is not None
        # サーバーモード切替ボタンは Tailscale 未インストールのフールプルーフで
        # ビルド時から interactive=False(ui/pages.py・稜裁定 2026-07-25)。復帰時に
        # 無条件で True を返すとそのゲートを剥がすため、同じ判定を保持する
        # (is_tailscale_installed は subprocess 不使用のパス解決+キャッシュ=軽い)。
        from backend.server.tailscale import is_tailscale_installed
        switch_allowed = is_tailscale_installed()

        if is_busy:
            return (
                gr.update(interactive=False),  # exit_btn
                gr.update(interactive=False),  # restart_btn
                f"""<div style='text-align: center; padding: 15px; background-color: #ff9800; color: white; border-radius: 8px; font-size: 16px;'>
                    <strong>{t('hdl.system_status.extracting')}</strong>
                </div>""",
                gr.update(interactive=False),  # refresh_history_btn
                gr.update(interactive=False)  # switch_to_server_btn
            )
        else:
            # 文言・スタイルはビルド既定(ui/pages.py の status_text)と同一に保つ:
            # この関数はページ読み込み時の demo.load からも呼ばれるため、食い違うと
            # 開くたびに見た目が変わる(サーバーモードでは「(サーバーモード)」が消える)。
            status_key = (
                'system.status_running_server'
                if app_state.server_mode_enabled else 'system.status_running'
            )
            return (
                gr.update(interactive=True),
                gr.update(interactive=True),
                f"""<div style='text-align: center; padding: 15px; background-color: #4CAF50; color: white; border-radius: 8px; font-size: 16px;'>
                    <strong>{t(status_key)}</strong>
                </div>""",
                gr.update(interactive=has_character),  # refresh_history_btn - only if character selected
                gr.update(interactive=switch_allowed)  # switch_to_server_btn - keeps the Tailscale gate
            )
    except Exception as e:
        logger.warning(f"Error checking extraction status: {e}")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
