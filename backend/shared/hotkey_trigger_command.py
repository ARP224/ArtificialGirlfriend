"""
backend/shared/hotkey_trigger_command.py

Shared remote-hotkey command interface for Artificial Girlfriend.

Decouples the command *producer* (the WebSocket transport, which receives
``hotkey_*_recording`` messages from the AG Client Addon) from the *domain*
that validates state and triggers the recording action (a ui-layer handler).
The producer calls :func:`dispatch_hotkey_trigger`; the ui layer registers
its handler via :func:`register_hotkey_trigger_handler` at composition-root
wiring (ui/app.py). Same inversion pattern as
:mod:`backend.shared.text_prompt_command`.

A 500 ms debounce lives here — the single choke point for all producers —
so key repeats / double-fires cannot race the browser click round trip
(the old Chrome extension had the same guard on its side).

It has no backend or ui dependencies — only the standard library — so any
layer may depend on it downward.
"""

import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# A handler receives the intent ("start" | "stop" | "toggle") and returns
# {"success": bool, "reason": str}. On success the actual recording action
# runs via the browser click round trip (trigger_record_click broadcast),
# which re-applies every existing guard (JS state machine + record_speech).
HotkeyTriggerHandler = Callable[[str], Dict[str, Any]]

HOTKEY_DEBOUNCE_SECONDS = 0.5

_handler: Optional[HotkeyTriggerHandler] = None
_last_dispatch_time: float = 0.0


def register_hotkey_trigger_handler(handler: HotkeyTriggerHandler) -> None:
    """Register the ui-layer handler for remote hotkey triggers.

    Last registration wins, so repeated wiring stays safe.
    """
    global _handler
    _handler = handler


def dispatch_hotkey_trigger(intent: str) -> Dict[str, Any]:
    """Dispatch a remote hotkey trigger ("start" | "stop" | "toggle").

    If no handler is registered (a wiring error), returns an error-shaped
    dict so the caller can frame it as a response rather than crashing.
    Dispatches inside the debounce window are rejected with reason
    "debounced".
    """
    global _last_dispatch_time
    if _handler is None:
        logger.error("No hotkey-trigger handler registered")
        return {"success": False, "reason": "no_handler_registered"}
    now = time.monotonic()
    if now - _last_dispatch_time < HOTKEY_DEBOUNCE_SECONDS:
        logger.debug(f"[HotkeyTrigger] Debounced {intent}")
        return {"success": False, "reason": "debounced"}
    _last_dispatch_time = now
    return _handler(intent)
