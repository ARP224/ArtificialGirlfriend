"""設定>埋め込みモデル一覧 (get_embedding_model_choices) のスモーク。

契約（2026-08-14 稜GO＝get_caps統一）: Ollamaモデルは capability の
"embedding" で絞り込み、判定不能（known=False）のみ名前ヒューリスティック
（"embed" を含む）へフォールバック。リスト構築時に prefetch で先読みする
（会話一覧 get_all_available_models と同じ様式＝再pullのdigest変化検知と
負キャッシュ解除を prefetch が担う）。
"""

import pytest

from backend.shared.api_settings import get_embedding_model_choices

_MODELS = ["nomic-embed:latest", "chat:7b", "old-embed:1b", "old-chat:1b"]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """実HTTPを封じる: prefetchは呼び出し記録のみ・API設定は空・
    Ollamaモデル一覧は固定。get_caps は conftest の autouse フェイク。"""
    calls = []
    monkeypatch.setattr(
        "backend.llm.ollama_capabilities.prefetch",
        lambda models=None: calls.append(models))
    monkeypatch.setattr(
        "backend.shared.api_settings.load_api_settings", lambda: {})
    monkeypatch.setattr(
        "backend.llm.ollama_integration.list_ollama_models",
        lambda: (list(_MODELS), ""))
    return calls


def test_capability_filter_with_name_fallback(fake_ollama_caps):
    fake_ollama_caps("nomic-embed:latest", completion=False, embedding=True)
    fake_ollama_caps("chat:7b")  # completion=True / embedding=False
    # old-embed:1b / old-chat:1b は未登録=判定不能→名前フォールバック

    values = [v for _, v in get_embedding_model_choices()]

    # 判定済み: embedding capabilityの有無がそのまま採否
    # 判定不能: 名前に "embed" を含むものだけ採用
    assert values == ["ollama::nomic-embed:latest", "ollama::old-embed:1b"]


def test_prefetch_is_called_with_listed_models(_hermetic):
    get_embedding_model_choices()
    assert _hermetic == [_MODELS]
