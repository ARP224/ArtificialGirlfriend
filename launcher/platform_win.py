"""
launcher/platform_win.py

Windows adapter for the tray launcher (Mac port plan Phase 1).

Everything win32-specific that tray_app.py used to hold inline lives
here: window enumeration/close/focus (PS1 FindWindowByTitle parity via
ctypes), Chrome discovery, MessageBox dialogs, venv/spawn specifics, and
the pystray/toast-identity patches (absorbed from the former
launcher/tray_notify_identity.py). tray_app.py imports this module (or
platform_mac.py on darwin) under a common name and only calls the
common API:

    platform_setup(app_name, icon_path)
    find_front() / close_front() / focus_front() / open_front(url)
    alert(text) / confirm(text) -> bool
    venv_python() -> Path
    spawn_kwargs() -> dict

Never imports backend/* or ui/* (launcher layer rule).
"""

import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger('tray')

ROOT = Path(__file__).resolve().parent.parent

CREATE_NO_WINDOW = 0x08000000
WM_CLOSE = 0x0010

MB_ICONERROR = 0x10
MB_ICONQUESTION = 0x20
MB_YESNO = 0x04
IDYES = 6

# Window titles that identify the AG Chrome app window.
# 'rtificial Girlfriend' matches both the correct spelling (Artificial) and
# any window still titled with the legacy misspelling (Airtificial).
# NOTE: ':7860' を含めない — 通常の Chrome ウィンドウで localhost:7860 を
# 開いているだけのタブタイトルにマッチし、無関係なウィンドウごと WM_CLOSE で
# 閉じてしまうため(page の <title> は 'Artificial Girlfriend' なので上2つで足りる)。
WINDOW_TITLE_PATTERNS = ('rtificial Girlfriend', 'AG Admin')

APP_USER_MODEL_ID = 'ArtificialGirlfriend.Tray'

# Shell_NotifyIcon balloon flags (not exposed by pystray._util.win32)
NIIF_USER = 0x00000004        # use the tray icon's hIcon as the balloon icon
NIIF_LARGE_ICON = 0x00000020  # ... at large-icon size


# ---------------------------------------------------------------------------
# Process / spawn specifics
# ---------------------------------------------------------------------------

def venv_python() -> Path:
    """Interpreter used to spawn the backend (console-less variant is the
    caller's concern — the backend spawn redirects stdio anyway)."""
    return ROOT / 'venv' / 'Scripts' / 'python.exe'


def spawn_kwargs() -> dict:
    """Extra subprocess kwargs: hide the console window on Windows."""
    return {'creationflags': CREATE_NO_WINDOW}


# ---------------------------------------------------------------------------
# Win32 window helpers (PS1 FindWindowByTitle parity, via ctypes)
# ---------------------------------------------------------------------------

