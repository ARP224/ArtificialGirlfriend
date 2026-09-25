"""Control-flow trace golden.

Drives the real chain generate_reply -> _generate_reply_task -> _run_tool_call_loop
with a scripted FakeLLM and records an ORDERED event list (task enqueue, TTS play
points, send_ui_update firings) plus the final result. The snapshot pins the
control flow so a refactor cannot silently reorder tool dispatch,
UI updates, or the approval branch.

Determinism (no threads / disk / audio):
  * No memory_manager registered -> the async memory-save thread is never spawned
    (conversation_manager.py:1967 guard); the turn falls back to the in-memory
    short_term_buffer.
  * control_trace seam no-ops TTS (records text only) and runs the task inline.
  * Tool dispatch / command security / command log are mocked to fixed results.

Author discipline (anti-"嘘の緑"): after first green, OPEN each
tests/golden/snapshots/flow_*.txt and confirm the event ORDER matches the real
control flow (e.g. scenario 2: speak pre-text -> talk_theme_block -> spinner ->
speak final; scenario 3: approval_pending -> approval_resolved -> command block).
"""

from __future__ import annotations
from collections import OrderedDict

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # make _snapshot importable
from _snapshot import assert_golden, render_trace  # noqa: E402
from conftest import FakeLLM, reply, tool_call  # noqa: E402

from backend.tools.command_executor import CommandResult
from backend.tools.command_security import SecurityResult, SecurityVerdict

_FIXT = Path(__file__).parent.parent / "fixtures" / "characters"


def _load(name: str) -> dict:
    return json.loads((_FIXT / name).read_text(encoding="utf-8"))


# --- Scenario 1: no tool, simple reply (ollama) ------------------------------
def test_flow_01_no_tool(
    frozen_time, patch_leaves, control_trace, make_state, make_cm
):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    llm = FakeLLM(script=[reply("こんにちは、今日はどんな一日だった？")])
    state = make_state(conversation_active=True, active_llm_cache=OrderedDict({"nox": llm}))
    cm = make_cm(state, config)

    result = cm.generate_reply("やあ", character_id="nox")

    assert_golden("flow_01_no_tool", render_trace(control_trace.events, result))


# --- Scenario 2: one tool call -> final reply (talk_theme) -------------------
def test_flow_02_one_tool(
    frozen_time, patch_leaves, control_trace, make_state, make_cm, monkeypatch
):
    config = _load("s1_api_minimal.json")  # anthropic
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
        conversation_active=True,
        talk_theme_enabled=True,
        active_llm_cache=OrderedDict({"lumina": llm}),
    )
    cm = make_cm(state, config)

    result = cm.generate_reply("何か話そう", character_id="lumina")

    assert_golden("flow_02_one_tool", render_trace(control_trace.events, result))


class _AutoApproveEvent:
    """Stand-in for state.command_approval_event in the greylist scenario.

    The synchronous test has no second thread to approve between
    _set_pending_approval (which clears the event and nulls the result) and
    _wait_for_approval. This fake models "by the time wait() returns, the user
    has approved": clear/set are no-ops and wait() injects the result.
    """

    def __init__(self, state, result="accepted"):
        self._state = state
        self._result = result

    def clear(self):
        pass

    def set(self):
        pass

    def wait(self, timeout=None):
        self._state.command_approval_result = self._result
        return True


# --- Scenario 3: greylist command -> approval wait -> execute ----------------
def test_flow_03_greylist_approval(
    frozen_time, patch_leaves, control_trace, make_state, make_cm, monkeypatch
):
    config = _load("s1_api_minimal.json")  # anthropic
    patch_leaves()
    monkeypatch.setattr(
        "backend.tools.command_security.check_command",
        lambda command, language="ja": SecurityResult(SecurityVerdict.REQUIRE_APPROVAL, "確認が必要なコマンドです"),
    )
    monkeypatch.setattr(
        "backend.tools.command_executor.execute_command",
        lambda command, language="ja": CommandResult(success=True, output="(コマンド実行結果)"),
    )

    class _DummyLog:
        def add(self, *a, **k):
            pass

    monkeypatch.setattr(
        "backend.tools.command_executor.get_command_log", lambda: _DummyLog()
    )
    llm = FakeLLM(script=[
        tool_call("execute_command", {"command": "shutdown /s", "reason": "PCを休ませたい"},
                  content="コマンドを実行するね。"),
        reply("実行しておいたよ！"),
    ])
    state = make_state(
        conversation_active=True,
        command_execution_enabled=True,
        active_llm_cache=OrderedDict({"lumina": llm}),
    )
    state.command_approval_event = _AutoApproveEvent(state, result="accepted")
    cm = make_cm(state, config)

    result = cm.generate_reply("PCを消しといて", character_id="lumina")

    assert_golden("flow_03_greylist_approval", render_trace(control_trace.events, result))
