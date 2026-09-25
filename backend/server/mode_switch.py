"""
backend/server/mode_switch.py

Server/local mode switch service (ST-B).

Precondition checks and config writes for mode switching, shared by all
three consumers (local UI "Server Mode" button, admin "ローカルモードに
切り替え" button, tray via control API) so the logic exists exactly once.

Restart triggering deliberately stays with the caller — the composition
root owns process lifecycle (_execute_shutdown), this module does not.

Both functions return (ok, error_message). error_message is the
user-facing text, localized via backend.shared.i18n.t (empty on success);
callers wrap it in their own presentation (HTML div / tray notification /
in-app toast).
"""

import logging

from backend.shared.i18n import t

logger = logging.getLogger(__name__)


def prepare_switch_to_server(progress=None) -> tuple:
    """Check Tailscale + cert, then persist server_mode=enabled.

    progress: optional CertProgress callback (backend/server/tailscale.py),
    passed through so the System page can stream cert attempt counts.
    """
    from backend.server.tailscale import check_tailscale_available, ensure_valid_cert

    if not check_tailscale_available():
        return False, t('mode_switch.tailscale_not_running')
    try:
        ensure_valid_cert(progress)
    except Exception as cert_err:
        return False, t('mode_switch.cert_failed', error=cert_err)

    from backend.shared.launch_config import update_launch_config_value
    if not update_launch_config_value("server_mode", "enabled", True):
        return False, t('mode_switch.config_update_failed')
    logger.info("Mode switch prepared: -> server")
    return True, ""


def prepare_switch_to_local() -> tuple:
    """Disconnect remote sessions, then persist server_mode=disabled."""
    from backend.server.session_manager import get_session_manager
    get_session_manager().force_disconnect()

    try:
        from backend.shared.launch_config import update_launch_config_value
        if not update_launch_config_value("server_mode", "enabled", False):
            return False, t('mode_switch.config_update_failed')
    except Exception as e:
        return False, t('mode_switch.config_update_error', error=e)
    logger.info("Mode switch prepared: -> local")
    return True, ""
