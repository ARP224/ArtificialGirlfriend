"""
backend/shared/prompt_token_display.py

プロンプトログ上部のトークン数表示（2026-08-14 稜裁定=1本表示）の
値選択とHTML文字列化。真実源はここ1箇所:
- 消費者①: backend.get_last_llm_prompt（UI初期値のpull経路）
- 消費者②: llm各統合モジュールのWS配信（push経路。gr.Timer経由の可視要素
  更新はskip+show_progress=hiddenでもちらつく既知問題のため、表示更新は
  ws_client_js の 'prompt_token_info' アクションによるDOM直接更新で行う）

選択規則: Ollamaは「実測(prompt_eval_count)と推定の大きい方」。KVキャッシュ
再利用ターンの実測は新規評価分のみ=フル長を下回るが、キャッシュは
コンテキスト枠を占有し続ける（計算を省くだけで容量は省かない）ため、
num_ctx判断の基準はフル長=その場合は推定で補う。APIはusage実測1本。
"""

import html as _html
import re
from typing import Any, Dict, Optional, Tuple

_BOLD_MD = re.compile(r"\*\*(.+?)\*\*")


def select_ollama_display(est: Optional[int],
                          actual: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
    """Ollamaの表示値を選ぶ: (count, source)。sourceは'actual'|'estimated'|None。"""
    est = est or 0
    actual = actual or 0
    if actual and actual >= est:
        return actual, "actual"
    if est:
        return est, "estimated"
    if actual:
        return actual, "actual"
    return None, None


def format_token_line(tokens: Optional[Dict[str, Any]]) -> str:
    """tokens（get_last_llm_promptの"tokens"形）→ 表示用HTML1行。

    i18n文字列の**強調**は<b>へ変換する（表示先はJS専有divのinnerHTML）。
    表示できる情報が無ければ空文字（divは空のまま）。
    """
    from backend.shared.i18n import t
    if not tokens:
        return ""

    if tokens.get("provider") == "ollama":
        count = tokens.get("count")
        if count is None:
            return ""
        source = (t('promptlog.src_actual') if tokens.get("source") == "actual"
                  else t('promptlog.src_est'))
        num_ctx = tokens.get("num_ctx")
        if num_ctx:
            line = t('promptlog.tokens_ollama', count=f"{count:,}",
                     source=source, num_ctx=f"{num_ctx:,}")
        else:
            line = t('promptlog.tokens_ollama_noctx', count=f"{count:,}",
                     source=source)
        if tokens.get("source") == "estimated":
            line += "<br>" + t('promptlog.tokens_note_est')
        return _BOLD_MD.sub(r"<b>\1</b>", line)

    # API providers
    model = _html.escape(tokens.get("model") or "")
    count = tokens.get("count")
    if count:
        line = t('promptlog.tokens_api', count=f"{count:,}", model=model)
    else:
        line = t('promptlog.tokens_api_pending', model=model)
    return _BOLD_MD.sub(r"<b>\1</b>", line)
