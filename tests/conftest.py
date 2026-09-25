"""Shared test seams for the verification harness.

Hard rule: the three load-bearing files (app.py / conversation_manager.py /
backend.py) are NOT edited. Every seam below is reached via state injection or
monkeypatch only.

The seam set deliberately covers every leaf prompt provider that
`_build_prompt_messages` lazy-imports and that hits disk / wall-clock. They are
all patched here — see `patch_leaves`.
"""

from __future__ import annotations

import copy
import datetime as _dt
import logging as _logging
import os as _os
import threading as _threading
import time as _time
from collections import OrderedDict as _OrderedDict
from types import SimpleNamespace

import pytest

# The app emits INFO logs through the root logger during prompt building. With
# pytest's default fd-level capture, that emission path closes a file descriptor
# pytest relies on -> "I/O operation on closed file". Silencing stdlib logging
# during tests removes the trigger without touching the load-bearing files and
# keeps default capture (so test output stays readable).
_logging.disable(_logging.CRITICAL)

from backend.llm.api_integration import APIResponse
from backend.conversation_manager import ConversationManager

# ---------------------------------------------------------------------------
# UI language pin (deterministic t() output)
# ---------------------------------------------------------------------------
# backend.shared.i18n resolves the UI language ONCE per process from the OS
# (Windows: GetUserDefaultUILanguage — env vars ignored; macOS/Linux: LANG).
# Left alone, `pytest tests/` turns green or red depending on the shell it
# runs in (Mac 実測 2026-08-17: LANG=C → t() returns en → 2 tests asserting
# Japanese t() text failed; same suite green on a ja Windows box). Pin it here
# so the gate is machine-independent. Default = "en" on purpose: it is the
# opposite of the ja dev box, so a test that pastes on-screen Japanese into an
# assert fails at authoring time instead of on the other machine. Override
# with AG_TEST_LANG=ja (or any locales/<xx>.json) — Verified-by lines should
# run both. Tests that need one language explicitly can monkeypatch
# `backend.shared.i18n.resolve_language` themselves after resetting `_language`.
# Done in pytest_configure (before collection) so even import-time t() calls
# in product modules see the pinned language. Product code / resolution order
# untouched — every seam lives in conftest, never in product code.


def pytest_configure(config):
    import backend.shared.i18n as _i18n
    lang = _os.environ.get("AG_TEST_LANG", "en")
    _i18n._language = None  # drop anything resolved during conftest import
    _i18n.resolve_language = lambda: lang
    config._ag_test_lang = lang


def pytest_report_header(config):
    return f"AG UI language pinned to '{getattr(config, '_ag_test_lang', '?')}' (AG_TEST_LANG)"

# ---------------------------------------------------------------------------
# Frozen clock (deterministic prompts)
# ---------------------------------------------------------------------------
# `_build_prompt_messages` does a *function-local* `from datetime import
# datetime` (conversation_manager.py:2365), which shadows the module-level
# binding. Patching `conversation_manager.datetime` therefore does NOT work.
# We patch the `datetime` class on the `datetime` *module* instead — the
# function-local from-import resolves the attribute at call time, so it picks
# up the frozen subclass.
FIXED_DATETIME = _dt.datetime(2026, 6, 20, 14, 30, 0)
FIXED_EPOCH = 1_781_000_000.0  # arbitrary fixed wall clock for time.time()


class _FrozenDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirror datetime.now signature
        return FIXED_DATETIME


@pytest.fixture
def frozen_time(monkeypatch):
    """Freeze `datetime.now()` and `time.time()` to fixed values."""
    monkeypatch.setattr(_dt, "datetime", _FrozenDateTime)
    monkeypatch.setattr(_time, "time", lambda: FIXED_EPOCH)
    return FIXED_DATETIME


# ---------------------------------------------------------------------------
# Hermetic Ollama capability lookup (C5): the tool gates consult
# ollama_capabilities.get_caps for ollama configs. The real function reads the
# developer's character_data/ollama_capabilities.json and may attempt HTTP —
# neither is allowed in tests. Default = 判定不能 (fail-closed) so every
# pre-C5 ollama fixture keeps its "all gates closed" bytes. Scenarios that
# exercise a tools-capable Ollama model register it via `fake_ollama_caps`.
# tests/smoke/test_ollama_capabilities.py re-pins the REAL function (it tests
# the cache itself, with its own HTTP fakes).
# ---------------------------------------------------------------------------
_FAKE_OLLAMA_CAPS: dict = {}
_UNKNOWN_CAPS = {"known": False, "tools": False, "vision": False,
                 "completion": False, "embedding": False,
                 "context_length": None}


