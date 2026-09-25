"""
openai_stt.py

OpenAI transcription API client for the STT API route.

Pure leaf module: requests + numpy + stdlib only. No app imports — the API
key and model are passed in by the caller (AudioInputManager dispatch), so
this file stays testable and layer-clean.

Contract mirrors the local faster-whisper path: raise RuntimeError on any
failure (audio_input's transcribe functions are raise-on-failure), return
the transcribed text on success.
"""

import io
import logging
import wave

import numpy as np
import requests

logger = logging.getLogger(__name__)

OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"

# OpenAI hard limit for transcription uploads. A 10-minute max recording at
# 16 kHz 16-bit mono is ~19.2 MB, so the WAV path always fits; the guard
# protects the file path (arbitrary browser blobs).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

DEFAULT_TIMEOUT = 60.0


def numpy_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Encode float32 [-1, 1] mono audio as 16-bit PCM WAV bytes.

    Args:
        audio: 1-D float32 (or float64) numpy array in [-1, 1].
        sample_rate: Sample rate in Hz (e.g. 16000).

    Returns:
        Complete WAV file as bytes.
    """
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    return buf.getvalue()


def transcribe_bytes(
    audio_bytes: bytes,
    filename: str,
    api_key: str,
    model: str,
    language: str = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Transcribe an audio payload via the OpenAI transcription API.

    Args:
        audio_bytes: Complete audio file bytes (WAV, WebM, etc.).
        filename: Filename hint for the multipart upload ("audio.wav", ".webm").
        api_key: OpenAI API key.
        model: Transcription model id (choices built by
            backend.shared.api_settings.get_openai_stt_model_choices).
        language: Optional ISO-639-1 language code ("en", "ja").
        timeout: Request timeout in seconds.

    Returns:
        Transcribed text (stripped; may be empty — callers decide silence UX).

    Raises:
        RuntimeError: On missing key, oversized payload, HTTP errors,
            timeouts, and connection failures.
    """
    from .errors import (
        STTError, STT_API_KEY_MISSING, STT_AUDIO_TOO_LARGE, STT_API_TIMEOUT,
        STT_API_UNREACHABLE, STT_API_AUTH, STT_API_RATE_LIMITED,
        STT_API_HTTP_ERROR, STT_API_BAD_RESPONSE,
    )
    if not api_key:
        raise STTError(STT_API_KEY_MISSING,
                       "OpenAI API key is not set. Configure it in the API Setting tab.")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        size_mb = f"{len(audio_bytes) / (1024 * 1024):.1f}"
        raise STTError(
            STT_AUDIO_TOO_LARGE,
            f"Audio is too large for the OpenAI API ({size_mb} MB > 25 MB limit).",
            size_mb=size_mb,
        )

    data = {"model": model}
    if language:
        data["language"] = language

    try:
        response = requests.post(
            OPENAI_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, audio_bytes)},
            data=data,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as e:
        raise STTError(STT_API_TIMEOUT,
                       f"OpenAI transcription request timed out ({timeout:.0f}s).",
                       timeout=f"{timeout:.0f}") from e
    except requests.exceptions.ConnectionError as e:
        raise STTError(STT_API_UNREACHABLE,
                       "Cannot connect to the OpenAI API server.") from e

    if response.status_code in (401, 403):
        raise STTError(
            STT_API_AUTH,
            f"OpenAI API key is invalid or access denied ({response.status_code}).",
            status=response.status_code,
        )
    if response.status_code == 429:
        raise STTError(STT_API_RATE_LIMITED,
                       "OpenAI API rate limit exceeded (429). Try again shortly.")
    if response.status_code != 200:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise STTError(
            STT_API_HTTP_ERROR,
            f"OpenAI transcription failed (HTTP {response.status_code})"
            + (f": {detail}" if detail else "."),
            status=response.status_code,
            detail=f": {detail}" if detail else "",
        )

    try:
        text = response.json().get("text", "")
    except ValueError as e:
        raise STTError(STT_API_BAD_RESPONSE,
                       "OpenAI transcription returned an unreadable response.") from e

    logger.info(f"[OpenAI STT] model={model} lang={language} text='{text.strip()}'")
    return text.strip()
