"""
ui/conversation/display.py

チャット表示設定(フォントサイズ)のハンドラ。
"""

from ..state import app_state
from backend.shared.i18n import t


def update_chat_font_size(font_size: int) -> str:
    """
    Update chat font size setting.
    Note: This does NOT trigger a chat history re-render to avoid flicker.
    JavaScript will handle the immediate visual update.
    
    Args:
        font_size: Font size in pixels
        
    Returns:
        str: Status message
    """
    app_state.chat_font_size = font_size
    app_state.add_log_message("debug", f"Chat font size updated to {font_size}px")
    
    # Save settings
    from ..settings_manager import save_app_state_settings
    save_app_state_settings(app_state)
    
    return t('conv.font_size_status', size=font_size)
