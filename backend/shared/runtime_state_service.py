"""
backend/runtime_state_service.py

Domain adapter: the runtime-state provider that reads the spine god object
``backend.backend._backend_state`` (V2/V3 inversion, B3.5).

The WebSocket transport reads runtime values through
:mod:`backend.shared.runtime_state`; this module registers the provider that actually
pulls them off ``_backend_state``. Imported for side effect (self-registration),
like B3's :mod:`backend.shared.feature_toggle_service`.

``_backend_state`` is resolved at *call time* (via ``getattr`` on the
``backend.backend`` module) rather than imported at module load, so:

  * test seams that monkeypatch ``backend.backend._backend_state`` are honoured
    (mirrors ``backend.shared.feature_toggle_service`` from B3), and
  * no import cycle forms when this module is imported during start-up.

Scope is *reads only* — the snapshot is a copy of current values. Writes back
into ``_backend_state`` (command-approval result, ELYTH stop event, image-buffer
append) stay in the transport and are deferred to BackendState teardown
(B6/B10, §3.3), since the spine must not be edited in ST3.
"""

import logging
from typing import Dict

from backend.shared.runtime_state import (
    _DEFAULT_FEATURE_STATUS,
    _default_snapshot,
    register_runtime_state_provider,
)

logger = logging.getLogger(__name__)


def _read_runtime_snapshot() -> Dict:
    """Build a snapshot from the live ``_backend_state`` (or defaults if unset)."""
    # Resolve the god object at call time so monkeypatch seams are honoured and
    # no import cycle forms at module load.
    from backend import backend as _backend

    state = getattr(_backend, "_backend_state", None)
    if state is None:
        return _default_snapshot()

    feature_status = {
        key: getattr(state, key, default)
        for key, default in _DEFAULT_FEATURE_STATUS.items()
    }
    return {
        "feature_status": feature_status,
        "active_character_id": state.active_character_id or "",
        "image_count": state.image_buffer.get_count(),
        "conversation_active": bool(state.conversation_active),
        "is_generating": bool(state.is_generating),
        "document_count": state.document_buffer.get_count(),
        "document_chars": state.document_buffer.get_total_chars(),
    }


register_runtime_state_provider(_read_runtime_snapshot)
