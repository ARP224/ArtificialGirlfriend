"""
backend/shared/token_state.py

Token / model-context tracking state for Artificial Girlfriend (shared/state layer).

This is the home for the *runtime* token-budgeting bookkeeping that the
conversation subsystem keeps in memory: the per-model context-window cache and
model-info cache, the currently-active context window, the token-estimation
method selector, and the prompt-truncation history (plus the last full prompt /
last truncation info kept for diagnostics). It came out of the
BackendState god-object split: these fields used to live
directly on ``backend/backend.py``'s ``BackendState`` and are read/written in
place by ``backend/conversation/character_manager.py`` (model context-window
discovery / activation), ``backend/conversation/prompt_builder.py`` (truncation
event logging on the prompt-build path), and ``backend.py`` (the system-status
report that surfaces the caches/history).

Placement: mirrors the other state carve-outs (CommandState / ActivationRegistry
/ MemoryCaches) — it lives in ``backend/shared/`` and is pure stdlib state.

Layering: this module owns no behavior — only the state container. The context-
window discovery / truncation logic stays with its callers and reaches these
fields via the backward-compat properties on BackendState. So this module has no
backend/ui imports — only the standard library — and depends downward only.

Note on ``prompt_truncation_history``: it is a pure tracking/logging side-effect
recorded *after* ``token_manager.manage_prompt`` has already decided what to
truncate; it does not feed back into the prompt body, so moving its ownership
here is invisible to the model-facing contract (the byte-exact prompt goldens
stay green — the property returns the same list object, so the in-place
``.append(...)`` and the ``[-100:]`` reassignment behave identically).
"""

from typing import Any, Dict, List, Optional


class TokenState:
    """Owns the token / model-context tracking fields carved out of BackendState.

    BackendState holds a single instance as ``_backend_state.token`` and keeps
    backward-compat properties for the legacy ``_backend_state.<field>`` access
    paths, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be set directly in
    ``BackendState.__init__``.
    """

    def __init__(self) -> None:
        # Model context management
        self.model_context_cache: Dict[str, int] = {}
        self.model_info_cache: Dict[str, Dict[str, Any]] = {}
        self.active_model_context_window: Optional[int] = None

        # Prompt truncation tracking
        self.prompt_truncation_history: List[Dict] = []
        self.last_full_prompt_before_truncation: Optional[str] = None
        self.last_truncation_info: Optional[Dict] = None
