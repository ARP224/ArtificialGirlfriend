"""
backend/tools/motion_pngtuber_launcher.py

Handles launching and managing the MotionPNGPlayer Electron application.
"""

import subprocess
import os
import logging
import threading
import time
from typing import Optional, Callable
from pathlib import Path

from backend.shared.i18n import current_language, t

logger = logging.getLogger(__name__)

# Global state for the Electron process
_electron_process: Optional[subprocess.Popen] = None
_process_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_on_close_callback: Optional[Callable] = None

# Path to the MotionPNGPlayer directory (project root, CWD-independent).
# NOTE: this module lives at backend/tools/, so go up 3 levels to reach the repo root.
MOTION_PNGTUBER_DIR = Path(__file__).parent.parent.parent / "MotionPNGPlayer"

# Character assets live in <player>/Asset/<folder name>/ (J2: configs hold the
# bare folder name; the player resolves names the same way on its side).
ASSET_DIR = MOTION_PNGTUBER_DIR / "Asset"


def resolve_character_folder(value: str) -> str:
    """Resolve a bare asset-folder name to <player>/Asset/<name>.

    Explicit paths (absolute, or containing a separator) pass through as-is —
    backward compatibility with configs that still hold full paths.
    """
    if not value:
        return value
    if os.path.isabs(value) or "/" in value or "\\" in value:
        return value
    return str(ASSET_DIR / value)


def list_asset_folders() -> list[str]:
    """Return the character asset folder names under <player>/Asset (sorted)."""
    try:
        return sorted(p.name for p in ASSET_DIR.iterdir() if p.is_dir())
    except OSError:
        return []


def _electron_missing_message() -> str:
    """Localized, actionable message for a missing Electron install.

    インストーラー名はOS別の実ファイル名（翻訳対象外）を {installer} で
    カタログ文へ埋める。Mac の正規セットアップは npm install 単体でなく
    'Install MotionPNGPlayer.command'(node_modules 再構築+起動アプレット
    生成)。Windows 文言のままだと Mac ユーザーを誤誘導する。
    """
    installer = ("Install Artificial Girlfriend (Windows).bat" if os.name == 'nt'
                 else "Install Artificial Girlfriend (Mac).command")
    return t('appear.electron_missing', installer=installer)


def _electron_binary(node_modules: Path) -> Optional[Path]:
    """Resolve the real Electron binary (dist/…) without relying on node.

    node_modules/.bin/electron は '#!/usr/bin/env node' シバンの cli.js
    シムで、起動時に PATH 上の node を要求する。アプレット経由(GUI起動)の
    AG は launchd の既定 PATH (/usr/bin:/bin:/usr/sbin:/sbin) で動き
    Homebrew の node が見えない=シムは exit 127 で即死し「起動成功→無表示」
    になる(Mac実機 2026-07-23)。裁定E(GUI起動はPATH裸名禁止・実パスのみ)に
    従い、electron パッケージ同梱 path.txt (実バイナリへの相対パスの正・
    cli.js 自身も同じ解決をする)で dist/ の実バイナリを直接叩く。
    Windows も同じ解決で dist/electron.exe を直接叩く(npx 不使用)。
    """
    pkg = node_modules / "electron"
    try:
        rel = (pkg / "path.txt").read_text(encoding="utf-8").strip()
        if rel:
            candidate = pkg / "dist" / rel
            if candidate.exists():
                return candidate
    except OSError:
        pass
    # path.txt が無い/指す先が無い場合の既知レイアウト (mac / linux / windows)
    for rel in ("Electron.app/Contents/MacOS/Electron", "electron", "electron.exe"):
        candidate = pkg / "dist" / rel
        if candidate.exists():
            return candidate
    return None


def is_electron_running() -> bool:
    """Check if the Electron process is currently running."""
    global _electron_process
    with _process_lock:
        if _electron_process is None:
            return False
        # Check if process is still running
        poll_result = _electron_process.poll()
        if poll_result is not None:
            # Process has terminated
            _electron_process = None
            return False
        return True


