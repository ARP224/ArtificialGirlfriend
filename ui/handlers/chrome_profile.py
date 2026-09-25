"""
ui/handlers/chrome_profile.py

System page dropdown for the Chrome profile used to open the app window
(Mac port plan Phase 2, 2026-07-20 ruling F).

Persists launcher.chrome_profile in launch_config.json via
backend/shared/launch_config.py (mode_switch precedent); the tray reads
it with its own load_launch_config on the next front open — this handler
is the single writer. State is always re-read after a write, the
dropdown never holds a mirror state (S17 lesson). '' in the dropdown
maps to null in the config (= no --profile-directory flag, the previous
behavior).
"""

import logging

import gradio as gr

from backend.shared.i18n import t
from backend.shared.launch_config import (
    get_launch_config_value,
    update_launch_config_value,
)

logger = logging.getLogger(__name__)


def current_chrome_profile() -> str:
    """Saved profile folder for the dropdown value ('' = unspecified)."""
    return get_launch_config_value('launcher', 'chrome_profile', None) or ''


def handle_chrome_profile_change(selected: str):
    """
    Persist the selected profile folder ('' -> null = current behavior).

    Returns:
        gr.update for the dropdown, set to the actual persisted state.
    """
    ok = update_launch_config_value(
        'launcher', 'chrome_profile', selected or None)
    actual = current_chrome_profile()
    if not ok:
        gr.Warning(t('system.chrome_profile_save_failed'))
    else:
        gr.Info(t('system.chrome_profile_saved'))
    return gr.update(value=actual)
