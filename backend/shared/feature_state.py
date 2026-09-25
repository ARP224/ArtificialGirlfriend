"""
backend/shared/feature_state.py

Runtime feature-flag state for Artificial Girlfriend (shared/settings layer).

This is the home for the *runtime* feature-toggle flags — the in-memory
``<feature>_enabled`` booleans that gate optional capabilities during a
conversation. It came out of the BackendState field-split: the toggle
setters and the feature-status reader
used to live on ``backend/backend.py`` and mutate ``_backend_state`` directly.

Layering: this module owns no state of its own. Every function takes the state
container (the ``_backend_state`` BackendState) injected as ``state`` and, where
a toggle resets a per-turn rate-limit tracker, a ``get_conversation_manager``
callable injected by the caller. So this module has no backend/ui imports —
only the standard library — and depends downward only.

Persistence is a *separate* concern handled by ``backend/shared/settings_store.py``
via ``backend/shared/feature_toggle_service.py``; this module only flips the runtime
flag and returns the current status.

``backend/backend.py`` keeps thin delegates (``set_<feature>_enabled`` /
``get_feature_status``) so the public surface is unchanged: ``backend.<name>``,
the ``backend/__init__`` re-exports, and the call-time
``getattr(backend.backend, "set_<feature>_enabled")`` seam used by
``feature_toggle_service`` and by test monkeypatches all keep working.
(``camera_device_index`` は Phase 3 で廃止 — デバイス選択はブラウザ提供者側へ)
"""

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class FeatureState:
    """Owns the runtime feature-toggle flags carved out of BackendState.

    These are the in-memory ``<feature>_enabled`` booleans that the setters
    in this module flip and that ``get_feature_status`` reads. BackendState holds a single instance as
    ``_backend_state.features`` and keeps backward-compat properties for the
    legacy ``_backend_state.<flag>`` access paths, so existing call sites are
    unchanged while ownership now lives here. Defaults mirror the values that
    used to be set directly in ``BackendState.__init__``.

    Note: ``server_mode`` (startup mode, not a user toggle) and the
    ``command_approval_*`` runtime state stay on BackendState — only the
    user-facing feature toggles managed by this module live here.
    """

    def __init__(self) -> None:
        self.pc_status_enabled: bool = False
        self.screen_capture_enabled: bool = False
        self.talk_theme_enabled: bool = True
        self.speechless_enabled: bool = False
        self.notes_enabled: bool = False
        self.command_execution_enabled: bool = False
        self.image_generation_enabled: bool = False
        self.camera_capture_enabled: bool = False
        self.ambient_camera_enabled: bool = False
        self.deep_search_enabled: bool = False
        self.elyth_enabled: bool = False


