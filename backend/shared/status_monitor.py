"""
backend/status_monitor.py

Status monitoring service for memory-task completion detection
(long-term memory extraction + relationship update = backend.is_memory_task_running).
Sends WebSocket notifications when those tasks finish.

Note: General service status (Ollama/Whisper/TTS) is checked on-demand
at page load, character switch, and conversation start — not polled.
"""

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StatusMonitor:
    """
    Monitors memory-task status (extraction / relationship update) and sends
    WebSocket updates when it completes.
    General service status (Ollama, Whisper, TTS) is no longer polled here;
    it is updated on-demand by the UI layer.
    """

    def __init__(self):
        """Initialize the status monitor"""
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.previous_extracting: bool = False  # Track extraction state
        self.check_interval = 1  # Check every 1 second (for extraction monitoring)
        self._lock = threading.Lock()

    def check_extracting_status(self) -> bool:
        """
        Check if a memory task that must not be interrupted is running
        (extraction or relationship update; same predicate as the UI/tray guards).

        Returns:
            bool: True if a memory task is in progress
        """
        try:
            from backend import is_memory_task_running
            return is_memory_task_running()
        except Exception as e:
            logger.debug(f"[Status Monitor] Error checking memory task status: {e}")
            return False

    def send_extraction_update(self, is_extracting: bool):
        """Send memory-task status update via WebSocket.

        アクション名 extraction_started/completed と payload キー is_extracting は
        JS/モバイル/admin/リプレイバッファが読む線路上の名前=不変。意味は
        「記憶タスク(抽出+relationship)の開始/完了」に広がっている。
        """
        try:
            from backend.shared.ui_events import publish_ui_update

            action = "extraction_completed" if not is_extracting else "extraction_started"
            success = publish_ui_update(
                action,
                reason="status_changed",
                data={"is_extracting": is_extracting, "timestamp": time.time()}
            )

            if success:
                logger.info(f"[Status Monitor] Extraction {action} notification sent")
            else:
                logger.debug("[Status Monitor] No WebSocket clients connected for extraction update")

        except Exception as e:
            logger.error(f"[Status Monitor] Failed to send extraction update: {e}")

    def monitor_loop(self):
        """Main monitoring loop - monitors extraction status only"""
        logger.info("[Status Monitor] Starting monitoring loop (extraction only)")

        # Wait before first check to ensure WebSocket server is ready
        time.sleep(5)

        while self.running:
            try:
                # Check extraction status every second (lightweight check)
                current_extracting = self.check_extracting_status()

                with self._lock:
                    # Only send notification when the memory task ENDS
                    # (start notification is sent immediately when task is added
                    # in conversation_manager, for both extraction and the early
                    # relationship update)
                    if self.previous_extracting and not current_extracting:
                        logger.info("[Status Monitor] Memory tasks completed - sending update")
                        self.send_extraction_update(False)

                    self.previous_extracting = current_extracting

                time.sleep(self.check_interval)

            except Exception as e:
                logger.error(f"[Status Monitor] Error in monitoring loop: {e}")
                time.sleep(self.check_interval)

        logger.info("[Status Monitor] Monitoring loop stopped")

    def start(self):
        """Start the status monitoring thread"""
        with self._lock:
            if self.running:
                logger.warning("[Status Monitor] Already running")
                return

            self.running = True
            self.thread = threading.Thread(target=self.monitor_loop, daemon=True)
            self.thread.start()
            logger.info("[Status Monitor] Started successfully (extraction only)")

    def stop(self):
        """Stop the status monitoring thread"""
        with self._lock:
            if not self.running:
                logger.warning("[Status Monitor] Not running")
                return

            self.running = False

        if self.thread:
            self.thread.join(timeout=self.check_interval + 1)
            logger.info("[Status Monitor] Stopped successfully")

    def is_running(self) -> bool:
        """Check if monitor is running"""
        return self.running


# Global instance
_status_monitor: Optional[StatusMonitor] = None
_monitor_lock = threading.Lock()


def get_status_monitor() -> StatusMonitor:
    """
    Get the global status monitor instance.

    Returns:
        StatusMonitor: The global monitor instance
    """
    global _status_monitor

    with _monitor_lock:
        if _status_monitor is None:
            _status_monitor = StatusMonitor()

    return _status_monitor


def start_status_monitoring():
    """Start the global status monitoring service"""
    monitor = get_status_monitor()
    monitor.start()
    return monitor


def stop_status_monitoring():
    """Stop the global status monitoring service"""
    monitor = get_status_monitor()
    monitor.stop()


# Export main functions
__all__ = [
    'StatusMonitor',
    'get_status_monitor',
    'start_status_monitoring',
    'stop_status_monitoring'
]
