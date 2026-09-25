"""Smoke tests for the Live Camera prompt note wiring.

conversation_manager sets config["_ambient_frame_present"] only on turns
where a frame passed the barrier; prompt_builder must then (and only then)
append the <ambient_camera_note> section. Uses the golden harness fixtures
(frozen clock, fake leaves) but plain assertions — the byte contract of
existing scenarios stays owned by tests/golden/.
"""

import json
from pathlib import Path

_FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "characters"


def _load(name: str) -> dict:
    return json.loads((_FIXT / name).read_text(encoding="utf-8"))


def test_note_present_when_frame_flag_set(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    state = make_state()
    cm = make_cm(state, config)
    config["_ambient_frame_present"] = True

    messages = cm._build_prompt_messages("nox", user_text="いま何が見える？", config=config)

    system = messages[0]["content"]
    assert "<ambient_camera_note>" in system
    from backend.shared.prompt_i18n import get_prompt_language, prompt_text
    expected = prompt_text("ambient_camera.frame_note", get_prompt_language(config))
    assert expected in system


def test_note_absent_without_frame_flag(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    state = make_state()
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("nox", user_text="いま何が見える？", config=config)

    assert "<ambient_camera_note>" not in messages[0]["content"]
