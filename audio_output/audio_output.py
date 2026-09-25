"""
audio_output.py

This module handles all Text-to-Speech (TTS) operations and audio playback
for Artificial Girlfriend, using Style-Bert-VITS2 and sounddevice.

Key Functions:
    init_audio_output()
        Prepares any global resources needed for audio output (e.g., logging).

    configure_tts_model(model_path, config_path, style_vectors_path, device="cuda")
        Loads or switches to a specific TTS model. Also loads the Japanese
        BERT model if needed (SBV2 is the ja-only route; en is Kokoro's job).

    unload_tts_model()
        Unloads the current TTS model to free resources.

    text_to_speech(text, style="Neutral", speaker_id=0)
        Performs TTS inference to convert text into an audio waveform.

    check_gpu_memory()
        Checks available GPU memory and determines if there's enough for TTS.

Requirements Reflected:
    - Single TTS model loaded at a time (global current_tts_model).
    - No file saving; only real-time playback.
    - If style is invalid or leads to an error, fall back to "Neutral" once.
    - Raise errors (and log) on GPU out-of-memory or audio device issues.
    - SBV2 is Japanese-only by design (2026-07-26 ruling): JP-Extra models are
      the ecosystem norm, English is served by the Kokoro provider instead.
      A non-JP-Extra model still works here - it simply speaks Japanese.
    - All operations are local/offline, using Style-Bert-VITS2 for TTS.
"""

import logging
import os
import sounddevice as sd
import numpy as np
from typing import Tuple, List, Optional, Dict, Any
import gc
import threading
import glob

from . import ascii_data_mirror
from . import kokoro_engine

# Style-Bert-VITS2 は import だけで transformers/numba/scipy(冷時~11秒)を引き込む
# ため、モデルロードと同じタイミング(_ensure_sbv2)まで import 自体を遅延する。
# None のままなら未import(TTSModel への代入が「import完了」フラグを兼ねる)。
TTSModel = None
bert_models = None
Languages = None

logger = logging.getLogger(__name__)

# Configuration constants
# Repo-root anchored (audio_output/ is a top-level package) so model discovery
# does not depend on the process CWD.
DEFAULT_TTS_BASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sbv2_models",
)
# SBV2 は日本語専用(2026-07-26 裁定: JP-Extra がエコシステムの実態・英語は
# Kokoro プロバイダが担う)。言語→定数の対応は _ensure_sbv2() が充填する
# (空=未import。ja のみ=推論言語は常に JP)。
SBV2_LANGUAGES: Dict[str, Any] = {}
# ja 推論にSBV2が要求するBERTモデル(HuggingFace名)。
DEFAULT_BERT_MODELS = {
    "ja": "ku-nlp/deberta-v2-large-japanese-char-wwm",
}
# BERT のローカル配置フォルダ。bert/<lang>/ (repo直下) に config.json ごと
# モデル一式を置けば、HFキャッシュより優先してそのフォルダを使う。
# 2026-08-01 からインストーラーが fetch_bert_model でここへ事前配置する
# (クリーンOSの初回キャラ選択が同期DLで約1分固まっていた対策)。
# ユーザーによる手動上書きも従来どおり可。sbv2_models/ の外に置くのは、
# ユーザー投入フォルダ「bert」との統合事故を防ぐため(2026-08-02)。
DEFAULT_BERT_BASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bert",
)
CPU_FALLBACK_ENABLED = False  # Set to False as per Section 3.2 requirement
MIN_GPU_MEMORY_MB = 2000  # Minimum recommended GPU memory in MB


# Global references to the current TTS model and language BERT
current_tts_model = None
current_bert_language = None

# Active TTS provider: "sbv2" (local Style-Bert-VITS2, ja), "kokoro" (local
# Kokoro-82M, en) or "elevenlabs" (API). configure_tts_model() /
# configure_kokoro_tts() / configure_elevenlabs_tts() set it at character
# activation; text_to_speech() dispatches on it. Kokoro engine state lives in
# kokoro_engine.py (module import is cheap; the kokoro stack itself is lazy).
current_tts_provider = "sbv2"
current_elevenlabs_voice_id = None

# Thread lock for TTS model access synchronization
_tts_model_lock = threading.Lock()

# _ensure_sbv2 の二重チェック用(並行呼び出しの競合を直列化。通常は起動時の
# 開店前仕込み(ui/app.py _preload_ml_stacks)が先に済ませている)
_sbv2_import_lock = threading.Lock()

_diagnostics_logged = False


