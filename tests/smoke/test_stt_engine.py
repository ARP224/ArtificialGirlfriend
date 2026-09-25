"""
tests/smoke/test_stt_engine.py

STT engine dispatch (audio_input) — no network, no local model load.

Load-bearing assertions:
- In API mode transcribe_audio/transcribe_file never touch the local model
  (ensure_model_loaded is patched to explode).
- Silence raises the SAME "No speech detected" error BEFORE any upload
  (identical UX to the local route, no wasted API calls).
- numpy_to_wav_bytes produces a decodable 16 kHz 16-bit mono WAV.
- unload_model is idempotent and does not touch recording state.
"""

import io
import wave

import numpy as np
import pytest

import audio_input.audio_input as stt_mod
from audio_input import openai_stt
from audio_input.audio_input import AudioInputManager


def _api_manager(monkeypatch, api_model="whisper-1"):
    mgr = AudioInputManager()
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("openai", api_model))
    monkeypatch.setattr(
        mgr, "ensure_model_loaded",
        lambda *a, **k: pytest.fail("API mode must not load the local model"))
    return mgr


def test_numpy_to_wav_bytes_roundtrip():
    sr = 16000
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)

    wav_bytes = openai_stt.numpy_to_wav_bytes(tone, sr)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == sr
        assert wav_file.getnframes() == sr
        frames = np.frombuffer(wav_file.readframes(sr), dtype=np.int16)
    # Amplitude survives the int16 round trip
    assert abs(frames.max() / 32767 - 0.5) < 0.01


def test_api_dispatch_uses_api_and_clears_buffer(monkeypatch):
    mgr = _api_manager(monkeypatch)
    mgr.language = "ja"
    mgr.audio_buffer = [np.full(16000, 0.1, dtype=np.float32)]

    captured = {}

    def fake_transcribe_bytes(audio_bytes, filename, api_key, model, language=None, **kw):
        captured.update(filename=filename, api_key=api_key, model=model,
                        language=language, size=len(audio_bytes))
        return " こんにちは "

    monkeypatch.setattr(openai_stt, "transcribe_bytes", fake_transcribe_bytes)
    monkeypatch.setattr(
        "backend.shared.api_settings.load_api_settings",
        lambda: {"openai": {"api_key": "sk-test"}})

    text = mgr.transcribe_audio()

    assert text == "こんにちは"
    assert captured["model"] == "whisper-1"
    assert captured["language"] == "ja"
    assert captured["api_key"] == "sk-test"
    assert captured["filename"] == "audio.wav"
    assert mgr.audio_buffer == []  # cleared only after success


def test_api_silence_raises_before_upload(monkeypatch):
    mgr = _api_manager(monkeypatch)
    mgr.audio_buffer = [np.zeros(16000, dtype=np.float32)]

    monkeypatch.setattr(
        openai_stt, "transcribe_bytes",
        lambda *a, **k: pytest.fail("silent audio must not be uploaded"))

    with pytest.raises(RuntimeError, match="No speech detected"):
        mgr.transcribe_audio()
    # Buffer preserved on failure (same as the local route)
    assert len(mgr.audio_buffer) == 1


def test_api_missing_key_raises_clear_error(monkeypatch):
    mgr = _api_manager(monkeypatch)
    mgr.audio_buffer = [np.full(16000, 0.1, dtype=np.float32)]
    monkeypatch.setattr(
        "backend.shared.api_settings.load_api_settings",
        lambda: {"openai": {"api_key": ""}})

    with pytest.raises(RuntimeError, match="OpenAI API key is not set"):
        mgr.transcribe_audio()


