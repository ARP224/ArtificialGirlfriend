"""
backend/shared/command_state.py

Runtime command-approval state for Artificial Girlfriend (shared/state layer).

This is the home for the *runtime* command-approval handshake — the in-memory
fields that coordinate the "AI proposes a shell command → human approves/denies"
flow between the conversation thread (which blocks waiting for a decision) and
the UI/websocket threads (which set the decision). It came out of the
BackendState god-object split: these five fields used
to live directly on ``backend/backend.py``'s ``BackendState`` and be mutated
in place by ``conversation_manager`` (proposer/waiter) and by
``websocket_server`` / ``ui.conversation`` / ``ui.hotkey_service`` (deciders).

Layering: this module owns no behavior — only the state container. The approval
flow logic (``_set_pending_approval`` / ``_wait_for_approval`` on
``ConversationManager``, and the accept/deny/interrupt setters on the UI side)
is unchanged and reaches these fields via the backward-compat properties on
BackendState. So this module has no backend/ui imports — only the standard
library — and depends downward only (mirrors FeatureState).

Note: the ``command_execution_enabled`` *toggle* is NOT here — it is a feature
flag and was carved out to ``FeatureState``. This module owns only the
per-approval runtime handshake.
"""

import threading
from typing import Dict, Optional


class CommandState:
    """Owns the runtime command-approval handshake carved out of BackendState.

    BackendState holds a single instance as ``_backend_state.command`` and keeps
    backward-compat properties for the legacy ``_backend_state.<field>`` access
    paths, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be set directly in
    ``BackendState.__init__``.
    """

    def __init__(self) -> None:
        self.command_approval_pending: bool = False
        self.command_approval_event: threading.Event = threading.Event()
        self.command_approval_result: Optional[str] = None
        self.command_pending_info: Optional[Dict] = None
        self._pending_interruption: bool = False
