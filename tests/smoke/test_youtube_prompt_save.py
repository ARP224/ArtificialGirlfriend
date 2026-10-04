"""ui/handlers/youtube.py — the tab's system-prompt editor must surface a
failed character-config save. backend.edit_character returns
{success: False} instead of raising; the save handler used to ignore the
return value and report "settings saved" even when the prompt was not written.
"""

import pytest

import backend
from ui.handlers import youtube as yt_ui


def test_prompt_save_failure_raises(monkeypatch):
    monkeypatch.setattr(yt_ui, "_load_char_youtube_prompt", lambda cid: "old")
    monkeypatch.setattr(
        backend, "edit_character",
        lambda cid, updates: {"success": False, "error": "access denied",
                              "error_type": "PERMISSION"})
    with pytest.raises(RuntimeError, match="access denied"):
        yt_ui._save_char_youtube_prompt("char1", "new prompt")


def test_prompt_save_writes_only_when_changed(monkeypatch):
    calls = []
    monkeypatch.setattr(yt_ui, "_load_char_youtube_prompt", lambda cid: "old")
    monkeypatch.setattr(
        backend, "edit_character",
        lambda cid, updates: calls.append((cid, updates)) or {"success": True})
    yt_ui._save_char_youtube_prompt("char1", " new prompt ")
    assert calls == [("char1", {"youtube_system_prompt": "new prompt"})]
    # 無変更は書かない（edit_character はアクティブキャラの再activateを伴う）
    yt_ui._save_char_youtube_prompt("char1", "old")
    assert len(calls) == 1
