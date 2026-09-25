"""
ui/prompt_log_state.py

Prompt-log state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the "last prompt" knobs the UI keeps around for the prompt
inspector display: the most recent prompt text, when it was generated, and which
character it was for. It came out of the AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` (as the
``last_prompt_text`` / ``last_prompt_timestamp`` / ``last_prompt_character``
fields) and are written by ``ui/components.py`` (prompt-generation display) and
reset by ``ui/character_ui.py`` (on character switch).

Placement: mirrors the sibling carve-outs (DataState / AudioState /
AutoPromptState / TalkThemeState / FeatureState) but lives under ``ui/`` because
this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The prompt-log
*behavior* (recording the last prompt, resetting it) stays in its call sites and
reaches these fields through AppState's backward-compat properties, so the
model-facing contract is unaffected (prompt display is a UI/real-machine path,
outside the harness — so the smoke import + a real-AppState round-trip are the gate).
"""

from typing import Optional


class PromptLogState:
    """Owns the prompt-log knobs carved out of AppState.

    AppState holds a single instance as ``app_state.prompt_log`` and keeps
    backward-compat properties for the legacy ``app_state.last_prompt_text`` /
    ``app_state.last_prompt_timestamp`` / ``app_state.last_prompt_character``
    access paths, so existing call sites are unchanged while ownership now lives
    here. Defaults mirror the values that used to be declared directly on
    ``AppState``.
    """

    def __init__(self) -> None:
        self.text: str = "No prompts generated yet in this session."
        self.timestamp: Optional[str] = None
        self.character: Optional[str] = None