def test_api_transcribe_file_uploads_bytes(monkeypatch, tmp_path):
    mgr = _api_manager(monkeypatch, api_model="gpt-4o-mini-transcribe")
    webm = tmp_path / "clip.webm"
    webm.write_bytes(b"\x1a\x45\xdf\xa3fake-webm")

    captured = {}

    def fake_transcribe_bytes(audio_bytes, filename, api_key, model, language=None, **kw):
        captured.update(audio_bytes=audio_bytes, filename=filename, model=model)
        return "browser text"

    monkeypatch.setattr(openai_stt, "transcribe_bytes", fake_transcribe_bytes)
    monkeypatch.setattr(
        "backend.shared.api_settings.load_api_settings",
        lambda: {"openai": {"api_key": "sk-test"}})

    assert mgr.transcribe_file(str(webm)) == "browser text"
    assert captured["audio_bytes"].startswith(b"\x1a\x45\xdf\xa3")
    assert captured["filename"] == "clip.webm"
    assert captured["model"] == "gpt-4o-mini-transcribe"


def test_transcribe_bytes_error_mapping(monkeypatch):
    from types import SimpleNamespace
    import requests

    def _post_status(code):
        return lambda *a, **k: SimpleNamespace(
            status_code=code, json=lambda: {"error": {"message": "boom"}})

    monkeypatch.setattr(requests, "post", _post_status(401))
    with pytest.raises(RuntimeError, match="invalid or access denied"):
        openai_stt.transcribe_bytes(b"x", "a.wav", "sk", "whisper-1")

    monkeypatch.setattr(requests, "post", _post_status(429))
    with pytest.raises(RuntimeError, match="rate limit"):
        openai_stt.transcribe_bytes(b"x", "a.wav", "sk", "whisper-1")

    with pytest.raises(RuntimeError, match="API key is not set"):
        openai_stt.transcribe_bytes(b"x", "a.wav", "", "whisper-1")

    with pytest.raises(RuntimeError, match="too large"):
        openai_stt.transcribe_bytes(
            b"x" * (openai_stt.MAX_UPLOAD_BYTES + 1), "a.wav", "sk", "whisper-1")


def test_unload_model_idempotent_and_preserves_recording_state():
    mgr = AudioInputManager()
    mgr.is_recording = True
    mgr.audio_buffer = [np.zeros(10, dtype=np.float32)]
    mgr.model = object()

    mgr.unload_model()
    assert mgr.model is None
    mgr.unload_model()  # second call: no-op, no exception
    assert mgr.model is None

    # Engine toggle must not disturb an in-progress recording
    assert mgr.is_recording is True
    assert len(mgr.audio_buffer) == 1


def test_local_default_unaffected(monkeypatch):
    """The suite-wide conftest pin keeps the local engine; the local path
    still requires the model (regression guard for the dispatch insert)."""
    mgr = AudioInputManager()
    mgr.audio_buffer = [np.full(16000, 0.1, dtype=np.float32)]
    monkeypatch.setattr(mgr, "ensure_model_loaded", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="faster-whisper model is not available"):
        mgr.transcribe_audio()


def _local_manager(monkeypatch):
    """Manager pinned to the local engine with load/release side effects
    recorded instead of executed (no real model, no threads)."""
    mgr = AudioInputManager()
    calls = []
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("faster_whisper", "whisper-1"))
    monkeypatch.setattr(mgr, "start_model_load", lambda: calls.append("load"))
    monkeypatch.setattr(mgr, "_release_model", lambda: calls.append("release"))
    return mgr, calls


def test_set_model_size_dispatch(monkeypatch):
    mgr, calls = _local_manager(monkeypatch)

    # Invalid size: rejected, params untouched
    assert mgr.set_model_size("bogus") == "invalid"
    assert mgr._model_params["model_size"] == "turbo"

    # Nothing loaded -> params updated, load kicked (status arrives via WS)
    assert mgr.set_model_size("small") == "reloading"
    assert mgr._model_params["model_size"] == "small"
    assert calls == ["load"]

    # Requested size already loaded -> no worker (caller reports via Gradio)
    mgr.model = object()
    mgr._loaded_model_size = "small"
    assert mgr.set_model_size("small") == "already_loaded"
    assert calls == ["load"]

    # Different size loaded -> hot swap: release then reload
    assert mgr.set_model_size("tiny") == "reloading"
    assert calls == ["load", "release", "load"]

    # Another size's load in flight (cannot cancel) -> queued: the caller
    # announces the queue; the worker-end convergence performs the swap
    mgr.model = None
    mgr._loaded_model_size = None
    mgr._model_loading = True
    assert mgr.set_model_size("base") == "queued"
    assert mgr._model_params["model_size"] == "base"


