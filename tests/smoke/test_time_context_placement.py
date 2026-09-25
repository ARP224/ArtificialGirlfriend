"""時刻注記の配置回帰テスト(2026-08-01 稜裁定: system先頭→送信時の最新user
メッセージ先頭・非永続へ移設)。

プロンプトゴールデン(test_prompt_messages)は _build_prompt_messages 単体を
見るためuserメッセージを含まない。「送信コピーにだけ時刻が付き、保存履歴
には残らない」という移設の本体はゴールデンでは守れないので、flowゴールデン
と同じハーネス(FakeLLMがinvokeに渡ったmessagesを記録)でここに固定する。
"""

from __future__ import annotations
from collections import OrderedDict

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # make conftest importable
from conftest import FakeLLM, reply  # noqa: E402

_FIXT = Path(__file__).parent.parent / "fixtures" / "characters"

# conftest.FIXED_DATETIME (2026-06-20 14:30) を build_current_time_context の
# 書式で表したもの(書式は移設前のsystem先頭時代とバイト同一)
TIME_PREFIX = "(Current time: 2026/06/20(Sat) 14:30)\n\n"


def test_time_prefix_on_sent_copy_only(
    frozen_time, patch_leaves, control_trace, make_state, make_cm
):
    config = json.loads(
        (_FIXT / "s1_ollama_minimal.json").read_text(encoding="utf-8"))
    patch_leaves()
    llm = FakeLLM(script=[reply("こんにちは、今日はどんな一日だった？")])
    state = make_state(
        conversation_active=True, active_llm_cache=OrderedDict({"nox": llm}))
    cm = make_cm(state, config)

    result = cm.generate_reply("やあ", character_id="nox")
    assert result.get("success") is True

    # 送信された最終userメッセージには時刻が前置されている
    sent = llm.calls[0]["messages"]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"] == TIME_PREFIX + "やあ"
    # systemには時刻が入っていない(旧方式の名残ゼロ)
    assert sent[0]["role"] == "system"
    assert "(Current time:" not in sent[0]["content"]
    # 保存側(memory_manager不在時のフォールバック短期バッファ)は生のまま
    buffered = [m for m in state.short_term_buffer.get("nox", [])
                if m["role"] == "user"]
    assert buffered, "user message was not buffered"
    assert buffered[-1]["content"] == "やあ"
