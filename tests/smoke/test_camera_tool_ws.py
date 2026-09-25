"""Smoke tests for the WS-backed capture_camera tool (Phase 3, 2026-07-16).

capture_image no longer touches OpenCV: it asks the Live Camera provider
page for one frame (request_tool_frame) and moves it into the existing
temp_captures/{character_id}/ layout. The three LLM-facing outcomes are the
contract (S14: tool result notes reach the model): success note, camera
not connected, capture failed/timeout.
"""

import pytest

import backend.shared.ambient_camera_state as acs
from backend.shared.prompt_i18n import prompt_text
from backend.tools import camera_capture as cc


@pytest.fixture(autouse=True)
def _redirect_captures(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "TEMP_CAPTURES_DIR", tmp_path / "temp_captures")


def _tool_call(reason="様子が見たい"):
    return {"name": "capture_camera", "arguments": {"reason": reason}}


def test_capture_success_moves_frame_into_character_dir(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpegbytes")
    monkeypatch.setattr(acs, "is_provider_available", lambda: True)
    monkeypatch.setattr(acs, "request_tool_frame", lambda timeout: str(frame))

    result = cc.dispatch_camera_tool("charX", _tool_call(), language="ja")

    assert result["status"] == "success"
    assert result["result"] == prompt_text("res.camera.captured", "ja")
    saved = result["image_path"]
    assert "charX" in saved
    with open(saved, "rb") as f:
        assert f.read() == b"jpegbytes"
    assert not frame.exists()  # moved, not copied


def test_capture_not_connected_note(monkeypatch):
    monkeypatch.setattr(acs, "is_provider_available", lambda: False)

    result = cc.dispatch_camera_tool("charX", _tool_call(), language="ja")

    assert result["status"] == "error"
    assert result["result"] == prompt_text("res.camera.not_connected", "ja")


def test_capture_timeout_note(monkeypatch):
    monkeypatch.setattr(acs, "is_provider_available", lambda: True)
    monkeypatch.setattr(acs, "request_tool_frame", lambda timeout: None)

    result = cc.dispatch_camera_tool("charX", _tool_call(), language="ja")

    assert result["status"] == "error"
    assert result["result"] == prompt_text("res.camera.capture_failed", "ja")


def test_unknown_tool_note_unchanged(monkeypatch):
    monkeypatch.setattr(acs, "is_provider_available", lambda: True)

    result = cc.dispatch_camera_tool("charX", {"name": "bogus", "arguments": {}}, language="ja")

    assert result["status"] == "error"