def _log_tts_stack_diagnostics() -> None:
    """
    ML スタックのバージョン/ABI 互換診断(旧 init_audio_output 内から移設)。

    torch/numba/pyworld を起動経路で import しないよう、_ensure_sbv2() 経由で
    最初のTTS利用(通常は起動時の開店前仕込み)時に1回だけ実行する。
    ABI 不整合の実弾チェック(tensor→numpy)は初回推論より必ず前に走る。
    """
    global _diagnostics_logged
    if _diagnostics_logged:
        return
    _diagnostics_logged = True

    try:
        import torch
        logger.info(f"PyTorch version: {torch.__version__}")
        # Try to create a simple tensor to verify numpy compatibility
        # (the .numpy() call itself is the check — it raises on ABI mismatch)
        test_tensor = torch.tensor([1.0])
        test_tensor.numpy()
        logger.debug("PyTorch-NumPy compatibility check passed")
    except ImportError:
        logger.debug("PyTorch not available for compatibility check")
    except Exception as e:
        logger.warning(f"PyTorch-NumPy compatibility issue detected: {e}")

    # Check other C-extension packages that might cause ABI issues
    try:
        import numba
        logger.info(f"Numba version: {numba.__version__}")
    except ImportError:
        logger.debug("Numba not available")
    except Exception as e:
        logger.warning(f"Numba compatibility issue: {e}")

    try:
        import pyworld
        logger.info(f"PyWorld version: {pyworld.__version__}")
    except ImportError:
        logger.debug("PyWorld not available")
    except Exception as e:
        logger.warning(f"PyWorld compatibility issue: {e}")

    try:
        import style_bert_vits2
        if hasattr(style_bert_vits2, '__version__'):
            logger.info(f"Style-Bert-VITS2 version: {style_bert_vits2.__version__}")
        else:
            logger.info("Style-Bert-VITS2 is installed (version info not available)")
    except ImportError:
        logger.error("Style-Bert-VITS2 not installed!")

    # Check GPU availability for TTS. CUDA無しはエラーではない: キャラ有効化時に
    # character_manager が device="cpu" を選ぶ正規経路(Macは常にこちら)。
    # CPU_FALLBACK_ENABLED=False はGPU OOM時にCPUへ落とさない規定であって
    # CPU起動を妨げない。
    has_gpu, available_memory = check_gpu_memory()
    if has_gpu:
        logger.info(f"GPU available with {available_memory:.2f}MB free memory")
    else:
        logger.info("No CUDA GPU with sufficient memory detected; TTS will run on CPU.")


def _ensure_sbv2() -> None:
    """Style-Bert-VITS2(とそのMLスタック)を初回TTS利用時に import する(冪等)。"""
    global TTSModel, bert_models, Languages
    if TTSModel is not None:
        return
    with _sbv2_import_lock:
        if TTSModel is not None:
            return
        # 非ASCIIインストールパス対策: MeCab 辞書を ASCII 複製へ向ける(ASCII なら no-op)
        ascii_data_mirror.prepare_openjtalk_dict()
        from style_bert_vits2.nlp import bert_models as _bert_models
        from style_bert_vits2.constants import Languages as _Languages
        from style_bert_vits2.tts_model import TTSModel as _TTSModel
        bert_models = _bert_models
        Languages = _Languages
        SBV2_LANGUAGES.update({
            "ja": _Languages.JP,
        })
        _log_tts_stack_diagnostics()
        # 最後に代入(完了フラグ兼用): ここまで済むまで並行呼び出しを
        # _sbv2_import_lock 待ちに留める
        TTSModel = _TTSModel


def init_audio_output() -> None:
    """
    Initializes the audio output environment.
    Verifies audio device availability and sets up logging.

    :raises RuntimeError: If no audio output devices are available or if default device is not working
    """
    logger.info("Audio output module initializing...")
    
    # Check numpy version for compatibility
    try:
        import numpy as np
        logger.info(f"NumPy version: {np.__version__}")
        
        # Check for numpy 2.x which is incompatible with pre-compiled style-bert-vits2
        if np.__version__.startswith('2.'):
            logger.error("NumPy 2.x detected! Style-Bert-VITS2 requires NumPy 1.x for ABI compatibility.")
            logger.error("Please downgrade NumPy: pip install 'numpy<2.0' --force-reinstall")
            raise RuntimeError("NumPy 2.x is not compatible with Style-Bert-VITS2. Please use NumPy 1.x")

        # torch/numba/pyworld/Style-Bert-VITS2 のバージョン・ABI診断は
        # _log_tts_stack_diagnostics() へ移設(起動経路で重量importをしない
        # ため。初回TTS利用=通常は開店前仕込み時に1回実行される)

    except ImportError:
        logger.error("NumPy not installed!")
        raise

    # Check audio device availability - Section 3.4 requires exception for unavailable audio devices
    try:
        devices = sd.query_devices()
        output_devices = [d for d in devices if d['max_output_channels'] > 0]

        if not output_devices:
            error_msg = "No audio output devices available"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        default_output = sd.query_devices(kind='output')
        logger.info(f"Default audio output device: {default_output['name']}")
        logger.info(f"Available audio devices: {len(output_devices)}")

        # Verify default device is actually working
        try:
            sd.check_output_settings(device=None, channels=1, samplerate=44100)
        except sd.PortAudioError as e:
            error_msg = f"Default audio device is not working properly: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    except Exception as e:
        error_msg = f"Audio device initialization failed: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)  # Per Section 3.4, must raise exceptions for audio device issues

    # GPU 可用性ログも _log_tts_stack_diagnostics() へ移設(check_gpu_memory が
    # torch を import するため。configure_tts_model 時の GPU メモリ検証は従来通り)

    logger.info("Audio output module initialized successfully.")


