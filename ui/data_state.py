"""
ui/data_state.py

Data-collections state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the in-memory *data collections* that the UI keeps around
for display: the chat history (rendered into the conversation panel) and the
log messages (surfaced in the log view). (The error-popup queue moved to
backend/shared/popup_state.py when popups switched to WS+JS delivery — the
WS transport drains it, so it must live below both ui and server.)
It came out of the AppState god-object split —
these fields used to live directly on ``ui/state.py``'s ``AppState`` and are
read/written in place by ``ui/components.py`` (history/log rendering) and
``ui/conversation.py`` (history load/clear).

Placement: mirrors the backend state carve-outs (CommandState / TokenState /
MemoryCaches) but lives under ``ui/`` because this is UI-layer state. It is a
pure stdlib state container.

Layering: this module owns no behavior — only the state container. The
manipulation methods (``append_chat_message`` / ``add_log_message`` /
``clear_chat_history``) stay on AppState because they also touch the chat-HTML-cache fields
(``_chat_history_version`` / ``_cached_chat_html``, a different cluster) and the
module-level logger / thread-local recursion guard. Those methods reach these
collections through AppState's backward-compat properties, so the model-facing
contract is unaffected (chat_history / log_messages are UI display paths, harness
out — so the smoke import + a real-AppState round-trip are the gate).
"""

from typing import List, Tuple


class DataState:
    """Owns the data collections carved out of AppState.

    AppState holds a single instance as ``app_state.data`` and keeps
    backward-compat properties for the legacy ``app_state.<field>`` access paths,
    so existing call sites are unchanged while ownership now lives here. Defaults
    mirror the ``field(default_factory=list)`` values that used to be declared
    directly on ``AppState``.
    """

    def __init__(self) -> None:
        # (speaker, text, is_ai, timestamp, images, documents)
        self.chat_history: List[Tuple[str, str, bool, str, list]] = []
        self.log_messages: List[str] = []
