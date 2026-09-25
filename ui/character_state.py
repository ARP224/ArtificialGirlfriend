"""
ui/character_state.py

Active-character state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the "active character" knobs the UI keeps around: the active
character's id, the path to its icon, and its display name. It came out of the
AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` (as the
``active_character_id`` / ``active_character_icon`` / ``active_character_name``
fields) and are read/written by character switching (``ui/character_ui.py``),
the conversation flow (``ui/conversation.py``), rendering (``ui/components.py``)
and various status/handler call sites. NOTE: this is the *UI-side* active
character; the backend god-object ``_backend_state.active_character_id`` is a
separate object deliberately kept at the backend root and is
unaffected by this carve-out.

Placement: mirrors the sibling carve-outs (DataState / AudioState /
AutoPromptState / TalkThemeState / FeatureState) but lives under ``ui/`` because
this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The character
*behavior* (switching, activation, rendering) stays in its call sites and reaches
these fields through AppState's backward-compat properties, so the model-facing
contract is unaffected (character display is a UI/real-machine path, outside the
harness — so the smoke import + a real-AppState round-trip are the gate).
"""

from typing import Optional


class CharacterState:
    """Owns the active-character knobs carved out of AppState.

    AppState holds a single instance as ``app_state.character`` and keeps
    backward-compat properties for the legacy ``app_state.active_character_id`` /
    ``app_state.active_character_icon`` / ``app_state.active_character_name``
    access paths, so existing call sites are unchanged while ownership now lives
    here. Defaults mirror the values that used to be declared directly on
    ``AppState``.
    """

    def __init__(self) -> None:
        self.id: Optional[str] = None
        self.icon: Optional[str] = None
        self.name: Optional[str] = None
