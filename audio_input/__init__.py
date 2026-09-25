"""
Audio Input Module for Artificial Girlfriend

This module handles audio capture and speech-to-text transcription.
"""

from .audio_input import (
    VALID_MODEL_SIZES,
    init_audio_input,
    configure_stt,
    start_model_load,
    is_model_ready,
    is_model_loading,
    unload_model,
    set_model_size,
    start_recording,
    stop_recording,
    transcribe_audio,
    transcribe_file,
    get_available_devices,
    precheck_input_device,
    refresh_device_table,
    last_recording_fallback,
    set_max_recording_duration,
    is_currently_recording,
    cleanup
)

__all__ = [
    'VALID_MODEL_SIZES',
    'init_audio_input',
    'configure_stt',
    'start_model_load',
    'is_model_ready',
    'is_model_loading',
    'unload_model',
    'set_model_size',
    'start_recording',
    'stop_recording',
    'transcribe_audio',
    'transcribe_file',
    'get_available_devices',
    'precheck_input_device',
    'refresh_device_table',
    'last_recording_fallback',
    'set_max_recording_duration',
    'is_currently_recording',
    'cleanup'
]