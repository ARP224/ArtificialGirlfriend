"""
ui/handlers/remote_switch.py

System page toggle for the remote mode-switch listener opt-in
(server_mode.remote_switch_enabled in launch_config).

Persists the flag and starts/stops the listener immediately — no restart
needed (the composition root always configure()s it in local mode).
State is re-read from launch_config after the write — the checkbox never
holds a mirror state (S17 lesson).
"""

import logging

import gradio as gr

from backend.server.remote_switch_listener import get_remote_switch_listener
from backend.shared.i18n import t
from backend.shared.launch_config import (
    get_launch_config_value,
    update_launch_config_value,
)

logger = logging.getLogger(__name__)


def remote_switch_status_html() -> str:
    """Initial server-side render of the JS-owned status line.

    Evaluated on every browser page load (callable component value); after
    that the WS 'remote_switch_status' push is the only writer of the inner
    div — Gradio never updates it (one-writer rule, S17/JS専有の原則).
    """
    import html
    # white-space:pre-line — 文言の\n(URL2本の改行 稜依頼 2026-07-25)を、
    # 初期HTMLとWSのtextContent書込みの両方で改行として描画する。
    return ("<div id='remote-switch-status-text' class='helper-text'"
            " style='white-space:pre-line'>"
            f"{html.escape(remote_switch_status_text())}</div>")


def remote_switch_status_text() -> str:
    """Listener state as a System-page status line (empty while stopped)."""
    listener = get_remote_switch_listener()
    status = listener.status
    if status == 'starting':
        return t('system.remote_switch_status_starting')
    if status == 'waiting_tailscale':
        return t('system.remote_switch_status_waiting')
    if status == 'listening':
        # The listener serves the switch page on ANY GET path (/, /mobile, ...),
        # so the mobile bookmark works too — show both URLs like the Admin page.
        base_url = listener.listen_url or ''
        return t('system.remote_switch_status_listening',
                 url=base_url,
                 mobile_url=f"{base_url}/mobile" if base_url else '')
    if status == 'cert_failed':
        return t('system.remote_switch_status_cert_failed')
    return ''


def handle_remote_switch_toggle(enabled: bool):
    """
    Persist the requested opt-in state and apply it to the listener.

    The status line below the checkbox is NOT updated here — start()/stop()
    fire the listener's on_status_change, which pushes the new text over WS
    to the JS-owned div (single writer).

    Returns:
        gr.update for the checkbox, set to the actual persisted state.
    """
    # フールプルーフ防御: チェックボックスはビルド時にグレー化済みだが、
    # 古い画面からのON操作はここで止める(OFFは常に許可)
    if enabled:
        from backend.server.tailscale import is_tailscale_installed
        if not is_tailscale_installed():
            gr.Warning(t('system.server_mode_needs_tailscale'))
            actual = bool(get_launch_config_value(
                'server_mode', 'remote_switch_enabled', False))
            return gr.update(value=actual)

    ok = update_launch_config_value(
        'server_mode', 'remote_switch_enabled', bool(enabled))
    actual = bool(get_launch_config_value(
        'server_mode', 'remote_switch_enabled', False))
    if not ok:
        gr.Warning(t('system.remote_switch_save_failed'))
    elif actual:
        get_remote_switch_listener().start()
        gr.Info(t('system.remote_switch_enabled_info'))
    else:
        get_remote_switch_listener().stop()
        gr.Info(t('system.remote_switch_disabled_info'))
    return gr.update(value=actual)
