"""Layer 1 — cheap deterministic audio-path smoke.

Purpose: a constant *plumbing* regression guard for the voice
path — STT in -> reply -> TTS out — run every phase, cheaply and deterministically.
The probabilistic model quality (real whisper / real TTS acoustics) is Layer 2
(`tests/scripts/baseline.py`, run manually); the WS接続層 and TTS音響品質 are
explicitly OUT of harness scope.

Assertions:
  ① completes without exception
  ② the expected control path is reached (trace established)
  ③ STT returns NON-EMPTY text from the fixed WAV
  ④ TTS produces NON-EMPTY audio
We assert byte/acoustic NOTHING — only non-empty + wiring.

Seam decision (flagged during the harness build: "_play_tts_blocking 本物 vs 別シーム = 要判断"):
  - STT (`test_stt_*`): run the REAL `transcribe_file` wrapper with only the heavy
    whisper *model* faked -> exercises our wrapper plumbing (the join+strip),
    not GGML inference.
  - TTS function (`test_tts_*`): run the REAL `text_to_speech` wrapper with only
    the heavy TTS *model* faked -> exercises input validation + return-shape
    plumbing, not the synthesiser.
  - Voice turn (`test_voice_turn_*`): drive the REAL generate_reply chain; the
    in-turn TTS play point stays the `control_trace` recording no-op, because the
    real `_play_tts_blocking` blocks on a browser playback ACK = WS layer,
    out of scope. The end-to-end test therefore proves the STT-text -> reply ->
    TTS-invocation *wiring*, with the model contracts proven separately above.

Author discipline (anti-"嘘の緑"): these assert non-empty, so a seam that
silently returns "" would FAIL — confirm by temporarily making a fake return ""
and seeing red (mirrors the meta tests).
"""

from __future__ import annotations
from collections import OrderedDict

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))  # import conftest helpers
from conftest import FakeLLM, reply, tool_call  # noqa: E402

import audio_input.audio_input as stt_mod
import audio_output.audio_output as tts_mod

_WAV = Path(__file__).parent.parent / "fixtures" / "audio" / "s1_fixed.wav"
_FIXT = Path(__file__).parent.parent / "fixtures" / "characters"


# ---------------------------------------------------------------------------
# Fakes for the heavy probabilistic models (the only thing we replace).
# ---------------------------------------------------------------------------
class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeWhisper:
    """Stands in for the faster-whisper model. Yields fixed segments so the real
    `transcribe_file` wrapper (join + strip) runs deterministically."""

    def transcribe(self, file_path, **kwargs):
        info = type("Info", (), {"language": "ja"})()
        return [_FakeSegment("  おはよう、"), _FakeSegment("元気？  ")], info


class _FakeTTSModel:
    """Stands in for the Style-Bert-VITS2 model. `infer` returns (sr, audio)."""

    def get_available_styles(self):
        return ["Neutral"]

    def infer(self, text, language=None, speaker_id=0, style="Neutral"):
        # Non-empty fixed waveform; content is irrelevant (we never compare bytes).
        return 44100, np.zeros(2048, dtype=np.float32) + 0.1


# ---------------------------------------------------------------------------
# ③ STT: real wrapper, faked model -> non-empty text from the fixed WAV.
# ---------------------------------------------------------------------------
def test_stt_wrapper_returns_nonempty_from_fixed_wav(monkeypatch):
    assert _WAV.exists(), f"fixed WAV fixture missing: {_WAV}"
    monkeypatch.setattr(stt_mod._audio_manager, "model", _FakeWhisper())

    text = stt_mod.transcribe_file(str(_WAV))

    assert text  # ③ non-empty
    assert text == "おはよう、元気？"  # join of segments, outer whitespace stripped


# ---------------------------------------------------------------------------
# ④ TTS: real wrapper, faked model -> non-empty audio + valid sample rate.
# ---------------------------------------------------------------------------
def test_tts_wrapper_returns_nonempty_audio(monkeypatch):
    monkeypatch.setattr(tts_mod, "current_tts_model", _FakeTTSModel())

    audio_data, sr = tts_mod.text_to_speech("これはスモークテストです")

    assert audio_data is not None and len(audio_data) > 0  # ④ non-empty
    assert sr > 0


# ---------------------------------------------------------------------------
# ①②: real STT-text -> generate_reply -> in-turn TTS play point wiring.
# ---------------------------------------------------------------------------
def test_voice_turn_wires_stt_to_reply_to_tts(
    monkeypatch, frozen_time, patch_leaves, control_trace, make_state, make_cm
):
    import json

    config = json.loads((_FIXT / "s1_api_minimal.json").read_text(encoding="utf-8"))
    patch_leaves()
    monkeypatch.setattr(stt_mod._audio_manager, "model", _FakeWhisper())
    monkeypatch.setattr(
        "backend.tools.talk_theme_tools.dispatch_talk_theme_tool",
        lambda character_id, tc, language="ja": "トークテーマを設定しました。",
    )

    # 1) STT: real wrapper turns the fixed WAV into the user's text.
    user_text = stt_mod.transcribe_file(str(_WAV))
    assert user_text  # ③ guard at the turn entry too

    # 2) Reply: a talk_theme turn so the deterministic in-turn TTS play point
    #    (_display_pending_steps) is exercised — a plain reply plays TTS only on
    #    the browser side (out of scope).
    llm = FakeLLM(script=[
        tool_call("set_talk_theme", {"theme": "天気"}, content="いい天気だね。"),
        reply("散歩でもどう？"),
    ])
    state = make_state(
        conversation_active=True,
        talk_theme_enabled=True,
        active_llm_cache=OrderedDict({"lumina": llm}),
    )
    cm = make_cm(state, config)

    result = cm.generate_reply(user_text, character_id="lumina")  # ① no exception

    # ② control path reached: turn enqueued + TTS play points recorded in order,
    #    carrying the reply text (proves reply -> TTS wiring). generate_reply
    #    returns a plain dict (not an object).
    assert result["success"]
    assert result["response"]
    kinds = [e["event"] for e in control_trace.events]
    assert kinds[0] == "enqueue"
    tts_texts = [e["text"] for e in control_trace.events if e["event"] == "tts"]
    assert tts_texts == ["いい天気だね。", "散歩でもどう？"]
