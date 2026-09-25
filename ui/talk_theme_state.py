"""
ui/talk_theme_state.py

Talk-theme state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the "talk theme" knobs the UI keeps around: a change-detection
version counter, the cached current-theme string, and the flag that records whether
the theme panel is enabled (i.e. a conversation has started). It came out of the
AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` (as the
``_talk_theme_version`` / ``_cached_talk_theme`` / ``_theme_panel_enabled`` private
fields) and are read/written by ``ui/conversation_functions.py`` (theme refresh /
update / clear helpers and the panel enable/disable on conversation start/stop).

Placement: mirrors the sibling carve-outs (DataState / AudioState / AutoPromptState;
backend CommandState / TokenState / MemoryCaches) but lives under ``ui/`` because this
is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The talk-theme
*behavior* (refresh/update/clear, panel toggling) stays in
``ui/conversation_functions.py`` and reaches these fields through AppState's
backward-compat properties, so the model-facing contract is unaffected (talk theme is
a UI/real-machine path, outside the harness — so the smoke import + a real-AppState
round-trip are the gate).
"""


class TalkThemeState:
    """Owns the talk-theme knobs carved out of AppState.

    AppState holds a single instance as ``app_state.talk_theme_state`` and keeps
    backward-compat properties for the legacy ``app_state._talk_theme_version`` /
    ``app_state._cached_talk_theme`` / ``app_state._theme_panel_enabled`` access
    paths, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.version: int = 0  # Version for detecting changes
        self.cached_theme: str = ""  # Cache for current theme
        # Whether theme panel is enabled (conversation started)
        self.panel_enabled: bool = False
