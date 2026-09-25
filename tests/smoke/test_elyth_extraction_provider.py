"""Contract: ELYTH post-session memory extraction runs for ALL providers.

対象: backend/elyth/elyth_relationship_manager.run_elyth_memory_extraction。
旧実装は Ollama を無条件 return でスキップしていた（ELYTH API専用時代の残骸・
2026-08-29 撤去）。このテストは provider="ollama" でも抽出が実行され、
関係性ファイルと長期記憶断片の両方が保存されることを固定する。
LLM/セッションログは monkeypatch（conftest 様式=製品コードは曲げない）。
"""

import json
from types import SimpleNamespace

import pytest

from backend.elyth import elyth_relationship_manager as erm

CID = "test_char"


class _FakeLLM:
    def __init__(self, payload: str):
        self._payload = payload
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self._payload)


class _FakeMemoryManager:
    def __init__(self):
        self.saved = []

    def add_memory_manual(self, category, content):
        self.saved.append((category, content))
        return {"success": True}


@pytest.fixture
def rel_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(erm, "ELYTH_RELATIONSHIP_DIR", tmp_path)
    return tmp_path


def _run_extraction(monkeypatch, provider: str, fake_llm: _FakeLLM):
    """Wire fakes into the call-time import seams and run one extraction."""
    captured = {}

    def fake_create_llm_client(**kwargs):
        captured.update(kwargs)
        return {"success": True, "response": fake_llm}

    from backend.llm import api_integration
    monkeypatch.setattr(api_integration, "create_llm_client", fake_create_llm_client)

    from backend.elyth import elyth_memory
    monkeypatch.setattr(
        elyth_memory, "load_session_logs",
        lambda character_id, limit=3: [
            {"timestamp": "2026-08-29T00:00:00", "turns": [
                {"content": "posted hello on ELYTH", "metadata": {}},
            ]},
        ],
    )

    mm = _FakeMemoryManager()
    state = SimpleNamespace(memory_managers={CID: mm})
    config = {"model_provider": provider, "model_name": "test-model"}
    erm.run_elyth_memory_extraction(state, CID, config)
    return captured, mm


def test_extraction_runs_for_ollama_provider(monkeypatch, rel_dir):
    payload = json.dumps({
        "relationships": {"@aituber": "creative and friendly"},
        "memories": ["Chatted with @aituber about music on ELYTH"],
    })
    fake_llm = _FakeLLM(payload)

    captured, mm = _run_extraction(monkeypatch, "ollama", fake_llm)

    assert captured.get("model_provider") == "ollama"
    assert fake_llm.calls, "extraction LLM call must run for Ollama characters"
    assert mm.saved == [("elyth", "Chatted with @aituber about music on ELYTH")]
    saved = json.loads((rel_dir / f"{CID}.json").read_text(encoding="utf-8"))
    assert saved["relationships"]["@aituber"] == "creative and friendly"


def test_extraction_runs_for_api_provider(monkeypatch, rel_dir):
    payload = json.dumps({"relationships": {}, "memories": ["API path memory"]})
    fake_llm = _FakeLLM(payload)

    captured, mm = _run_extraction(monkeypatch, "anthropic", fake_llm)

    assert captured.get("model_provider") == "anthropic"
    assert mm.saved == [("elyth", "API path memory")]
