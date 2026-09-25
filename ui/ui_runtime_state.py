"""
ui/ui_runtime_state.py

UI runtime/session state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the transient *runtime* flags the UI keeps around for a
single conversation session: whether a conversation has started, whether a
response is currently being generated, the id of the in-flight request (used to
dedup concurrent responses) and the fallback timer-mode "needs UI update" dirty
flag. It came out of the AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` (as the
``conversation_started`` / ``response_generating`` /
``current_request_id`` / ``_needs_ui_update`` fields). They carry heavy read/write
traffic across the conversation flow (``ui/conversation.py``), rendering
(``ui/components.py``), the hotkey path (``ui/hotkey_service.py`` /
``ui/handlers/hotkey.py``) and the fallback timer (``ui/handlers/lifecycle.py``).

Placement: mirrors the sibling carve-outs (DataState / AudioState / ...) but lives
under ``ui/`` because this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The runtime
*behavior* (conversation lifecycle, response/TTS streaming, request dedup, UI
refresh) stays in its call sites and reaches these flags through AppState's
backward-compat properties, so the model-facing contract is unaffected (these are
UI/real-machine paths, outside the harness — so the smoke import + a real-AppState
round-trip are the gate).
"""

from typing import Optional


class UIRuntimeState:
    """Owns the transient UI runtime flags carved out of AppState.

    AppState holds a single instance as ``app_state.ui_runtime`` and keeps
    backward-compat properties for the legacy ``app_state.conversation_started`` /
    ``app_state.response_generating`` /
    ``app_state.current_request_id`` / ``app_state._needs_ui_update`` access paths,
    so existing call sites are unchanged while ownership now lives here. Defaults
    mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.conversation_started: bool = False
        self.response_generating: bool = False
        # Request tracking to prevent multiple concurrent responses
        self.current_request_id: Optional[str] = None
        # UI update flag for fallback timer mode
        self.needs_ui_update: bool = False
