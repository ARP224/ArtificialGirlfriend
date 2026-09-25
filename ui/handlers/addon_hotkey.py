"""
ui/handlers/addon_hotkey.py

AG Client Addon hotkey glue: registers the domain handler for remote hotkey
triggers (backend.shared.hotkey_trigger_command). Validates start/stop/toggle
intent against app state, then asks connected browser pages to click the
record button via ui_events ("trigger_record_click" broadcast). Driving the
real button keeps every existing guard in the loop (JS MediaRecorder state
machine, record_speech M19 atomic claim, is_generating defence), in both
local and server mode — the same path the old Chrome extension exercised
via executeScript.
"""

import logging

from backend.shared.hotkey_trigger_command import register_hotkey_trigger_handler
from backend.shared.ui_events import publish_ui_update

from ..state import app_state

logger = logging.getLogger(__name__)


def handle_hotkey_trigger(intent: str) -> dict:
    """Validate a remote hotkey intent and trigger the record-button click.

    Returns {"success": bool, "reason": str} for the transport to frame as
    a hotkey_response. The button click is a toggle, so start/stop intents
    are only forwarded when the server-side recording state agrees —
    otherwise a "start" while recording would silently stop it.
    """
    if not app_state.conversation_started:
        return {"success": False, "reason": "conversation_not_started"}

    try:
        from backend.backend import _backend_state as _bs
        if _bs and _bs.is_generating:
            return {"success": False, "reason": "generating"}
    except Exception:
        pass

    recording = app_state.recording_start_time is not None
    if intent == "start" and recording:
        return {"success": False, "reason": "already_recording"}
    if intent == "stop" and not recording:
        return {"success": False, "reason": "not_recording"}

    delivered = publish_ui_update(
        "trigger_record_click", reason=f"addon_hotkey_{intent}")
    if not delivered:
        return {"success": False, "reason": "no_connected_client"}
    logger.info(f"[AddonHotkey] {intent} -> trigger_record_click broadcast "
                f"(recording={recording})")
    return {"success": True, "reason": intent}


def setup_addon_hotkey_handler() -> None:
    """Register the handler at composition-root wiring (ui/app.py)."""
    register_hotkey_trigger_handler(handle_hotkey_trigger)
    logger.info("[AddonHotkey] Remote hotkey trigger handler registered")
