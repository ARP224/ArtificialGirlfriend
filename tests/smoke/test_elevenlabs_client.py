"""
tests/smoke/test_elevenlabs_client.py

ElevenLabs TTS route (audio_output) — no network, no SBV2 model load.

Load-bearing assertions:
- MP3 bytes decode to the (mono float32, sample_rate) contract that
  downstream lipsync/AAC/WS playback consumes (in-memory PyAV round trip).
- text_to_speech dispatches to the ElevenLabs route WITHOUT an SBV2 model
  loaded, and keeps the same return contract.
- configure_elevenlabs_tts validates BEFORE unloading (an invalid config
  must not destroy the loaded SBV2 state).
- cleanup() resets the provider to sbv2.
"""

import io
from types import SimpleNamespace

import av
import numpy as np
import pytest
import requests

import audio_output.audio_output as tts_mod
from audio_output import elevenlabs_client


def _make_mp3_bytes(sr=44100, seconds=0.25):
    """Encode a sine tone to MP3 in memory with PyAV (same lib as decode)."""
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(int(sr * seconds)) / sr))
    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="mp3")
    stream = container.add_stream("mp3", rate=sr)
    frame = av.AudioFrame.from_ndarray(
        tone.astype(np.float32).reshape(1, -1), format="fltp", layout="mono")
    frame.sample_rate = sr
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buf.getvalue()


@pytest.fixture
def _restore_provider_state(monkeypatch):
    """Register the provider globals for restoration after each test."""
    monkeypatch.setattr(tts_mod, "current_tts_provider", tts_mod.current_tts_provider)
    monkeypatch.setattr(tts_mod, "current_elevenlabs_voice_id",
                        tts_mod.current_elevenlabs_voice_id)
    monkeypatch.setattr(tts_mod, "current_bert_language", tts_mod.current_bert_language)


def test_decode_mp3_roundtrip_contract():
    mp3 = _make_mp3_bytes(sr=44100, seconds=0.25)

    audio, sr = elevenlabs_client._decode_mp3_to_numpy(mp3)

    assert sr == 44100
    assert audio.dtype == np.float32
    assert audio.ndim == 1  # mono
    # MP3 pads with encoder delay; the tone length must be roughly preserved
    assert abs(len(audio) - 0.25 * 44100) < 0.1 * 44100
    assert np.max(np.abs(audio)) > 0.1  # non-silent


def test_decode_garbage_raises():
    with pytest.raises(RuntimeError, match="decode"):
        elevenlabs_client._decode_mp3_to_numpy(b"this is not an mp3")


def test_synthesize_error_mapping(monkeypatch):
    def _post_status(code):
        return lambda *a, **k: SimpleNamespace(
            status_code=code, json=lambda: {"detail": {"message": "boom"}},
            content=b"")

    monkeypatch.setattr(requests, "post", _post_status(401))
    # 401 must carry the API's own detail: ElevenLabs uses 401 both for an
    # invalid key and for a scoped key missing a permission (実踏 2026-07-12).
    with pytest.raises(RuntimeError, match=r"rejected \(401\)\. boom"):
        elevenlabs_client.synthesize("hi", "voice1", "model1", "key")

    monkeypatch.setattr(requests, "post", _post_status(429))
    with pytest.raises(RuntimeError, match="quota"):
        elevenlabs_client.synthesize("hi", "voice1", "model1", "key")

    with pytest.raises(RuntimeError, match="API key is not set"):
        elevenlabs_client.synthesize("hi", "voice1", "model1", "")

    with pytest.raises(RuntimeError, match="voice is not configured"):
        elevenlabs_client.synthesize("hi", "", "model1", "key")


def test_synthesize_success_decodes_payload(monkeypatch):
    mp3 = _make_mp3_bytes()
    captured = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, body=json)
        return SimpleNamespace(status_code=200, content=mp3)

    monkeypatch.setattr(requests, "post", fake_post)

    audio, sr = elevenlabs_client.synthesize("こんにちは", "voice1", "model1", "key")

    assert sr == 44100 and len(audio) > 0
    assert "voice1" in captured["url"]
    assert captured["headers"]["xi-api-key"] == "key"
    assert captured["body"] == {"text": "こんにちは", "model_id": "model1"}
    assert "language_code" not in captured["body"]  # multilingual rejects it
    assert captured["params"]["output_format"] == "mp3_44100_128"


def test_text_to_speech_dispatches_to_elevenlabs(monkeypatch, _restore_provider_state):
    # No SBV2 model loaded at all — the API route must not require one.
    monkeypatch.setattr(tts_mod, "current_tts_model", None)
    tts_mod._set_provider_locked("elevenlabs", "voice1")

    monkeypatch.setattr(
        "backend.shared.api_settings.get_elevenlabs_api_key", lambda: "key")
    monkeypatch.setattr(
        "backend.shared.api_settings.get_elevenlabs_model_id",
        lambda: "eleven_multilingual_v2")

    fake_audio = np.full(1024, 0.1, dtype=np.float32)
    captured = {}

    def fake_synthesize(text, voice_id, model_id, api_key, **kw):
        captured.update(text=text, voice_id=voice_id, model_id=model_id)
        return fake_audio, 44100

    monkeypatch.setattr(elevenlabs_client, "synthesize", fake_synthesize)

    audio, sr = tts_mod.text_to_speech("テストです", style="通常")

    assert sr == 44100 and audio is fake_audio
    assert captured == {"text": "テストです", "voice_id": "voice1",
                        "model_id": "eleven_multilingual_v2"}


def test_text_to_speech_elevenlabs_missing_key_raises(monkeypatch, _restore_provider_state):
    tts_mod._set_provider_locked("elevenlabs", "voice1")
    monkeypatch.setattr(
        "backend.shared.api_settings.get_elevenlabs_api_key", lambda: "")

    with pytest.raises(RuntimeError, match="ElevenLabs API key is not set"):
        tts_mod.text_to_speech("テストです")


def test_configure_elevenlabs_validates_before_unloading(monkeypatch, _restore_provider_state):
    monkeypatch.setattr(
        tts_mod, "unload_tts_model",
        lambda: pytest.fail("invalid voice_id must not unload the SBV2 model"))

    with pytest.raises(ValueError, match="voice_id is empty"):
        tts_mod.configure_elevenlabs_tts("")


def test_configure_elevenlabs_unloads_sbv2_and_bert(monkeypatch, _restore_provider_state):
    calls = []
    monkeypatch.setattr(tts_mod, "unload_tts_model", lambda: calls.append("model"))
    monkeypatch.setattr(tts_mod, "_unload_bert", lambda: calls.append("bert"))

    tts_mod.configure_elevenlabs_tts("voice1")

    assert calls == ["model", "bert"]
    assert tts_mod.get_current_tts_provider() == "elevenlabs"
    assert tts_mod.current_elevenlabs_voice_id == "voice1"


def test_cleanup_resets_provider(_restore_provider_state):
    tts_mod._set_provider_locked("elevenlabs", "voice1")

    tts_mod.cleanup()

    assert tts_mod.get_current_tts_provider() == "sbv2"
    assert tts_mod.current_elevenlabs_voice_id is None