def check_gpu_memory() -> Tuple[bool, float]:
    """
    Checks available GPU memory and returns whether there's enough for a typical model.
    
    :return: Tuple of (has_enough_memory: bool, available_memory_mb: float)
    """
    try:
        import torch
        if not torch.cuda.is_available():
            logger.warning("CUDA not available")
            return False, 0
            
        # Get available memory
        available = torch.cuda.get_device_properties(0).total_memory
        reserved = torch.cuda.memory_reserved(0)
        allocated = torch.cuda.memory_allocated(0)
        available_memory = (available - reserved - allocated) / (1024 ** 2)  # MB
        
        # Check against minimum required memory
        has_enough = available_memory > MIN_GPU_MEMORY_MB
        return has_enough, available_memory
    except Exception as e:
        logger.error(f"Error checking GPU memory: {e}")
        return False, 0


def unload_tts_model() -> None:
    """
    Unloads the current TTS model and frees GPU memory.
    Should be called before loading a new model or when shutting down.
    """
    global current_tts_model
    
    with _tts_model_lock:
        if current_tts_model:
            try:
                logger.info("Unloading TTS model to free resources")
                # Delete model to free memory
                current_tts_model = None
                
                # Force garbage collection to free CUDA memory
                try:
                    import torch
                    gc.collect()
                    torch.cuda.empty_cache()
                except ImportError:
                    # If torch is not available, just do garbage collection
                    gc.collect()
                    
                logger.info("TTS model unloaded successfully")
            except Exception as e:
                logger.error(f"Error unloading TTS model: {e}")
    # Kokoro state lives outside the SBV2 globals: free it on every provider
    # switch too (no-op when it was never loaded — does not import kokoro).
    kokoro_engine.unload()


def _set_provider_locked(provider: str, voice_id: Optional[str]) -> None:
    """Update the provider globals. Caller MUST hold _tts_model_lock."""
    global current_tts_provider, current_elevenlabs_voice_id
    current_tts_provider = provider
    current_elevenlabs_voice_id = voice_id


def get_current_tts_provider() -> str:
    """The active TTS provider: "sbv2", "kokoro" or "elevenlabs" (for status displays)."""
    with _tts_model_lock:
        return current_tts_provider


def _unload_bert() -> None:
    """Free the BERT models/tokenizers that SBV2 keeps in library globals.

    unload_tts_model() alone leaves BERT weights resident (they live in
    style_bert_vits2.nlp.bert_models module state, not on the TTSModel) —
    for an ElevenLabs character the whole point is freeing that VRAM.
    Resetting current_bert_language makes the next SBV2 activation reload
    BERT naturally.
    """
    global current_bert_language
    if bert_models is None:
        # SBV2 未import = BERT は何もロードされていない(解放不要。ここで
        # import を誘発しない — ElevenLabs 専用利用では最後まで import しない)
        return
    try:
        bert_models.unload_all_models()
        bert_models.unload_all_tokenizers()
    except Exception as e:
        logger.warning(f"Error unloading BERT models: {e}")
    with _tts_model_lock:
        current_bert_language = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Error during GPU cleanup after BERT unload: {e}")
    logger.info("BERT models unloaded")


def configure_elevenlabs_tts(voice_id: str) -> None:
    """Activate the ElevenLabs TTS route for the current character.

    Validates FIRST, then unloads SBV2+BERT (never break the loaded SBV2
    state for an invalid config), then flips the provider globals. No GPU
    checks and no language handling: the multilingual models auto-detect.

    :param voice_id: ElevenLabs voice id from the character's tts_model_config.
    :raises ValueError: If voice_id is empty (caught by the activation's
        non-critical TTS try/except).
    """
    if not voice_id or not isinstance(voice_id, str):
        raise ValueError("ElevenLabs voice_id is empty - check the character's TTS setting.")

    # Free all local TTS VRAM: the SBV2 model AND its BERT companions.
    unload_tts_model()
    _unload_bert()

    with _tts_model_lock:
        _set_provider_locked("elevenlabs", voice_id)
    logger.info(f"ElevenLabs TTS configured (voice_id: {voice_id}); SBV2/BERT unloaded")


def configure_kokoro_tts(voice_name: str) -> None:
    """Activate the Kokoro (English local) TTS route for the current character.

    Same order as ElevenLabs: validate FIRST (never break the loaded SBV2
    state for a broken config), then free SBV2+BERT+old Kokoro state, then
    load the voice and flip the provider. English only by design — ja is
    served by SBV2, so no language handling here (the voice prefix encodes
    the accent: af_/am_=US, bf_/bm_=GB).

    :param voice_name: Kokoro voice name from the character's tts_model_config
        (file stem under kokoro/voices/ at the repo root).
    :raises ValueError/FileNotFoundError: On empty voice or missing assets
        (caught by the activation's non-critical TTS try/except).
    :raises RuntimeError: If loading the model fails.
    """
    kokoro_engine.validate_assets(voice_name)

    # Free all other local TTS memory: SBV2 model, its BERT companions, and
    # any previously loaded Kokoro state (unload_tts_model covers all three).
    unload_tts_model()
    _unload_bert()

    # Same device policy as SBV2 (82M runs fine on CPU; use the GPU when
    # present). Unlike SBV2 there is no hard GPU requirement.
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    kokoro_engine.load(voice_name, device)

    with _tts_model_lock:
        _set_provider_locked("kokoro", None)
    logger.info(f"Kokoro TTS configured (voice: {voice_name}); SBV2/BERT unloaded")