def set_pc_status_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle PC Status feature on/off.
    When turning off, also disables Screen Capture.

    Args:
        enabled: Whether to enable PC Status

    Returns:
        Dict with current feature status
    """
    state.pc_status_enabled = enabled
    if not enabled:
        state.screen_capture_enabled = False

    # The Chrome-extension WS server (port 5002) follows the toggle so a
    # disabled feature keeps no listener open (boot-time gate: backend.py).
    # Call-time import — shared must not import tools at module load (layering).
    try:
        if enabled:
            from backend.tools.pc_status_manager import start_pc_status_server
            if not start_pc_status_server():
                logger.warning("[PC Status] WS server failed to start (port 5002 in use?)")
        else:
            from backend.tools.pc_status_manager import stop_pc_status_server
            stop_pc_status_server()
    except Exception as e:
        logger.warning(f"[PC Status] WS server toggle failed: {e}")

    logger.info(f"[PC Status] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_screen_capture_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle Screen Capture feature on/off.
    Requires PC Status to be enabled.

    Args:
        enabled: Whether to enable Screen Capture

    Returns:
        Dict with current feature status
    """
    if enabled and not state.pc_status_enabled:
        return {
            "success": False,
            "error": "PC Status must be enabled before enabling Screen Capture",
            "pc_status_enabled": False,
            "screen_capture_enabled": False
        }

    state.screen_capture_enabled = enabled
    logger.info(f"[Screen Capture] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_talk_theme_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle Talk Theme feature on/off.

    Args:
        enabled: Whether to enable Talk Theme

    Returns:
        Dict with current feature status
    """
    state.talk_theme_enabled = enabled
    logger.info(f"[Talk Theme] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_speechless_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle Speechless mode on/off.
    When enabled, TTS is skipped entirely and AI response text is shown immediately.

    Args:
        enabled: Whether to enable Speechless mode

    Returns:
        Dict with current feature status
    """
    state.speechless_enabled = enabled
    logger.info(f"[Speechless] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_notes_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle Notes feature on/off.

    Args:
        enabled: Whether to enable notes

    Returns:
        Dict with current feature status
    """
    state.notes_enabled = enabled
    logger.info(f"[Notes] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_command_execution_enabled(
    state, enabled: bool, get_conversation_manager: Callable
) -> Dict[str, Any]:
    """
    Toggle Command Execution feature on/off.

    Args:
        enabled: Whether to enable command execution

    Returns:
        Dict with current feature status
    """
    state.command_execution_enabled = enabled
    # Reset rate limit tracker on toggle to allow fresh usage
    if enabled:
        try:
            cm = get_conversation_manager()
            cm.reset_command_tracker()
        except Exception:
            pass
    logger.info(f"[Command Execution] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_image_generation_enabled(
    state, enabled: bool, get_conversation_manager: Callable
) -> Dict[str, Any]:
    """
    Toggle Image Generation feature on/off.

    Args:
        enabled: Whether to enable image generation

    Returns:
        Dict with current feature status
    """
    state.image_generation_enabled = enabled
    # Reset rate limit tracker on toggle to allow fresh testing
    if enabled:
        try:
            cm = get_conversation_manager()
            cm.reset_image_gen_tracker()
        except Exception:
            pass
    logger.info(f"[ImageGen] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_camera_capture_enabled(
    state, enabled: bool, get_conversation_manager: Callable
) -> Dict[str, Any]:
    """
    Toggle Camera Capture feature on/off.

    Args:
        enabled: Whether to enable camera capture

    Returns:
        Dict with current feature status
    """
    state.camera_capture_enabled = enabled
    # Camera OFF also turns Live Camera auto-attach off (same cascade shape as
    # PC Status -> Screen Capture)
    if not enabled:
        state.ambient_camera_enabled = False
    # Reset rate limit tracker on toggle to allow fresh testing
    if enabled:
        try:
            cm = get_conversation_manager()
            cm.reset_camera_tracker()
        except Exception:
            pass
    logger.info(f"[CameraCapture] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_ambient_camera_enabled(state, enabled: bool) -> Dict[str, Any]:
    """
    Toggle Live Camera (auto-attach a frame on every user send) on/off.
    Requires Camera Capture to be enabled — mirrors the Screen Capture /
    PC Status dependency.

    Args:
        enabled: Whether to enable Live Camera auto-attach

    Returns:
        Dict with current feature status
    """
    if enabled and not state.camera_capture_enabled:
        return {
            "success": False,
            "error": "Camera must be enabled before enabling Live Camera",
            "camera_capture_enabled": False,
            "ambient_camera_enabled": False
        }

    state.ambient_camera_enabled = enabled
    logger.info(f"[LiveCamera] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_deep_search_enabled(
    state, enabled: bool, get_conversation_manager: Callable
) -> Dict[str, Any]:
    """
    Toggle Deep Search feature on/off.

    Args:
        enabled: Whether to enable deep search

    Returns:
        Dict with current feature status
    """
    state.deep_search_enabled = enabled
    # Reset rate limit trackers on toggle to allow fresh testing
    if enabled:
        try:
            cm = get_conversation_manager()
            cm.reset_search_web_tracker()
            cm.reset_read_webpage_tracker()
        except Exception:
            pass
    logger.info(f"[DeepSearch] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def set_elyth_enabled(state, enabled: bool) -> Dict[str, Any]:
    """Toggle ELYTH feature on/off for normal conversation mode."""
    state.elyth_enabled = enabled
    logger.info(f"[ELYTH] {'Enabled' if enabled else 'Disabled'}")
    return get_feature_status(state)


def get_feature_status(state) -> Dict[str, Any]:
    """
    Get current feature status.

    Returns:
        Dict with feature status
    """
    return {
        "success": True,
        "pc_status_enabled": state.pc_status_enabled,
        "screen_capture_enabled": state.screen_capture_enabled,
        "talk_theme_enabled": state.talk_theme_enabled,
        "speechless_enabled": state.speechless_enabled,
        "command_execution_enabled": state.command_execution_enabled,
        "notes_enabled": state.notes_enabled,
        "image_generation_enabled": state.image_generation_enabled,
        "camera_capture_enabled": state.camera_capture_enabled,
        "ambient_camera_enabled": state.ambient_camera_enabled,
        "deep_search_enabled": state.deep_search_enabled,
        "elyth_enabled": state.elyth_enabled,
    }
