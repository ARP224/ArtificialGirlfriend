"""
ui/handlers/ui_language.py

System page dropdown for the UI display language.

Persists display.language via settings_store (single source of truth). Gradio
labels are fixed at Blocks build time, so the new language takes effect on the
next application restart — the dropdown sits on the System page next to the
Restart button. The dropdown is re-read from the store after the write and
never holds a mirror state (S17 lesson, same as startup_toggle).
"""

import logging

import gradio as gr

from backend.shared.i18n import t
from backend.shared.settings_store import get_setting, update_setting

logger = logging.getLogger(__name__)


def handle_language_change(value: str):
    """Persist the selected UI language.

    Returns:
        gr.update for the dropdown, set to the actual persisted value.
    """
    ok = update_setting('display', 'language', value)
    actual = get_setting('display', 'language', 'auto')
    if ok:
        logger.info(f"UI language setting saved: {value}")
        gr.Info(t('system.language_saved'))
    else:
        gr.Warning(t('system.language_save_failed'))
    return gr.update(value=actual)