def test_set_model_size_api_engine_defers(monkeypatch):
    mgr = AudioInputManager()
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("openai", "whisper-1"))
    monkeypatch.setattr(
        mgr, "start_model_load",
        lambda: pytest.fail("API engine must not warm the local model"))

    assert mgr.set_model_size("tiny") == "deferred"
    assert mgr._model_params["model_size"] == "tiny"  # takes effect on next local use


def test_converge_model_size_reloads_on_mid_load_switch(monkeypatch):
    """A size switch that lands while a load is in flight must win: the
    worker-end convergence check releases the stale model and reloads."""
    mgr, calls = _local_manager(monkeypatch)
    mgr.model = object()
    mgr._loaded_model_size = "turbo"
    mgr._model_params["model_size"] = "small"

    mgr._converge_model_size()
    assert calls == ["release", "load"]

    # Loaded size matches the wanted size -> stable, no reload loop
    calls.clear()
    mgr._loaded_model_size = "small"
    mgr._converge_model_size()
    assert calls == []


def test_converge_model_size_yields_to_engine_switch(monkeypatch):
    """Engine switched to the API mid-load: the deferred unload owns the
    model — convergence must not fight it by reloading."""
    mgr = AudioInputManager()
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("openai", "whisper-1"))
    monkeypatch.setattr(
        mgr, "start_model_load",
        lambda: pytest.fail("convergence must not reload for the API engine"))
    mgr.model = object()
    mgr._loaded_model_size = "turbo"
    mgr._model_params["model_size"] = "small"

    mgr._converge_model_size()  # no reload, no exception


def test_load_worker_publishes_status_and_records_size(monkeypatch):
    """Full worker pass with a fake model: downloading -> ready, loaded size
    recorded, loading flag cleared, done event set."""
    mgr = AudioInputManager()
    events = []
    monkeypatch.setattr(
        mgr, "_publish_model_status",
        lambda state, key, **fmt: events.append(state))
    monkeypatch.setattr(mgr, "_is_model_cached", lambda size: False)
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("faster_whisper", "whisper-1"))

    class FakeModel:
        def __init__(self, size, device=None, compute_type=None):
            pass

        def transcribe(self, *a, **k):
            return [], None

    monkeypatch.setattr(stt_mod, "WhisperModel", FakeModel)
    mgr._model_params = {"model_size": "small", "device": "cpu", "compute_type": "int8"}
    mgr._model_loading = True

    mgr._load_model_worker()

    assert events == ["downloading", "ready"]
    assert mgr._loaded_model_size == "small"
    assert mgr._model_loading is False
    assert mgr._model_load_done.is_set()


