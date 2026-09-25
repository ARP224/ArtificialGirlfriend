"""
ui/chat_cache_state.py

Chat-HTML render-cache state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the rendered-chat-HTML cache the UI keeps around to avoid
re-rendering the conversation panel on every poll: the cached HTML string and the
monotonic version counter used as its invalidation key. It came out of the
AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` (as the
``_cached_chat_html`` / ``_chat_history_version`` fields). The version counter is
bumped (``+= 1``) on every chat mutation across ``ui/conversation.py`` /
``ui/conversation_functions.py`` and in AppState's own
``append_chat_message`` / ``clear_chat_history`` methods; ``ui/components.py``
reads both to decide whether the cached HTML is still valid.

NOTE: the chat-mutation *methods* (``append_chat_message`` / ``clear_chat_history``)
stay on AppState (they also touch the DataState collections and the module-level
logger); they reach these cache fields through AppState's backward-compat
properties. The ``_chat_history_version += 1`` compound assignment goes through the
compat property getter+setter, so the increment lands on the owner transparently
(same pattern as TalkThemeState's ``version += 1``).

Placement: mirrors the sibling carve-outs (DataState / AudioState / ...) but lives
under ``ui/`` because this is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The caching
*behavior* (render, invalidate) stays in its call sites and reaches these fields
through AppState's backward-compat properties, so the model-facing contract is
unaffected (chat HTML is a UI display path, outside the harness — so the smoke
import + a real-AppState round-trip are the gate).
"""


class ChatCacheState:
    """Owns the chat-HTML render cache carved out of AppState.

    AppState holds a single instance as ``app_state.chat_cache`` and keeps
    backward-compat properties for the legacy ``app_state._cached_chat_html`` /
    ``app_state._chat_history_version`` access paths, so existing call sites are
    unchanged while ownership now lives here. Defaults mirror the values that used
    to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.cached_html: str = ""
        self.history_version: int = 0
