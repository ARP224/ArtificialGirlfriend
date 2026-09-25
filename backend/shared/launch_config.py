"""
backend/launch_config.py

Launch configuration management for Artificial Girlfriend.
Handles reading and writing of launch_config.json, which controls
system-level behavior such as server mode.

This is separate from user_settings.json (UI preferences) because
server mode is a system operation mode, not a user preference.
"""

import json
import logging
from typing import Any, Dict

# Settings dir resolution lives in the shared layer (B1: moved out of
# ui.settings_manager, which removed the backend->ui hard import cycle / V1).
from backend.shared.settings_store import get_settings_dir

logger = logging.getLogger(__name__)

LAUNCH_CONFIG_FILE = get_settings_dir() / 'launch_config.json'

DEFAULT_LAUNCH_CONFIG = {
    "server_mode": {
        "enabled": False,
        "session_timeout_minutes": 60,
        # Local mode only: when True, listen on the Tailscale IP with a
        # minimal HTTPS page that lets a remote device trigger the switch
        # to server mode (backend/server/remote_switch_listener.py).
        "remote_switch_enabled": False
    },
    "launcher": {
        # Web UI port (Gradio local mode / uvicorn server mode). Change this
        # if another app (e.g. Stable Diffusion WebUI) already uses 7860.
        # Read by ui/app.py (bind), launcher/tray_app.py (URL + monitoring)
        # and ui/admin_app.py (displayed URLs).
        "web_port": 7860,
        # Localhost-only control API port (backend side, see control_api.py)
        "control_port": 7865,
        # Tray launcher single-instance / command port (tray side)
        "tray_port": 7866,
        "startup_timeout_seconds": 90,
        # Chrome profile folder (e.g. "Profile 1") used when the tray opens
        # the app window. null = no --profile-directory flag (previous
        # behavior). Written by ui/handlers/chrome_profile.py; consumed by
        # launcher/tray_app.py via load_launch_config (single writer).
        "chrome_profile": None,
        # Environment overrides applied by run.py before CUDA/torch imports
        # (machine-specific GPU pinning lives here, not in the repo)
        "env": {}
    }
}


def load_launch_config() -> Dict[str, Any]:
    """
    Load launch configuration from file.

    Returns:
        Dict containing launch configuration, merged with defaults
        to ensure all keys exist. If the file does not exist or is
        unreadable, returns defaults.
    """
    try:
        if LAUNCH_CONFIG_FILE.exists():
            # utf-8-sig: BOM-tolerant. This file is hand-edited by users
            # (web_port, GPU pinning env) and PowerShell 5.1 / some editors
            # save UTF-8 with a BOM; plain 'utf-8' made json.load fail and
            # the whole config silently fall back to defaults, while run.py
            # and tray_app (both utf-8-sig) kept honouring it — split brain.
            with open(LAUNCH_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)
                logger.info(f"Loaded launch config from {LAUNCH_CONFIG_FILE}")

                # Deep merge with defaults to ensure all keys exist
                merged = _deep_merge(DEFAULT_LAUNCH_CONFIG, config)
                return merged
        else:
            logger.info("No launch config file found, using defaults")
            return _deep_copy(DEFAULT_LAUNCH_CONFIG)
    except Exception as e:
        logger.error(f"Failed to load launch config: {e}")
        return _deep_copy(DEFAULT_LAUNCH_CONFIG)


def save_launch_config(config: Dict[str, Any]) -> bool:
    """
    Save launch configuration to file.

    Args:
        config: Dictionary containing launch configuration

    Returns:
        True if successful, False otherwise
    """
    try:
        LAUNCH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(LAUNCH_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved launch config to {LAUNCH_CONFIG_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to save launch config: {e}")
        return False


def get_launch_config_value(section: str, key: str, default: Any = None) -> Any:
    """
    Get a specific value from the launch configuration.

    Args:
        section: Configuration section (e.g., 'server_mode')
        key: Key within the section (e.g., 'enabled')
        default: Default value if not found

    Returns:
        The configuration value, or default if not found
    """
    try:
        config = load_launch_config()
        return config.get(section, {}).get(key, default)
    except Exception as e:
        logger.error(f"Failed to get launch config value {section}.{key}: {e}")
        return default


def update_launch_config_value(section: str, key: str, value: Any) -> bool:
    """
    Update a specific value in the launch configuration and save.

    Args:
        section: Configuration section (e.g., 'server_mode')
        key: Key within the section (e.g., 'enabled')
        value: New value

    Returns:
        True if successful, False otherwise
    """
    try:
        config = load_launch_config()

        if section not in config:
            config[section] = {}
        config[section][key] = value

        return save_launch_config(config)
    except Exception as e:
        logger.error(f"Failed to update launch config value {section}.{key}: {e}")
        return False


def _deep_merge(defaults: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge overrides into defaults. For nested dicts, merge recursively.
    Values in overrides take precedence.
    """
    result = defaults.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _deep_copy(d: Dict[str, Any]) -> Dict[str, Any]:
    """Simple deep copy for nested dicts with primitive values."""
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _deep_copy(value)
        else:
            result[key] = value
    return result
