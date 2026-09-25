"""
ui/hotkey_service.py

Service to handle communication between global hotkeys and UI components.
This module bridges the gap between the backend hotkey handler and the UI.
"""

import logging
import threading
import queue
import time
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum

from backend.shared.hotkey_handler import get_hotkey_handler, HotkeyAction

logger = logging.getLogger(__name__)


class UIAction(Enum):
    """UI actions that can be triggered by hotkeys"""
    START_RECORDING = "start_recording"
    STOP_RECORDING = "stop_recording"


@dataclass
class UIActionRequest:
    """Request to perform a UI action"""
    action: UIAction
    timestamp: float


class HotkeyService:
    """
    Service to handle hotkey events and communicate with the UI.
    
    This service runs in the background and processes hotkey events,
    ensuring proper state management and UI updates.
    """
    
    def __init__(self):
        """Initialize the hotkey service"""
        self.action_queue = queue.Queue(maxsize=10)
        self.ui_callbacks = {}
        self.is_running = False
        self.worker_thread = None

        # Get the hotkey handler instance
        self.hotkey_handler = get_hotkey_handler()
        
    def set_ui_callbacks(self,
                        start_recording: Optional[Callable] = None,
                        stop_recording: Optional[Callable] = None,
                        get_app_state: Optional[Callable] = None) -> None:
        """
        Set UI callback functions.

        Args:
            start_recording: Function to start recording
            stop_recording: Function to stop recording
            get_app_state: Function to get current app state
        """
        self.ui_callbacks['start_recording'] = start_recording
        self.ui_callbacks['stop_recording'] = stop_recording
        self.ui_callbacks['get_app_state'] = get_app_state
        logger.info("UI callbacks registered for hotkey service")
    
    def _handle_start_recording(self) -> None:
        """Handle start recording hotkey"""
        try:
            # Check app state if callback is available
            if self.ui_callbacks.get('get_app_state'):
                app_state = self.ui_callbacks['get_app_state']()

                # Check if we can start recording
                if not app_state.conversation_started:
                    return  # No active conversation

                if not app_state.active_character_id:
                    return  # No character selected

                if app_state.recording_start_time is not None:
                    return  # Already recording

            # Queue the action for the UI thread
            if self.ui_callbacks.get('start_recording'):
                self.action_queue.put(UIActionRequest(
                    action=UIAction.START_RECORDING,
                    timestamp=time.time()
                ))

        except Exception as e:
            logger.error(f"Error handling start recording hotkey: {e}")
    
    def _handle_stop_recording(self) -> None:
        """Handle stop recording hotkey"""
        try:
            # Check app state if callback is available
            if self.ui_callbacks.get('get_app_state'):
                app_state = self.ui_callbacks['get_app_state']()
                
                # Check if we can stop recording
                if not app_state.conversation_started:
                    return  # No active conversation
                    
                if app_state.recording_start_time is None:
                    return  # Not currently recording
            
            # Queue the action for the UI thread
            if self.ui_callbacks.get('stop_recording'):
                self.action_queue.put(UIActionRequest(
                    action=UIAction.STOP_RECORDING,
                    timestamp=time.time()
                ))
                
        except Exception as e:
            logger.error(f"Error handling stop recording hotkey: {e}")
    
    def start(self) -> bool:
        """
        Start the hotkey service.
        
        Returns:
            bool: True if started successfully
        """
        if self.is_running:
            return True
        
        try:
            # Register hotkey callbacks
            self.hotkey_handler.register_callback(
                HotkeyAction.START_RECORDING,
                self._handle_start_recording
            )
            self.hotkey_handler.register_callback(
                HotkeyAction.STOP_RECORDING,
                self._handle_stop_recording
            )

            # Start the hotkey handler
            if not self.hotkey_handler.start():
                logger.error("Failed to start hotkey handler")
                return False
            
            # Start worker thread for processing UI actions
            self.is_running = True
            self.worker_thread = threading.Thread(
                target=self._process_actions,
                name="hotkey-service-worker",
                daemon=True
            )
            self.worker_thread.start()
            
            logger.info("Hotkey service started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start hotkey service: {e}")
            self.is_running = False
            return False
    
    def _process_actions(self) -> None:
        """Process queued UI actions in a separate thread"""
        while self.is_running:
            try:
                # Wait for actions with timeout
                action_request = self.action_queue.get(timeout=0.5)
                
                # Process the action
                if action_request.action == UIAction.START_RECORDING:
                    callback = self.ui_callbacks.get('start_recording')
                    if callback:
                        callback()

                elif action_request.action == UIAction.STOP_RECORDING:
                    callback = self.ui_callbacks.get('stop_recording')
                    if callback:
                        callback()

            except queue.Empty:
                # No actions to process - this is normal
                pass
            except Exception as e:
                logger.error(f"Error processing UI action: {e}")
    
    def stop(self) -> None:
        """Stop the hotkey service"""
        if not self.is_running:
            return
        
        logger.info("Stopping hotkey service...")
        
        # Stop the service
        self.is_running = False
        
        # Stop the hotkey handler
        self.hotkey_handler.stop()
        
        # Wait for worker thread to finish
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        
        # Clear the action queue
        while not self.action_queue.empty():
            try:
                self.action_queue.get_nowait()
            except queue.Empty:
                break
        
        logger.info("Hotkey service stopped")
    
    def get_status(self) -> dict:
        """
        Get the current status of the hotkey service.
        
        Returns:
            dict: Status information
        """
        return {
            'service_running': self.is_running,
            'handler_active': self.hotkey_handler.is_active(),
            'queue_size': self.action_queue.qsize()
        }


# Global instance
_hotkey_service: Optional[HotkeyService] = None


def get_hotkey_service() -> HotkeyService:
    """
    Get the global hotkey service instance.
    
    Returns:
        HotkeyService: The global hotkey service
    """
    global _hotkey_service
    if _hotkey_service is None:
        _hotkey_service = HotkeyService()
    return _hotkey_service


def cleanup_hotkey_service() -> None:
    """Clean up the global hotkey service"""
    global _hotkey_service
    if _hotkey_service:
        _hotkey_service.stop()
        _hotkey_service = None