@pytest.fixture(autouse=True)
def _pin_ollama_caps(monkeypatch):
    monkeypatch.setattr(
        "backend.llm.ollama_capabilities.get_caps",
        lambda model_name: dict(
            _FAKE_OLLAMA_CAPS.get(model_name, _UNKNOWN_CAPS)),
    )


@pytest.fixture
def fake_ollama_caps():
    """モデル→capability を登録する(テスト内のみ・終了時に掃除)。

    Usage: fake_ollama_caps("qwen-tools-test", tools=True, vision=False)
    """
    added = []

    def _set(model_name, tools=False, vision=False, completion=True,
             embedding=False, context_length=None):
        # completion既定True: チャットモデルは全て持つ。embedding専用モデルの
        # シナリオだけが completion=False / embedding=True を明示する
        _FAKE_OLLAMA_CAPS[model_name] = {
            "known": True, "tools": tools, "vision": vision,
            "completion": completion, "embedding": embedding,
            "context_length": context_length,
        }
        added.append(model_name)

    yield _set
    for name in added:
        _FAKE_OLLAMA_CAPS.pop(name, None)


# ---------------------------------------------------------------------------
# Hermetic STT engine (the dispatch reads the developer's real
# user_settings.json — a machine with the OpenAI toggle ON must not turn the
# suite red). Tests of the API route re-patch _stt_engine_config themselves.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _pin_stt_engine_local(monkeypatch):
    from audio_input.audio_input import AudioInputManager
    monkeypatch.setattr(
        AudioInputManager,
        "_stt_engine_config",
        lambda self: ("faster_whisper", "whisper-1"),
    )


