"""ELYTHセッション実行可否判定(backend/elyth/elyth_availability.py)の契約テスト。

契約(稜裁定 2026-08-15):
- APIプロバイダは常に実行可(capability照会手段が無い=対応前提)
- Ollamaは tools capability 必須。確定非対応= "no_tools"(強制OFF対象)、
  判定不能= "unknown"(fail-closedスキップ・ON設定温存)
- get_caps は conftest の autouse フェイク(未登録モデル=判定不能)
"""

from backend.elyth.elyth_availability import (
    BLOCK_NO_TOOLS,
    BLOCK_UNKNOWN,
    block_reason,
)


def test_api_providers_always_allowed():
    for provider in ["anthropic", "openai", "google", "xai"]:
        assert block_reason(provider, "any-model") is None


def test_ollama_tools_capable_allowed(fake_ollama_caps):
    fake_ollama_caps("qwen-tools-test", tools=True)
    assert block_reason("ollama", "qwen-tools-test") is None


def test_ollama_no_tools_blocked(fake_ollama_caps):
    # vision-onlyモデル(スクショの test(vision) 相当)は確定非対応
    fake_ollama_caps("gemma-vision-test", tools=False, vision=True)
    assert block_reason("ollama", "gemma-vision-test") == BLOCK_NO_TOOLS


def test_ollama_unknown_is_fail_closed():
    assert block_reason("ollama", "never-probed-model") == BLOCK_UNKNOWN


def test_ollama_empty_model_name_is_unknown():
    assert block_reason("ollama", "") == BLOCK_UNKNOWN
    assert block_reason("ollama", None) == BLOCK_UNKNOWN
