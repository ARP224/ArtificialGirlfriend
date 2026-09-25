"""
ui/conversation/command_format.py

コマンドステップ(grey-list コマンド実行)の HTML 整形を1箇所に集約する。

ライブ経路(generation.py・step dict 由来)とリロード経路(history.py・DB文字列を
正規表現でパース)が同じ整形表を二重実装しており、"interrupted" がライブ側にしか
なくリロード後に "❓" になる不一致が発生していた。真実源をここに一本化する。
"""

import html as _html

from backend.shared.i18n import t

# status -> (アイコン, ラベル)。両経路で共有する唯一の整形表。
COMMAND_STATUS_ICONS = {
    "executed": "⚡",
    "approved": "✅",
    "pending": "⏳",
    "blocked": "🚫",
    "denied": "❌",
    "error": "💥",
    "interrupted": "🔇",
}

COMMAND_STATUS_LABELS = {
    "executed": t("gen.cmd_status_success"),
    "approved": t("gen.cmd_status_success"),
    "error": t("gen.cmd_status_error"),
    "blocked": t("gen.cmd_status_blocked"),
    "denied": t("gen.cmd_status_denied"),
    "pending": t("gen.cmd_status_pending"),
    "interrupted": t("gen.cmd_status_interrupted"),
}


def render_command_step_html(status: str, command: str, reason: str, result_text: str = "") -> str:
    """Build the command-step chat HTML. Takes RAW (unescaped) strings and
    escapes them internally, so both call sites pass raw values consistently."""
    status_icon = COMMAND_STATUS_ICONS.get(status, "❓")
    status_label = COMMAND_STATUS_LABELS.get(status, "")
    result_html = ""
    if result_text:
        safe_result = _html.escape(result_text[:1000])
        result_html = f'<details><summary>{t("gen.cmd_result")}</summary><pre>{safe_result}</pre></details>'
    return (
        f'<div class="cmd-header">{status_icon} {_html.escape(command)} — {status_label}</div>'
        f'<div class="cmd-reason">{_html.escape(reason)}</div>'
        f'{result_html}'
    )
