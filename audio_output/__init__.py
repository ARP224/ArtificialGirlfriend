"""
Audio Output Module for Artificial Girlfriend

This module provides Text-to-Speech (TTS) and audio playback functionality
using Style-Bert-VITS2 and sounddevice.
"""

from .audio_output import (
    init_audio_output,
    configure_tts_model,
    configure_elevenlabs_tts,
    configure_kokoro_tts,
    get_current_tts_provider,
    unload_tts_model,
    text_to_speech,
    get_available_styles,
    check_gpu_memory,
    # Browser playback / MotionPNGTuber integration
    calculate_lipsync_frames,
    audio_to_aac_mp4_base64,
    cleanup
)

__all__ = [
    'init_audio_output',
    'configure_tts_model',
    'configure_elevenlabs_tts',
    'configure_kokoro_tts',
    'get_current_tts_provider',
    'unload_tts_model',
    'text_to_speech',
    'get_available_styles',
    'check_gpu_memory',
    # Browser playback / MotionPNGTuber integration
    'calculate_lipsync_frames',
    'audio_to_aac_mp4_base64',
    'cleanup'
]