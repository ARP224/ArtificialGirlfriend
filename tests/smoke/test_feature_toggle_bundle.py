"""camera/ambient 一括連動（vision-only Ollamaモデル）のスモークテスト。

契約（2026-08-11 稜裁定）: vision-only（toolsなし）のOllamaモデルでは
camera単独ONが無意味（撮影ツールはtools軸が要る）なため、
- camera ON → ambient も同時ON
- ON中はどちらをOFFにしても両方OFF
- ambient側の有効化が拒否されたら camera も巻き戻して両方OFFを保つ
tools+vision両対応モデルでは従来の2段階操作（連動なし）のまま。

backend.backend（合成根に近い公開API境界）は import せず、sys.modules へ
フェイクを差して handler の呼出時 import を捕まえる（テストのために製品
コードを曲げない=conftest方針の適用）。
"""

import sys
import types

import pytest

import backend as backend_pkg
import backend.shared.feature_toggle_service as fts  # noqa: F401  ハンドラ自己登録(冪等)
from backend.shared.feature_commands import dispatch_feature_toggle


@pytest.fixture
def wire(monkeypatch):
    """フェイクbackendモジュール+記録器を配線する。"""
    calls = {"camera": [], "ambient": [], "settings": []}
    flags = {"camera_capture_enabled": False, "ambient_camera_enabled": False}

    def set_camera(v):
        flags["camera_capture_enabled"] = v
        if not v:
            # spine setter の既存カスケード(camera OFF→ambient OFF)を再現
            flags["ambient_camera_enabled"] = False
        calls["camera"].append(v)
        return {"success": True, **flags}

    def set_ambient(v):
        if v and not flags["camera_capture_enabled"]:
            # feature_state.set_ambient_camera_enabled の既存拒否経路を再現
            return {"success": False,
                    "error": "Camera must be enabled before enabling Live Camera"}
        flags["ambient_camera_enabled"] = v
        calls["ambient"].append(v)
        return {"success": True, **flags}

    fake_backend = types.SimpleNamespace(
        _backend_state=types.SimpleNamespace(
            active_character_id="nox", server_mode=False),
        load_character_config=lambda cid: {
            "model_provider": "ollama", "model_name": "gemma-vision-test"},
        set_camera_capture_enabled=set_camera,
        set_ambient_camera_enabled=set_ambient,
        get_feature_status=lambda: dict(flags),
    )
    monkeypatch.setitem(sys.modules, "backend.backend", fake_backend)
    monkeypatch.setattr(backend_pkg, "backend", fake_backend, raising=False)
    monkeypatch.setattr(fts, "update_setting",
                        lambda sec, key, val: calls["settings"].append((key, val)))
    monkeypatch.setattr(fts, "is_feature_supported", lambda f: True)
    monkeypatch.setattr(
        "backend.shared.feature_availability.get_availability", lambda: {})
    return calls, flags


def test_vision_only_camera_on_turns_both_on(wire, fake_ollama_caps):
    calls, flags = wire
    fake_ollama_caps("gemma-vision-test", tools=False, vision=True)

    result = dispatch_feature_toggle("camera_capture", True)

    assert flags == {"camera_capture_enabled": True,
                     "ambient_camera_enabled": True}
    assert result.get("success") is True
    # 両方とも永続化されている
    assert ("camera_capture_enabled", True) in calls["settings"]
    assert ("ambient_camera_enabled", True) in calls["settings"]


def test_vision_only_ambient_off_turns_both_off(wire, fake_ollama_caps):
    calls, flags = wire
    fake_ollama_caps("gemma-vision-test", tools=False, vision=True)
    dispatch_feature_toggle("camera_capture", True)

    dispatch_feature_toggle("ambient_camera", False)

    assert flags == {"camera_capture_enabled": False,
                     "ambient_camera_enabled": False}
    assert ("camera_capture_enabled", False) in calls["settings"]


def test_vision_only_camera_off_turns_both_off(wire, fake_ollama_caps):
    calls, flags = wire
    fake_ollama_caps("gemma-vision-test", tools=False, vision=True)
    dispatch_feature_toggle("camera_capture", True)

    dispatch_feature_toggle("camera_capture", False)

    assert flags == {"camera_capture_enabled": False,
                     "ambient_camera_enabled": False}
    # 既存カスケードの永続化(ambient側)も生きている
    assert ("ambient_camera_enabled", False) in calls["settings"]


def test_dual_capable_model_keeps_two_step_behavior(wire, fake_ollama_caps):
    calls, flags = wire
    fake_ollama_caps("gemma-vision-test", tools=True, vision=True)

    dispatch_feature_toggle("camera_capture", True)
    assert flags["ambient_camera_enabled"] is False  # 連動しない

    dispatch_feature_toggle("ambient_camera", True)
    dispatch_feature_toggle("ambient_camera", False)
    # ambient単独OFFでcameraは維持(従来挙動)
    assert flags["camera_capture_enabled"] is True


def test_bundle_rolls_back_camera_when_ambient_rejected(
        wire, fake_ollama_caps, monkeypatch):
    calls, flags = wire
    fake_ollama_caps("gemma-vision-test", tools=False, vision=True)
    # ambient側だけ有効化拒否になる状況を作る
    monkeypatch.setattr(
        "backend.shared.feature_availability.get_availability",
        lambda: {"ambient_camera": "avail.reason_ollama_unknown"})

    result = dispatch_feature_toggle("camera_capture", True)

    assert result.get("success") is False
    # 片肺ON(camera単独)は残らない=両方OFFへ巻き戻し
    assert flags == {"camera_capture_enabled": False,
                     "ambient_camera_enabled": False}
    assert ("camera_capture_enabled", False) in calls["settings"]
