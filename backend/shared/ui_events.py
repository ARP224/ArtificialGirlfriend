"""
backend/shared/ui_events.py

Shared UI-update event interface for Artificial Girlfriend.

This is the *shared / infra-interface layer* home for "notify the browser" events.
It decouples event *producers* (UI/domain code that wants to push a state change
to the front-end) from the *transport* that actually delivers them (the WebSocket
server). Producers call :func:`publish_ui_update`; the transport registers itself
as a subscriber via :func:`register_ui_update_subscriber`.

It has no backend or ui dependencies — only the standard library — so any layer
may depend on it downward.

History: producers used to import the transport directly
(``from backend.server.websocket_server import send_ui_update``), i.e. an upward /
implementation-facing dependency. This interface was introduced so the
dependency is inverted:

    producers (ui.conversation, backend.shared.status_monitor)
        -> backend.shared.ui_events  (this module, the interface)
        <- backend.server.websocket_server  (the transport, a subscriber)

The transport keeps ``send_ui_update`` as its real implementation; it merely
registers a thin subscriber here. ``conversation_manager`` still calls
``backend.server.websocket_server.send_ui_update`` directly in places — that
import points downward (app -> transport) so it breaks no layering rule, but
new code should publish via this interface instead.
"""

import logging
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# A subscriber receives the same triple the legacy ``send_ui_update`` took and
# returns whether the update was delivered.
UiUpdateSubscriber = Callable[[str, str, Optional[dict]], bool]

_subscribers: List[UiUpdateSubscriber] = []


def register_ui_update_subscriber(subscriber: UiUpdateSubscriber) -> None:
    """Register a transport that delivers UI updates.

    Idempotent: registering the same callable twice is a no-op so a module that
    self-registers at import time stays safe under repeated imports.
    """
    if subscriber not in _subscribers:
        _subscribers.append(subscriber)


def unregister_ui_update_subscriber(subscriber: UiUpdateSubscriber) -> None:
    """Remove a previously registered subscriber (no error if absent)."""
    try:
        _subscribers.remove(subscriber)
    except ValueError:
        pass


def publish_ui_update(action: str, reason: str = "", data: dict = None) -> bool:
    """Publish a UI-update event to every registered transport.

    Drop-in for the legacy ``send_ui_update``: same ``(action, reason, data)``
    signature and ``bool`` return (``True`` if any subscriber reported delivery).
    With the single WebSocket subscriber that AG registers, this is behaviourally
    identical to calling ``send_ui_update`` directly — including letting a
    transport exception propagate to the caller, which the call sites already
    handle as before.
    """
    delivered = False
    for subscriber in list(_subscribers):
        if subscriber(action, reason, data):
            delivered = True
    return delivered
