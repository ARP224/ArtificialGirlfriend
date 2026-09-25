"""
backend/shared/runtime_state.py

Shared read-only runtime-state snapshot interface.

This is the *shared / infra-interface layer* home for "read a current runtime
value" snapshots. It decouples *readers* (infra/UI code that wants the current
character / feature-toggle / status values) from the *owner* of that state (the
domain god object ``backend.backend._backend_state``). Readers call the
``get_*`` accessors below; the owning side registers a provider via
:func:`register_runtime_state_provider`.

It has no backend or ui dependencies — only the standard library — so any layer
may depend on it downward.

History: the WebSocket transport used to read the domain god object directly
(``from backend.backend import _backend_state``), i.e. an upward /
implementation-facing dependency. This interface was introduced so the
dependency is inverted:

    readers (backend.server.websocket_server)
        -> backend.shared.runtime_state  (this module, the interface)
        <- backend.shared.runtime_state_service  (the domain adapter / provider)

Mirrors the ``backend.shared.ui_events`` and ``backend.shared.feature_commands``
shared leaves. Scope is *reads only*: writes into the owning state
(command-approval result, ELYTH stop event, image-buffer append) mutate domain
state through their own paths, not through this interface.
"""

import logging
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-key defaults for the feature-toggle snapshot. These mirror the historical
# ``_backend_state.<flag> if _backend_state else <default>`` fallbacks at the old
# call sites: talk_theme defaults ON, everything else OFF (notes was flipped to
# OFF 2026-08-01 — gated features must default OFF, see settings_store).
_DEFAULT_FEATURE_STATUS = {
    "pc_status_enabled": False,
    "screen_capture_enabled": False,
    "talk_theme_enabled": True,
    "speechless_enabled": False,
    "command_execution_enabled": False,
    "notes_enabled": False,
    "image_generation_enabled": False,
    "camera_capture_enabled": False,
    "ambient_camera_enabled": False,
    "deep_search_enabled": False,
    "elyth_enabled": False,
}


def _default_snapshot() -> Dict:
    """The safe snapshot used when no provider is registered (e.g. before the
    domain adapter imports, or in tests). Matches the old ``else <default>``
    branches at each call site."""
    return {
        "feature_status": dict(_DEFAULT_FEATURE_STATUS),
        "active_character_id": "",
        "image_count": 0,
        "conversation_active": False,
        "is_generating": False,
        "document_count": 0,
        "document_chars": 0,
    }


# A provider returns a full snapshot dict (same keys as ``_default_snapshot``).
RuntimeStateProvider = Callable[[], Dict]

_provider: Optional[RuntimeStateProvider] = None


def register_runtime_state_provider(provider: RuntimeStateProvider) -> None:
    """Register the owner-side provider that reads the live runtime state.

    Last registration wins (single owner). The domain adapter
    ``backend.shared.runtime_state_service`` self-registers at import time.
    """
    global _provider
    _provider = provider


def unregister_runtime_state_provider() -> None:
    """Drop the registered provider (no error if none); accessors fall back to
    defaults afterwards. Mainly for tests."""
    global _provider
    _provider = None


def get_runtime_snapshot() -> Dict:
    """Return the current runtime snapshot, merged over safe defaults.

    If no provider is registered, or the provider raises/returns falsy, the
    defaults are returned — readers get a usable snapshot instead of an error,
    which preserves the ``if _backend_state else <default>`` semantics every old
    call site already had.
    """
    snapshot = _default_snapshot()
    if _provider is not None:
        try:
            result = _provider()
        except Exception:  # pragma: no cover - owner-side defensive fallback
            logger.exception("runtime-state provider raised; using defaults")
            result = None
        if result:
            snapshot.update(result)
    return snapshot


def get_feature_status() -> Dict:
    """The 10 feature-toggle booleans (``<feature>_enabled`` keys)."""
    return get_runtime_snapshot()["feature_status"]


def get_active_character_id() -> str:
    """The active character id, or ``""`` if none / state unavailable."""
    return get_runtime_snapshot()["active_character_id"]


def get_document_counts() -> Tuple[int, int]:
    """``(document_count, document_chars)`` for the attach-status text."""
    snapshot = get_runtime_snapshot()
    return snapshot["document_count"], snapshot["document_chars"]
