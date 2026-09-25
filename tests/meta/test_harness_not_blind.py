"""Bug-injection meta tests — proof the harness is NOT blind.

Principle: "緑" is
only trustworthy if a real defect turns it red. Each test below takes a correct
artifact (so the un-mutated form still MATCHES its golden — proving the setup is
valid), injects ONE deliberate defect, and asserts the golden comparison now
raises AssertionError. Production code is never permanently modified; defects are
applied to a copy of the produced artifact.

The four defect classes:
  1. _build_prompt_messages section order swapped        -> prompt golden must FAIL
  2. tool schema `required` array loses one element      -> tool-schema golden must FAIL
  3. system-prompt XML tag renamed (<system_prompt>→<sys>) -> prompt golden must FAIL
  4. control-flow trace events reordered                 -> control-flow golden must FAIL

If any of these does NOT raise, the corresponding golden is blind and cannot be
trusted.
"""

from __future__ import annotations
from collections import OrderedDict

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # _snapshot / conftest importable
from _snapshot import assert_golden, assert_golden_json, render_messages, render_trace  # noqa: E402
from conftest import FakeLLM, reply, tool_call  # noqa: E402

from backend.tools.camera_capture import get_camera_tool_definitions_for_provider

# When regenerating goldens, assert_golden WRITES instead of comparing, so these
# "must FAIL" assertions would be meaningless — skip the whole module.
pytestmark = pytest.mark.skipif(
    os.environ.get("UPDATE_GOLDEN", "") not in ("", "0", "false", "False"),
    reason="meta tests assert byte-comparison failures; skipped while UPDATE_GOLDEN regenerates",
)

_FIXT = Path(__file__).parent.parent / "fixtures" / "characters"
_RAISES = pytest.raises(AssertionError, match="Golden mismatch")


def _load(name: str) -> dict:
    return json.loads((_FIXT / name).read_text(encoding="utf-8"))


def _build_s1_03(make_state, make_cm, patch_leaves) -> list:
    """Reproduce prompt-golden scenario 3 (api minimal) — the artifact under test."""
    config = _load("s1_api_minimal.json")
    patch_leaves()
    cm = make_cm(make_state(), config)
    return cm._build_prompt_messages("lumina", user_text="", config=config)


# --- Case 1: section order swap (prompt golden) -------------------------------
def test_inject_section_reorder_fails_b0a1(frozen_time, make_state, make_cm, patch_leaves):
    messages = _build_s1_03(make_state, make_cm, patch_leaves)
    # Sanity: the correct artifact matches its golden.
    assert_golden("s1_03_api_minimal", render_messages(messages))

    # Inject: swap the first two XML sections inside the system message.
    defective = copy.deepcopy(messages)
    blocks = defective[0]["content"].split("\n\n")
    assert len(blocks) >= 2, "scenario must have >=2 system sections to reorder"
    blocks[0], blocks[1] = blocks[1], blocks[0]
    defective[0]["content"] = "\n\n".join(blocks)

    with _RAISES:
        assert_golden("s1_03_api_minimal", render_messages(defective))


# --- Case 3: XML tag rename (prompt golden) ----------------------------------
def test_inject_tag_rename_fails_b0a1(frozen_time, make_state, make_cm, patch_leaves):
    messages = _build_s1_03(make_state, make_cm, patch_leaves)
    assert_golden("s1_03_api_minimal", render_messages(messages))  # sanity

    defective = copy.deepcopy(messages)
    assert "system_prompt" in defective[0]["content"]
    defective[0]["content"] = defective[0]["content"].replace("system_prompt", "sys")

    with _RAISES:
        assert_golden("s1_03_api_minimal", render_messages(defective))


# --- Case 2: required array element removed (tool-schema golden) --------------
def test_inject_required_removal_fails_b0a2():
    providers = ["ollama", "openai", "anthropic", "xai", "google"]
    schemas = {p: get_camera_tool_definitions_for_provider(p, "ja") for p in providers}
    # Sanity: correct schemas match the golden.
    assert_golden_json("tool_camera", schemas)

    defective = copy.deepcopy(schemas)
    required = defective["anthropic"][0]["input_schema"]["required"]
    assert required, "expected a non-empty required array to remove from"
    required.pop()

    with _RAISES:
        assert_golden_json("tool_camera", defective)


# --- Case 4: control-flow trace reordered (control-flow golden) ---------------
def test_inject_trace_reorder_fails_b0a3(
    frozen_time, patch_leaves, control_trace, make_state, make_cm, monkeypatch
):
    config = _load("s1_api_minimal.json")
    patch_leaves()
    monkeypatch.setattr(
        "backend.tools.talk_theme_tools.dispatch_talk_theme_tool",
        lambda character_id, tc, language="ja": "トークテーマを「宇宙旅行」に設定しました。",
    )
    llm = FakeLLM(script=[
        tool_call("set_talk_theme", {"theme": "宇宙旅行"}, content="テーマを決めるね。"),
        reply("じゃあ宇宙旅行の話をしようか！"),
    ])
    state = make_state(
        conversation_active=True, talk_theme_enabled=True,
        active_llm_cache=OrderedDict({"lumina": llm}),
    )
    cm = make_cm(state, config)
    result = cm.generate_reply("何か話そう", character_id="lumina")

    events = control_trace.events
    # Sanity: the real trace matches its golden.
    assert_golden("flow_02_one_tool", render_trace(events, result))

    # Inject: swap two adjacent events (reorders the UI firing sequence).
    defective = list(events)
    assert len(defective) >= 4
    defective[2], defective[3] = defective[3], defective[2]

    with _RAISES:
        assert_golden("flow_02_one_tool", render_trace(defective, result))
