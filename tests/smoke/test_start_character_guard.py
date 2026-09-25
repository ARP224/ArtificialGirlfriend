"""会話開始前のキャラクター設定ガード(稜裁定 2026-09-25)。

LLM モデル / キャラクターボイスが未設定なら start_conversation をブロックし、
「既存キャラクター編集」へ誘導する。同梱モデルキャラクターは LLM 未設定
(日本語2人は声も)で出荷されるので、これが最初の案内になる(稜 Mac 実機:
LLM 未設定の Cecilia で会話が始まってしまった)。判定は設定値の有無だけで、
実照会はしない(モデル不在・声の読み込み失敗は従来どおり活性化/生成側)。
"""

import pytest

from backend.shared.i18n import t

SBV2 = {"provider": "sbv2", "model_path": "sbv2_models/x/model.safetensors",
        "config_path": "c", "style_vectors_path": "s"}
SBV2_UNSET = {"provider": "sbv2", "model_path": "", "config_path": "", "style_vectors_path": ""}
KOKORO = {"provider": "kokoro", "voice_name": "bf_alice"}


@pytest.mark.parametrize("config, expected", [
    ({"model_name": "qwen3:14b", "tts_model_config": SBV2}, None),
    ({"model_provider": "anthropic", "model_name": "claude-x", "tts_model_config": KOKORO}, None),
    ({"model_name": "gpt", "tts_model_config": {"provider": "elevenlabs", "voice_id": "abc",
                                                 "voice_name": "V"}}, None),
    ({"ollama_model_name": "legacy", "tts_model_config": SBV2}, None),  # 旧形式のモデル名
    # 同梱 Momo/Cecilia の出荷状態
    ({"model_name": "", "tts_model_config": SBV2_UNSET}, "start_no_llm_voice"),
    # 同梱 Stella の出荷状態
    ({"model_name": "", "tts_model_config": KOKORO}, "start_no_llm"),
    ({"model_name": "qwen3:14b", "tts_model_config": SBV2_UNSET}, "start_no_voice"),
    ({"model_name": "qwen3:14b"}, "start_no_voice"),  # tts_model_config 自体が無い
    ({"model_name": "qwen3:14b", "tts_model_config": {"provider": "kokoro", "voice_name": ""}},
     "start_no_voice"),
    ({"model_name": "qwen3:14b", "tts_model_config": {"provider": "elevenlabs", "voice_id": ""}},
     "start_no_voice"),
])
def test_character_ready_guard(make_state, make_cm, config, expected):
    result = make_cm(make_state(), config)._check_character_ready("cid")
    if expected is None:
        assert result is None
    else:
        assert result["success"] is False
        assert result["error_code"] == expected
        # UI は err.<code> を翻訳して表示する: 両言語のカタログに実在すること
        assert t(f"err.{expected}") != f"err.{expected}"


def test_guard_accepts_standardized_response_shape(make_state, make_cm):
    wrapped = {"success": True, "result": {"model_name": "", "tts_model_config": SBV2_UNSET}}
    assert make_cm(make_state(), wrapped)._check_character_ready("cid")["error_code"] == "start_no_llm_voice"
    # 設定が読めないときは縮退(会話を止めない = 従来どおり活性化側が扱う)
    assert make_cm(make_state(), {"success": False, "error": "nf"})._check_character_ready("cid") is None


def test_guard_degrades_when_loader_raises(make_state):
    from backend.conversation_manager import ConversationManager

    def boom(character_id):
        raise RuntimeError("disk")

    cm = ConversationManager(make_state(), boom, lambda *a, **k: None, lambda *a, **k: None)
    assert cm._check_character_ready("cid") is None


def test_start_conversation_blocks_before_embedding_guard(make_state, make_cm, monkeypatch):
    state = make_state(active_character_id="cid", conversation_active=False)
    cm = make_cm(state, {"model_name": "", "tts_model_config": SBV2_UNSET})
    embedding_calls = []
    monkeypatch.setattr(cm, "_check_embedding_ready", lambda: embedding_calls.append(1))
    result = cm.start_conversation()
    assert result["success"] is False
    assert result["error_code"] == "start_no_llm_voice"
    assert embedding_calls == []  # キャラクターガードが埋め込みガードより先
    assert state.conversation_active is False
