"""
tests/smoke/test_tts_character_config.py

Character-layer TTS provider mapping (ui/character_ui.py helpers).

- _build_tts_model_config: dropdown value -> tts_model_config dict
  (sbv2 3-path shape byte-compatible with legacy saves; elevenlabs tagged
  shape with voice_name looked up from the saved voices list).
- tts_display_name: info-display label for both providers (shared by
  switch_character and refresh_character_info_on_start).
"""

from backend.shared.i18n import t
from ui.character_ui import _build_tts_model_config, tts_display_name


def test_build_sbv2_config_matches_legacy_shape():
    from ui.constants import TTS_MODELS_DIR, TTS_MODEL_FILE, TTS_CONFIG_FILE, TTS_STYLE_VECTORS_FILE

    cfg = _build_tts_model_config("sbv2::Vermariss")

    assert cfg["provider"] == "sbv2"
    assert cfg["model_path"] == str(TTS_MODELS_DIR / "Vermariss" / TTS_MODEL_FILE)
    assert cfg["config_path"] == str(TTS_MODELS_DIR / "Vermariss" / TTS_CONFIG_FILE)
    assert cfg["style_vectors_path"] == str(TTS_MODELS_DIR / "Vermariss" / TTS_STYLE_VECTORS_FILE)


def test_build_sbv2_config_accepts_legacy_bare_value():
    # A stale dropdown value without the provider prefix decodes as sbv2
    assert _build_tts_model_config("Vermariss")["provider"] == "sbv2"


def test_build_kokoro_config_is_voice_name_only():
    cfg = _build_tts_model_config("kokoro::af_heart")

    # No paths in the kokoro shape: assets are fixed under kokoro/ (repo root)
    # and the activation resolves them from the voice name alone.
    assert cfg == {"provider": "kokoro", "voice_name": "af_heart"}


def test_build_elevenlabs_config_looks_up_voice_name(monkeypatch):
    monkeypatch.setattr(
        "backend.shared.api_settings.get_elevenlabs_voices",
        lambda: [{"voice_id": "abc123", "name": "MyVoice"}])

    cfg = _build_tts_model_config("elevenlabs::abc123")

    assert cfg == {"provider": "elevenlabs", "voice_id": "abc123", "voice_name": "MyVoice"}


def test_build_elevenlabs_config_falls_back_to_voice_id(monkeypatch):
    monkeypatch.setattr(
        "backend.shared.api_settings.get_elevenlabs_voices", lambda: [])

    cfg = _build_tts_model_config("elevenlabs::abc123")

    assert cfg["voice_name"] == "abc123"


def test_validate_rejects_language_engine_mismatch():
    # ja=SBV2 / en=Kokoro (2026-07-26 ruling); ElevenLabs passes either way
    from ui.character_ui import validate_character_data

    ok = ("Name", "sbv2::Vermariss", "ollama::gemma")
    assert validate_character_data(*ok, stt_language="ja") is None
    assert validate_character_data("Name", "kokoro::af_heart", "ollama::gemma",
                                   stt_language="en") is None
    assert validate_character_data("Name", "elevenlabs::abc", "ollama::gemma",
                                   stt_language="ja") is None
    assert validate_character_data("Name", "elevenlabs::abc", "ollama::gemma",
                                   stt_language="en") is None
    assert validate_character_data("Name", "sbv2::Vermariss", "ollama::gemma",
                                   stt_language="en") == t('charui.tts_language_mismatch_sbv2')
    assert validate_character_data("Name", "kokoro::af_heart", "ollama::gemma",
                                   stt_language="ja") == t('charui.tts_language_mismatch_kokoro')


def test_tts_display_name_both_providers():
    assert tts_display_name(
        {"provider": "elevenlabs", "voice_id": "abc", "voice_name": "MyVoice"}
    ) == "MyVoice (ElevenLabs)"
    assert tts_display_name(
        {"provider": "kokoro", "voice_name": "af_heart"}
    ) == "af_heart (KokoroTTS)"
    # voice_name missing -> voice_id
    assert tts_display_name(
        {"provider": "elevenlabs", "voice_id": "abc"}
    ) == "abc (ElevenLabs)"
    # Legacy SBV2 config (no provider tag) -> folder name from the path
    assert tts_display_name(
        {"model_path": "sbv2_models/Vermariss/model.safetensors"}
    ) == "Vermariss"
    # Empty/missing config -> the translated unknown label
    assert tts_display_name({}) == t('charui.unknown')
    assert tts_display_name(None) == t('charui.unknown')


def test_llm_display_name_unknown_when_model_unset():
    # 同梱モデルキャラクターは LLM 未設定で出荷 → provider 既定の "(Ollama)" を
    # 出さず「不明」(稜 Mac 実機 2026-09-25)
    from ui.character_ui import llm_display_name
    assert llm_display_name({"model_provider": "ollama", "model_name": ""}) == t('charui.unknown')
    assert llm_display_name({}) == t('charui.unknown')
    assert llm_display_name(None) == t('charui.unknown')


def test_llm_display_name_model_with_provider():
    from backend.shared.api_settings import get_provider_display_name
    from ui.character_ui import llm_display_name
    assert llm_display_name({"model_provider": "ollama", "model_name": "qwen3:14b"}) == (
        f"qwen3:14b ({get_provider_display_name('ollama')})")
    assert llm_display_name({"model_provider": "anthropic", "model_name": "claude-x"}) == (
        f"claude-x ({get_provider_display_name('anthropic')})")
    # 旧形式のモデル名も表示に使う
    assert llm_display_name({"ollama_model_name": "legacy"}).startswith("legacy (")
