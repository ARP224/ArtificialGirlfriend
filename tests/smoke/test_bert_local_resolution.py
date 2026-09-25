"""
tests/smoke/test_bert_local_resolution.py

_resolve_bert_model — BERT 指定のローカル解決(no network, no model load)。

Load-bearing assertions:
- ① bert/<lang>/ (repo直下) に config.json があればそのフォルダを最優先で返す
  (デフォルトモデル使用時のみ。独自モデル名指定時はスキップ)。
- ② フォルダが無ければ HF キャッシュの snapshot パス(local_files_only)を返す。
- ③ キャッシュも無ければハブIDをそのまま返す(従来挙動への劣化)。
  ヘルパーはどんな例外でも呼び出し元を壊さない。
"""

import huggingface_hub
import pytest

import audio_output.audio_output as tts_mod

HUB_ID = "ku-nlp/deberta-v2-large-japanese-char-wwm"


@pytest.fixture
def no_local_folder(tmp_path, monkeypatch):
    """①が成立しない空の bert ベースフォルダを指す状態にする。"""
    monkeypatch.setattr(tts_mod, "DEFAULT_BERT_BASE_PATH", str(tmp_path / "bert"))
    return tmp_path


def test_local_folder_wins_for_default_model(tmp_path, monkeypatch):
    local_dir = tmp_path / "bert" / "ja"
    local_dir.mkdir(parents=True)
    (local_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tts_mod, "DEFAULT_BERT_BASE_PATH", str(tmp_path / "bert"))
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download",
        lambda *a, **k: pytest.fail("folder hit must not consult HF cache"),
    )

    resolved = tts_mod._resolve_bert_model(HUB_ID, "ja", is_default=True)
    assert resolved == str(local_dir)


def test_local_folder_skipped_for_custom_model(tmp_path, monkeypatch):
    """独自 huggingface_model_name 指定時は言語別フォルダを使わない。"""
    local_dir = tmp_path / "bert" / "ja"
    local_dir.mkdir(parents=True)
    (local_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tts_mod, "DEFAULT_BERT_BASE_PATH", str(tmp_path / "bert"))
    snapshot = tmp_path / "snapshot"
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download", lambda *a, **k: str(snapshot)
    )

    resolved = tts_mod._resolve_bert_model("someone/custom-bert", "ja", is_default=False)
    assert resolved == str(snapshot)


def test_cache_snapshot_path_used_offline(no_local_folder, monkeypatch):
    calls = {}

    def fake_snapshot_download(repo_id, **kwargs):
        calls["repo_id"] = repo_id
        calls["kwargs"] = kwargs
        return "C:/fake/hub/snapshots/abc"

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    resolved = tts_mod._resolve_bert_model(HUB_ID, "ja", is_default=True)
    assert resolved == "C:/fake/hub/snapshots/abc"
    assert calls["repo_id"] == HUB_ID
    # ネットワーク接続ゼロの保証はこのフラグが担う(契約)
    assert calls["kwargs"].get("local_files_only") is True


def test_falls_back_to_hub_id_when_uncached(no_local_folder, monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("not in cache")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", raise_not_found)

    resolved = tts_mod._resolve_bert_model(HUB_ID, "ja", is_default=True)
    assert resolved == HUB_ID
