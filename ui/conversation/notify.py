"""
ui/conversation/notify.py

会話UIのUI更新通知の発行口。publish_ui_update(backend.shared.ui_events)への
橋渡しと、通知失敗時のログのみを責務とする。
"""

import logging

from ..state import app_state

logger = logging.getLogger(__name__)


def notify_ui_update(reason: str = "", data: dict = None) -> bool:
    """
    Notify UI to update via WebSocket or fallback mechanism.
    
    Args:
        reason: Reason for the update
        data: Optional additional data
        
    Returns:
        bool: True if notification was sent successfully
    """
    try:
        from backend.shared.ui_events import publish_ui_update
        
        # Try to send via WebSocket
        if publish_ui_update("update_chat", reason, data):
            logger.debug(f"[UI Update] WebSocket notification sent: {reason}")
            return True
    except ImportError:
        logger.warning("[UI Update] WebSocket server not available")
    except Exception as e:
        logger.error(f"[UI Update] Failed to send WebSocket notification: {e}")
    
    # Fallback: Set flag for timer-based update
    app_state._needs_ui_update = True
    app_state._chat_history_version += 1
    logger.debug(f"[UI Update] Fallback flag set: {reason}")
    return False
