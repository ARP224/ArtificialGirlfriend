"""
ui/conversation/commands.py

コマンド実行の許可/拒否ハンドラ(チャット内の承認/拒否ボタンから呼ばれる)。
"""

import logging

logger = logging.getLogger(__name__)


def handle_command_deny() -> None:
    """Handle command denial (Deny button)."""
    from backend.backend import _backend_state
    if not _backend_state or not _backend_state.command_approval_pending:
        return
    logger.info("[Command] User denied command execution")
    _backend_state.command_approval_result = "denied"
    _backend_state.command_approval_event.set()
    # Send WS notification to disable buttons (backup — _wait_for_approval also sends this)
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("command_approval_resolved")
    except Exception:
        pass


def handle_command_accept() -> None:
    """Handle command acceptance (Accept button)."""
    from backend.backend import _backend_state
    if not _backend_state or not _backend_state.command_approval_pending:
        return
    logger.info("[Command] User accepted command execution")
    _backend_state.command_approval_result = "accepted"
    _backend_state.command_approval_event.set()
    # Send WS notification to disable buttons (backup — _wait_for_approval also sends this)
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("command_approval_resolved")
    except Exception:
        pass
