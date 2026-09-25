"""
tests/smoke/test_elyth_instructions_i18n.py

ELYTH共通指示文の言語追従ガード。
旧実装は初回起動時のUI言語でデフォルト文言を user_settings.json に焼き込み、
以後UI言語を変えても追従しなかった(2026-07-31根治)。現行仕様:
- 未編集 = キー無し。使用時に resolve_elyth_instructions がUI言語で解決
- 編集値(空文字含む)はそのまま尊重
- 過去の焼き込み(いずれかの言語の現行原文とstrip一致)は起動時にキー削除へ移行
"""

import json

import pytest

from backend.shared import settings_store


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """user_settings.json を一時ファイルへ差し替え(キャッシュも無効化)。"""
    path = tmp_path / "user_settings.json"
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", path)
    monkeypatch.setattr(settings_store, "_settings_cache", None)
    monkeypatch.setattr(settings_store, "_settings_cache_key", None)
    return path


def _write(path, instructions):
    path.write_text(
        json.dumps({"elyth": {"instructions": instructions}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_baked_default_is_migrated_to_key_removal(isolated_settings):
    from backend.elyth.elyth_session_manager import _migrate_unedited_default_instructions
    from backend.shared.prompt_i18n import prompt_section

    _write(isolated_settings, prompt_section("elyth_default_instructions", "ja"))
    _migrate_unedited_default_instructions()
    assert settings_store.get_setting("elyth", "instructions", None) is None


def test_custom_instructions_survive_migration(isolated_settings):
    from backend.elyth.elyth_session_manager import _migrate_unedited_default_instructions

    _write(isolated_settings, "カスタム指示文")
    _migrate_unedited_default_instructions()
    assert settings_store.get_setting("elyth", "instructions", None) == "カスタム指示文"


def test_unset_resolves_to_ui_language_default(isolated_settings, monkeypatch):
    from backend.elyth import elyth_session_manager as esm
    from backend.shared import i18n
    from backend.shared.prompt_i18n import prompt_section

    monkeypatch.setattr(i18n, "current_language", lambda: "en")
    assert esm.resolve_elyth_instructions() == prompt_section(
        "elyth_default_instructions", "en")


def test_custom_value_wins_over_catalog(isolated_settings):
    from backend.elyth.elyth_session_manager import resolve_elyth_instructions

    _write(isolated_settings, "custom text")
    assert resolve_elyth_instructions() == "custom text"