def test_publish_model_status_payload(monkeypatch):
    """Payload contract for the WS status line: message always present;
    mic_text present only for the local engine (preparing while busy,
    restored to ready on terminal states)."""
    from backend.shared import ui_events

    mgr = AudioInputManager()
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("faster_whisper", "whisper-1"))

    events = []

    def _sub(action, reason, data):
        events.append((action, data))
        return True

    ui_events.register_ui_update_subscriber(_sub)
    try:
        mgr._publish_model_status(
            "downloading", 'hdl.stt_engine.local_model_downloading', model='small')
        mgr._publish_model_status(
            "ready", 'hdl.stt_engine.local_model_ready', model='small')

        monkeypatch.setattr(
            mgr, "_stt_engine_config", lambda: ("openai", "whisper-1"))
        mgr._publish_model_status(
            "ready", 'hdl.stt_engine.local_model_ready', model='small')
    finally:
        ui_events.unregister_ui_update_subscriber(_sub)

    # 各遷移で stt_model_status(Audio Setting行) + update_status(右上3行の
    # 再描画トリガー=Whisper行が実モデル状態表示になった 2026-07-22) の2本
    assert [a for a, _ in events] == ["stt_model_status", "update_status"] * 3
    busy, done, api = (d for a, d in events if a == "stt_model_status")
    assert "small" in busy["message"] and "small" in done["message"]
    assert busy["mic_text"] and done["mic_text"]
    assert busy["mic_text"] != done["mic_text"]  # preparing vs ready
    assert busy["record_disabled"] is True   # record blocked while busy
    assert done["record_disabled"] is False  # unblocked on completion
    assert "mic_text" not in api  # API engine: mic/button not touched
    assert "record_disabled" not in api


def test_cached_model_path_is_offline_only(monkeypatch):
    """_cached_model_path は local_files_only=True(ディスク照会のみ)が契約。
    照会失敗は None(名前渡し=従来のオンライン取得への劣化)。"""
    import faster_whisper.utils as fw_utils

    calls = {}

    def fake_download_model(size, **kwargs):
        calls["size"] = size
        calls["kwargs"] = kwargs
        return "C:/fake/hub/whisper-small"

    monkeypatch.setattr(fw_utils, "download_model", fake_download_model)
    mgr = AudioInputManager()

    assert mgr._cached_model_path("small") == "C:/fake/hub/whisper-small"
    assert calls["size"] == "small"
    assert calls["kwargs"].get("local_files_only") is True

    def _boom(*a, **k):
        raise FileNotFoundError("not in cache")

    monkeypatch.setattr(fw_utils, "download_model", _boom)
    assert mgr._cached_model_path("small") is None


@pytest.mark.parametrize("resolved,expected", [
    ("C:/fake/hub/whisper-small", "C:/fake/hub/whisper-small"),  # キャッシュパス優先
    (None, "small"),  # 解決失敗は名前渡し=従来挙動
])
def test_load_worker_model_path_resolution(monkeypatch, resolved, expected):
    """キャッシュ完備時は解決済みローカルパスを WhisperModel に渡す
    (HFハブ照会=ネットワーク不調時のハング源を経路ごと回避する契約)。"""
    mgr = AudioInputManager()
    monkeypatch.setattr(mgr, "_publish_model_status", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_is_model_cached", lambda size: True)
    monkeypatch.setattr(mgr, "_cached_model_path", lambda size: resolved)
    monkeypatch.setattr(
        mgr, "_stt_engine_config", lambda: ("faster_whisper", "whisper-1"))

    received = {}

    class FakeModel:
        def __init__(self, size_or_path, device=None, compute_type=None):
            received["model"] = size_or_path

        def transcribe(self, *a, **k):
            return [], None

    monkeypatch.setattr(stt_mod, "WhisperModel", FakeModel)
    mgr._model_params = {"model_size": "small", "device": "cpu", "compute_type": "int8"}
    mgr._model_loading = True

    mgr._load_model_worker()

    assert received["model"] == expected


def test_load_worker_failure_publishes_error(monkeypatch):
    mgr = AudioInputManager()
    events = []
    monkeypatch.setattr(
        mgr, "_publish_model_status",
        lambda state, key, **fmt: events.append(state))
    monkeypatch.setattr(mgr, "_is_model_cached", lambda size: True)

    def _boom(*a, **k):
        raise RuntimeError("no such model")

    monkeypatch.setattr(stt_mod, "WhisperModel", _boom)
    mgr._model_params = {"model_size": "small", "device": "cpu", "compute_type": "int8"}
    mgr._model_loading = True

    mgr._load_model_worker()  # must not raise

    assert events == ["loading", "error"]
    assert mgr.model is None
    assert mgr._model_loading is False
    assert mgr._model_load_done.is_set()