def _discover_model_file(path: str, file_pattern: str, file_description: str, base_path: str = DEFAULT_TTS_BASE_PATH) -> str:
    """
    Intelligently discover model files. If the exact path exists, use it.
    If not, but the directory exists, look for files matching the pattern.
    
    :param path: The provided file path (may not exist)
    :param file_pattern: Pattern to match files (e.g., "*.safetensors", "config.json", "*.npy")
    :param file_description: Description for error messages (e.g., "model", "config", "style vectors")
    :param base_path: Base directory for TTS models
    :return: The resolved file path
    :raises FileNotFoundError: If no suitable file is found
    """
    # Handle None or empty path
    if not path:
        raise FileNotFoundError(f"No path provided for {file_description}")
    
    # Handle relative paths
    if not os.path.isabs(path):
        full_path = os.path.join(base_path, path)
    else:
        full_path = path
    
    # If exact file exists, use it (backward compatibility)
    if os.path.isfile(full_path):
        logger.debug(f"Using specified {file_description} file: {full_path}")
        return full_path
    
    # Try to discover in the parent directory
    dir_path = os.path.dirname(full_path)
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"{file_description} directory not found: {dir_path}")
    
    # Search for matching files
    matches = sorted(glob.glob(os.path.join(dir_path, file_pattern)))
    
    if not matches:
        raise FileNotFoundError(f"No {file_pattern} files found in {dir_path}")
    
    # For .safetensors, prefer files with "model" in name
    if file_pattern == "*.safetensors" and len(matches) > 1:
        model_matches = [m for m in matches if "model" in os.path.basename(m).lower()]
        if model_matches:
            matches = model_matches
    
    selected = matches[0]
    logger.info(f"Auto-discovered {file_description}: {os.path.basename(selected)}")
    
    if len(matches) > 1:
        logger.debug(f"Multiple files found, selected: {os.path.basename(selected)}")
    
    return selected


def _resolve_bert_model(bert_model: str, lang_key: str, is_default: bool) -> str:
    """BERT モデル指定を、可能ならネットワーク不要のローカル実体パスへ解決する。

    transformers はハブID文字列を渡すと、キャッシュ完備でも毎回 huggingface.co へ
    etag 照合に行き、ネットワーク不調時はファイルごとにタイムアウトまでハングする
    (2026-07-18 実測: キャラ切替が70〜81秒)。ローカルパスを渡せば一切照会しない。

    解決順序:
      ① bert/<lang>/ (repo直下) — デフォルトモデル使用時のみ有効な手動上書き手段
      ② HFキャッシュの snapshot パス — local_files_only=True はディスク照会のみ
      ③ ハブIDのまま返す — キャッシュ未完備(真の初回)のみオンライン取得=従来挙動
    """
    if is_default:
        local_dir = os.path.join(DEFAULT_BERT_BASE_PATH, lang_key)
        if os.path.isfile(os.path.join(local_dir, "config.json")):
            logger.info(f"BERT resolved from local folder: {local_dir}")
            return local_dir
    try:
        from huggingface_hub import snapshot_download
        snapshot_path = snapshot_download(bert_model, local_files_only=True)
        logger.info(f"BERT resolved from local HF cache (offline): {snapshot_path}")
        return snapshot_path
    except Exception:
        # キャッシュ未完備や huggingface_hub の版差はここに落ちる。解決失敗で
        # TTSロードを壊さず、従来挙動(ハブIDのままオンライン取得)へ劣化する。
        logger.info(f"BERT not cached locally; will fetch from HF hub: {bert_model}")
        return bert_model


