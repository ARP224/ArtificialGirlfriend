"""
tests/smoke/test_elevenlabs_settings.py

ElevenLabs settings layer (api_settings.py) — no network, requests mocked.

The load-bearing assertions:
- fetch_elevenlabs_models MUST filter can_do_text_to_speech: /v1/models also
  returns STT ("Scribe"), music and voice-conversion models; an unfiltered
  list would offer models that cannot speak (AG goes silent).
- decode_tts_value falls back to sbv2 for legacy bare folder names.
- _map_config_paths leaves an elevenlabs tts_model_config untouched
  (no path keys present -> no mapping), locking backward compatibility.
"""

import json
from types import SimpleNamespace

import pytest
import requests

from backend.shared import api_settings


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect api_settings.json to tmp and reset the mtime cache."""
    monkeypatch.setattr(api_settings, 'API_SETTINGS_FILE', tmp_path / 'api_settings.json')
    monkeypatch.setattr(api_settings, '_settings_cache', None)
    monkeypatch.setattr(api_settings, '_settings_cache_mtime', None)
    return tmp_path / 'api_settings.json'


def _fake_response(payload):
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: payload,
    )


def _set_key(key):
    settings = api_settings.load_api_settings()
    settings["elevenlabs"]["api_key"] = key
    api_settings.save_api_settings(settings)


def test_fetch_elevenlabs_models_filters_non_tts(isolated_settings, monkeypatch):
    _set_key("test-key")
    # Real API shape (実測 2026-07-12): token_cost_factor is 1.0 for EVERY
    # model; the real multiplier is model_rates.character_cost_multiplier.
    payload = [
        {"model_id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2",
         "can_do_text_to_speech": True, "token_cost_factor": 1.0,
         "model_rates": {"character_cost_multiplier": 1.0}},
        {"model_id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5",
         "can_do_text_to_speech": True, "token_cost_factor": 1.0,
         "model_rates": {"character_cost_multiplier": 0.5}},
        {"model_id": "scribe_v1", "name": "Scribe v1 (STT)",
         "can_do_text_to_speech": False, "token_cost_factor": 1.0},
        {"model_id": "eleven_music_v1", "name": "Music"},  # flag missing -> excluded
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _fake_response(payload))

    result = api_settings.fetch_elevenlabs_models()

    assert result["success"] is True
    ids = [m["model_id"] for m in result["models"]]
    assert ids == ["eleven_multilingual_v2", "eleven_flash_v2_5"]
    assert "scribe_v1" not in ids

    # persisted with the model_rates-based cost factor
    saved = api_settings.load_api_settings()["elevenlabs"]["available_models"]
    assert saved[1]["cost_factor"] == 0.5
    assert saved[0]["cost_factor"] == 1.0


def test_fetch_elevenlabs_voices_sorts_user_first(isolated_settings, monkeypatch):
    _set_key("test-key")
    # Real API shape (実測 2026-07-12): /v1/voices returns the ~20 stock
    # voices (category "premade") BEFORE the user's registered ones. Stock
    # voices are kept (only ones usable via API on the free plan) but sorted
    # BELOW the user's own voices (稜裁定 2026-07-12).
    payload = {"voices": [
        {"voice_id": "stock1", "name": "Roger", "category": "premade"},
        {"voice_id": "abc123", "name": "MyVoice", "category": "cloned"},
        {"voice_id": "pro456", "name": "Emmaline", "category": "professional"},
        {"voice_id": "", "name": "broken"},  # dropped: no id
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _fake_response(payload))

    result = api_settings.fetch_elevenlabs_voices()

    assert result["success"] is True
    assert [v["name"] for v in result["voices"]] == ["MyVoice", "Emmaline", "Roger"]
    saved = api_settings.load_api_settings()["elevenlabs"]["available_voices"]
    assert saved == result["voices"]
    assert saved[2]["category"] == "premade"


def test_voice_display_choices_mark_premade(isolated_settings):
    settings = api_settings.load_api_settings()
    settings["elevenlabs"]["available_voices"] = [
        {"voice_id": "abc123", "name": "MyVoice", "category": "cloned"},
        {"voice_id": "stock1", "name": "Roger", "category": "premade"},
        {"voice_id": "old1", "name": "Legacy"},  # pre-category save -> user voice
    ]
    api_settings.save_api_settings(settings)

    choices = api_settings.get_elevenlabs_voice_display_choices()

    assert choices[0] == ("MyVoice", "abc123")
    assert choices[1][1] == "stock1"
    assert "Roger" in choices[1][0] and choices[1][0] != "Roger"  # free marker attached
    assert choices[2] == ("Legacy", "old1")


def test_fetch_without_key_fails_cleanly(isolated_settings):
    assert api_settings.fetch_elevenlabs_voices()["success"] is False
    assert api_settings.fetch_elevenlabs_models()["success"] is False


def test_fetch_401_surfaces_api_detail(isolated_settings, monkeypatch):
    """ElevenLabs uses 401 for BOTH invalid keys and scoped keys missing a
    permission (voices_read 等) — the UI message must carry the API's detail
    so the two are distinguishable (実踏 2026-07-12)."""
    _set_key("scoped-key")

    detail = {"detail": {"status": "missing_permissions",
                         "message": "The API key you used is missing the permission voices_read."}}

    def _raise_401():
        err_response = SimpleNamespace(status_code=401, json=lambda: detail)
        raise requests.exceptions.HTTPError(response=err_response)

    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: SimpleNamespace(raise_for_status=_raise_401, json=lambda: {}))

    result = api_settings.fetch_elevenlabs_voices()

    assert result["success"] is False
    assert "voices_read" in result["message"]


def test_model_choices_include_cost_factor(isolated_settings):
    settings = api_settings.load_api_settings()
    settings["elevenlabs"]["available_models"] = [
        {"model_id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5",
         "cost_factor": 0.5},
        # A list saved before the cost-field fix (legacy key) must still label
        {"model_id": "eleven_turbo_v2_5", "name": "Eleven Turbo v2.5",
         "token_cost_factor": 0.5},
    ]
    api_settings.save_api_settings(settings)

    choices = api_settings.get_elevenlabs_model_choices()

    assert len(choices) == 2
    label, value = choices[0]
    assert value == "eleven_flash_v2_5"
    assert "Eleven Flash v2.5" in label
    assert "0.5" in label  # cost factor from the API, not a price table
    assert "0.5" in choices[1][0]  # legacy token_cost_factor fallback


def test_get_all_tts_choices_encoding_and_key_gating(isolated_settings, tmp_path, monkeypatch):
    settings = api_settings.load_api_settings()
    settings["elevenlabs"]["available_voices"] = [
        {"voice_id": "abc123", "name": "MyVoice", "category": "cloned"},
        {"voice_id": "stock1", "name": "Roger", "category": "premade"},
    ]
    api_settings.save_api_settings(settings)

    # Hermetic Kokoro voices dir (the repo's real kokoro/ assets must not
    # leak into the test — the scan is patched to a tmp dir)
    from audio_output import kokoro_engine
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    for voice in ("af_heart", "bf_emma"):
        (voices_dir / f"{voice}.pt").write_bytes(b"")
    monkeypatch.setattr(kokoro_engine, "KOKORO_VOICES_DIR", str(voices_dir))

    # No key + language filter -> local engine matching the language only
    assert api_settings.get_all_tts_choices(["Vermariss"], language="ja") == [
        ("Vermariss (Style-bert-vits2)", "sbv2::Vermariss"),
    ]
    assert api_settings.get_all_tts_choices(["Vermariss"], language="en") == [
        ("af_heart (KokoroTTS)", "kokoro::af_heart"),
        ("bf_emma (KokoroTTS)", "kokoro::bf_emma"),
    ]
    # No language -> every local engine (page-load fallback)
    choices = api_settings.get_all_tts_choices(["Vermariss"])
    assert ("Vermariss (Style-bert-vits2)", "sbv2::Vermariss") in choices
    assert ("af_heart (KokoroTTS)", "kokoro::af_heart") in choices

    # Key set -> ElevenLabs voices appended regardless of language
    # (user voice plain, stock marked free)
    _set_key("test-key")
    choices = api_settings.get_all_tts_choices(["Vermariss"], language="ja")
    assert choices[0] == ("Vermariss (Style-bert-vits2)", "sbv2::Vermariss")
    assert all(not v.startswith("kokoro::") for _lbl, v in choices)
    assert ("MyVoice (ElevenLabs)", "elevenlabs::abc123") in choices
    stock_label = [lbl for lbl, val in choices if val == "elevenlabs::stock1"][0]
    assert stock_label.startswith("Roger (ElevenLabs / ")  # free marker in label

    # Encoded-value roundtrip for the new provider
    assert api_settings.decode_tts_value("kokoro::af_heart") == ("kokoro", "af_heart")


def test_openai_stt_model_choices_extends_from_refresh(isolated_settings):
    # Before any refresh: baseline only
    assert api_settings.get_openai_stt_model_choices() == api_settings.OPENAI_STT_BASE_MODELS

    settings = api_settings.load_api_settings()
    settings["openai"]["available_models"] = [
        "gpt-4o",                        # chat model -> excluded
        "gpt-4o-transcribe",             # already in baseline -> no duplicate
        "gpt-5-transcribe",              # future model -> appears after refresh
        "gpt-4o-transcribe-diarize",     # diarize -> excluded (別レスポンス形)
        "whisper-2",                     # future whisper -> appears
    ]
    api_settings.save_api_settings(settings)

    choices = api_settings.get_openai_stt_model_choices()

    assert choices[:3] == api_settings.OPENAI_STT_BASE_MODELS
    assert "gpt-5-transcribe" in choices
    assert "whisper-2" in choices
    assert "gpt-4o" not in choices
    assert "gpt-4o-transcribe-diarize" not in choices
    assert choices.count("gpt-4o-transcribe") == 1


def test_decode_tts_value():
    assert api_settings.decode_tts_value("sbv2::Vermariss") == ("sbv2", "Vermariss")
    assert api_settings.decode_tts_value("elevenlabs::abc123") == ("elevenlabs", "abc123")
    # Legacy bare folder name (pre-provider dropdown values)
    assert api_settings.decode_tts_value("Vermariss") == ("sbv2", "Vermariss")


def test_load_api_settings_backfills_elevenlabs(isolated_settings):
    # Simulate a settings file from before the elevenlabs section existed
    isolated_settings.write_text(json.dumps({"openai": {"api_key": "x"}}), encoding="utf-8")

    settings = api_settings.load_api_settings()

    assert settings["elevenlabs"]["api_key"] == ""
    assert settings["elevenlabs"]["available_voices"] == []
    assert settings["elevenlabs"]["model_id"] == "eleven_multilingual_v2"
    assert settings["openai"]["api_key"] == "x"


def test_save_api_key_supports_elevenlabs(isolated_settings):
    result = api_settings.save_api_key("elevenlabs", " el-key ")
    assert result["success"] is True
    assert api_settings.get_elevenlabs_api_key() == "el-key"


def test_fetch_provider_models_guard_without_models_url(isolated_settings):
    # elevenlabs (and ollama) have no models_url -> must fail cleanly, not KeyError
    assert api_settings.fetch_provider_models("elevenlabs")["success"] is False
    assert api_settings.fetch_provider_models("ollama")["success"] is False


def test_map_config_paths_passes_elevenlabs_untouched():
    from backend.conversation.character_manager import _map_config_paths

    config = {
        "name": "Test",
        "tts_model_config": {
            "provider": "elevenlabs",
            "voice_id": "abc123",
            "voice_name": "MyVoice",
        },
    }
    mapped = _map_config_paths(config, lambda p: "MAPPED::" + p)
    assert mapped["tts_model_config"] == config["tts_model_config"]


def test_settings_store_stt_defaults(tmp_path, monkeypatch):
    from backend.shared import settings_store
    monkeypatch.setattr(settings_store, 'SETTINGS_FILE', tmp_path / 'user_settings.json')

    assert settings_store.get_setting('audio', 'stt_engine') == 'faster_whisper'
    assert settings_store.get_setting('audio', 'stt_api_model') == 'whisper-1'

    # Existing user_settings.json without the new keys merges them in
    (tmp_path / 'user_settings.json').write_text(
        json.dumps({"audio": {"beep_enabled": False}}), encoding="utf-8")
    assert settings_store.get_setting('audio', 'stt_engine') == 'faster_whisper'
    assert settings_store.get_setting('audio', 'beep_enabled') is False
