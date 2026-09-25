"""
backend/shared/feature_commands.py

Shared feature-toggle command interface for Artificial Girlfriend.

This is the *shared / infra-interface layer* home for "enable/disable a feature"
commands. It decouples the command *producer* (the WebSocket transport, which
receives toggle requests from the front-end) from the *domain* that actually
flips the runtime flag (the ``backend.backend.set_*_enabled`` setters). The
producer calls :func:`dispatch_feature_toggle`; a domain adapter registers the
handler that performs the effect via :func:`register_feature_toggle_handler`.

It has no backend or ui dependencies — only the standard library — so any layer
may depend on it downward.

History: the transport used to import the domain directly
(``from backend.backend import set_pc_status_enabled`` ... ×10), i.e. an
infra -> upper-layer reflux. This interface was introduced so the
dependency is inverted:

    producer (backend.server.websocket_server)
        -> backend.shared.feature_commands  (this module, the interface)
        <- backend.shared.feature_toggle_service  (the domain adapter, a handler)
"""

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# A handler receives the desired enabled state and returns the feature-status
# result dict (the same dict the legacy ``set_*_enabled`` setters returned).
FeatureToggleHandler = Callable[[bool], Dict[str, Any]]

_handlers: Dict[str, FeatureToggleHandler] = {}


def register_feature_toggle_handler(feature: str, handler: FeatureToggleHandler) -> None:
    """Register the domain handler for a feature-toggle command.

    Last registration wins, so a module that self-registers at import time stays
    safe under repeated imports.
    """
    _handlers[feature] = handler


def unregister_feature_toggle_handler(feature: str) -> None:
    """Remove a previously registered handler (no error if absent)."""
    _handlers.pop(feature, None)


def dispatch_feature_toggle(feature: str, enabled: bool) -> Dict[str, Any]:
    """Dispatch a feature-toggle command to its registered domain handler.

    Returns the handler's feature-status result dict. If no handler is
    registered for ``feature`` (a wiring error), returns an error-shaped dict so
    the caller can frame it as a response rather than crashing.
    """
    handler = _handlers.get(feature)
    if handler is None:
        logger.error("No feature-toggle handler registered for %r", feature)
        return {"success": False, "error": f"No handler registered for feature '{feature}'"}
    return handler(enabled)
