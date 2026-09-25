"""既存キャラクター編集の「設定を読込」は、声/LLM が未設定・選択肢に無い
キャラクターでもエラーにしない(稜 Mac 実機 2026-09-25: 同梱モデルキャラクターの
読み込みで「Value: is not in the list of choices」→全項目「エラー」)。

Gradio はドロップダウンに choices 外の値(空文字を含む)を渡すとイベント全体を
失敗させるので、未設定/提供されていない値は None(未選択)+トーストで返し、
必須判定は保存時の validate_character_data に任せる(既存)。
"""

import pytest

from backend.shared.i18n import t
from ui import character_ui as cu

SBV2_UNSET = {"provider": "sbv2", "model_path": "", "config_path": "", "style_vectors_path": ""}


def _config(**over):
    cfg = {
        "character_id": "cid", "name": "Momo", "summary_text": "Model Character",
        "icon_path": "", "faster_whisper_config": {"language": "ja"},
        "model_provider": "ollama", "model_name": "", "tts_model_config": SBV2_UNSET,
        "system_prompt": "p", "motion_pngtuber_folder": "Momo",
        "elyth_system_prompt": "", "elyth_api_key": "",
    }
    cfg.update(over)
    return cfg


@pytest.fixture
def env(monkeypatch):
    """バックエンド照会と選択肢の実体を差し替え、gr.Warning を記録に置き換える。"""
    warnings = []
    holder = {"config": _config()}
    monkeypatch.setattr(cu.gr, "Warning", lambda msg, *a, **k: warnings.append(msg))
    monkeypatch.setattr(cu.backend, "load_character_config",
                        lambda cid: {"success": True, "result": dict(holder["config"])})
    monkeypatch.setattr(cu.backend, "list_ollama_models",
                        lambda: {"success": True, "result": ["qwen3:14b"]})
    monkeypatch.setattr("ui.handlers.api_settings._list_sbv2_models", lambda: ["Amamomemo"])
    monkeypatch.setattr("backend.shared.api_settings.get_all_available_models",
                        lambda ollama: [("qwen3:14b (Ollama)", "ollama::qwen3:14b")])
    monkeypatch.setattr("backend.shared.api_settings.get_elevenlabs_api_key", lambda: "")
    holder["warnings"] = warnings
    return holder


def _load(env):
    out = cu.load_character_for_edit("cid")
    assert len(out) == 12
    return out[4], out[7]  # edit_tts_dropdown, edit_ollama_dropdown


def _values(update):
    return [c[1] if isinstance(c, (tuple, list)) else c for c in update["choices"]]


def test_unset_voice_and_llm_load_as_none_with_toast(env):
    tts, model = _load(env)
    assert tts["value"] is None and "sbv2::Amamomemo" in _values(tts)
    assert model["value"] is None and "ollama::qwen3:14b" in _values(model)
    assert env["warnings"] == [t("charui.edit_missing_both")]


def test_set_values_are_kept_when_offered(env):
    env["config"] = _config(
        model_name="qwen3:14b",
        tts_model_config={"provider": "sbv2",
                          "model_path": "sbv2_models/Amamomemo/model.safetensors",
                          "config_path": "sbv2_models/Amamomemo/config.json",
                          "style_vectors_path": "sbv2_models/Amamomemo/style_vectors.npy"})
    tts, model = _load(env)
    assert tts["value"] == "sbv2::Amamomemo"
    assert model["value"] == "ollama::qwen3:14b"
    assert env["warnings"] == []


def test_saved_llm_no_longer_offered_loads_as_none(env):
    env["config"] = _config(
        model_name="gone:7b",
        tts_model_config={"provider": "sbv2",
                          "model_path": "sbv2_models/Amamomemo/model.safetensors",
                          "config_path": "c", "style_vectors_path": "s"})
    tts, model = _load(env)
    assert tts["value"] == "sbv2::Amamomemo"
    assert model["value"] is None
    assert env["warnings"] == [t("charui.edit_missing_model")]


def test_saved_voice_no_longer_offered_loads_as_none(env):
    env["config"] = _config(
        model_name="qwen3:14b",
        tts_model_config={"provider": "sbv2",
                          "model_path": "sbv2_models/Removed/model.safetensors",
                          "config_path": "c", "style_vectors_path": "s"})
    tts, model = _load(env)
    assert tts["value"] is None
    assert model["value"] == "ollama::qwen3:14b"
    assert env["warnings"] == [t("charui.edit_missing_voice")]


def test_no_selection_returns_none_not_empty_string(env):
    out = cu.load_character_for_edit("")
    assert out[4] == cu.gr.update(value=None)
    assert out[7] == cu.gr.update(value=None)


def test_dropdown_update_helper():
    upd, ok = cu._dropdown_update([("A", "a"), ("B", "b")], "b")
    assert ok and upd["value"] == "b" and upd["choices"] == [("A", "a"), ("B", "b")]
    upd, ok = cu._dropdown_update([("A", "a")], "zzz")
    assert not ok and upd["value"] is None
    upd, ok = cu._dropdown_update([("A", "a")], "")
    assert not ok and upd["value"] is None
    upd, ok = cu._dropdown_update(None, "keep")  # choices 不明: 空でなければ素通し
    assert ok and upd["value"] == "keep" and "choices" not in upd
    upd, ok = cu._dropdown_update(None, "")
    assert not ok and upd["value"] is None
