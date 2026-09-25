"""
launcher/platform_mac.py

macOS adapter for the tray launcher (Mac port plan 3-1).

Presents the same API as launcher/platform_win.py; tray_app.py imports
one side under the common name `plat`. Windows never imports this
module. Everything is fail-soft: log + neutral return, never raise
(plan §2 — Mac側Claude Codeがログだけで一次切り分けできる状態を保つ).

P1〜P5プローブ実測(2026-07-20 M1 Mac Studio / macOS 26.5)に基づく決定:
  - Window ops = AppleScript against Chrome's scripting dictionary.
    `tell application "Google Chrome"` LAUNCHES Chrome if it is not
    running, so every AppleScript call is guarded by a psutil process
    check (auto-launch trap, plan rev2).
  - close: `close (every window whose title contains ...)` closes only
    the matched windows (P2-verified).
  - focus: `set index to 1` + `activate` — activate is app-level, so
    other Chrome windows may rise too (P2). AXRaise refinement is a
    Phase 4 item if this annoys in practice.
  - Chrome discovery = fixed paths -> ~/Applications -> mdfind, absolute
    paths only, never a bare name on PATH (ruling E). No webbrowser
    fallback on macOS (ruling E: 誤プロファイル問題) — open_front
    returns False and the tray notifies the URL instead.
  - Menu freshness = periodic update_menu() republish (P3: callables are
    re-evaluated ONLY on update_menu(); background-thread update_menu is
    crash-free in practice; NSMenuDelegate patching stays untested).
  - Dock icon = hide via Accessory activation policy in post_icon_setup
    (P3: a plain python process shows a Dock rocket otherwise).
  - Menu bar icon = pystray patch (platform_setup): @2x bitmap presented at
    the bar's point size, 18pt body, template image (稜指摘 2026-08-16:
    pystray hands AppKit a 22px bitmap that Retina upsamples → blurry, and
    a fixed-white icon vanishes on a light menu bar).
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

from launcher.tray_i18n import t

logger = logging.getLogger('tray')

ROOT = Path(__file__).resolve().parent.parent

# Same patterns as platform_win.py — keep the two in sync by hand
# (§4 minimal duplication, tray_i18n precedent; see platform_win.py for
# why ':7860' must NOT be added).
WINDOW_TITLE_PATTERNS = ('rtificial Girlfriend', 'AG Admin')

CHROME_PROCESS_NAME = 'Google Chrome'
CHROME_BUNDLE_BINARY = 'Contents/MacOS/Google Chrome'


# ---------------------------------------------------------------------------
# Process / spawn specifics
# ---------------------------------------------------------------------------

def venv_python() -> Path:
    """Interpreter used to spawn the backend (POSIX venv layout)."""
    return ROOT / 'venv' / 'bin' / 'python'


def spawn_kwargs() -> dict:
    """Extra subprocess kwargs (none needed on macOS)."""
    return {}


# ---------------------------------------------------------------------------
# Chrome discovery (ruling E: 3-stage, absolute paths only)
# ---------------------------------------------------------------------------

def resolve_chrome_binary() -> str:
    """/Applications -> ~/Applications -> mdfind; '' when not installed."""
    candidates = [
        Path('/Applications/Google Chrome.app'),
        Path.home() / 'Applications' / 'Google Chrome.app',
    ]
    for app in candidates:
        binary = app / CHROME_BUNDLE_BINARY
        if binary.exists():
            logger.info(f"Chrome resolved: {binary}")
            return str(binary)
    try:
        result = subprocess.run(
            ['/usr/bin/mdfind', 'kMDItemCFBundleIdentifier == "com.google.Chrome"'],
            capture_output=True, encoding='utf-8', errors='replace', timeout=5,
        )
        for line in (result.stdout or '').splitlines():
            binary = Path(line.strip()) / CHROME_BUNDLE_BINARY
            if binary.exists():
                logger.info(f"Chrome resolved via mdfind: {binary}")
                return str(binary)
    except Exception as e:
        logger.warning(f"mdfind Chrome lookup failed: {e}")
    logger.warning("Google Chrome not found (fixed paths + mdfind)")
    return ''


def _chrome_running() -> bool:
    """Guard for every AppleScript call: `tell application "Google Chrome"`
    would LAUNCH Chrome when it is not running (auto-launch trap)."""
    try:
        import psutil
        for proc in psutil.process_iter(['name']):
            if proc.info.get('name') == CHROME_PROCESS_NAME:
                return True
        return False
    except Exception as e:
        # Fail toward "not running": skipping a window op is harmless,
        # silently launching Chrome is not.
        logger.warning(f"Chrome process check failed ({e}) — assuming not running")
        return False


# ---------------------------------------------------------------------------
# AppleScript window management
# ---------------------------------------------------------------------------

def _osascript(script: str, timeout=10):
    """Run osascript -e; CompletedProcess or None on error/timeout."""
    try:
        return subprocess.run(
            ['/usr/bin/osascript', '-e', script],
            capture_output=True, encoding='utf-8', errors='replace',
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"osascript failed: {e}")
        return None


def _as_string(text: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _title_filter() -> str:
    """AppleScript boolean over `it` titles: pattern1 or pattern2 ..."""
    return ' or '.join(
        f'title contains {_as_string(p)}' for p in WINDOW_TITLE_PATTERNS)


def find_front() -> int:
    """First AG Chrome window id, or 0 (Automation permission or Chrome
    absence both land on 0 — the failure is visible in the tray log)."""
    if not _chrome_running():
        return 0
    script = (
        f'tell application "{CHROME_PROCESS_NAME}" to '
        f'get id of windows whose ({_title_filter()})'
    )
    result = _osascript(script)
    if result is None or result.returncode != 0:
        if result is not None:
            logger.warning(f"find_front AppleScript error: {(result.stderr or '').strip()}")
        return 0
    ids = (result.stdout or '').strip()
    if not ids:
        return 0
    try:
        return int(ids.split(',')[0].strip())
    except ValueError:
        return 0


def close_front() -> None:
    """Close all AG windows (matched windows only — P2-verified safe)."""
    if not _chrome_running():
        return
    logger.info("Closing AG windows (AppleScript)")
    script = (
        f'tell application "{CHROME_PROCESS_NAME}" to '
        f'close (every window whose ({_title_filter()}))'
    )
    result = _osascript(script)
    if result is not None and result.returncode != 0:
        logger.warning(f"close_front AppleScript error: {(result.stderr or '').strip()}")


def _screen_visible_bounds_for(window_bounds):
    """Visible area of the screen CONTAINING the window, as AppleScript
    bounds (left, top, right, bottom), or None when AppKit is unavailable.

    Coordinate contract (M1実測 2026-07-22: サブモニターで最大化する
    バグの教訓): AppleScript bounds are global coords with the origin at
    the PRIMARY screen's top-left; Cocoa frames are bottom-left origin.
    The top-left conversion must therefore use the PRIMARY screen height
    (screens()[0]) — using the target screen's own height lands the
    rectangle on another display. Maximizing on the window's current
    screen also matches Windows SW_MAXIMIZE semantics.
    """
    try:
        from AppKit import NSScreen
        screens = NSScreen.screens()
        if not screens:
            return None
        primary_h = screens[0].frame().size.height
        center_x = (window_bounds[0] + window_bounds[2]) / 2.0
        center_y = primary_h - (window_bounds[1] + window_bounds[3]) / 2.0
        target = None
        for screen in screens:
            f = screen.frame()
            if (f.origin.x <= center_x <= f.origin.x + f.size.width
                    and f.origin.y <= center_y <= f.origin.y + f.size.height):
                target = screen
                break
        if target is None:
            target = screens[0]
        vis = target.visibleFrame()
        left = int(vis.origin.x)
        top = int(primary_h - (vis.origin.y + vis.size.height))
        right = int(vis.origin.x + vis.size.width)
        bottom = int(primary_h - vis.origin.y)
        return left, top, right, bottom
    except Exception as e:
        logger.debug(f"visible bounds unavailable: {e}")
        return None


def focus_front() -> bool:
    """Raise the first AG window, maximized to the visible area of the
    screen it is currently on (platform_win SW_MAXIMIZE parity).
    Two AppleScript steps: probe id+bounds, then set bounds/raise by
    window id. activate is app-level (P2): other Chrome windows may come
    forward too — AXRaise is a refinement candidate.
    Returns True if an AG window was found and raised."""
    if not _chrome_running():
        return False
    probe = (
        f'tell application "{CHROME_PROCESS_NAME}"\n'
        f'  set matches to (every window whose ({_title_filter()}))\n'
        f'  if (count of matches) = 0 then return ""\n'
        f'  set w to item 1 of matches\n'
        f'  set b to bounds of w\n'
        f'  return (id of w as text) & "|" & (item 1 of b as text) & "," & '
        f'(item 2 of b as text) & "," & (item 3 of b as text) & "," & '
        f'(item 4 of b as text)\n'
        f'end tell'
    )
    result = _osascript(probe)
    if result is None or result.returncode != 0:
        if result is not None:
            logger.warning(f"focus_front probe error: {(result.stderr or '').strip()}")
        return False
    payload = (result.stdout or '').strip()
    if not payload:
        return False
    try:
        wid_text, bounds_text = payload.split('|', 1)
        wid = int(wid_text)
        current = tuple(int(float(v)) for v in bounds_text.split(','))
    except ValueError:
        logger.warning(f"focus_front: unparseable probe result: {payload!r}")
        return False
    bounds = _screen_visible_bounds_for(current)
    bounds_line = ''
    if bounds:
        bounds_line = (f'  set bounds of window id {wid} to '
                       f'{{{bounds[0]}, {bounds[1]}, {bounds[2]}, {bounds[3]}}}\n')
        logger.debug(f"focus_front: window {wid} {current} -> maximize {bounds}")
    script = (
        f'tell application "{CHROME_PROCESS_NAME}"\n'
        f'{bounds_line}'
        f'  set index of window id {wid} to 1\n'
        f'  activate\n'
        f'end tell'
    )
    result = _osascript(script)
    if result is None or result.returncode != 0:
        if result is not None:
            logger.warning(f"focus_front raise error: {(result.stderr or '').strip()}")
        return False
    return True


def open_front(url: str, profile_dir: str = None) -> bool:
    """Spawn a Chrome --app window (binary direct exec, ruling E).

    Returns False when Chrome is missing — the caller notifies the URL
    (no webbrowser fallback on macOS: 自動起動NG裁定=誤プロファイル問題).
    The spawned pid is deliberately not tracked: with Chrome already
    running it hands the window off and exits (P2 単一インスタンス実証).
    """
    chrome = resolve_chrome_binary()
    if not chrome:
        return False
    args = [chrome, f'--app={url}']
    if profile_dir:
        args.append(f'--profile-directory={profile_dir}')
        logger.info(f"Opening front with Chrome profile: {profile_dir}")
    try:
        subprocess.Popen(args)
    except OSError as e:
        logger.error(f"Chrome spawn failed: {e}")
        return False
    time.sleep(1.0)
    for _ in range(15):
        if focus_front():
            break
        time.sleep(0.3)
    return True


# ---------------------------------------------------------------------------
# Dialogs (osascript; blocking, MessageBoxW parity)
# ---------------------------------------------------------------------------

def alert(text: str, title: str = 'Artificial Girlfriend') -> None:
    """Blocking error dialog."""
    script = (
        f'display alert {_as_string(title)} '
        f'message {_as_string(text)} as critical'
    )
    result = _osascript(script, timeout=None)
    if result is None or result.returncode != 0:
        # Dialog could not be shown — the text must still reach the user
        # somewhere; the log is the fallback channel.
        logger.error(f"alert (dialog failed): {title}: {text}")


def confirm(text: str, title: str = 'Artificial Girlfriend') -> bool:
    """Blocking Yes/No dialog. True iff the user chose Yes.

    osascript exits non-zero when the cancel button is pressed, so the
    return code IS the answer. Dialog failure counts as No (safe default
    for destructive confirmations: quit / mode switch).
    """
    yes, no = t('tray.dialog.yes'), t('tray.dialog.no')
    script = (
        f'display dialog {_as_string(text)} with title {_as_string(title)} '
        f'buttons {{{_as_string(no)}, {_as_string(yes)}}} '
        f'default button {_as_string(yes)} cancel button {_as_string(no)} '
        f'with icon caution'
    )
    result = _osascript(script, timeout=None)
    if result is None:
        logger.error(f"confirm (dialog failed — answering No): {title}: {text}")
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Startup hooks
# ---------------------------------------------------------------------------

# --- Menu bar icon presentation (pystray patch) ---------------------------
# pystray(darwin) `_assert_image` resamples the PIL icon to
# NSStatusBar.thickness() (22pt) *pixels* and hands AppKit a 22x22 bitmap;
# a Retina display upsamples it 2x, so it looks blurry next to native
# (@2x / template / vector) menu bar extras. Native extras are also ~18pt
# tall inside the 22pt bar and are template images (AppKit tints them
# black/white to match the bar's appearance).
MENUBAR_ICON_SCALE = 2         # render @2x, present at 1x point size
MENUBAR_ICON_BODY_PT = 18      # icon body height inside the 22pt bar


def _patch_pystray_menubar_icon() -> None:
    """Retina-sharp, bar-sized, template menu bar icon (same seam as the
    pystray patches in platform_win.platform_setup). Fail-soft: the
    original (blurry) behaviour stays if anything is missing."""
    try:
        import io
        import AppKit
        import Foundation
        import PIL.Image
        from pystray import _darwin
    except Exception as e:
        logger.warning(f"pystray menubar icon patch skipped ({e}) — default rendering")
        return

    def _assert_image(self):
        thickness = int(self._status_bar.thickness())
        size = (thickness, thickness)
        if self._icon_image and self._icon_image.size() == size:
            return
        px = thickness * MENUBAR_ICON_SCALE
        body = min(px, MENUBAR_ICON_BODY_PT * MENUBAR_ICON_SCALE)
        canvas = PIL.Image.new('RGBA', (px, px), (0, 0, 0, 0))
        icon = self._icon.convert('RGBA').resize((body, body), PIL.Image.LANCZOS)
        off = (px - body) // 2
        canvas.alpha_composite(icon, (off, off))
        buf = io.BytesIO()
        canvas.save(buf, 'png')
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(buf.getvalue()))
        image.setSize_(size)        # 44px bitmap shown at 22pt = @2x representation
        image.setTemplate_(True)    # AppKit tints black/white to match the bar
        self._icon_image = image
        self._status_item.button().setImage_(self._icon_image)

    _darwin.Icon._assert_image = _assert_image
    logger.info("pystray menubar icon patch installed (@2x / 18pt / template)")


def platform_setup(app_name: str, icon_path) -> None:
    """Pre-icon startup work. Toast identity is a Windows concern; darwin
    notifications are pystray's osascript `display notification` (Script
    Editor identity — 稜裁定 2026-08-16, see tray_app.TrayApp.notify).
    Installs the menu bar icon patch (must precede pystray Icon creation)."""
    logger.info(f"platform_mac: setup for {app_name} (icon={icon_path})")
    _patch_pystray_menubar_icon()


MENU_REFRESH_SECONDS = 3


def post_icon_setup(icon) -> None:
    """After the pystray icon exists (inside run(setup)):

    1. Hide the Dock icon (P3: plain python shows a Dock rocket).
       Accessory policy keeps the status item but drops Dock/menubar.
    2. Menu freshness: pystray darwin re-evaluates label/enabled
       callables ONLY inside update_menu() (P3) — even opening the menu
       does not. Republish periodically from a daemon thread
       (P3-verified crash-free on macOS 26.5).
    """
    try:
        from AppKit import NSApplication
        # 1 = NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(1)
        logger.info("Dock icon hidden (Accessory activation policy)")
    except Exception as e:
        logger.warning(f"Dock activation policy failed ({e}) — Dock icon stays")

    def refresh():
        while True:
            time.sleep(MENU_REFRESH_SECONDS)
            try:
                icon.update_menu()
            except Exception as e:
                logger.warning(f"Periodic menu refresh failed: {e}")
                return
    threading.Thread(target=refresh, daemon=True, name='menu-refresh').start()
    logger.info(f"Menu refresh thread started ({MENU_REFRESH_SECONDS}s)")