def _find_ag_windows() -> list:
    """Find all AG Chrome app window handles (may span both mode UIs)."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not any(pattern in title for pattern in WINDOW_TITLE_PATTERNS):
            return True
        # PS1 parity: only accept Chrome windows
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            import psutil
            if psutil.Process(pid.value).name().lower() != 'chrome.exe':
                return True
        except Exception:
            pass  # psutil unavailable / process gone — accept by title alone
        found.append(hwnd)
        return True  # keep enumerating (collect all)

    user32.EnumWindows(enum_proc, 0)
    return found


def find_front() -> int:
    """Find the AG Chrome app window handle, or 0."""
    windows = _find_ag_windows()
    return windows[0] if windows else 0


def close_front() -> None:
    """Ask all AG windows to close (WM_CLOSE — same as clicking the X)."""
    user32 = ctypes.windll.user32
    for hwnd in _find_ag_windows():
        logger.info(f"Closing AG window (hwnd={hwnd})")
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def focus_front() -> bool:
    """SW_RESTORE -> SW_MAXIMIZE -> foreground (PS1 parity).

    Returns True if an AG window was found and focused.
    """
    hwnd = find_front()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    time.sleep(0.1)
    user32.ShowWindow(hwnd, 3)   # SW_MAXIMIZE
    user32.SetForegroundWindow(hwnd)
    return True


def find_chrome_exe() -> str:
    """Locate chrome.exe via App Paths registry, then common locations."""
    import winreg
    subkey = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                path, _ = winreg.QueryValueEx(key, None)
                if path and Path(path).exists():
                    return path
        except OSError:
            continue
    for env_var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(env_var)
        if base:
            candidate = Path(base) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
            if candidate.exists():
                return str(candidate)
    return ''


def open_front(url: str, profile_dir: str = None) -> bool:
    """Open a new Chrome app window (PS1 parity incl. maximize loop);
    default browser as fallback when Chrome is missing (Windows-only
    behavior — kept unchanged by the 2026-07-20 Mac ruling).

    profile_dir: Chrome profile folder (launcher.chrome_profile). None =
    no --profile-directory flag, the pre-Phase-2 behavior (ruling F).

    Always returns True: on Windows something always opens (Chrome or
    the default browser). platform_mac returns False when Chrome is
    missing, which makes the tray notify the URL instead.
    """
    chrome = find_chrome_exe()
    if not chrome:
        logger.warning("chrome.exe not found — falling back to default browser")
        import webbrowser
        webbrowser.open(url)
        return True
    args = [chrome, f'--app={url}']
    if profile_dir:
        args.append(f'--profile-directory={profile_dir}')
        logger.info(f"Opening front with Chrome profile: {profile_dir}")
    subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
    time.sleep(1.0)
    for _ in range(15):
        if focus_front():
            break
        time.sleep(0.3)
    return True


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

def alert(text: str, title: str = 'Artificial Girlfriend') -> None:
    """Blocking error box (MessageBoxW, MB_ICONERROR)."""
    ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONERROR)


def confirm(text: str, title: str = 'Artificial Girlfriend') -> bool:
    """Blocking Yes/No question box. True iff the user chose Yes."""
    answer = ctypes.windll.user32.MessageBoxW(
        None, text, title, MB_YESNO | MB_ICONQUESTION)
    return answer == IDYES


# ---------------------------------------------------------------------------
# Toast identity + pystray patches
# (absorbed from launcher/tray_notify_identity.py, 2026-07-20)
#
# Tray notifications (pystray icon.notify -> Shell_NotifyIcon balloon ->
# Windows 10/11 toast) otherwise show up attributed to "Python" with a gray
# auto-generated monogram, because the toast identity is derived from
# pythonw.exe. Two independent, fail-soft fixes:
#
#   1. App identity (AUMID): register
#      HKCU\Software\Classes\AppUserModelId\<AUMID> (DisplayName + IconUri)
#      and tag the current process with it via
#      SetCurrentProcessExplicitAppUserModelID. Windows then attributes our
#      toasts to "Artificial Girlfriend" with the logo .ico instead of
#      deriving identity from the interpreter executable. Must be called
#      before any window (including the tray's message window) is created.
#
#   2. Balloon icon: pystray's _notify sends NIF_INFO with no dwInfoFlags,
#      so the toast carries no image of its own. Patch _notify to pass
#      NIIF_USER | NIIF_LARGE_ICON, which makes the shell reuse the tray
#      icon (the logo) as the notification icon. Runtime patch lives in the
#      repo on purpose (site-packages hand edits don't survive venv
#      rebuilds — see ui/gradio_patches.py precedent).
#
# Registry writes are per-user (HKCU) and idempotent. Every entry point is
# fail-soft: on any error the tray keeps working with plain notifications.
# ---------------------------------------------------------------------------

def _register_app_identity(display_name: str, icon_path) -> None:
    """Give this process an explicit toast identity (name + icon)."""
    try:
        import winreg
        key_path = r'Software\Classes\AppUserModelId\{}'.format(APP_USER_MODEL_ID)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, display_name)
            winreg.SetValueEx(key, 'IconUri', 0, winreg.REG_SZ, str(icon_path))
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception as e:
        logger.warning(
            f"App identity registration failed ({e}) — "
            "notifications keep the default (Python) identity")


def _patch_pystray_notify_icon() -> None:
    """Make pystray balloons show the tray icon (logo) in the toast."""
    try:
        from pystray import _win32
        from pystray._util import win32

        def _notify(self, message, title=None):
            self._message(
                win32.NIM_MODIFY,
                win32.NIF_INFO,
                szInfo=message,
                szInfoTitle=title or self.title or '',
                dwInfoFlags=NIIF_USER | NIIF_LARGE_ICON)

        _win32.Icon._notify = _notify
    except Exception as e:
        logger.warning(
            f"pystray notify patch failed ({e}) — toasts keep default icon")


def _patch_pystray_menu_refresh() -> None:
    """右クリックでポップアップを出す直前にメニューを再構築するパッチ。

    pystray(win32)はHMENUをupdate_menu()呼出時にしか作り直さないため、
    「画面を開く/閉じる」のような表示のたびに変わりうる動的ラベルは、ユーザーが
    ウィンドウのXで閉じた直後などに古いまま表示される。WM_RBUTTONUPは
    メッセージループスレッドで処理されるので、ここでの再構築はスレッド安全。
    Icon生成前に呼ぶこと(_message_handlersが__init__でメソッドを束縛するため)。
    """
    import pystray
    from pystray._util import win32

    original = pystray.Icon._on_notify

    def _on_notify(self, wparam, lparam):
        if lparam == win32.WM_RBUTTONUP:
            try:
                self._update_menu()
            except Exception:
                logger.warning("Menu refresh before popup failed", exc_info=True)
        return original(self, wparam, lparam)

    pystray.Icon._on_notify = _on_notify


def platform_setup(app_name: str, icon_path) -> None:
    """Windows-only startup fixes, in the order tray_app.main() used them.

    Must precede any window creation (tray message window) and pystray
    Icon construction.
    """
    _register_app_identity(app_name, icon_path)
    _patch_pystray_notify_icon()
    _patch_pystray_menu_refresh()


def post_icon_setup(icon) -> None:
    """No-op on Windows: menu freshness is handled by the WM_RBUTTONUP
    patch above, and there is no Dock to hide (darwin counterpart)."""
