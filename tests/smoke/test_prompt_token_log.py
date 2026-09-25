"""プロンプトログのトークン数表示のスモーク。

契約（2026-08-14 稜裁定）: 表示は1本。
- Ollama: 「実測(prompt_eval_count)と推定の大きい方」。KVキャッシュ再利用
  ターンの実測は新規評価分のみ＝フル値を下回るため、その場合は推定で補う
  （KVキャッシュはコンテキスト枠を占有し続ける＝num_ctx判断の基準はフル長）。
- API: 応答usageの実測1本（常にフル値・推定なし）。応答未着はpending。
- 取り違えガード: 保存時のseq一致時のみ実測を追記（並行リクエスト対策）。
ネットワークには一切触れない（レコード関数を直接呼ぶ）。
"""

import backend.llm.api_integration as api_mod
import backend.llm.ollama_integration as oi
from backend.backend import get_last_llm_prompt


def _ollama_payload(content: str, num_ctx: int = 32000) -> dict:
    return {
        "model": "test:1b",
        "messages": [{"role": "system", "content": content}],
        "options": {"num_ctx": num_ctx},
    }


def test_ollama_save_records_estimate_and_resets_actual():
    seq = oi._save_last_request_json(_ollama_payload("hello world " * 50))
    rec = oi.get_last_ollama_request_json()
    assert seq > 0
    assert rec["est_tokens"] and rec["est_tokens"] > 0
    assert rec["num_ctx"] == 32000
    assert rec["actual_tokens"] is None

    oi._attach_last_ollama_actual_tokens(seq, 4321)
    assert oi.get_last_ollama_request_json()["actual_tokens"] == 4321


def test_ollama_stale_seq_attach_is_dropped():
    seq_old = oi._save_last_request_json(_ollama_payload("first"))
    oi._save_last_request_json(_ollama_payload("second"))
    oi._attach_last_ollama_actual_tokens(seq_old, 9999)
    assert oi.get_last_ollama_request_json()["actual_tokens"] is None


def test_ollama_selection_estimated_when_cache_reuse_shrinks_actual():
    # 実測(部分値) < 推定 → 推定を表示（sourceで区別）
    seq = oi._save_last_request_json(_ollama_payload("word " * 500))
    est = oi.get_last_ollama_request_json()["est_tokens"]
    oi._attach_last_ollama_actual_tokens(seq, 10)

    tokens = get_last_llm_prompt()["tokens"]
    assert tokens["provider"] == "ollama"
    assert tokens["count"] == est and tokens["source"] == "estimated"
    assert tokens["num_ctx"] == 32000


def test_ollama_selection_actual_when_full_eval():
    # 実測 >= 推定（フル評価ターン）→ 実測を表示
    seq = oi._save_last_request_json(_ollama_payload("short"))
    oi._attach_last_ollama_actual_tokens(seq, 5000)

    tokens = get_last_llm_prompt()["tokens"]
    assert tokens["count"] == 5000 and tokens["source"] == "actual"


def test_api_branch_pending_then_actual():
    seq = api_mod._save_last_api_request_json(
        {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
        model_label="gpt-test (OpenAI)")

    tokens = get_last_llm_prompt()["tokens"]
    assert tokens["provider"] == "api"
    assert tokens["pending"] is True and tokens["count"] is None
    assert tokens["model"] == "gpt-test (OpenAI)"

    api_mod._attach_last_api_actual_tokens(seq, 2203)
    tokens = get_last_llm_prompt()["tokens"]
    assert tokens["pending"] is False
    assert tokens["count"] == 2203 and tokens["source"] == "actual"


def test_select_ollama_display_rules():
    from backend.shared.prompt_token_display import select_ollama_display
    assert select_ollama_display(1000, 0) == (1000, "estimated")
    assert select_ollama_display(1000, 150) == (1000, "estimated")  # KV再利用
    assert select_ollama_display(1000, 1200) == (1200, "actual")    # フル評価
    assert select_ollama_display(0, 500) == (500, "actual")
    assert select_ollama_display(None, None) == (None, None)


def test_format_token_line_is_html_not_markdown():
    # 表示先はJS専有divのinnerHTML（WS直接更新=ちらつき対策）なので
    # markdownの**はHTMLの<b>へ変換されていること
    from backend.shared.prompt_token_display import format_token_line
    line = format_token_line({"provider": "ollama", "count": 1234,
                              "source": "actual", "num_ctx": 32000,
                              "model": None})
    assert "1,234" in line and "32,000" in line
    assert "<b>" in line and "**" not in line

    est_line = format_token_line({"provider": "ollama", "count": 1234,
                                  "source": "estimated", "num_ctx": 32000,
                                  "model": None})
    assert "<br>" in est_line  # KVキャッシュ注記つき

    api_line = format_token_line({"provider": "api", "count": 99,
                                  "source": "actual", "num_ctx": None,
                                  "model": "gpt <x> (OpenAI)"})
    assert "&lt;x&gt;" in api_line  # modelはHTMLエスケープされる
    assert format_token_line(None) == ""


def test_api_usage_extractor_covers_three_provider_shapes():
    assert api_mod._extract_input_tokens({"usage": {"input_tokens": 11}}) == 11
    assert api_mod._extract_input_tokens({"usage": {"prompt_tokens": 22}}) == 22
    assert api_mod._extract_input_tokens(
        {"usageMetadata": {"promptTokenCount": 33}}) == 33
    assert api_mod._extract_input_tokens({}) == 0
