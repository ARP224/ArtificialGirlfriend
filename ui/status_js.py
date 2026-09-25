"""
ui/status_js.py

通知系Markdownの「自動消去」配線用JSを生成する共有の葉モジュール。

もともと ui/app.py に _status_auto_hide_js / _status_show_js として定義され、
ui/admin_app.py が同型をインライン複製していた（admin は合成根 app.py を
import できないため）。合成根に依存しない葉として切り出し、両者が
ここから import する（mobile_app.py の同型はエラー時のみ表示という
別挙動のためインラインのまま＝共通化しない）。

使い方（2点セット・稜依頼 2026-08-02）:
- 単発の通知イベント: チェーン末尾に
  .then(fn=lambda: None, js=status_auto_hide_js('<elem_id>'), queue=False)
- 多段チェーンで同じ欄を書き換える場合: 先頭に status_show_js で
  前回の隠しタイマーを解除し、末尾で status_auto_hide_js を武装する。
"""

# 通知を自動で隠すまでのミリ秒（稜裁定 2026-08-15: 全UIで5秒に統一）
STATUS_AUTO_HIDE_MS = 5000


def status_auto_hide_js(elem_id: str) -> str:
    """通知系Markdown(elem_id)を表示から一定時間で自動的に隠す配線用JS。

    表示はGradio側(visible=True)・非表示はJSのinline styleで書き手を分離。
    通知を出す各イベントの .then に載せ、実行のたびにinline上書きを解除
    (=再表示)してからタイマーを再武装する。同一文言の連続更新でsvelteが
    DOM書き換えをスキップしても .then は必ず走るので再表示が保証される。
    """
    return f"""
    () => {{
        const el = document.getElementById('{elem_id}');
        if (!el) return;
        el.style.removeProperty('display');
        window._statusHideTimers = window._statusHideTimers || {{}};
        clearTimeout(window._statusHideTimers['{elem_id}']);
        window._statusHideTimers['{elem_id}'] = setTimeout(() => {{
            el.style.display = 'none';
        }}, {STATUS_AUTO_HIDE_MS});
    }}
    """


def status_show_js(elem_id: str) -> str:
    """通知系Markdownの自動隠しを解除して再表示だけする(チェーン先頭用)。

    複数ステップで同じステータスを書き換えるチェーン(テキスト送信の
    送信済み→生成中→結果)では、前回の隠しタイマーが途中で発火すると
    中間表示が不可視になる。先頭で解除・末尾で status_auto_hide_js を
    武装する2点セットで使う(稜依頼 2026-08-02)。
    """
    return f"""
    () => {{
        const el = document.getElementById('{elem_id}');
        if (!el) return;
        el.style.removeProperty('display');
        if (window._statusHideTimers) clearTimeout(window._statusHideTimers['{elem_id}']);
    }}
    """
