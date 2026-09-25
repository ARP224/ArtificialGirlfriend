"""
tests/smoke/test_ollama_think_probe.py

思考を無効化できない Ollama モデル = 非対応（稜裁定 2026-08-16）。

対象: backend/llm/ollama_capabilities.py の probe_think_disable /
check_model_supported / ensure_model_supported と、
backend/conversation/character_manager.py の create/edit ゲート。

契約:
  - 判定は「think:false で投げた応答に thinking が乗るか」の実測のみ
    （/api/show では判別不能）。thinking capability が無ければプローブ無しで対応。
  - 結果は完全一致名のエントリにだけ保存・照会（Thinking 版と instruct 版は
    ベース名が同じ=ベース名フォールバックに乗せると誤適用）。
  - 同じ Ollama バージョンならキャッシュ、バージョンが変われば再プローブ。
  - 不明（停止中・capability 不明・モデル無し）は通す（fail-open）。
  - 非対応判定したモデルは即アンロード（keep_alive 0）。
All network I/O is faked — the suite never touches the network.
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

import backend.llm.ollama_capabilities as oc
from backend.shared.errors import AGError

_REAL_GET_CAPS = oc.get_caps


@pytest.fixture(autouse=True)
def _use_real_get_caps(monkeypatch):
    # conftest の autouse は get_caps をフェイクへ差し替える(ゲート系の密閉化)。
    # 本ファイルはキャッシュ実装そのものを検証するため実物へ戻す。
    monkeypatch.setattr(oc, "get_caps", _REAL_GET_CAPS)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._payload


def _wire(monkeypatch, tmp_path, *, tags, shows, chats, version="0.32.13", down=False):
    """URL で振り分ける HTTP フェイク。chats: model -> /api/chat 応答 payload。"""
    monkeypatch.setattr(oc, "CAPABILITIES_CACHE_FILE",
                        tmp_path / "ollama_capabilities.json")
    oc.reset_state()
    calls = {"tags": 0, "show": [], "chat": [], "generate": [], "version": 0}
    state = {"version": version}

    def fake_get(url, timeout=None):
        if down:
            raise requests.ConnectionError("ollama down")
        if url.endswith("/api/version"):
            calls["version"] += 1
            return _FakeResponse(200, {"version": state["version"]})
        calls["tags"] += 1
        return _FakeResponse(200, {"models": [{"name": n, "digest": d}
                                              for n, d in tags.items()]})

    def fake_post(url, json=None, timeout=None):
        if down:
            raise requests.ConnectionError("ollama down")
        if url.endswith("/api/show"):
            calls["show"].append(json["name"])
            if json["name"] not in shows:
                return _FakeResponse(404, {"error": "model not found"})
            return _FakeResponse(200, shows[json["name"]])
        if url.endswith("/api/chat"):
            calls["chat"].append(json["model"])
            assert json.get("think") is False  # プローブは必ず think:false
            if json["model"] not in chats:
                return _FakeResponse(404, {"error": "model not found"})
            return _FakeResponse(200, chats[json["model"]])
        if url.endswith("/api/generate"):
            calls["generate"].append((json["model"], json.get("keep_alive")))
            return _FakeResponse(200, {})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(oc.requests, "get", fake_get)
    monkeypatch.setattr(oc.requests, "post", fake_post)
    return calls, state


_THINKING_SHOW = {"capabilities": ["completion", "tools", "thinking"],
                  "model_info": {"qwen3vl.context_length": 262144}}
_PLAIN_SHOW = {"capabilities": ["completion", "tools"],
               "model_info": {"llama.context_length": 131072}}

_CHAT_IGNORES = {"message": {"role": "assistant", "content": "",
                             "thinking": "Okay, the user asks 2+2..."}}
_CHAT_HONORS = {"message": {"role": "assistant", "content": "4"}}
_CHAT_INLINE_THINK = {"message": {"role": "assistant",
                                  "content": "<think>\nhmm 2+2"}}


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def test_verdict_shapes():
    assert oc._think_probe_verdict(_CHAT_HONORS) is True
    assert oc._think_probe_verdict(_CHAT_IGNORES) is False
    assert oc._think_probe_verdict(_CHAT_INLINE_THINK) is False
    # 空 thinking フィールドは尊重扱い
    assert oc._think_probe_verdict({"message": {"content": "4", "thinking": ""}}) is True
    assert oc._think_probe_verdict({}) is True


# ---------------------------------------------------------------------------
# probe + cache
# ---------------------------------------------------------------------------

def test_unsupported_model_is_cached_by_exact_name_and_unloaded(monkeypatch, tmp_path):
    calls, _ = _wire(monkeypatch, tmp_path,
                     tags={"qwen3-vl:8b": "d1"},
                     shows={"qwen3-vl:8b": _THINKING_SHOW},
                     chats={"qwen3-vl:8b": _CHAT_IGNORES})
    assert oc.probe_think_disable("qwen3-vl:8b") is False
    assert calls["chat"] == ["qwen3-vl:8b"]
    assert calls["generate"] == [("qwen3-vl:8b", 0)]  # 即アンロード

    # 2回目はキャッシュ命中=HTTP なし
    assert oc.probe_think_disable("qwen3-vl:8b") is False
    assert calls["chat"] == ["qwen3-vl:8b"]

    data = json.loads((tmp_path / "ollama_capabilities.json").read_text(encoding="utf-8"))
    probe = data["models"]["qwen3-vl:8b"]["think_probe"]
    assert probe["disable_ok"] is False and probe["ollama_version"] == "0.32.13"


def test_sibling_tag_is_probed_separately_not_via_base_name(monkeypatch, tmp_path):
    # Thinking 版のエントリしか無い状態で instruct 版を照会 → get_caps は
    # ベース名フォールバックで capability を拾うが、判定は instruct 版自身へ
    # プローブして完全一致名で保存する(誤適用しない)
    calls, _ = _wire(monkeypatch, tmp_path,
                     tags={"qwen3-vl:8b": "d1", "qwen3-vl:8b-instruct": "d2"},
                     shows={"qwen3-vl:8b": _THINKING_SHOW,
                            "qwen3-vl:8b-instruct": _THINKING_SHOW},
                     chats={"qwen3-vl:8b": _CHAT_IGNORES,
                            "qwen3-vl:8b-instruct": _CHAT_HONORS})
    assert oc.probe_think_disable("qwen3-vl:8b") is False
    assert oc.probe_think_disable("qwen3-vl:8b-instruct") is True
    assert calls["chat"] == ["qwen3-vl:8b", "qwen3-vl:8b-instruct"]
    data = json.loads((tmp_path / "ollama_capabilities.json").read_text(encoding="utf-8"))
    assert data["models"]["qwen3-vl:8b"]["think_probe"]["disable_ok"] is False
    assert data["models"]["qwen3-vl:8b-instruct"]["think_probe"]["disable_ok"] is True


def test_version_change_reprobes(monkeypatch, tmp_path):
    calls, state = _wire(monkeypatch, tmp_path,
                         tags={"qwen3-vl:8b": "d1"},
                         shows={"qwen3-vl:8b": _THINKING_SHOW},
                         chats={"qwen3-vl:8b": _CHAT_IGNORES})
    assert oc.probe_think_disable("qwen3-vl:8b") is False
    state["version"] = "0.99.0"
    assert oc.probe_think_disable("qwen3-vl:8b") is False
    assert calls["chat"] == ["qwen3-vl:8b", "qwen3-vl:8b"]  # 再プローブ


def test_no_thinking_capability_needs_no_probe(monkeypatch, tmp_path):
    calls, _ = _wire(monkeypatch, tmp_path,
                     tags={"llama3.1:8b": "d1"},
                     shows={"llama3.1:8b": _PLAIN_SHOW},
                     chats={})
    assert oc.probe_think_disable("llama3.1:8b") is True
    assert calls["chat"] == []


def test_unknown_when_server_down_or_model_missing(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, tags={}, shows={}, chats={}, down=True)
    assert oc.probe_think_disable("qwen3-vl:8b") is None
    assert oc.check_model_supported("ollama", "qwen3-vl:8b") is True  # 不明=通す

    _wire(monkeypatch, tmp_path, tags={}, shows={}, chats={})
    assert oc.probe_think_disable("no-such-model:1b") is None


def test_ensure_raises_coded_error_only_for_ollama(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          tags={"qwen3-vl:8b": "d1"},
          shows={"qwen3-vl:8b": _THINKING_SHOW},
          chats={"qwen3-vl:8b": _CHAT_IGNORES})
    with pytest.raises(AGError) as ei:
        oc.ensure_model_supported("ollama", "qwen3-vl:8b")
    assert ei.value.ag_code == oc.UNSUPPORTED_MODEL_CODE == "model_thinking_unsupported"
    # API プロバイダは対象外(プローブもしない)
    oc.ensure_model_supported("anthropic", "qwen3-vl:8b")


# ---------------------------------------------------------------------------
# character_manager gate (create: always / edit: only when the model changes)
# ---------------------------------------------------------------------------

@contextmanager
def _plain_atomic(path, operation="write"):
    yield Path(path)


def _gate(monkeypatch, tmp_path, unsupported):
    import backend.conversation.character_manager as cm
    monkeypatch.setattr(cm, "CHARACTER_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setattr(cm, "MEMORY_DIR", str(tmp_path / "memory"))

    def fake_ensure(provider, model):
        if provider == "ollama" and model in unsupported:
            raise AGError(oc.UNSUPPORTED_MODEL_CODE, f"{model} unsupported", model=model)

    monkeypatch.setattr(oc, "ensure_model_supported", fake_ensure)
    return cm


def _info(name, model):
    return {"name": name, "summary_text": "", "icon_path": "",
            "tts_model_config": {}, "faster_whisper_config": {"language": "ja"},
            "system_prompt": "x", "model_provider": "ollama", "model_name": model}


def test_create_refuses_unsupported_model_before_writing(monkeypatch, tmp_path):
    cm = _gate(monkeypatch, tmp_path, unsupported={"bad:1b"})
    with pytest.raises(AGError) as ei:
        cm.create_character(_info("c1", "bad:1b"), _plain_atomic)
    assert ei.value.ag_code == oc.UNSUPPORTED_MODEL_CODE
    assert not (tmp_path / "configs").exists() or not list((tmp_path / "configs").glob("*.json"))
    # 対応モデルは作れる
    cid = cm.create_character(_info("c2", "good:1b"), _plain_atomic)
    assert list((tmp_path / "configs").glob("*.json"))
    assert cid


def test_edit_checks_only_when_model_changes(monkeypatch, tmp_path):
    cm = _gate(monkeypatch, tmp_path, unsupported=set())
    cid = cm.create_character(_info("c3", "legacy:1b"), _plain_atomic)
    # 作成後に "legacy:1b" が非対応と判明したケースを模す
    _gate(monkeypatch, tmp_path, unsupported={"legacy:1b", "bad:1b"})

    from types import SimpleNamespace
    state = SimpleNamespace(active_character_id=None)  # 非アクティブ編集=再ロード無し

    # 名前だけの編集(モデル不変)は通る=選択時の activate が最終防衛線
    cm.edit_character(state, cid, {"name": "renamed", "model_provider": "ollama",
                                   "model_name": "legacy:1b"}, _plain_atomic, lambda *a, **k: None)
    cfg = json.loads(next((tmp_path / "configs").glob("*.json")).read_text(encoding="utf-8"))
    assert cfg["name"] == "renamed"

    # 非対応モデルへの変更は拒否され、config は書き換わらない
    with pytest.raises(AGError):
        cm.edit_character(state, cid, {"name": "again", "model_provider": "ollama",
                                       "model_name": "bad:1b"}, _plain_atomic, lambda *a, **k: None)
    cfg = json.loads(next((tmp_path / "configs").glob("*.json")).read_text(encoding="utf-8"))
    assert cfg["name"] == "renamed" and cfg["model_name"] == "legacy:1b"
    assert not list((tmp_path / "configs").glob("*.bak.*"))
