"""
ui/handlers/startup_toggle.py

System page toggle for Windows startup registration (ST-D).

Delegates to launcher/startup_registry.py (stdlib-only leaf shared with
the tray menu). State is always re-read from the registry after a write
— the checkbox never holds a mirror state (S17 lesson).
"""

import logging

import gradio as gr

from backend.shared.i18n import t
from launcher.startup_registry import is_startup_enabled, set_startup_enabled

logger = logging.getLogger(__name__)


def handle_startup_toggle(enabled: bool):
    """
    Apply the requested startup registration state.

    Returns:
        gr.update for the checkbox, set to the actual registry state.
    """
    ok, err = set_startup_enabled(enabled)
    actual = is_startup_enabled()
    if not ok:
        gr.Warning(err)
    elif enabled:
        gr.Info(t('system.startup_registered'))
    else:
        gr.Info(t('system.startup_unregistered'))
    return gr.update(value=actual)