def launch_motion_pngtuber(ws_port: int, character_folder: str) -> tuple[bool, str]:
    """
    Launch the Motion PNG Tuber Electron application.

    The player window follows AG's UI language (passed via --language).

    Args:
        ws_port: WebSocket server port for communication
        character_folder: Path to the character's motion folder

    Returns:
        Tuple of (success: bool, message: str)
    """
    global _electron_process

    # Check if character folder is specified
    if not character_folder:
        logger.warning("[MotionPNGTuber] No character folder specified")
        return False, "No Motion PNG Tuber folder configured for this character"

    # Resolve bare asset-folder names to <player>/Asset/<name>
    character_folder = resolve_character_folder(character_folder)

    # Check if character folder exists
    if not os.path.isdir(character_folder):
        logger.error(f"[MotionPNGTuber] Character folder not found: {character_folder}")
        return False, f"Motion folder not found: {character_folder}"

    # Check if already running
    if is_electron_running():
        logger.info("[MotionPNGTuber] Already running")
        return True, "Motion PNG Tuber is already running"

    # Check if MotionPNGPlayer directory exists
    if not MOTION_PNGTUBER_DIR.exists():
        logger.error(f"[MotionPNGTuber] Player directory not found: {MOTION_PNGTUBER_DIR}")
        return False, t('appear.player_missing')

    # Check the Electron runtime is present (the installer downloads it into
    # node_modules/electron/dist). Fail with an actionable message instead of
    # letting Popen die on a missing path.
    node_modules = MOTION_PNGTUBER_DIR / "node_modules"
    if not node_modules.exists():
        logger.error("[MotionPNGTuber] node_modules not found — run the installer")
        return False, _electron_missing_message()

    try:
        with _process_lock:
            logger.info(f"[MotionPNGTuber] Launching with ws_port={ws_port}, folder={character_folder}")

            # Both OS: run the dist/ Electron binary directly. No node/npx —
            # Node.js はインストール前提から全廃(2026-07-29 案B)。PATH 裸名
            # 起動は GUI 起動の PATH 差で即死する(Mac 実踏 2026-07-23)ため
            # 実パスのみ。
            electron_bin = _electron_binary(node_modules)
            if electron_bin is None:
                logger.error(
                    f"[MotionPNGTuber] Electron dist binary not found under {node_modules / 'electron'}")
                return False, _electron_missing_message()
            cmd = [
                str(electron_bin), ".",
                "--ws-port", str(ws_port),
                "--character-folder", character_folder,
                "--language", current_language()
            ]

            # Start process. Use DEVNULL, not PIPE: nothing reads these pipes,
            # so a verbose Electron would fill the ~64KB OS pipe buffer and hang.
            _electron_process = subprocess.Popen(
                cmd,
                cwd=str(MOTION_PNGTUBER_DIR),
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            logger.info(f"[MotionPNGTuber] Process started with PID: {_electron_process.pid}")

            # 即死検出: 起動系の失敗(依存欠如・シバン解決不能等)は1秒以内に
            # 死ぬ。黙って「起動成功」を返すとGUI起動ではログも見えず
            # 無反応にしか見えない(Mac実機 2026-07-23)ため、ここで露出させる。
            time.sleep(1.0)
            exit_code = _electron_process.poll()
            if exit_code is not None:
                logger.error(
                    f"[MotionPNGTuber] Electron died immediately (exit code {exit_code})")
                _electron_process = None
                return False, t('appear.electron_died', code=exit_code)

            # Start process monitor thread
            _start_process_monitor()

            return True, "Motion PNG Tuber launched successfully"

    except FileNotFoundError as e:
        logger.error(f"[MotionPNGTuber] electron binary not found: {e}")
        return False, _electron_missing_message()
    except Exception as e:
        logger.error(f"[MotionPNGTuber] Failed to launch: {e}")
        return False, t('appear.launch_failed', error=str(e))


def stop_motion_pngtuber() -> tuple[bool, str]:
    """
    Stop the Motion PNG Tuber Electron application.

    On Windows with shell=True, terminate()/kill() only kills the cmd.exe
    shell wrapper, leaving the actual Electron process alive. We use
    taskkill /F /T to kill the entire process tree.

    Returns:
        Tuple of (success: bool, message: str)
    """
    global _electron_process

    with _process_lock:
        if _electron_process is None:
            return True, "Motion PNG Tuber is not running"

        try:
            pid = _electron_process.pid
            logger.info(f"[MotionPNGTuber] Stopping process tree (PID: {pid})...")

            if os.name == 'nt':
                # Windows: kill entire process tree (shell=True creates cmd.exe wrapper)
                subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    capture_output=True,
                    timeout=10
                )
            else:
                _electron_process.terminate()

            # Wait for the tracked process to finish
            try:
                _electron_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("[MotionPNGTuber] Process did not terminate, forcing kill")
                try:
                    _electron_process.kill()
                    _electron_process.wait(timeout=2)
                except Exception:
                    pass

            _electron_process = None
            logger.info("[MotionPNGTuber] Process stopped")
            return True, "Motion PNG Tuber stopped"

        except Exception as e:
            logger.error(f"[MotionPNGTuber] Error stopping process: {e}")
            _electron_process = None
            return False, f"Error stopping: {str(e)}"


def get_status() -> dict:
    """
    Get the current status of Motion PNG Tuber.

    Returns:
        Status dictionary with 'running' and 'pid' fields
    """
    global _electron_process

    with _process_lock:
        if _electron_process is None:
            return {"running": False, "pid": None}

        poll_result = _electron_process.poll()
        if poll_result is not None:
            _electron_process = None
            return {"running": False, "pid": None}

        return {"running": True, "pid": _electron_process.pid}


def set_on_close_callback(callback: Optional[Callable]) -> None:
    """
    Set a callback function to be called when the Electron process closes.

    Args:
        callback: Function to call when process closes (no arguments)
    """
    global _on_close_callback
    _on_close_callback = callback
    logger.debug(f"[MotionPNGTuber] Close callback set: {callback is not None}")


def _start_process_monitor() -> None:
    """Start a background thread to monitor the Electron process."""
    global _monitor_thread

    def monitor():
        global _electron_process, _on_close_callback
        logger.debug("[MotionPNGTuber] Process monitor started")

        while True:
            time.sleep(1)  # Check every second

            with _process_lock:
                if _electron_process is None:
                    logger.debug("[MotionPNGTuber] Process is None, stopping monitor")
                    break

                poll_result = _electron_process.poll()
                if poll_result is not None:
                    # Process has terminated
                    logger.info(f"[MotionPNGTuber] Process terminated with code: {poll_result}")
                    _electron_process = None

                    # Call the close callback
                    if _on_close_callback:
                        try:
                            _on_close_callback()
                        except Exception as e:
                            logger.error(f"[MotionPNGTuber] Error in close callback: {e}")
                    break

        logger.debug("[MotionPNGTuber] Process monitor stopped")

    _monitor_thread = threading.Thread(target=monitor, name="motion-pngtuber-monitor", daemon=True)
    _monitor_thread.start()


# Cleanup function to be called on application shutdown
def cleanup():
    """Clean up Motion PNG Tuber process on application shutdown."""
    if is_electron_running():
        logger.info("[MotionPNGTuber] Cleaning up on shutdown...")
        stop_motion_pngtuber()
