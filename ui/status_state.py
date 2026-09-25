"""
ui/status_state.py

Subsystem-status / history-load state for the Artificial Girlfriend UI
(UI/state layer).

This is the home for the UI's "are the subsystems up?" status flags and the
history-load progress/error state: whether the backend is reachable, whether
audio input/output are available, whether a history load is in progress and the
last history-load error message. It came out of the AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` under the
"# State tracking" comment (as the ``backend_available`` / ``audio_input_available``
/ ``audio_output_available`` / ``is_loading_history`` / ``history_load_error``
fields). They are written at init / on-error by ``ui/app.py`` and the
history-load flow (``ui/conversation.py``) and read for conditional UI behavior by
``ui/status_checker.py``, ``ui/components.py``, ``ui/character_ui.py`` and
``ui/conversation.py`` (e.g. audio_output_available gates the TTS / speechless
fallback). The dead write-only ``initialization_complete`` flag that used to sit
in this block was removed as dead code (稜 decision 2026-06-28).

Placement: mirrors the sibling carve-outs (DataState / AudioState / ...) but lives
under ``ui/`` because this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The status
*behavior* (health checks, history load) stays in its call sites and reaches these
flags through AppState's backward-compat properties, so the model-facing contract
is unaffected (these are UI/real-machine paths, outside the harness — so the smoke
import + a real-AppState round-trip are the gate).
"""

from typing import Optional


class StatusState:
    """Owns the subsystem-status / history-load flags carved out of AppState.

    AppState holds a single instance as ``app_state.status`` and keeps
    backward-compat properties for the legacy ``app_state.backend_available`` /
    ``app_state.audio_input_available`` / ``app_state.audio_output_available`` /
    ``app_state.is_loading_history`` / ``app_state.history_load_error`` access
    paths, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.backend_available: bool = False
        self.audio_input_available: bool = False
        self.audio_output_available: bool = False
        self.is_loading_history: bool = False
        self.history_load_error: Optional[str] = None