def configure_tts_model(
    model_path: str,
    config_path: str,
    style_vectors_path: str,
    device: str = "cuda",
    huggingface_model_name: Optional[str] = None,
    base_path: str = DEFAULT_TTS_BASE_PATH
) -> None:
    """
    Loads (or switches to) a specific Style-Bert-VITS2 TTS model
    and the Japanese BERT model it requires. SBV2 is the ja-only route
    (2026-07-26 ruling) - English characters use configure_kokoro_tts().

    :param model_path: Path to the TTS model .safetensors or .pth file.
    :param config_path: Path to the TTS config.json file.
    :param style_vectors_path: Path to the style_vectors.npy file.
    :param device: "cuda" or "cpu". Defaults to "cuda".
    :param huggingface_model_name: Optional HuggingFace name overriding the
                                  default Japanese BERT.
    :param base_path: Base directory for TTS models. Defaults to "sbv2_models/".
    :raises RuntimeError: If loading fails (invalid paths, GPU errors, etc.).
    """
    global current_tts_model, current_bert_language

    # 遅延importの実体化(初回のみ数秒。通常は起動時の開店前仕込みが先に
    # 済ませている。以降の SBV2_LANGUAGES / bert_models / TTSModel 参照の前提)
    _ensure_sbv2()

    # First unload any existing model to free resources
    unload_tts_model()
    
    # Use auto-discovery for all three files
    try:
        # Support both .safetensors and .pth files for models
        try:
            model_path = _discover_model_file(model_path, "*.safetensors", "model", base_path)
        except FileNotFoundError:
            # Fallback to .pth files if .safetensors not found
            model_path = _discover_model_file(model_path, "*.pth", "model", base_path)
        config_path = _discover_model_file(config_path, "config.json", "config", base_path)
        style_vectors_path = _discover_model_file(style_vectors_path, "*.npy", "style vectors", base_path)
    except FileNotFoundError as e:
        logger.error(f"TTS file discovery failed: {e}")
        raise

    # Check GPU memory if using CUDA (Section 3.2: GPU OOM - must raise error, no CPU fallback)
    if device.lower() == "cuda":
        has_gpu, available_memory = check_gpu_memory()
        if not has_gpu:
            error_msg = f"Insufficient GPU memory ({available_memory:.2f}MB). GPU required for TTS per requirements."
            logger.error(error_msg)
            raise RuntimeError(error_msg)
    
    # 1. Load the Japanese BERT if not already resident (SBV2 = ja-only)
    lang_key = "ja"

    bert_load_success = False
    try:
        if current_bert_language != lang_key:
            logger.info(f"Loading BERT model for TTS language '{lang_key}'.")
            # Use provided model name or default from configuration
            bert_model = huggingface_model_name or DEFAULT_BERT_MODELS[lang_key]
            bert_model = _resolve_bert_model(
                bert_model, lang_key, is_default=huggingface_model_name is None
            )
            logger.debug(f"BERT model name: {bert_model}")
            try:
                bert_models.load_model(SBV2_LANGUAGES[lang_key], bert_model)
                bert_models.load_tokenizer(SBV2_LANGUAGES[lang_key], bert_model)
                bert_load_success = True
            except TypeError as te:
                if "NoneType" in str(te):
                    logger.warning(f"BERT model loading encountered NoneType error (may be a library issue): {te}")
                    logger.warning("Continuing with TTS model loading despite BERT error...")
                else:
                    raise
            except Exception as bert_error:
                logger.error(f"Error loading BERT model/tokenizer: {bert_error}")
                logger.error(f"BERT model type: {type(bert_model)}, value: {bert_model}")
                raise

            if bert_load_success:
                with _tts_model_lock:
                    current_bert_language = lang_key


    except (TypeError, OSError) as te:
        error_str = str(te)
        if "NoneType" in error_str or "stat: path should be string" in error_str:
            logger.warning(f"BERT model loading encountered known issue: {te}")
            logger.warning("This appears to be a bug in the Style-Bert-VITS2 library.")
            logger.warning("The BERT model may have loaded successfully despite the error.")
            logger.warning("Continuing with TTS model loading...")
            # Don't raise - allow TTS model loading to continue
        else:
            logger.error(f"Failed to load BERT model for language '{lang_key}': {te}")
            # For non-critical BERT errors, log but continue
            logger.warning("BERT loading failed but continuing with TTS model loading...")
    except Exception as e:
        logger.error(f"Failed to load BERT model for language '{lang_key}': {e}")
        logger.warning(f"BERT loading failed with: {type(e).__name__}: {e}")
        logger.warning("Continuing with TTS model loading despite BERT error...")
        # Don't raise - allow TTS model loading to continue
    
    # 2. Load the Style-Bert-VITS2 model
    try:
        logger.info(f"Loading TTS model from: {model_path}")
        
        # Pre-check for numpy compatibility
        try:
            import numpy as np
            logger.debug(f"Loading TTS with NumPy {np.__version__}")
        except ImportError:
            logger.error("NumPy not available for TTS model loading")
            raise
        
        tts_model = TTSModel(
            model_path=model_path,
            config_path=config_path,
            style_vec_path=style_vectors_path,
            device=device
        )
        # If successful, set as current TTS model
        with _tts_model_lock:
            current_tts_model = tts_model
            _set_provider_locked("sbv2", None)
        logger.info(f"TTS model loaded successfully (inference language: {lang_key}).")
        
        # Log available styles (TTSModel has no get_available_styles method;
        # the module-level function reads style2id — M27)
        logger.info(f"Available styles: {get_available_styles()}")
    except Exception as e:
        logger.error(f"Failed to load TTS model: {e}")
        raise


def get_available_styles() -> List[str]:
    """
    Returns a list of available style names for the currently loaded model.

    :return: List of style names or empty list if model not loaded
    """
    with _tts_model_lock:
        if not current_tts_model:
            logger.warning("No TTS model loaded, cannot get available styles")
            return []

        # Style-Bert-VITS2 の TTSModel は style2id: dict[name->id] を持つ
        # (get_available_styles メソッドは存在しない)。実モデルのスタイル名を返す。
        style2id = getattr(current_tts_model, 'style2id', None)
        if isinstance(style2id, dict) and style2id:
            return list(style2id.keys())

        # Fallback (model without style2id): 英語デフォルト
        logger.warning("TTS model has no style2id; falling back to default styles")
        return ["Neutral", "Happy", "Sad", "Angry"]



