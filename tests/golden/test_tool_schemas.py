"""Tool-schema golden.

Calls each of the 8 ``get_*_tool_definitions_for_provider`` functions across all
5 providers (ollama / openai / anthropic / xai / google) and snapshots the
returned JSON. No real model is needed — these are pure functions of ``provider``.

Why all 5 providers (decision 2026-06-20, 稜): even xAI/Google, which the project
does not currently run, are captured. It is free (just calling functions), and
the provider-specific transforms — especially ``_to_gemini_schema`` ×8 — are the
prime breakage surface for any future tool-registry consolidation. This golden
is the invariant such a refactor must still meet — identical JSON.

Author discipline (anti-"嘘の緑"): after first green, OPEN each
tests/golden/snapshots/tool_*.json and confirm ollama is [], the anthropic
variant uses ``input_schema``, openai/xai use the flat ``type:function`` shape,
and google nests ``function_declarations`` with an uppercased gemini schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # make _snapshot importable
from _snapshot import assert_golden_json  # noqa: E402

from backend.tools.camera_capture import get_camera_tool_definitions_for_provider
from backend.tools.command_executor import get_tool_definitions_for_provider as get_command_tool_definitions_for_provider
from backend.tools.deep_search_tools import get_deep_search_tool_definitions_for_provider
from backend.elyth.elyth_tools import get_elyth_tool_definitions_for_provider
from backend.tools.image_generator import get_image_tool_definitions_for_provider
from backend.tools.map_search_tools import get_map_search_tool_definitions_for_provider
from backend.memory.note_manager import get_note_tool_definitions_for_provider
from backend.tools.talk_theme_tools import get_talk_theme_tool_definitions_for_provider

PROVIDERS = ["ollama", "openai", "anthropic", "xai", "google"]

# 7 of the 8 functions take only ``provider``. elyth (mode arg) is handled
# separately below so all 3 modes are fixed.
_SIMPLE_TOOLS = {
    "tool_camera": get_camera_tool_definitions_for_provider,
    "tool_command": get_command_tool_definitions_for_provider,
    "tool_deep_search": get_deep_search_tool_definitions_for_provider,
    "tool_image": get_image_tool_definitions_for_provider,
    "tool_map_search": get_map_search_tool_definitions_for_provider,
    "tool_note": get_note_tool_definitions_for_provider,
    "tool_talk_theme": get_talk_theme_tool_definitions_for_provider,
}


def test_simple_tool_schemas():
    """7 single-arg tool functions × 5 providers, one snapshot file each.

    ``language="ja"`` must keep these snapshots byte-identical to the
    pre-i18n era (language is now a required argument — no default).
    """
    for name, fn in _SIMPLE_TOOLS.items():
        schemas = {provider: fn(provider, "ja") for provider in PROVIDERS}
        assert_golden_json(name, schemas)


def test_simple_tool_schemas_en():
    """Same 7 functions × 5 providers with language="en" (プロンプトi18n Phase 4)."""
    for name, fn in _SIMPLE_TOOLS.items():
        schemas = {provider: fn(provider, "en") for provider in PROVIDERS}
        assert_golden_json(f"{name}_en", schemas)


def test_elyth_tool_schemas():
    """ELYTH tool defs across all 3 modes × 5 providers (single snapshot).

    The 3 modes select different source-tool sets (session=all, conversation=
    create_post only, winddown=latter-half subset) — each is a distinct schema
    surface worth pinning.
    """
    modes = ["session", "conversation", "winddown"]
    schemas = {
        mode: {p: get_elyth_tool_definitions_for_provider(p, mode=mode, language="ja") for p in PROVIDERS}
        for mode in modes
    }
    assert_golden_json("tool_elyth", schemas)


def test_elyth_tool_schemas_en():
    """ELYTH tool defs, English descriptions (プロンプトi18n Phase 4)."""
    modes = ["session", "conversation", "winddown"]
    schemas = {
        mode: {
            p: get_elyth_tool_definitions_for_provider(p, mode=mode, language="en")
            for p in PROVIDERS
        }
        for mode in modes
    }
    assert_golden_json("tool_elyth_en", schemas)
