"""
launcher/startup_registry.py

Auto-run at sign-in registration for the tray launcher (OS-branched).

Stdlib-only leaf module, shared by two consumers:
  - launcher/tray_app.py (tray menu toggle)
  - ui/handlers/startup_toggle.py (System page toggle)
Neither direction creates a runtime dependency on app code — this module
imports nothing from backend/ui/launcher.

Windows: writes HKCU\\...\\Run (no admin rights needed). The command
line is resolved from the repo location at call time, so no absolute
paths live in the repo. State queries read the registry directly — no
mirror state anywhere (S17 lesson).

macOS (Mac 3-10): a LaunchAgent plist at
~/Library/LaunchAgents/com.artificialgirlfriend.tray.plist runs
`/usr/bin/open -a <repo>/Artificial Girlfriend.app` at login. Going
through the installer-generated applet (not the venv python directly)
keeps the TCC responsible process identical to a manual launch, so the
permissions the user granted keep working. State = plist existence
(live read, no mirror — S17). winreg is imported inside the nt branch
only: a top-level import here used to kill the whole UI build on macOS
via ui/pages.py -> this module (the prime Mac boot blocker, 0-5).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'ArtificialGirlfriend'
LEGACY_VALUE_NAME = 'AirtificialGirlfriend'  # 旧綴り: enable/disable時に掃除

ROOT = Path(__file__).resolve().parent.parent

# macOS: applet built by "Install Artificial Girlfriend (Mac).command" (3-2)
MAC_APPLET = ROOT / 'Artificial Girlfriend.app'
LAUNCH_AGENT_LABEL = 'com.artificialgirlfriend.tray'


def _launch_agent_path() -> Path:
    return (Path.home() / 'Library' / 'LaunchAgents'
            / f'{LAUNCH_AGENT_LABEL}.plist')


def startup_command() -> str:
    """Registered launch command (Windows: Run key line / macOS: LaunchAgent)."""
    if os.name != 'nt':
        return f'/usr/bin/open -a "{MAC_APPLET}"'
    pythonw = ROOT / 'venv' / 'Scripts' / 'pythonw.exe'
    tray_app = ROOT / 'launcher' / 'tray_app.py'
    return f'"{pythonw}" "{tray_app}"'


def is_startup_enabled() -> bool:
    """True if registered (live read — registry value / plist existence)."""
    if os.name != 'nt':
        return _launch_agent_path().exists()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in (VALUE_NAME, LEGACY_VALUE_NAME):
                try:
                    winreg.QueryValueEx(key, name)
                    return True
                except OSError:
                    continue
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> tuple:
    """
    Register/unregister auto-start. Returns (ok, error_message).

    Enabling rewrites the value unconditionally, so a stale command
    (e.g. repo moved) heals itself on re-enable.
    """
    if os.name != 'nt':
        return _set_startup_enabled_mac(enabled)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            # 旧綴りの登録は常に掃除（enable/disable どちらでも）
            try:
                winreg.DeleteValue(key, LEGACY_VALUE_NAME)
            except FileNotFoundError:
                pass
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
                logger.info(f"Startup registration enabled: {startup_command()}")
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                    logger.info("Startup registration disabled")
                except FileNotFoundError:
                    pass  # already unregistered
        return True, ""
    except OSError as e:
        logger.error(f"Startup registration failed: {e}")
        return False, f"スタートアップ登録の変更に失敗しました: {e}"


def _set_startup_enabled_mac(enabled: bool) -> tuple:
    """LaunchAgent registration (Mac 3-10). Returns (ok, error_message).

    ProgramArguments goes through `/usr/bin/open -a <applet>` so the TCC
    responsible process is the applet — the same identity as a manual
    launch — instead of launchd running the venv python directly (which
    would strand the user's granted permissions on a different subject).
    """
    import plistlib
    import subprocess
    plist_path = _launch_agent_path()
    if enabled:
        if not MAC_APPLET.exists():
            return False, ('インストーラ(Install Artificial Girlfriend '
                           '(Mac).command)が未実行のため登録できません')
        payload = {
            'Label': LAUNCH_AGENT_LABEL,
            'RunAtLoad': True,
            'ProgramArguments': ['/usr/bin/open', '-a', str(MAC_APPLET)],
        }
        try:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(plist_path, 'wb') as f:
                plistlib.dump(payload, f)
            logger.info(f"LaunchAgent registered: {plist_path}")
            return True, ""
        except OSError as e:
            logger.error(f"LaunchAgent registration failed: {e}")
            return False, f"スタートアップ登録の変更に失敗しました: {e}"
    # disable: remove the plist, then bootout the current session copy
    # (idempotent — not loaded / already gone are both fine)
    try:
        if plist_path.exists():
            plist_path.unlink()
            logger.info(f"LaunchAgent removed: {plist_path}")
        try:
            subprocess.run(
                ['launchctl', 'bootout',
                 f'gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}'],
                capture_output=True, timeout=10)
        except Exception as e:
            logger.debug(f"launchctl bootout skipped: {e}")
        return True, ""
    except OSError as e:
        logger.error(f"LaunchAgent removal failed: {e}")
        return False, f"スタートアップ登録の変更に失敗しました: {e}"