def text_to_speech(text: str, style: str = "Neutral", speaker_id: int = 0,
                   noise_scale: float = 0.4, noise_scale_w: float = 0.7, 
                   length_scale: float = 1.0, sdp_ratio: float = 0.1) -> Tuple['np.ndarray', int]:
    """
    Converts the provided text into an audio waveform using the currently
    loaded TTS model.

    :param text: The input text to be spoken.
    :param style: The style label to use (default "Neutral").
    :param speaker_id: If the model supports multiple speakers, specify the ID here.
    :param noise_scale: Randomness in generation (NOT USED - kept for future compatibility)
    :param noise_scale_w: Pronunciation variation (NOT USED - kept for future compatibility)
    :param length_scale: Speech speed (NOT USED - kept for future compatibility)
    :param sdp_ratio: Tone naturalness (NOT USED - kept for future compatibility)
    :return: Tuple of (audio_data, sample_rate)
    :raises ValueError: If text is None, empty, or invalid
    :raises RuntimeError: If TTS model is not configured or inference fails
    """
    # Handle None input explicitly
    if text is None:
        error_msg = "Text input cannot be None"
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    # Type conversion if needed
    if not isinstance(text, str):
        original_type = type(text).__name__
        try:
            text = str(text)
            logger.warning(f"Input text was not a string. Converted from {original_type} to string.")
        except Exception as e:
            error_msg = f"Failed to convert input to string: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    # Validate input text
    if not text or not text.strip():
        error_msg = "Empty text provided for TTS"
        logger.warning(error_msg)
        raise ValueError(error_msg)

    # Check text length to prevent memory exhaustion
    MAX_TTS_TEXT_LENGTH = 5000  # ~2-3 minutes of speech
    if len(text) > MAX_TTS_TEXT_LENGTH:
        logger.warning(f"Text too long for TTS ({len(text)} chars), truncating to {MAX_TTS_TEXT_LENGTH}")
        text = text[:MAX_TTS_TEXT_LENGTH] + "..."

    # Provider dispatch: snapshot under the lock, call the API outside it
    # (a long HTTP round-trip must not block character switches).
    with _tts_model_lock:
        provider_snapshot = current_tts_provider
        voice_id_snapshot = current_elevenlabs_voice_id

    if provider_snapshot == "elevenlabs":
        return _text_to_speech_elevenlabs(text, voice_id_snapshot, style)

    if provider_snapshot == "kokoro":
        # style/speaker_id are SBV2 concepts; Kokoro voices carry their own
        # delivery (and the app does not use styles anywhere).
        return kokoro_engine.synthesize(text)

    # SBV2経路: 遅延importの実体化(モデル設定済みなら configure 時に済んで
    # いるため実質 no-op。後段の Languages / SBV2_LANGUAGES 参照の前提)
    _ensure_sbv2()

    with _tts_model_lock:
        if not current_tts_model:
            error_msg = "No TTS model configured. Please call configure_tts_model() first."
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Keep a reference to the model while holding the lock
        tts_model = current_tts_model
        # SBV2 は ja 専用: 推論言語は常に JP(非 JP-Extra モデルでも日本語話者
        # として動く。英語は Kokoro プロバイダの担当)。
        infer_language = Languages.JP

    # Style validation (Section 3.3: Invalid Style - fall back to "Neutral" and log warning)
    available_styles = get_available_styles()
    original_style = style

    if available_styles and style not in available_styles:
        logger.warning(f"Style '{style}' not found in available styles: {available_styles}. Falling back to 'Neutral'.")
        style = "Neutral"  # Fallback style

        if available_styles and style not in available_styles:
            error_msg = f"Neither requested style '{original_style}' nor fallback style '{style}' are available."
            logger.error(error_msg)
            raise ValueError(error_msg)

    # Attempt the inference with the validated style
    audio_data = None
    sr = 0
    
    try:
        try:
            # First attempt with requested/validated style
            logger.debug(f"Attempting TTS with style: {style}")
            sr, audio_data = tts_model.infer(
                text=text,
                language=infer_language,
                speaker_id=speaker_id,
                style=style
            )
        except RuntimeError as e:
            # Check for GPU OOM (Section 3.2: GPU OOM - must raise error, no CPU fallback)
            if "out of memory" in str(e).lower():
                error_msg = "GPU out of memory during TTS inference. Cannot proceed per requirements."
                logger.error(error_msg)
                raise RuntimeError(error_msg) from e
            else:
                # If it's not an OOM error but might be a style error, try neutral (Section 3.3)
                if style == original_style and style != "Neutral":
                    style_error = f"Failed TTS inference with style '{style}': {e}"
                    logger.warning(style_error)
                    logger.info("Falling back to 'Neutral' style.")

                    style = "Neutral"

                    try:
                        # Retry with "Neutral"
                        sr, audio_data = tts_model.infer(
                            text=text,
                            language=infer_language,
                            speaker_id=speaker_id,
                            style="Neutral"
                        )
                    except Exception as neutral_error:
                        error_msg = f"TTS inference failed with fallback style 'Neutral': {neutral_error}"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg) from neutral_error
                else:
                    # Not a style error or already using fallback style
                    error_msg = f"TTS inference failed: {e}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg) from e

    except Exception as e:
        error_str = str(e)
        
        # Check for numpy ABI compatibility error
        if "numpy.dtype size changed" in error_str or "binary incompatibility" in error_str:
            error_msg = f"NumPy ABI compatibility error detected: {e}"
            logger.error(error_msg)
            logger.error("This typically happens when packages were compiled against different NumPy versions.")
            logger.error("Solution: Try reinstalling numpy and related packages:")
            logger.error("  1. pip uninstall numpy torch torchaudio")
            logger.error("  2. pip install --no-cache-dir numpy torch torchaudio")
            logger.error("  3. Restart the application")
            
            # Provide additional diagnostic info
            try:
                import numpy as np
                logger.error(f"Current NumPy version: {np.__version__}")
            except Exception:
                pass

            raise RuntimeError("TTS failed due to NumPy compatibility issue. Please reinstall packages.") from e
        else:
            error_msg = f"TTS inference failed: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    # Validate output (detect empty or corrupted audio data)
    if audio_data is None or len(audio_data) == 0:
        error_msg = "TTS inference produced empty audio data"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    # Log successful generation
    logger.info(f"TTS generated {len(audio_data)} samples at {sr}Hz (style: {style}, original: {original_style})")
    # Note: Style-Bert-VITS2 doesn't support generation parameters in infer() method
    # Parameters are kept in function signature for potential future use
    
    # Add diagnostic logging for audio data
    try:
        import numpy as np
        if audio_data is not None and hasattr(audio_data, '__len__'):
            peak_level = np.max(np.abs(audio_data))
            nan_count = np.sum(np.isnan(audio_data))
            inf_count = np.sum(np.isinf(audio_data))
            
            # For int16 data, show the actual range
            if audio_data.dtype == np.int16:
                logger.info(f"[TTS DIAGNOSTIC] Peak level (int16): {peak_level:.0f} / 32767, NaN count: {nan_count}, Inf count: {inf_count}")
                normalized_peak = peak_level / 32767.0
                logger.info(f"[TTS DIAGNOSTIC] Normalized peak would be: {normalized_peak:.4f}")
            else:
                logger.info(f"[TTS DIAGNOSTIC] Peak level: {peak_level:.4f}, NaN count: {nan_count}, Inf count: {inf_count}")
                
            logger.info(f"[TTS DIAGNOSTIC] Audio data type: {audio_data.dtype}, shape: {audio_data.shape}")
            
            # Safely convert samples to list
            try:
                first_samples = audio_data[:10].tolist() if len(audio_data) >= 10 else audio_data.tolist()
                logger.info(f"[TTS DIAGNOSTIC] First 10 samples: {first_samples}")
            except Exception as e:
                logger.debug(f"[TTS DIAGNOSTIC] Could not log samples: {e}")
            
            if nan_count > 0 or inf_count > 0:
                logger.warning(f"[TTS WARNING] Audio contains {nan_count} NaN and {inf_count} Inf values!")
            
            # Check peak level based on data type
            if audio_data.dtype in [np.int16, np.int32]:
                # For integer types, don't warn about exceeding 1.0
                pass
            elif peak_level > 1.0:
                logger.warning(f"[TTS WARNING] Audio peak level exceeds 1.0: {peak_level}")
        else:
            logger.warning("[TTS WARNING] Audio data is None or has no length attribute")
    except Exception as diag_error:
        logger.error(f"[TTS DIAGNOSTIC ERROR] Failed to analyze audio data: {diag_error}")
    
    return audio_data, sr


