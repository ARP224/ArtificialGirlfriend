"""
ui/handlers/hotkey.py

Global-hotkey ↔ UI glue: wires the hotkey service callbacks to recording and
command-approval actions. Extracted verbatim from ui/app.py (B11c) as a
self-contained UI handler leaf (registry-seam home: ui/handlers/<feature>.py).
"""

import logging

from ..hotkey_service import get_hotkey_service
from ..state import app_state

logger = logging.getLogger(__name__)


def setup_hotkey_callbacks() -> None:
    """
    Set up hotkey service callbacks to interact with the UI.
    """
    logger.info("[HOTKEY-SETUP] Setting up hotkey callbacks...")
    
    try:
        hotkey_service = get_hotkey_service()
        logger.info("[HOTKEY-SETUP] Got hotkey service instance")
        
        # Create callback functions that interact with the UI
        def hotkey_start_recording():
            """Start recording via hotkey"""
            logger.info("[HOTKEY-CALLBACK] hotkey_start_recording called")
            try:
                # Check if we can start recording
                if not app_state.conversation_started:
                    logger.warning("[HOTKEY-CALLBACK] Start ignored: No active conversation")
                    return
                    
                if not app_state.active_character_id:
                    logger.warning("[HOTKEY-CALLBACK] Start ignored: No character selected")
                    return
                    
                if app_state.recording_start_time is not None:
                    logger.warning("[HOTKEY-CALLBACK] Start ignored: Already recording")
                    return
                
                logger.info("[HOTKEY-CALLBACK] All checks passed, calling external_start_recording")
                # Trigger the external recording function
                from ..conversation import external_start_recording
                result = external_start_recording()
                logger.info(f"[HOTKEY-CALLBACK] external_start_recording returned: {result}")
                if result:
                    logger.info("[HOTKEY-CALLBACK] Recording started successfully via global hotkey")
                    
            except Exception as e:
                logger.error(f"[HOTKEY-CALLBACK] Error in hotkey start recording: {e}", exc_info=True)
        
        def hotkey_stop_recording():
            """Stop recording via hotkey"""
            logger.info("[HOTKEY-CALLBACK] hotkey_stop_recording called")
            try:
                # Check if we can stop recording
                if not app_state.conversation_started:
                    logger.warning("[HOTKEY-CALLBACK] Stop ignored: No active conversation")
                    return
                    
                if app_state.recording_start_time is None:
                    logger.warning("[HOTKEY-CALLBACK] Stop ignored: Not currently recording")
                    return
                
                logger.info("[HOTKEY-CALLBACK] All checks passed, calling external_stop_recording")
                # Trigger the external recording function
                from ..conversation import external_stop_recording
                result = external_stop_recording()
                logger.info(f"[HOTKEY-CALLBACK] external_stop_recording returned: {result}")
                if result:
                    logger.info("[HOTKEY-CALLBACK] Recording stopped successfully via global hotkey")
                    
            except Exception as e:
                logger.error(f"[HOTKEY-CALLBACK] Error in hotkey stop recording: {e}", exc_info=True)
        
        def get_app_state_callback():
            """Get current app state for hotkey validation"""
            return app_state

        # Set the callbacks
        logger.info("[HOTKEY-SETUP] Setting UI callbacks...")
        hotkey_service.set_ui_callbacks(
            start_recording=hotkey_start_recording,
            stop_recording=hotkey_stop_recording,
            get_app_state=get_app_state_callback
        )
        logger.info("[HOTKEY-SETUP] UI callbacks set successfully")
        
        # Start the hotkey service
        logger.info("[HOTKEY-SETUP] Starting hotkey service...")
        if hotkey_service.start():
            logger.info("[HOTKEY-SETUP] Global hotkey service started successfully")
            logger.info("[HOTKEY-SETUP] Global hotkeys enabled: Ctrl+1 (Start), Ctrl+0 (Stop)")
        else:
            logger.warning("[HOTKEY-SETUP] Failed to start global hotkey service")
            
    except Exception as e:
        logger.error(f"[HOTKEY-SETUP] Error setting up hotkey callbacks: {e}", exc_info=True)
