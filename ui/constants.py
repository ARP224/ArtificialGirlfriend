"""
ui/constants.py

UI constants and configuration values for the Artificial Girlfriend application.
This module centralizes all hardcoded values used across the UI modules.
"""

import os
from pathlib import Path

# Get the application root directory (parent of ui directory)
APP_ROOT = Path(__file__).parent.parent.resolve()

# Audio settings
AUDIO_SAMPLE_RATE = 44100
BEEP_DURATION = 0.2  # seconds
START_BEEP_FREQUENCY = 880  # Hz (A5)
STOP_BEEP_FREQUENCY = 440   # Hz (A4)
MAX_RECORDING_DURATION = 300  # 5 minutes maximum recording time
RECORDING_WARNING_TIME = 240  # Show warning at 4 minutes

# File handling
VALID_IMAGE_EXTENSIONS = frozenset(['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'])
ICON_OUTPUT_SIZE = 512  # Resized icon dimensions (px)
MAX_ICON_PIXELS = 25_000_000  # Max pixel count before resize (5000x5000)

# Character limits
MAX_CHARACTER_NAME_LENGTH = 100
MAX_LOG_MESSAGES = 100
MAX_CHAT_HISTORY_SIZE = 50  # Maximum number of messages to keep in chat history

# Error detection. User-facing error replies (shown in chat in place of an AI
# line) all start with this language-independent marker; is_error_response()
# keys on it so TTS never reads an error aloud (M9). Build these strings ONLY
# via error_handler.user_error() — the old free-form "[Error..." prefix list
# broke silently whenever wording changed, and would break again under
# translation.
ERROR_MESSAGE_PREFIX = "[⚠] "

# Directory paths - single truth source is backend/shared/constants.py
# (ui -> backend.shared is a downward import; ICONS_DIR keeps its UI-side name)
from backend.shared.constants import (  # noqa: E402
    CHARACTER_ICONS_DIR as ICONS_DIR,
    LOGS_DIR,
    TTS_MODELS_DIR,
)

DEFAULT_ICON_PATH = ICONS_DIR / "default.png"

# Timer intervals (in seconds)
LOG_REFRESH_INTERVAL = 1.0

# Retry settings
MAX_BACKEND_INIT_RETRIES = 3
RETRY_DELAY = 1  # seconds

# TTS model path components
TTS_MODEL_FILE = "model.safetensors"
TTS_CONFIG_FILE = "config.json"
TTS_STYLE_VECTORS_FILE = "style_vectors.npy"