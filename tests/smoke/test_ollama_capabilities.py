"""
tests/smoke/test_ollama_capabilities.py

Ollama capability キャッシュ (backend/llm/ollama_capabilities.py)。

契約: 2軸判定（tools/vision）の真実源。キャッシュ命中はHTTPゼロ・
digest変更でだけ再照会・capabilitiesフィールド欠落（旧Ollama）は
known=False・接続失敗は負キャッシュTTL内の再照会をしない。
All network I/O is faked — the suite never touches the network.
"""

import pytest
import requests

import backend.llm.ollama_capabilities as oc

# conftest の autouse _pin_ollama_caps は get_caps をフェイクへ差し替える
# (ゲート系テストの密閉化)。本ファイルはキャッシュ実装そのものを検証する
# ため、収集時に実物を捕まえて各テストで復元する。
_REAL_GET_CAPS = oc.get_caps


@pytest.fixture(autouse=True)
def _use_real_get_caps(monkeypatch):
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


def _wire(monkeypatch, tmp_path, tags, shows, down=False):
    """HTTPを偽装し、キャッシュファイルをtmpへ隔離。呼び出し回数を返す。"""
    monkeypatch.setattr(oc, "CAPABILITIES_CACHE_FILE",
                        tmp_path / "ollama_capabilities.json")
    oc.reset_state()
    calls = {"tags": 0, "show": []}

    def fake_get(url, timeout=None):
        calls["tags"] += 1
        if down:
            raise requests.ConnectionError("ollama down")
        payload = {"models": [{"name": n, "digest": d}
                              for n, d in tags.items()]}
        return _FakeResponse(200, payload)

    def fake_post(url, json=None, timeout=None):
        calls["show"].append(json["name"])
        if down:
            raise requests.ConnectionError("ollama down")
        if json["name"] not in shows:
            return _FakeResponse(404, {"error": "model not found"})
        return _FakeResponse(200, shows[json["name"]])

    monkeypatch.setattr(oc.requests, "get", fake_get)
    monkeypatch.setattr(oc.requests, "post", fake_post)
    return calls


_QWEN_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"qwen3.context_length": 40960},
}
_GEMMA_SHOW = {
    "capabilities": ["completion", "vision"],
    "model_info": {"gemma3.context_length": 131072},
}


def test_prefetch_then_cached_get_caps_makes_no_http(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path,
                  tags={"qwen3:14b": "sha1"}, shows={"qwen3:14b": _QWEN_SHOW})

    oc.prefetch()
    caps = oc.get_caps("qwen3:14b")

    assert caps == {"known": True, "tools": True, "vision": False,
                    "completion": True, "embedding": False,
                    "thinking": True,  # 思考無効化プローブの対象judge(2026-08-16)
                    "context_length": 40960}
    assert calls["show"] == ["qwen3:14b"]

    # キャッシュ命中はHTTPゼロ（同一プロセス2回目）
    oc.get_caps("qwen3:14b")
    assert calls["show"] == ["qwen3:14b"]

    # ファイルへ永続化されている（プロセス再起動相当 = メモリ破棄後も命中）
    oc.reset_state()
    assert oc.get_caps("qwen3:14b")["tools"] is True
    assert calls["show"] == ["qwen3:14b"]


def test_base_name_lookup_absorbs_tag_variation(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          tags={"qwen3:14b": "sha1"}, shows={"qwen3:14b": _QWEN_SHOW})
    oc.prefetch()

    assert oc.get_caps("qwen3")["tools"] is True


def test_same_digest_skips_show_and_changed_digest_refetches(
        monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path,
                  tags={"qwen3:14b": "sha1"}, shows={"qwen3:14b": _QWEN_SHOW})
    oc.prefetch()
    oc.prefetch()
    assert calls["show"] == ["qwen3:14b"]  # 同一digestは再照会しない

    # 同名タグの再pull（digest変化）→ 再照会して新しい値になる
    calls2 = _wire(monkeypatch, tmp_path,
                   tags={"qwen3:14b": "sha2"}, shows={"qwen3:14b": _GEMMA_SHOW})
    oc.prefetch()
    assert calls2["show"] == ["qwen3:14b"]
    assert oc.get_caps("qwen3:14b")["vision"] is True


def test_old_server_without_capabilities_field_is_unknown(
        monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, tags={"llama2:7b": "sha1"},
          shows={"llama2:7b": {"model_info": {}}})
    oc.prefetch()

    caps = oc.get_caps("llama2:7b")
    assert caps["known"] is False
    assert caps["tools"] is False and caps["vision"] is False


def test_lazy_fill_without_prefetch_then_prefetch_heals_digest(
        monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path,
                  tags={"qwen3:14b": "sha1"}, shows={"qwen3:14b": _QWEN_SHOW})

    # 先読みなし（更新前からの既存キャラ相当）→ 遅延照会で埋まる
    assert oc.get_caps("qwen3:14b")["tools"] is True
    assert calls["show"] == ["qwen3:14b"] and calls["tags"] == 0

    # digest未知(None)のため、次のprefetchが実digestと突き合わせて補完する
    oc.prefetch()
    assert calls["show"] == ["qwen3:14b", "qwen3:14b"]
    oc.prefetch()
    assert calls["show"] == ["qwen3:14b", "qwen3:14b"]  # 以後は安定


def test_connection_failure_negative_cache_blocks_retry(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, tags={}, shows={}, down=True)

    assert oc.get_caps("qwen3:14b")["known"] is False
    assert calls["show"] == ["qwen3:14b"]

    # TTL内の再照会はHTTPに触らない（UIを2秒timeoutで待たせない）
    assert oc.get_caps("qwen3:14b")["known"] is False
    assert calls["show"] == ["qwen3:14b"]

    # prefetch はユーザー操作起点なのでTTLを無視して試行する
    oc.prefetch()
    assert calls["tags"] == 1


def test_prefetch_prunes_uninstalled_models(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          tags={"qwen3:14b": "sha1", "gemma3:12b": "sha9"},
          shows={"qwen3:14b": _QWEN_SHOW, "gemma3:12b": _GEMMA_SHOW})
    oc.prefetch()
    assert oc.get_caps("gemma3:12b")["vision"] is True

    calls = _wire(monkeypatch, tmp_path, tags={"qwen3:14b": "sha1"},
                  shows={"qwen3:14b": _QWEN_SHOW})
    # 前のキャッシュファイルは同じtmp_pathに残っている＝reset後も読み込まれる
    oc.prefetch()
    assert calls["show"] == []  # digest不変なので再照会なし
    assert oc.get_caps("qwen3:14b")["known"] is True
    # 掃除済み（この照会は遅延fetchを試みるが404=unknownのまま）
    assert oc.get_caps("gemma3:12b")["known"] is False
