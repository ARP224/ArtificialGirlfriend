"""キャラ編集モデルドロップダウンのOllama capability注記（C9）のスモーク。

契約（2026-08-11 稜裁定）: リスト構築時に prefetch で先読みし、判定済み
モデルには "(Ollama, tools+vision)" 等の注記、判定不能は素の "(Ollama)"。
encoded value("ollama::model")は注記の有無に関わらず不変（保存値の契約）。
"""

import pytest

from backend.shared.api_settings import get_all_available_models


@pytest.fixture(autouse=True)
def _no_prefetch_http(monkeypatch):
    """prefetch は実HTTPを叩くためテストでは呼び出し記録のみに置き換える。"""
    calls = []
    monkeypatch.setattr(
        "backend.llm.ollama_capabilities.prefetch",
        lambda models=None: calls.append(models))
    monkeypatch.setattr(
        "backend.shared.api_settings.load_api_settings", lambda: {})
    return calls


def test_annotations_reflect_capabilities(fake_ollama_caps):
    fake_ollama_caps("qwen-tools:14b", tools=True, vision=False)
    fake_ollama_caps("gemma-vl:4b", tools=False, vision=True)
    fake_ollama_caps("dual:8b", tools=True, vision=True)
    fake_ollama_caps("plain:7b", tools=False, vision=False)

    choices = dict(get_all_available_models(
        ["qwen-tools:14b", "gemma-vl:4b", "dual:8b", "plain:7b", "unknown:1b"]))
    labels = {v: k for k, v in choices.items()}

    assert labels["ollama::qwen-tools:14b"] == "qwen-tools:14b (Ollama, tools)"
    assert labels["ollama::gemma-vl:4b"] == "gemma-vl:4b (Ollama, vision)"
    assert labels["ollama::dual:8b"] == "dual:8b (Ollama, tools+vision)"
    assert labels["ollama::plain:7b"] == "plain:7b (Ollama, text-only)"
    # 判定不能(旧Ollama/未照会)は素の表記のまま
    assert labels["ollama::unknown:1b"] == "unknown:1b (Ollama)"


def test_embedding_only_model_excluded_from_chat_list(fake_ollama_caps):
    """completion capabilityが無いモデル(embedding専用等)は会話一覧に出さない
    (稜裁定 2026-08-14)。判定不能モデルは従来どおり掲載(fail-open)。"""
    fake_ollama_caps("nomic-embed:latest", completion=False)
    fake_ollama_caps("chat:7b")  # completion既定True

    choices = dict(get_all_available_models(
        ["nomic-embed:latest", "chat:7b", "unknown:1b"]))
    values = set(choices.values())

    assert "ollama::nomic-embed:latest" not in values
    assert "ollama::chat:7b" in values
    assert "ollama::unknown:1b" in values


def test_prefetch_is_called_with_listed_models(_no_prefetch_http):
    get_all_available_models(["a:1b", "b:2b"])
    assert _no_prefetch_http == [["a:1b", "b:2b"]]


def test_encoded_values_unchanged_by_annotation(fake_ollama_caps):
    fake_ollama_caps("qwen-tools:14b", tools=True)
    choices = get_all_available_models(["qwen-tools:14b"])
    assert choices[-1][1] == "ollama::qwen-tools:14b"
