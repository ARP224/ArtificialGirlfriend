"""
kokoro_engine.py

Kokoro-82M (English local TTS) engine state and synthesis for audio_output.

Role mirrors the SBV2 globals in audio_output.py: this module owns the loaded
KModel / KPipeline / voice pack, while audio_output.py owns the provider
dispatch (configure_kokoro_tts / text_to_speech). English only by design
(ja is served by SBV2, the JP-Extra route) — misaki[ja] is intentionally NOT
installed, so never pass Japanese lang codes here.

Assets are fixed files under kokoro/ at the repo root (fetched by the
installer):
    config.json / kokoro-v1_0.pth / voices/<voice>.pt
The voice file name (without .pt) is the character config's voice_name.
Kept outside sbv2_models/ (user drop-in territory) so a user folder named
"Kokoro" can never merge into these assets (2026-08-02). The folder does not
shadow the pip package `kokoro`: it has no __init__.py, so PEP 420 resolves
the real package from site-packages first.
"""

import gc
import glob
import logging
import os
import threading
from typing import List, Optional, Tuple

import numpy as np

from . import ascii_data_mirror

logger = logging.getLogger(__name__)

# Repo-root anchored (audio_output/ is a top-level package), same convention as
# DEFAULT_TTS_BASE_PATH in audio_output.py so discovery is CWD-independent.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KOKORO_DIR = os.path.join(_REPO_ROOT, "kokoro")
KOKORO_CONFIG_PATH = os.path.join(KOKORO_DIR, "config.json")
KOKORO_MODEL_PATH = os.path.join(KOKORO_DIR, "kokoro-v1_0.pth")
KOKORO_VOICES_DIR = os.path.join(KOKORO_DIR, "voices")
# Passed to KModel/KPipeline only to suppress their "defaulting repo_id"
# warnings — all files are local, nothing is ever downloaded here.
KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
SAMPLE_RATE = 24000  # Kokoro-82M is fixed 24kHz output

# kokoro は import だけで torch/spacy/misaki を引き込む(冷時~3秒)ため、SBV2 と
# 同じく実利用まで import を遅延する。KModel への代入が「import完了」フラグ。
KModel = None
KPipeline = None
_import_lock = threading.Lock()

# Loaded engine state. The 327MB KModel is shared across voices and kept when
# only the voice changes; _pipelines caches one KPipeline per lang code.
_state_lock = threading.Lock()
_model = None
_device: Optional[str] = None
_pipelines: dict = {}
_voice_name: Optional[str] = None
_voice_pack = None

# Serialize actual inference (KPipeline G2P internals are not documented as
# thread-safe; the app serializes speech anyway, this is a cheap guarantee).
_infer_lock = threading.Lock()


def _ensure_kokoro() -> None:
    """Materialize the lazy kokoro import (idempotent, double-checked)."""
    global KModel, KPipeline
    if KModel is not None:
        return
    with _import_lock:
        if KModel is not None:
            return
        from kokoro import KModel as _KModel
        from kokoro import KPipeline as _KPipeline

        KPipeline = _KPipeline
        # 最後に代入(完了フラグ兼用)
        KModel = _KModel
        logger.info("Kokoro TTS stack imported")


def voice_path(voice_name: str) -> str:
    return os.path.join(KOKORO_VOICES_DIR, f"{voice_name}.pt")


def _lang_code(voice_name: str) -> str:
    """Kokoro の声名接頭辞が言語を符号化している: af_/am_=US, bf_/bm_=GB."""
    return "a" if voice_name.startswith(("af_", "am_")) else "b"


def list_voices() -> List[str]:
    """Voice names (file stem) available under kokoro/voices/."""
    try:
        return sorted(
            os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(KOKORO_VOICES_DIR, "*.pt"))
        )
    except OSError as e:
        logger.warning(f"Failed to scan Kokoro voices: {e}")
        return []


def validate_assets(voice_name: str) -> None:
    """Raise with an actionable message if the fixed assets are missing.

    Called BEFORE the current provider is torn down (never break a loaded
    SBV2 state for a broken Kokoro config — same principle as ElevenLabs).
    """
    if not voice_name or not isinstance(voice_name, str):
        raise ValueError("Kokoro voice_name is empty - check the character's TTS setting.")
    for path, desc in (
        (KOKORO_CONFIG_PATH, "config"),
        (KOKORO_MODEL_PATH, "model"),
    ):
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Kokoro {desc} not found: {path} - run the installer to fetch "
                f"the Kokoro TTS assets."
            )
    if not os.path.isfile(voice_path(voice_name)):
        raise FileNotFoundError(
            f"Kokoro voice '{voice_name}' not found in {KOKORO_VOICES_DIR}"
        )


def load(voice_name: str, device: str) -> None:
    """Load (or switch to) a Kokoro voice. Reuses the KModel across voices."""
    global _model, _device, _pipelines, _voice_name, _voice_pack
    _ensure_kokoro()
    # 非ASCIIインストールパス対策: espeak-ng データを ASCII 複製へ向ける(ASCII なら no-op)。
    # kokoro import 後(misaki.espeak が既定パスを設定した後)・KPipeline 生成前に適用する
    ascii_data_mirror.apply_espeak_data_path()
    import torch

    with _state_lock:
        if _model is None or _device != device:
            logger.info(f"Loading Kokoro model on {device} from {KOKORO_MODEL_PATH}")
            _model = (
                KModel(
                    repo_id=KOKORO_REPO_ID,
                    config=KOKORO_CONFIG_PATH,
                    model=KOKORO_MODEL_PATH,
                )
                .to(device)
                .eval()
            )
            _device = device
            _pipelines = {}
        lang = _lang_code(voice_name)
        if lang not in _pipelines:
            _pipelines[lang] = KPipeline(
                lang_code=lang, model=_model, repo_id=KOKORO_REPO_ID
            )
        _voice_pack = torch.load(voice_path(voice_name), weights_only=True)
        _voice_name = voice_name
    logger.info(f"Kokoro voice loaded: {voice_name} (lang_code={lang}, device={device})")


def get_current_voice() -> Optional[str]:
    with _state_lock:
        return _voice_name


def synthesize(text: str) -> Tuple["np.ndarray", int]:
    """Kokoro route for text_to_speech (same (audio, sr) return contract).

    :raises RuntimeError: If the engine is not configured or yields no audio
        (callers treat TTS failures as non-critical, same as SBV2/ElevenLabs).
    """
    with _state_lock:
        if _model is None or _voice_pack is None or _voice_name is None:
            raise RuntimeError(
                "Kokoro TTS is not configured. Activate a character with a "
                "Kokoro voice first."
            )
        pipeline = _pipelines[_lang_code(_voice_name)]
        pack = _voice_pack

    with _infer_lock:
        chunks = [audio for _, _, audio in pipeline(text, voice=pack)]
    if not chunks:
        raise RuntimeError("Kokoro TTS produced no audio for the given text.")
    wav = np.concatenate([c.detach().cpu().numpy() for c in chunks]).astype(np.float32)
    return wav, SAMPLE_RATE


def unload() -> None:
    """Free the Kokoro model/voice (no-op when nothing is loaded).

    Never triggers the kokoro import: called from unload_tts_model() on every
    provider switch, including installs that never use Kokoro.
    """
    global _model, _device, _pipelines, _voice_name, _voice_pack
    with _state_lock:
        if _model is None and _voice_pack is None:
            return
        _model = None
        _device = None
        _pipelines = {}
        _voice_name = None
        _voice_pack = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    logger.info("Kokoro TTS engine unloaded")
