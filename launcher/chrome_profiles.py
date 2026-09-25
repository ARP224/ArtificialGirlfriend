"""
launcher/chrome_profiles.py

Chrome profile enumeration for the app-window profile selector
(Mac port plan Phase 2, 2026-07-20 ruling F).

Stdlib-only leaf module (startup_registry precedent), shared by:
  - ui/pages.py (System page dropdown choices)
  - nothing in launcher/ yet — the tray consumes only the SAVED folder
    name via launch_config (launcher.chrome_profile), never this module.

Reads Chrome's Local State (JSON) and returns the profiles from
profile.info_cache as [{'folder': ..., 'display_name': ...}]. Only the
file location branches per OS. Unreadable file or a lone Default
profile yields [] — the UI hides the dropdown entirely (= current
no-flag behavior stays the default).
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Chrome の未リネーム既定名 (en/ja)。既定名のままのプロファイルは
# Chrome 自身が gaia_name (Googleアカウント名) を表示する。
_DEFAULT_PROFILE_NAME_RE = re.compile(r'^(Person|ユーザー) \d+$')


def local_state_path() -> Path:
    """Chrome's Local State file for the default user data dir."""
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', ''))
        return base / 'Google' / 'Chrome' / 'User Data' / 'Local State'
    return (Path.home() / 'Library' / 'Application Support' / 'Google'
            / 'Chrome' / 'Local State')


def list_chrome_profiles() -> list:
    """
    Profiles as [{'folder', 'display_name'}], sorted by folder name.

    Returns [] when Local State is missing/unreadable or when there is
    no real choice (zero or one profile) — callers hide the selector.
    """
    try:
        with open(local_state_path(), 'r', encoding='utf-8') as f:
            info_cache = json.load(f).get('profile', {}).get('info_cache', {})
    except (OSError, ValueError) as e:
        logger.info(f"Chrome Local State unavailable ({e}) — no profile choices")
        return []
    if not isinstance(info_cache, dict):
        return []
    profiles = []
    for folder, info in info_cache.items():
        info = info or {}
        name = info.get('name') or folder
        # 未リネームのプロファイルは info_cache の name が "Person 1" 等の
        # 既定値のまま残り、Chrome 自身は gaia_name (Googleアカウント名) を
        # 表示するので合わせる。using_default_name はキー自体が無い個体が
        # ある(Mac実データ 2026-07-22: 既定名のままでも None)ため、フラグ
        # 単独に頼らず既定名パターン一致を同格の条件にする。
        if info.get('gaia_name') and (
                info.get('using_default_name')
                or _DEFAULT_PROFILE_NAME_RE.match(name)):
            name = info['gaia_name']
        profiles.append({'folder': folder, 'display_name': name})
    if len(profiles) <= 1:
        return []  # Default only — dropdown hidden (ruling F)
    profiles.sort(key=lambda p: p['folder'])
    return profiles
