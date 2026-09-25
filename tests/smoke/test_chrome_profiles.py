"""
tests/smoke/test_chrome_profiles.py

launcher/chrome_profiles.py の表示名解決の契約テスト。
形は Mac 実機の Local State (2026-07-22 還元#12 採取・値は匿名化) に合わせる:
未リネームのプロファイルは name が "Person 1" のままで、
using_default_name はキー自体が存在しないことがある。
"""

import json

from launcher import chrome_profiles


def _write_local_state(tmp_path, info_cache):
    p = tmp_path / "Local State"
    p.write_text(
        json.dumps({"profile": {"info_cache": info_cache}}),
        encoding="utf-8",
    )
    return p


def _profiles(tmp_path, monkeypatch, info_cache):
    path = _write_local_state(tmp_path, info_cache)
    monkeypatch.setattr(chrome_profiles, "local_state_path", lambda: path)
    return chrome_profiles.list_chrome_profiles()


def test_default_name_without_flag_uses_gaia_name(tmp_path, monkeypatch):
    # Mac実データの形: using_default_name キー無し + 既定名 "Person 1"
    result = _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "Person 1", "gaia_name": "Taro Yamada",
                    "user_name": "taro@example.com"},
        "Profile 3": {"name": "Ryo", "gaia_name": "Ryo"},
    })
    assert result == [
        {"folder": "Default", "display_name": "Taro Yamada"},
        {"folder": "Profile 3", "display_name": "Ryo"},
    ]


def test_using_default_name_flag_uses_gaia_name(tmp_path, monkeypatch):
    result = _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "Person 1", "gaia_name": "Taro Yamada",
                    "using_default_name": True},
        "Profile 1": {"name": "Work"},
    })
    assert result[0]["display_name"] == "Taro Yamada"


def test_japanese_default_name_uses_gaia_name(tmp_path, monkeypatch):
    result = _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "ユーザー 1", "gaia_name": "Taro Yamada"},
        "Profile 1": {"name": "Work"},
    })
    assert result[0]["display_name"] == "Taro Yamada"


def test_custom_name_wins_over_gaia_name(tmp_path, monkeypatch):
    # リネーム済みプロファイルはローカル名を維持 (Windows現行表示の維持)
    result = _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "Work", "gaia_name": "Taro Yamada"},
        "Profile 1": {"name": "Person 1", "gaia_name": "Hanako"},
    })
    assert result == [
        {"folder": "Default", "display_name": "Work"},
        {"folder": "Profile 1", "display_name": "Hanako"},
    ]


def test_default_name_without_gaia_stays(tmp_path, monkeypatch):
    # 未サインインの既定名プロファイルは "Person 2" のまま (Chromeと同じ)
    result = _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "Ryo", "gaia_name": "Ryo"},
        "Profile 2": {"name": "Person 2"},
    })
    assert result[1]["display_name"] == "Person 2"


def test_single_profile_hides_dropdown(tmp_path, monkeypatch):
    assert _profiles(tmp_path, monkeypatch, {
        "Default": {"name": "Person 1", "gaia_name": "Taro Yamada"},
    }) == []


def test_missing_local_state_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chrome_profiles, "local_state_path",
        lambda: tmp_path / "no-such-file")
    assert chrome_profiles.list_chrome_profiles() == []
