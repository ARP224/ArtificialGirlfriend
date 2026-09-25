"""
ui/display_state.py

Display-settings state for the Artificial Girlfriend UI (UI/state layer).

This is the home for UI display settings. Currently it owns the chat font size (in
pixels); it is the seed of the "display" cluster and may grow as further display
knobs are carved off AppState. It came out of the AppState god-object split.

This field used to live directly on ``ui/state.py``'s ``AppState`` (as the
``chat_font_size`` field) and is read by rendering (``ui/components.py`` /
``ui/pages.py`` / ``ui/mobile_app.py``), written by the font-size slider handler
(``ui/conversation.py::update_chat_font_size``) and persisted by
``ui/settings_manager.py``.

Placement: mirrors the sibling carve-outs (DataState / AudioState /
AutoPromptState / TalkThemeState / FeatureState) but lives under ``ui/`` because
this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The display
*behavior* (slider handling, persistence, rendering) stays in its call sites and
reaches this field through AppState's backward-compat property, so the
model-facing contract is unaffected (display is a UI/real-machine path, harness
out — so the smoke import + a real-AppState round-trip are the gate).
"""


class DisplayState:
    """Owns the UI display settings carved out of AppState.

    AppState holds a single instance as ``app_state.display`` and keeps a
    backward-compat property for the legacy ``app_state.chat_font_size`` access
    path, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.font_size: int = 14  # Chat font size in pixels (default 14px)
