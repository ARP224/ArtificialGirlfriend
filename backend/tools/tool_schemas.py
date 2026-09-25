"""Canonical tool-schema / provider-formatting helpers (ST3-B4・C1合流).

The 8 ``get_*_tool_definitions_for_provider`` functions across the backend each
carried a private copy of ``_to_gemini_schema`` (×8) plus a hand-written
provider-formatting loop with the *same* four branches (ollama / openai+xai /
anthropic / google). This module is the single canonical home for both:

- :func:`to_gemini_schema` — recursive JSON-Schema → Gemini (uppercase types)
  converter. This is the **superset** form: it recurses into both ``properties``
  and ``items`` so array element schemas are converted too. Seven of the eight
  callers carried a copy without the ``items`` branch, but their tool schemas
  contain no arrays, so the converted output is byte-identical; only elyth_tools
  has arrays and already used this superset form.
- :func:`format_tools_for_provider` — the canonical "format a list of tool
  descriptors for one provider" loop that every caller open-coded.

Invariant (golden ``tests/golden/test_tool_schemas.py``): the JSON returned by
each ``get_*_tool_definitions_for_provider`` must stay byte-exact (per language).
Module-load imports are stdlib only; :func:`localize_tools` lazily imports the
shared-leaf prompt catalog (backend.shared.prompt_i18n) at call time.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List


def to_gemini_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Convert JSON Schema type values to uppercase for Gemini API compatibility.

    Recurses through ``properties`` (object fields) and ``items`` (array element
    schemas) so nested ``type`` values are uppercased too. All other keys pass
    through unchanged.
    """
    result: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            result[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            result[key] = {k: to_gemini_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = to_gemini_schema(value)
        else:
            result[key] = value
    return result


def _localize_schema(schema: Dict[str, Any], key_prefix: str, language: str, prompt_text) -> None:
    """Replace description strings in a JSON-Schema subtree from the prompt catalog.

    Key scheme mirrors the catalog: ``tool.<tool>.param.<prop>[.items][.<sub>...]``.
    """
    props = schema.get("properties")
    if isinstance(props, dict):
        for pname, pschema in props.items():
            if "description" in pschema:
                pschema["description"] = prompt_text(f"{key_prefix}.{pname}", language)
            _localize_schema(pschema, f"{key_prefix}.{pname}", language, prompt_text)
            items = pschema.get("items")
            if isinstance(items, dict):
                if "description" in items:
                    items["description"] = prompt_text(f"{key_prefix}.{pname}.items", language)
                _localize_schema(items, f"{key_prefix}.{pname}.items", language, prompt_text)


def localize_tools(tools: List[Dict], language: str) -> List[Dict]:
    """Return a deep-copied tool list with description strings taken from the
    prompt catalog (keys ``tool.<name>.description`` / ``tool.<name>.param.*``).

    Tool structure (names, types, enums, required) stays in code; only the
    LLM-facing natural-language descriptions are language-switched.
    """
    from backend.shared.prompt_i18n import prompt_text

    localized: List[Dict] = []
    for tool in tools:
        t = copy.deepcopy(tool)
        t["description"] = prompt_text(f"tool.{t['name']}.description", language)
        _localize_schema(t.get("parameters", {}), f"tool.{t['name']}.param", language, prompt_text)
        localized.append(t)
    return localized


def format_tools_for_provider(provider: str, tools: List[Dict]) -> List[Dict]:
    """Format tool descriptors for the given provider's tool-call API.

    ``tools`` is a list of ``{"name", "description", "parameters"}`` descriptors.
    Returns provider-shaped definitions:

    - ``ollama`` → ``{"type": "function", "function": {...}}`` (OpenAI Chat
      Completions 形式。openai/xai の Responses API フラット形とは別物)
    - ``openai`` / ``xai`` → flat ``{"type": "function", ...}`` (Responses API)
    - ``anthropic`` → ``{"name", "description", "input_schema"}``
    - ``google`` → ``{"function_declarations": [{...}]}`` with an uppercased
      (Gemini) parameter schema

    Any other provider yields ``[]`` (no branch appends).
    """
    formatted: List[Dict] = []
    for tool in tools:
        if provider == "ollama":
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            })
        elif provider in ("openai", "xai"):
            formatted.append({
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            })
        elif provider == "anthropic":
            formatted.append({
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            })
        elif provider == "google":
            params = to_gemini_schema(tool["parameters"])
            formatted.append({
                "function_declarations": [{
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": params,
                }]
            })

    return formatted
