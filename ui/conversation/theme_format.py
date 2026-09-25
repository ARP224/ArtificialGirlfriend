"""
ui/conversation/theme_format.py

トークテーマカード(set/clear × AI/user)の HTML 整形を1箇所に集約する。

ライブ経路2本(generation.py=AI の FC・conversation_functions.py=ユーザーの
ボタン操作)とリロード経路(history.py=DB レコードからの復元)が同じカードを
別々に組み立てると乖離する(command_format.py が一本化された事故と同じ構図)。
真実源をここに置く。
"""

import html as _html

from backend.shared.i18n import t


def render_talk_theme_html(action: str, theme: str = "", by_user: bool = False) -> str:
    """Build the talk-theme card HTML shared by live and reload paths.

    Args:
        action: "set" or "clear" (anything not "set" renders the clear card)
        theme: Raw theme text for "set" — escaped here, callers pass it unescaped
        by_user: True for user-button changes, False for AI tool changes
    """
    if action == "set":
        key = "gen.talk_theme_set_user" if by_user else "gen.talk_theme_set"
        return f'<div class="theme-header">{t(key)}</div><div>{_html.escape(theme)}</div>'
    key = "gen.talk_theme_cleared_user" if by_user else "gen.talk_theme_cleared"
    return f'<div class="theme-header">{t(key)}</div>'