# ---------------------------------------------------------------------------
# ELYTH network guard: ELYTH HTTP must never leave the test
# process. All product HTTP goes through the module-level `requests` binding
# in backend.elyth.elyth_api — replace that binding (NOT requests.request
# globally, which other subsystems legitimately use) with a guard that fails
# loudly. Tests that need HTTP install their own fake over the same binding.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_elyth_http(monkeypatch):
    import requests as _requests

    import backend.elyth.elyth_api as _elyth_api

    class _GuardRequests:
        exceptions = _requests.exceptions  # keep except-clauses resolvable

        @staticmethod
        def request(*args, **kwargs):
            raise AssertionError(
                "Unpatched ELYTH HTTP call in tests — install a fake on "
                "backend.elyth.elyth_api.requests"
            )

    monkeypatch.setattr(_elyth_api, "requests", _GuardRequests)


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------
class FakeLLM:
    """Scripted LLM driven by the control-flow trace tests; the prompt-assembly
    golden never calls the LLM.

    Each ``invoke`` returns the next scripted item, which must be an
    ``APIResponse`` (the real production response type — the loop reads
    ``result_msg.content`` / ``result_msg.tool_calls`` as attributes, so a bare
    dict would not work). Use ``reply(...)`` / ``tool_call(...)`` to build a
    script faithfully.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def invoke(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        if self.script:
            return self.script.pop(0)
        return APIResponse(content="(fake reply)")


def reply(text):
    """A final assistant turn (text, no tool calls)."""
    return APIResponse(content=text)


def tool_call(name, arguments, content="", call_id="call_0"):
    """An assistant turn that requests one tool call."""
    return APIResponse(
        content=content,
        tool_calls=[{"id": call_id, "name": name, "arguments": arguments}],
    )


class FakeMemoryManager:
    """Stand-in for MemoryManager. Returns fixed memory/message lists so the
    semantic-search non-determinism is removed and only assembly is observed.

    Shapes mirror the real return contracts (Explore-verified 2026-06-20):
      get_memories_for_prompt -> List[{category, content, id}]
      get_messages_for_prompt -> List[{role, content, metadata?, images?, documents?}]
    """

    def __init__(self, memories=None, messages=None):
        self._memories = memories or []
        self._messages = messages or []

    def get_memories_for_prompt(self, user_text, provider):
        return copy.deepcopy(self._memories)

    def get_messages_for_prompt(self, limit=20):
        # `_build_prompt_messages` mutates message dicts; hand out deep copies
        # so scenarios stay isolated.
        return copy.deepcopy(self._messages)[-limit:]


# ---------------------------------------------------------------------------
# State + ConversationManager factories
# ---------------------------------------------------------------------------
_STATE_DEFAULTS = dict(
    talk_theme_enabled=False,
    pc_status_enabled=False,
    screen_capture_enabled=False,
    server_mode=False,
    command_execution_enabled=False,
    notes_enabled=False,
    camera_capture_enabled=False,
    image_generation_enabled=False,
    deep_search_enabled=False,
    elyth_enabled=False,
)


@pytest.fixture
def make_state():
    """Build a lightweight stand-in for BackendState.

    Only the attributes `_build_prompt_messages` / `_should_include_*` touch are
    provided. Flags default to disabled; scenarios flip what they need.
    """

    def _make(**overrides):
        base = dict(_STATE_DEFAULTS)
        base.update(overrides)
        base.setdefault("memory_managers", {})
        base.setdefault("short_term_buffer", {})
        base.setdefault("prompt_truncation_history", [])
        # OrderedDict, not {} — production is an LRU OrderedDict and the cache-hit
        # path calls move_to_end, which plain dict lacks.
        base.setdefault("active_llm_cache", _OrderedDict())
        base.setdefault("pending_requests", {})
        # Reached only by the control-flow path when no memory_manager is
        # registered: _generate_reply_task falls back to the in-memory
        # short_term_buffer under memory_lock (conversation_manager.py:2253-2260).
        base.setdefault("message_count_cache", {})
        base.setdefault("memory_lock", _threading.Lock())
        return SimpleNamespace(**base)

    return _make


@pytest.fixture
def make_cm():
    """Construct a ConversationManager around a fake state.

    `config_loader` returns the scenario config verbatim; only the ELYTH gate
    (`_should_include_elyth_tools`) re-loads config through it.
    """

    def _make(state, config):
        return ConversationManager(
            state,
            lambda character_id: config,
            lambda *a, **k: None,
            lambda *a, **k: None,
        )

    return _make


@pytest.fixture
def fake_memory_manager():
    """Return the FakeMemoryManager class so tests construct it with scenario
    data: `fake_memory_manager(memories=[...], messages=[...])`."""
    return FakeMemoryManager


# ---------------------------------------------------------------------------
# Leaf prompt providers (non-deterministic — disk / wall-clock). Patched to
# fixed values. The "mock memory, observe only assembly" principle is
# applied to the leaf nodes. Scenarios override individual entries.
# ---------------------------------------------------------------------------
_LEAF_DEFAULTS = dict(
    # relationship & notes are deterministic *content*, but read disk -> freeze.
    # relationship is ALWAYS built for API providers, so its default is a fixed
    # non-empty string (scenario 3 expects <relationship> present).
    relationship="【関係性】テスト用の固定された関係性サマリです。",
    note="【ノート】テスト用の固定ノート本文。",
    elyth_note="【ELYTHノート】テスト用の固定ELYTHノート。",
    location="",  # empty -> no <user_location> unless overridden
    has_valid_location=False,  # skip the 20-turn location-expiry branch
    api_settings={"google": {"api_key": ""}},  # image-gen gate OFF by default
    image_generation_model=("", ""),  # image-gen gate OFF by default
    google_maps_api_key="",  # map-tools gate OFF by default
    thumbnail=None,  # get_thumbnail_path_for -> None
    # camera-tool gate: a provider page is "connected" (feature flag still
    # rules first, so camera-off scenarios are unaffected)
    ambient_provider_available=True,
)


@pytest.fixture
def patch_leaves(monkeypatch):
    """Patch every non-deterministic leaf provider used by
    `_build_prompt_messages`. Call with overrides, e.g.
    `patch_leaves(location="現在地: 固定市", has_valid_location=True)`.
    """

    def _apply(**overrides):
        v = dict(_LEAF_DEFAULTS)
        v.update(overrides)
        monkeypatch.setattr(
            "backend.memory.relationship_manager.build_relationship_prompt",
            lambda character_id, language="ja": v["relationship"],
        )
        monkeypatch.setattr(
            "backend.memory.note_manager.build_note_prompt",
            lambda character_id, language="ja": v["note"],
        )
        monkeypatch.setattr(
            "backend.elyth.elyth_note_manager.build_elyth_note_prompt",
            lambda character_id, include_system_prompt=False, language="ja": v["elyth_note"],
        )
        monkeypatch.setattr(
            "backend.tools.location_manager.get_location_for_prompt",
            lambda language="ja": v["location"],
        )
        monkeypatch.setattr(
            "backend.tools.location_manager.has_valid_location",
            lambda: v["has_valid_location"],
        )
        # Live Camera provider presence (Phase 3): the camera-tool gate now
        # requires a connected provider page. Freeze it to "available" so the
        # all-tools scenarios keep exercising camera tool inclusion; scenarios
        # with camera_capture_enabled=False gate off before this check anyway.
        monkeypatch.setattr(
            "backend.shared.ambient_camera_state.is_provider_available",
            lambda: v["ambient_provider_available"],
        )
        monkeypatch.setattr(
            "backend.shared.api_settings.load_api_settings",
            lambda: copy.deepcopy(v["api_settings"]),
        )
        monkeypatch.setattr(
            "backend.shared.api_settings.get_image_generation_model",
            lambda: v["image_generation_model"],
        )
        monkeypatch.setattr(
            "backend.shared.api_settings.get_google_maps_api_key",
            lambda: v["google_maps_api_key"],
        )
        monkeypatch.setattr(
            "backend.shared.image_storage.get_thumbnail_path_for",
            lambda full_path: v["thumbnail"],
        )
        return v

    return _apply


# ---------------------------------------------------------------------------
# Seams for the control-flow / smoke tests.
# Defined now so later sessions don't re-derive them.
# ---------------------------------------------------------------------------
@pytest.fixture
def ui_spy():
    """Record `send_ui_update` calls as an ordered event list."""
    events = []

    def spy(*args, **kwargs):
        events.append({"args": args, "kwargs": kwargs})

    spy.events = events
    return spy


@pytest.fixture
def bypass_resources(monkeypatch):
    """Make the resource gate always pass (returns None)."""
    monkeypatch.setattr(
        "backend.conversation_manager.ConversationManager._check_resources_cached",
        lambda self: None,
    )


@pytest.fixture
def sync_enqueue(monkeypatch):
    """Run enqueued LLM tasks synchronously (no thread/future)."""

    def _direct(self, task_callable, *args, **kwargs):
        kwargs.pop("request_id", None)
        kwargs.pop("timeout", None)
        return task_callable(*args, **kwargs)

    monkeypatch.setattr(
        "backend.conversation_manager.ConversationManager._enqueue_llm_task",
        _direct,
    )


# ---------------------------------------------------------------------------
# Control-flow trace recorder
# ---------------------------------------------------------------------------
# Volatile / machine-specific payload keys to strip from captured UI events so
# the snapshot stays byte-stable (base64 blobs, random playback ids).
_VOLATILE_UI_KEYS = {
    "audio_base64", "image_base64", "thumbnail_base64", "full_base64",
    "playback_id", "lipsync_frames",
}


class _Trace:
    """Ordered control-flow event recorder.

    Captures, in execution order: the single per-turn task enqueue, every TTS
    play point (text only — the audio itself is non-deterministic), and every
    ``send_ui_update`` firing (type + payload with volatile keys stripped).
    """

    def __init__(self):
        self.events = []

    def add(self, kind, **fields):
        self.events.append({"event": kind, **fields})


@pytest.fixture
def control_trace(monkeypatch):
    """Wire the seams the control-flow tests need and return a `_Trace` recorder.

    - `_enqueue_llm_task` -> run the task synchronously and record the enqueue
      (task name only; the real request_id is a uuid4 = non-deterministic).
    - `_play_tts_blocking` -> no-op that records the spoken text (the real one
      synthesises audio AND blocks on a browser ack, neither deterministic).
    - `send_ui_update` -> record (type, sanitised data).
    - `consume_unrecorded_location` -> None (avoid global/disk state leak; the
      branch that uses it is skipped anyway when no memory_manager is set).
    """
    trace = _Trace()

    def _enqueue(self, task_callable, *args, **kwargs):
        kwargs.pop("request_id", None)
        kwargs.pop("timeout", None)
        trace.add("enqueue", task=task_callable.__name__)
        return task_callable(*args, **kwargs)

    def _tts(self, text):
        trace.add("tts", text=text)

    def _send_ui_update(event_type, data=None, **kwargs):
        payload = dict(data or {})
        payload.update(kwargs)
        clean = {k: v for k, v in payload.items() if k not in _VOLATILE_UI_KEYS}
        trace.add("ui_update", type=event_type, data=clean)

    monkeypatch.setattr(
        "backend.conversation_manager.ConversationManager._enqueue_llm_task",
        _enqueue,
    )
    monkeypatch.setattr(
        "backend.conversation_manager.ConversationManager._play_tts_blocking",
        _tts,
    )
    monkeypatch.setattr(
        "backend.server.websocket_server.send_ui_update", _send_ui_update
    )
    monkeypatch.setattr(
        "backend.tools.location_manager.consume_unrecorded_location", lambda: None
    )
    return trace