def _text_to_speech_elevenlabs(text: str, voice_id: Optional[str],
                               style: str) -> Tuple['np.ndarray', int]:
    """ElevenLabs route for text_to_speech (same return contract).

    style/speaker_id are SBV2 concepts: ElevenLabs voices carry their own
    delivery (console-side voice settings), so the argument is ignored.

    :raises RuntimeError: On missing key/voice or API failure (callers treat
        TTS failures as non-critical, same as SBV2 inference errors).
    """
    from backend.shared.api_settings import get_elevenlabs_api_key, get_elevenlabs_model_id

    api_key = get_elevenlabs_api_key()
    if not api_key:
        raise RuntimeError("ElevenLabs API key is not set. Configure it in the API Setting tab.")
    if not voice_id:
        raise RuntimeError("ElevenLabs voice is not configured for this character.")

    model_id = get_elevenlabs_model_id()
    if style not in ("Neutral", None):
        logger.debug(f"ElevenLabs TTS ignores SBV2 style '{style}'")

    from . import elevenlabs_client
    return elevenlabs_client.synthesize(text, voice_id, model_id, api_key)


def cleanup() -> None:
    """
    Clean up audio output resources including TTS model and GPU memory.
    Should be called during application shutdown to ensure proper resource release.
    """
    global current_tts_model, current_bert_language

    logger.info("Cleaning up audio output resources...")

    # Unload TTS model (includes gc.collect() and torch.cuda.empty_cache())
    unload_tts_model()

    # Reset BERT state (+ provider back to the local default)
    with _tts_model_lock:
        current_bert_language = None
        _set_provider_locked("sbv2", None)

    logger.info("Audio output cleanup complete")


