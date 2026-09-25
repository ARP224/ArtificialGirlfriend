"""
backend/shared/mac_permissions.py

macOS TCC permission preflight (Mac port plan 3-3).

darwin-only: importable everywhere, but callers must guard with
platform_caps.IS_MAC (call-time import) — nothing here is meant to run
on Windows. All checks are fail-soft ('unknown' on any error) because
the whole point is to make TCC's silent failures visible (denied mic =
silent empty recording, denied screen = wallpaper-only capture, denied
input monitoring = dead hotkeys, denied automation = dead window
control), never to add a new crash source.

Probe-verified APIs (P5, 2026-07-20 M1/macOS 26.5):
  - Quartz.CGPreflightScreenCaptureAccess()  / CGPreflightListenEventAccess()
    -> bool (no denied/undetermined distinction: False = 'not_granted')
  - AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    -> 0 notDetermined / 1 restricted / 2 denied / 3 authorized
  - AEDeterminePermissionToAutomateTarget (ctypes, askUserIfNeeded=False)
    -> 0 granted / -1744 undetermined / -1743 denied / -600 target not
    running (reported as 'unknown' — determining it would launch Chrome)

The TCC subject is the RESPONSIBLE PROCESS: permissions attach to
whatever launched python (the osacompile applet in the intended setup).
Statuses: 'granted' | 'denied' | 'undetermined' | 'not_granted' | 'unknown'.
"""

import ctypes
import logging

logger = logging.getLogger(__name__)

CHROME_BUNDLE_ID = b'com.google.Chrome'

# Statuses that deserve a user-facing warning ('unknown' does not: it
# means the check itself failed / target not running — no information).
WARN_STATUSES = frozenset({'denied', 'undetermined', 'not_granted'})


def _check_microphone() -> str:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        return {0: 'undetermined', 1: 'denied', 2: 'denied', 3: 'granted'}.get(
            int(status), 'unknown')
    except Exception as e:
        logger.warning(f"Microphone permission check failed: {e}")
        return 'unknown'


def _check_screen_recording() -> str:
    try:
        import Quartz
        return 'granted' if Quartz.CGPreflightScreenCaptureAccess() else 'not_granted'
    except Exception as e:
        logger.warning(f"Screen recording permission check failed: {e}")
        return 'unknown'


def _check_input_monitoring() -> str:
    try:
        import Quartz
        return 'granted' if Quartz.CGPreflightListenEventAccess() else 'not_granted'
    except Exception as e:
        logger.warning(f"Input monitoring permission check failed: {e}")
        return 'unknown'


class _AEDesc(ctypes.Structure):
    _fields_ = [('descriptorType', ctypes.c_uint32),
                ('dataHandle', ctypes.c_void_p)]


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode('ascii'), 'big')


def _check_automation_chrome() -> str:
    """Pure query (askUserIfNeeded=False) — never shows the consent dialog."""
    try:
        lib = ctypes.CDLL(
            '/System/Library/Frameworks/CoreServices.framework/CoreServices')
        desc = _AEDesc()
        err = lib.AECreateDesc(
            _fourcc('bund'),  # typeApplicationBundleID
            CHROME_BUNDLE_ID, len(CHROME_BUNDLE_ID), ctypes.byref(desc))
        if err != 0:
            logger.warning(f"AECreateDesc failed: {err}")
            return 'unknown'
        try:
            status = lib.AEDeterminePermissionToAutomateTarget(
                ctypes.byref(desc), _fourcc('****'), _fourcc('****'), False)
        finally:
            lib.AEDisposeDesc(ctypes.byref(desc))
        if status == 0:
            return 'granted'
        if status == -1744:   # errAEEventWouldRequireUserConsent
            return 'undetermined'
        if status == -1743:   # errAEEventNotPermitted
            return 'denied'
        if status == -600:    # procNotFound: Chrome not running — no info
            return 'unknown'
        logger.warning(f"AEDeterminePermissionToAutomateTarget: unexpected {status}")
        return 'unknown'
    except Exception as e:
        logger.warning(f"Automation permission check failed: {e}")
        return 'unknown'


def check_permissions() -> dict:
    """All four TCC permissions AG uses, as {name: status}."""
    return {
        'microphone': _check_microphone(),
        'screen_recording': _check_screen_recording(),
        'input_monitoring': _check_input_monitoring(),
        'automation_chrome': _check_automation_chrome(),
    }


def log_permission_status() -> dict:
    """One startup-log line per permission (plan §2: Mac側Claude Codeが
    ログだけで一次切り分けできる状態にする). Returns the statuses."""
    statuses = check_permissions()
    for name, status in statuses.items():
        log = logger.warning if status in WARN_STATUSES else logger.info
        log(f"[TCC] {name}: {status}")
    return statuses


def warn_permissions() -> list:
    """Permission names whose status deserves a user-facing warning."""
    return [name for name, status in check_permissions().items()
            if status in WARN_STATUSES]
