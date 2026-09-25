"""
ui/handlers/status_log.py

Small self-contained status / log helper handlers that were wired inline in
app.py's ``create_gradio_interface``:

* ``log_hotkey_status`` -- periodic/startup debug logging of the global-hotkey
  service + handler status (returns ``None``; no UI updates).
* ``refresh_command_log`` -- return the formatted command-execution log for the
  Logs tab. **Canonicalization (touch-once):** app.py had two byte-identical
  copies of this body (a manual refresh button handler and a Timer tick
  handler); they are consolidated here into one function that both wiring sites
  call.

B11 (ST4) extracts these here. The bodies use only module-level names
(``time``, ``gr``, ``logger``/``logging``, ``get_hotkey_service``,
``get_hotkey_handler``) and a call-time ``from backend.tools.command_executor import
get_command_log`` -- none closes over a local Gradio component variable, so the
move is a pure dedent (byte-identical) re-imported into app.py while the
``.click(fn=...)`` / ``.tick(fn=...)`` / ``.then(fn=...)`` wiring stays in
app.py. The module logger name changes from ``ui.app`` to
``ui.handlers.status_log`` (out of the deterministic contract, same as the
other ui/handlers modules).
"""

import logging

from ..hotkey_service import get_hotkey_service
from backend.shared.hotkey_handler import get_hotkey_handler

logger = logging.getLogger(__name__)


def log_hotkey_status():
    """Log hotkey system status periodically"""
    try:
        service = get_hotkey_service()
        service_status = service.get_status()

        handler = get_hotkey_handler()
        handler_status = handler.get_status()

        logger.debug(f"[HOTKEY-STATUS] Service: running={service_status['service_running']}, "
                   f"handler_active={service_status['handler_active']}, queue={service_status['queue_size']}")
        logger.debug(f"[HOTKEY-STATUS] Handler: pynput={handler_status['pynput_available']}, "
                   f"running={handler_status['is_running']}, callbacks={handler_status['registered_callbacks']}")

        # Log detailed info every 5 minutes in debug mode
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"[HOTKEY-STATUS] Full status: {handler_status}")
    except Exception as e:
        logger.error(f"[HOTKEY-STATUS] Error getting status: {e}")

    return None  # No UI updates


def refresh_command_log():
    from backend.tools.command_executor import get_command_log
    return get_command_log().format_for_display()