def calculate_lipsync_frames(audio_data: 'np.ndarray', sample_rate: int, fps: int = 60) -> List[Dict[str, Any]]:
    """
    Calculate lipsync frames from audio data for MotionPNGTuber integration.
    Uses the same algorithm as audio-worklet.js (700Hz lowpass filter).

    :param audio_data: NumPy array containing the audio waveform
    :param sample_rate: Sample rate of the audio
    :param fps: Frames per second for lipsync data (default 60)
    :return: List of dictionaries with keys: t (time_ms), rms, high, low
    """
    import math

    if audio_data is None or len(audio_data) == 0:
        logger.warning("Empty audio data provided to calculate_lipsync_frames")
        return []

    # Convert to float if necessary
    if audio_data.dtype == np.int16:
        audio_float = audio_data.astype(np.float32) / 32767.0
    elif audio_data.dtype == np.int32:
        audio_float = audio_data.astype(np.float32) / 2147483648.0
    elif audio_data.dtype in [np.float32, np.float64]:
        audio_float = audio_data.astype(np.float32)
    else:
        audio_float = audio_data.astype(np.float32)

    # Samples per frame
    samples_per_frame = max(1, sample_rate // fps)
    frames = []

    # 700Hz lowpass filter coefficient (same as audio-worklet.js)
    alpha = 1 - math.exp(-2 * math.pi * 700 / sample_rate)

    # One-pole lowpass low[n] = low[n-1] + alpha*(x[n] - low[n-1]) applied to
    # the whole waveform at once via lfilter (C implementation). The previous
    # per-sample Python loop cost ~44k iterations per second of audio and ran
    # before every spoken reply. Computed in float64 like the old loop
    # (float(sample) was a double); verified to produce identical values
    # after round(..., 6).
    from scipy.signal import lfilter
    x = audio_float.astype(np.float64)
    low = lfilter([alpha], [1.0, -(1.0 - alpha)], x)
    high = x - low

    n_samples = len(x)
    for frame_idx in range(0, n_samples, samples_per_frame):
        end = min(frame_idx + samples_per_frame, n_samples)
        n = end - frame_idx
        chunk = x[frame_idx:end]
        lo = low[frame_idx:end]
        hi = high[frame_idx:end]

        rms = math.sqrt(float(np.dot(chunk, chunk)) / n)
        low_avg = float(np.dot(lo, lo)) / n
        high_avg = float(np.dot(hi, hi)) / n

        time_ms = int(frame_idx / sample_rate * 1000)
        frames.append({
            "t": time_ms,
            "rms": round(rms, 6),
            "high": round(high_avg, 6),
            "low": round(low_avg, 6)
        })

    logger.debug(f"Generated {len(frames)} lipsync frames at {fps}fps")
    return frames


def audio_to_aac_mp4_base64(audio_data: 'np.ndarray', sample_rate: int,
                            bit_rate: int = 64000) -> str:
    """
    Convert NumPy audio data to AAC-in-MP4 format and encode as Base64.

    Uses PyAV (libavcodec) for AAC encoding. Output is a self-contained
    MP4 container suitable for browser ``decodeAudioData`` / ``<audio>``
    playback. Reduces payload size ~5-10x vs WAV for the same duration.

    :param audio_data: NumPy array containing the audio waveform (mono)
    :param sample_rate: Sample rate of the audio
    :param bit_rate: Target bit rate in bps (default 64000 = 64 kbps)
    :return: Base64-encoded MP4 file as string
    """
    import io
    import base64
    import av

    if audio_data is None or len(audio_data) == 0:
        logger.warning("Empty audio data provided to audio_to_aac_mp4_base64")
        return ""

    if audio_data.dtype in [np.float32, np.float64]:
        audio_clipped = np.clip(audio_data, -1.0, 1.0)
        audio_int16 = (audio_clipped * 32767).astype(np.int16)
    elif audio_data.dtype == np.int16:
        audio_int16 = audio_data
    elif audio_data.dtype == np.int32:
        audio_int16 = (audio_data / 65536).astype(np.int16)
    else:
        audio_int16 = audio_data.astype(np.int16)

    buffer = io.BytesIO()
    container = av.open(buffer, mode='w', format='mp4')
    stream = container.add_stream('aac', rate=sample_rate)
    stream.bit_rate = bit_rate
    stream.layout = 'mono'

    frame = av.AudioFrame.from_ndarray(
        audio_int16.reshape(1, -1), format='s16', layout='mono'
    )
    frame.rate = sample_rate
    frame.pts = 0

    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):  # flush
        container.mux(packet)
    container.close()

    mp4_bytes = buffer.getvalue()
    base64_str = base64.b64encode(mp4_bytes).decode('ascii')

    logger.debug(
        f"Converted audio to AAC/MP4 Base64: "
        f"{len(audio_int16) * 2} PCM bytes -> {len(mp4_bytes)} MP4 bytes "
        f"-> {len(base64_str)} base64 chars "
        f"(ratio: {len(mp4_bytes) / max(1, len(audio_int16) * 2):.3f}x)"
    )
    return base64_str