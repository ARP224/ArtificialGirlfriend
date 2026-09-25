"""
tests/smoke/test_talk_theme_display.py

トークテーマカード整形(ui/conversation/theme_format.py)のスモーク。
ライブ2経路(AI FC / ユーザーボタン)とリロード経路(history.py)が共有する
唯一の整形関数の出力形を固定する。ラベル文言は i18n 依存なので t() 経由で
組み立てた期待値と比較する(ロケール非依存)。
"""

import html

from backend.shared.i18n import t
from ui.conversation.theme_format import render_talk_theme_html


def test_ai_set_card():
    out = render_talk_theme_html("set", "テストしよう")
    expected = (
        f'<div class="theme-header">{t("gen.talk_theme_set")}</div>'
        f'<div>テストしよう</div>'
    )
    assert out == expected


def test_ai_clear_card():
    out = render_talk_theme_html("clear")
    assert out == f'<div class="theme-header">{t("gen.talk_theme_cleared")}</div>'


def test_user_set_card_uses_user_label():
    out = render_talk_theme_html("set", "友達の話", by_user=True)
    expected = (
        f'<div class="theme-header">{t("gen.talk_theme_set_user")}</div>'
        f'<div>友達の話</div>'
    )
    assert out == expected


def test_user_clear_card_uses_user_label():
    out = render_talk_theme_html("clear", by_user=True)
    assert out == f'<div class="theme-header">{t("gen.talk_theme_cleared_user")}</div>'


def test_theme_text_is_html_escaped():
    raw = 'a<script>alert("x")</script>&b'
    out = render_talk_theme_html("set", raw)
    assert "<script>" not in out
    assert html.escape(raw) in out


def test_unknown_action_falls_back_to_clear():
    # history.py は正規表現で set/clear のみ渡すが、防御的にclear側へ倒れること
    out = render_talk_theme_html("", "ignored")
    assert out == f'<div class="theme-header">{t("gen.talk_theme_cleared")}</div>'
