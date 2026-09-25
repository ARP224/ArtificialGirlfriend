"""
elevenlabs_client.py

ElevenLabs text-to-speech API client for the TTS API route.

Pure leaf module: requests + PyAV + numpy only. No app imports — voice_id,
model_id and the API key are passed in by the caller (audio_output dispatch).

Contract: synthesize() returns (audio_data: np.ndarray float32 mono, sample
rate) — the same shape text_to_speech() has always returned, so everything
downstream (lipsync, AAC encode, WS playback) consumes it unchanged.
"""

import io
import logging

import av
import numpy as np
import requests

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# mp3_44100_128 is available on every plan tier (PCM/WAV at 44.1 kHz are
# Pro+); PyAV decodes it back to numpy. No language_code is sent: the default
# multilingual models auto-detect and reject an explicit code.
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

DEFAULT_TIMEOUT = 90.0


def _frame_to_mono_float32(frame) -> np.ndarray:
    """Convert one PyAV audio frame to mono float32 [-1, 1]."""
    arr = frame.to_ndarray()
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        arr = arr.astype(np.float32) / 2147483648.0
    elif arr.dtype != np.float32:
        arr = arr.astype(np.float32)

    if arr.ndim == 2:
        if arr.shape[0] > 1:
            # Planar multi-channel: (channels, samples) -> downmix
            arr = arr.mean(axis=0)
        else:
            arr = arr[0]
            # Packed multi-channel: one row of interleaved samples -> downmix
            channels = getattr(frame.layout, "nb_channels", None)
            if channels is None:
                channels = len(getattr(frame.layout, "channels", ()) or ()) or 1
            if channels > 1:
                arr = arr.reshape(-1, channels).mean(axis=1)
    return arr


def _decode_mp3_to_numpy(mp3_bytes: bytes) -> "tuple[np.ndarray, int]":
    """Decode MP3 bytes to (mono float32 waveform, sample_rate) via PyAV.

    Raises:
        RuntimeError: If the payload cannot be decoded or contains no audio.
    """
    try:
        with av.open(io.BytesIO(mp3_bytes)) as container:
            sample_rate = 0
            chunks = []
            for frame in container.decode(audio=0):
                sample_rate = frame.sample_rate or sample_rate
                chunks.append(_frame_to_mono_float32(frame))
    except Exception as e:
        raise RuntimeError(f"Failed to decode ElevenLabs audio: {e}") from e

    if not chunks or not sample_rate:
        raise RuntimeError("ElevenLabs returned no decodable audio data.")
    return np.concatenate(chunks), sample_rate


def synthesize(
    text: str,
    voice_id: str,
    model_id: str,
    api_key: str,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    timeout: float = DEFAULT_TIMEOUT,
) -> "tuple[np.ndarray, int]":
    """Synthesize speech via the ElevenLabs API.

    Args:
        text: Text to speak (caller validates/truncates).
        voice_id: ElevenLabs voice id (from the character's tts_model_config).
        model_id: TTS model id (global setting, e.g. eleven_multilingual_v2).
        api_key: ElevenLabs API key.
        output_format: Audio output format query parameter.
        timeout: Request timeout in seconds.

    Returns:
        (audio_data, sample_rate): mono float32 waveform in [-1, 1].

    Raises:
        RuntimeError: On missing key/voice, HTTP errors, timeouts,
            connection failures, and undecodable audio.
    """
    if not api_key:
        raise RuntimeError("ElevenLabs API key is not set. Configure it in the API Setting tab.")
    if not voice_id:
        raise RuntimeError("ElevenLabs voice is not configured for this character.")

    try:
        response = requests.post(
            ELEVENLABS_TTS_URL.format(voice_id=voice_id),
            params={"output_format": output_format},
            headers={"xi-api-key": api_key},
            json={"text": text, "model_id": model_id},
            timeout=timeout,
        )
    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"ElevenLabs request timed out ({timeout:.0f}s).") from e
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError("Cannot connect to the ElevenLabs API server.") from e

    if response.status_code != 200:
        # The API's detail message matters: 401 covers both an invalid key
        # and a scoped key missing a permission (e.g. text_to_speech).
        detail = ""
        try:
            body = response.json().get("detail", {})
            detail = body.get("message", "") if isinstance(body, dict) else str(body)
        except Exception:
            pass
        if response.status_code in (401, 403):
            base = f"ElevenLabs API key rejected ({response.status_code})."
        elif response.status_code == 429:
            base = "ElevenLabs rate/quota limit exceeded (429). Check your plan usage."
        else:
            base = f"ElevenLabs synthesis failed (HTTP {response.status_code})."
        raise RuntimeError(f"{base} {detail}".strip())

    audio_data, sample_rate = _decode_mp3_to_numpy(response.content)
    logger.info(
        f"[ElevenLabs] synthesized {len(audio_data)} samples at {sample_rate}Hz "
        f"(voice={voice_id}, model={model_id})"
    )
    return audio_data, sample_rate
