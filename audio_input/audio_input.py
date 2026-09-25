"""
audio_input.py

Module: Audio Input for Artificial Girlfriend
----------------------------------------------
Responsible for:
  - Initializing microphone input with configurable parameters (default: 16 kHz, mono).
  - Providing manual start/stop recording functions.
  - Buffering audio data while recording.
  - Transcribing the buffered audio in chunks or as a whole after recording stops.
  - Configuring transcription language (e.g. "en", "ja") for faster-whisper.
  - Handling errors (no mic, STT model issues) and logging appropriately.

Requirements Summary (based on the Audio Input Requirements doc):
  - No streaming (push-to-talk only).
  - Manually triggered recording start/stop from the UI.
  - 16 kHz mono microphone capture; default OS device.
  - Uses faster-whisper for transcription offline.
  - Provide configure_stt(language) to set the transcription language.
  - Default max recording length of 600 seconds (auto-stop if exceeded).
  - Log events, warnings, and errors using Python's logging.
  - Raise or return errors to the caller (UI/orchestrator) where appropriate.
"""

import logging
import sys
import time
import threading
import os
import platform
import psutil
from typing import List, Dict, Optional, Union, Any
from threading import Event

try:
    import numpy as np
    import sounddevice as sd
except ImportError as e:
    raise ImportError(f"Required dependencies not available: {e}. Please install required packages.") from e

logger = logging.getLogger(__name__)

# faster_whisper は import だけで ctranslate2/torch/av(冷時~7秒)を引き込むため、
# モデルロード(既に遅延済)と同じタイミングまで import 自体も遅延する。None の
# ままなら未import。テストは本シンボルを monkeypatch で偽物に差し替える —
# non-None なら _ensure_whisper_import() は素通りするので偽物がそのまま使われる。
WhisperModel = None


def _ensure_whisper_import() -> None:
    """faster_whisper を初回モデルロード時に import する(冪等)。"""
    global WhisperModel
    if WhisperModel is None:
        try:
            from faster_whisper import WhisperModel as _WhisperModel
        except ImportError as e:
            raise ImportError(
                f"Required dependencies not available: {e}. Please install required packages."
            ) from e
        WhisperModel = _WhisperModel

# Optional: list of supported languages. If an unsupported language is set, fallback to "en".
SUPPORTED_LANGUAGES = ["en", "ja"]

# Valid faster-whisper model sizes. Single truth source shared with the UI
# dropdown (ui/pages.py). The set is fixed by the pinned faster-whisper
# version (requirements-windows.txt) — extend only when that pin is upgraded.
VALID_MODEL_SIZES = ["tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "turbo"]

# Windows の入力API優先度(小さいほど優先)。重複排除の代表選びと録音時の
# 候補試行順が同じ表を共有する(2箇所で食い違うと「一覧に見えるデバイス」と
# 「実際に開くデバイス」がズレる)。
WINDOWS_API_PRIORITY = {
    "Windows WASAPI": 1,
    "Windows DirectSound": 2,
    "MME": 3,
    "Windows WDM-KS": 4,
}

class AudioInputManager:
    """Class to manage audio input, recording, and transcription.
    
    Encapsulates all functionality related to audio capture and speech-to-text processing,
    eliminating global state issues and providing better resource management.
    """
    
    def __init__(self):
        """Initialize the AudioInputManager with default settings."""
        self.is_recording: bool = False
        self.audio_buffer: List[np.ndarray] = []
        self.stream: Optional[sd.InputStream] = None
        self.recording_start_time: Optional[float] = None
        self.language: str = "en"
        self.model: Optional["WhisperModel"] = None
        # ST-E: lazy model loading (trigger-and-wait). The model is no longer
        # loaded at process startup — Start Conversation warms it, and every
        # transcription entry point goes through ensure_model_loaded().
        self._model_lock = threading.Lock()
        self._model_loading: bool = False
        self._model_load_done = Event()
        # Size actually held by self.model right now ('next wanted' lives in
        # _model_params) — the two differ while a size switch is in flight.
        self._loaded_model_size: Optional[str] = None
        self._model_params: Dict[str, Any] = {
            "model_size": "turbo",
            "device": None,
            "compute_type": "float16",
        }
        self.max_recording_duration: int = 600  # Default 10 minutes
        self.recording_event = Event()
        # 直近の録音開始のフォールバック理由(last_recording_fallback 参照)
        self._recording_fallback: Optional[str] = None
        self._last_toggle_time: float = 0.0  # For rate limiting start/stop operations
        self._min_toggle_interval: float = 0.5  # Minimum seconds between toggle operations
        self._device_check_interval: int = 30  # Check device availability every 30 seconds
        self._last_device_check: float = 0.0
        self._last_quality_warning_time: float = 0.0  # Track last audio quality warning time
        self._quality_warning_interval: float = 5.0  # Log quality warnings at most every 5 seconds
        self._quality_issue_count: int = 0  # Count of consecutive quality issues
        self._resource_warning_threshold: Dict[str, float] = {
            "cpu": 90.0,  # CPU usage percentage
            "memory": 90.0  # Memory usage percentage
        }
        # Persistent psutil handle + primed counters for non-blocking CPU
        # sampling: cpu_percent(interval=None) returns the usage since the
        # previous call, and the first delta-call returns 0.0 — prime both
        # counters once here so later readings are meaningful.
        try:
            self._psutil_process: Optional[psutil.Process] = psutil.Process(os.getpid())
            psutil.cpu_percent(interval=None)
            self._psutil_process.cpu_percent(interval=None)
        except Exception:
            self._psutil_process = None
        self.audio_config = {
            "sample_rate": 16000,
            "channels": 1,
            "dtype": "float32"
        }
        self.transcription_config = {
            "beam_size": 5,
            "vad_filter": True,
            "max_chunk_size": 1000000,  # Max samples to process at once
            "hallucination_silence_threshold": 2,
        }

    def init_audio_input(
        self,
        model_size: str = "turbo",
        device: Optional[str] = None,
        compute_type: str = "float16"
    ) -> None:
        """
        Initialize the audio input module and load the faster-whisper model.

        Args:
            model_size: Which faster-whisper model to load (e.g. "small", "medium", "large-v2")
            device: The device to run faster-whisper on (e.g. "cuda", "cpu", or None for auto-detect)
            compute_type: Precision/quantization for the model (e.g. "float16", "int8_float16")

        Raises:
            RuntimeError: If the model fails to load
            ValueError: If invalid parameters are provided
        """
        # Validate parameters
        if model_size not in VALID_MODEL_SIZES:
            error_msg = f"Invalid model_size '{model_size}'. Must be one of: {', '.join(VALID_MODEL_SIZES)}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        valid_compute_types = ["default", "float16", "float32", "int8", "int8_float16"]
        if compute_type not in valid_compute_types:
            error_msg = f"Invalid compute_type '{compute_type}'. Must be one of: {', '.join(valid_compute_types)}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        if device is not None and device not in ["cuda", "cpu", "auto"]:
            logger.warning(f"Unusual device setting: '{device}'. Recommended values are 'cuda', 'cpu', or None for auto-detection.")

        # Check if any audio devices are available
        if not self.check_device_availability():
            logger.warning("No audio input devices detected during initialization. Microphone may not be available.")

        # ST-E: store parameters only — the model itself is loaded lazily
        # (start_model_load / ensure_model_loaded) so process startup does
        # not claim GPU VRAM.
        self._model_params = {
            "model_size": model_size,
            "device": device,
            "compute_type": compute_type,
        }
        logger.info(
            f"Audio input initialized (model '{model_size}' will be loaded on first use, "
            f"device='{device or 'auto'}', compute_type='{compute_type}')."
        )

    def is_model_ready(self) -> bool:
        """True if the faster-whisper model is loaded and usable."""
        return self.model is not None

    def is_model_loading(self) -> bool:
        """True while a model load/download worker is in flight."""
        return self._model_loading

    def start_model_load(self) -> None:
        """
        Kick off the model load in a background thread (non-blocking,
        idempotent). Used by Start Conversation to warm the model early.
        """
        with self._model_lock:
            if self.model is not None or self._model_loading:
                return
            self._model_loading = True
            self._model_load_done.clear()
        threading.Thread(
            target=self._load_model_worker,
            name="whisper-model-load",
            daemon=True,
        ).start()

    def ensure_model_loaded(self, timeout: Optional[float] = 300.0) -> bool:
        """
        Trigger-and-wait: start the load if nobody has, then wait for it.

        Every transcription entry point calls this, so audio recorded while
        the model is still loading is transcribed late but never dropped.
        Returns False on load failure or timeout (callers degrade); a later
        call triggers a fresh attempt (retry semantics).
        """
        if self.model is not None:
            return True
        self.start_model_load()
        self._model_load_done.wait(timeout)
        return self.model is not None

    def set_model_size(self, model_size: str) -> str:
        """
        Switch the local faster-whisper model size at runtime.

        Returns what will happen, so the caller can decide who reports status
        (single writer per flow — the caller answers when no worker runs,
        the worker's WS events otherwise):
          'invalid'        — unknown size, nothing changed
          'already_loaded' — requested model is the one loaded right now
          'reloading'      — a load worker runs; stt_model_status events
                             carry the status (download/load/ready)
          'queued'         — another size's load/download is in flight (it
                             cannot be cancelled); the swap happens after it
                             lands, via the worker-end convergence check
          'deferred'       — engine is the API route; params stored, the
                             model loads on the next local use
        """
        if model_size not in VALID_MODEL_SIZES:
            logger.warning(
                f"Invalid model_size '{model_size}'. Must be one of: {', '.join(VALID_MODEL_SIZES)}"
            )
            return 'invalid'

        self._model_params["model_size"] = model_size
        if self._stt_engine_config()[0] != "faster_whisper":
            return 'deferred'
        if self.model is not None and self._loaded_model_size == model_size:
            return 'already_loaded'
        with self._model_lock:
            loading_in_flight = self._model_loading
        if self.model is not None:
            self._release_model()
        # In-flight load of another size: start_model_load is a no-op here,
        # but that worker's convergence check reloads with the new params.
        self.start_model_load()
        return 'queued' if loading_in_flight else 'reloading'

    def _publish_model_status(self, state: str, message_key: str, **fmt) -> None:
        """Best-effort UI status push (Audio Setting status line via WS).

        publish_ui_update propagates transport exceptions by contract — a UI
        notification failure must never break the model load, so localization
        and delivery are both fully swallowed here.
        """
        try:
            from backend.shared.i18n import t
            from backend.shared.ui_events import publish_ui_update
            data = {"state": state, "message": t(message_key, **fmt)}
            # Mic indicator text rides along while the local engine is active:
            # busy states show 'preparing', terminal states restore 'ready'.
            # (Suffix built here again — audio_input must not import the
            # ui.conversation.recording helper: layer direction.)
            if self._stt_engine_config()[0] == "faster_whisper":
                busy = state in ("downloading", "loading")
                mic_key = 'micstat.preparing' if busy else 'micstat.ready'
                data["mic_text"] = t(mic_key) + " " + t('micstat.engine_local')
                # JS disables the record button while busy (server-side twin:
                # the record_speech guard). Absent = don't touch the button.
                data["record_disabled"] = busy
            publish_ui_update("stt_model_status", data=data)
            # 右上ステータス3行のWhisper行は実モデル状態を表示する
            # (ui/status_checker.py)ため、状態遷移(loading/ready/error)ごとに
            # update_statusで再描画をトリガーする。これが無いとロード完了の
            # 🟢が次の無関係なstatusイベントまで反映されない。
            publish_ui_update("update_status", reason=f"stt_model_{state}")
        except Exception as e:
            logger.debug(f"stt_model_status publish failed: {e}")

    def _is_model_cached(self, model_size: str) -> bool:
        """True if the model is already in the local HuggingFace cache.

        Distinguishes the 'downloading' status from plain 'loading'. Unknown
        (check itself failed) counts as cached — the generic loading text is
        never a lie, a false 'downloading' would be.
        """
        try:
            from faster_whisper.utils import download_model
            download_model(model_size, local_files_only=True)
            return True
        except Exception as e:
            if type(e).__name__ == "LocalEntryNotFoundError":
                return False
            logger.debug(f"Model cache check failed (assuming cached): {e}")
            return True

    def _cached_model_path(self, model_size: str) -> Optional[str]:
        """ローカルHFキャッシュ内のモデル実体パス(未キャッシュ/照会失敗はNone)。

        パスを WhisperModel に渡すとHFハブ照会が一切走らない(TTS側BERTの
        _resolve_bert_model と同型の対策。ハブID/サイズ名の文字列渡しだと
        キャッシュ完備でも毎回 etag 照合が走り、ネットワーク不調時は
        タイムアウトまでハングする)。None は従来挙動(名前渡し=オンライン
        取得)への安全な劣化。
        """
        try:
            from faster_whisper.utils import download_model
            return download_model(model_size, local_files_only=True)
        except Exception:
            return None

    def _converge_model_size(self) -> None:
        """Reload if the loaded model no longer matches the requested size.

        Runs at the very end of the load worker (after _model_loading is
        cleared, or start_model_load would no-op). Handles size switches that
        landed mid-load, including rapid successive switches: each worker
        re-checks the latest params, so the chain terminates on a match.
        """
        if self.model is None:
            return
        if self._stt_engine_config()[0] != "faster_whisper":
            return  # engine switched away mid-load; deferred unload owns the model
        wanted = self._model_params["model_size"]
        if self._loaded_model_size != wanted:
            logger.info(
                f"Model size changed during load ('{self._loaded_model_size}' -> '{wanted}') - reloading"
            )
            self._release_model()
            self.start_model_load()

    def _load_model_worker(self) -> None:
        """Background worker: resource check, device autodetect, load, self-test."""
        params = dict(self._model_params)
        model_size = params["model_size"]
        device = params["device"]
        compute_type = params["compute_type"]
        cached = self._is_model_cached(model_size)
        if cached:
            self._publish_model_status(
                "loading", 'hdl.stt_engine.local_model_loading', model=model_size)
        else:
            self._publish_model_status(
                "downloading", 'hdl.stt_engine.local_model_downloading', model=model_size)
        try:
            # Check system resources before loading model (which can be memory-intensive)
            resource_status = self._check_system_resources()
            if not resource_status["ok"]:
                logger.warning(f"System resources may be insufficient to load model: {resource_status['message']}")
                if resource_status["memory"] > 95:
                    logger.error(
                        f"Insufficient memory to load model. Memory usage: {resource_status['memory']:.1f}%"
                    )
                    self._publish_model_status(
                        "error", 'hdl.stt_engine.local_model_failed', model=model_size)
                    return

            # Auto-detect device if not specified
            if device is None:
                try:
                    import torch
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    logger.info(f"Auto-detected device: {device}")
                except ImportError:
                    device = "cpu"
                    logger.info("PyTorch not available, defaulting to CPU")

            # CPU適正化 (Mac 3-11): float16のままCPUに載せるとctranslate2が
            # float32へ暗黙変換(メモリ2倍・低速)。int8がCPUの定石(Apple
            # Silicon含む)。cuda経路(Windows既定)は不変。既定のfloat16の
            # ときだけ差し替え=明示指定(float32等)は尊重する。
            if device == "cpu" and compute_type == "float16":
                compute_type = "int8"
                logger.info("CPU device — compute_type float16 -> int8")

            # 遅延importの実体化(このロードスレッド内で初回のみ数秒かかる。
            # 通常は起動時の開店前仕込みが先に済ませている)
            _ensure_whisper_import()

            # Handle FR9: STT model load/transcription failed
            # キャッシュ完備ならローカルパスを渡してハブ照会を経路ごと回避
            # (未キャッシュ時は名前のまま=従来のオンライン取得)
            local_model_path = self._cached_model_path(model_size) if cached else None
            model = WhisperModel(
                local_model_path or model_size,
                device=device,
                compute_type=compute_type
            )
            logger.info(
                f"Faster-Whisper model '{model_size}' loaded on device='{device}', "
                f"compute_type='{compute_type}'."
            )

            # Verify model was loaded successfully with a simple test
            try:
                # Create a tiny silent audio sample (0.1s) to test transcription
                sample_rate = self.audio_config["sample_rate"]
                test_audio = np.zeros(int(sample_rate * 0.1), dtype=np.float32)
                model.transcribe(test_audio, language="en", task="transcribe")
                logger.info("Model test transcription successful")
            except Exception as test_error:
                logger.warning(f"Model loaded but test transcription failed: {test_error}")
                # Don't raise an exception here, just warn

            self.model = model
            self._loaded_model_size = model_size

            # Ready status: only when this result is still what the user wants
            # (params unchanged mid-load) and the engine is still local — the
            # convergence check / deferred unload own the stale cases and a
            # 'ready' for a model about to be dropped would be a lie.
            if (model_size == self._model_params["model_size"]
                    and self._stt_engine_config()[0] == "faster_whisper"):
                self._publish_model_status(
                    "ready", 'hdl.stt_engine.local_model_ready', model=model_size)

        except Exception as e:
            # Provide more specific error messages based on error type
            error_msg = f"Failed to initialize faster-whisper model: {e}"

            if "CUDA" in str(e) or "GPU" in str(e) or "nvidia" in str(e).lower():
                error_msg = f"GPU error loading model: {e}. Try using device='cpu' instead."
            elif "memory" in str(e).lower() or "allocation" in str(e).lower():
                error_msg = f"Memory error loading model: {e}. Try using a smaller model_size or device='cpu'."
            elif "not found" in str(e).lower() or "no such file" in str(e).lower():
                error_msg = f"Model file not found: {e}. Check if the model needs to be downloaded first."

            logger.error(error_msg)
            self._publish_model_status(
                "error", 'hdl.stt_engine.local_model_failed', model=model_size)
        finally:
            with self._model_lock:
                self._model_loading = False
            self._model_load_done.set()
        # After finally (loading flag cleared — start_model_load would no-op
        # otherwise): reload if the wanted size changed while we were loading.
        self._converge_model_size()

    def configure_stt(self, language: str) -> None:
        """
        Configure the language for speech-to-text (STT).
        If an unsupported language code is provided, fallback to "en" and log a warning.

        Args:
            language: Language code, e.g. "en", "ja"

        Raises:
            ValueError: If language parameter is empty or not a string
        """
        # Input validation
        if not language:
            error_msg = "Language code cannot be empty"
            logger.error(error_msg)
            raise ValueError(error_msg)

        if not isinstance(language, str):
            error_msg = f"Language code must be a string, got {type(language).__name__}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Handle EC4: Invalid STT Language
        if language not in SUPPORTED_LANGUAGES:
            logger.warning(
                f"Unsupported STT language '{language}'. Falling back to 'en'. "
                f"Supported languages: {', '.join(SUPPORTED_LANGUAGES)}"
            )
            self.language = "en"
        else:
            self.language = language

        logger.info(f"STT language set to '{self.language}'.")
    
    def set_max_recording_duration(self, seconds: int) -> None:
        """
        Set the maximum recording duration in seconds.
        
        Args:
            seconds: Maximum recording duration
        """
        if seconds <= 0:
            logger.warning(f"Invalid max duration: {seconds}. Using default 600 seconds.")
            self.max_recording_duration = 600
        else:
            self.max_recording_duration = seconds
            logger.info(f"Max recording duration set to {seconds} seconds.")
    
    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Dict[str, float],
        status: Optional[sd.CallbackFlags]
    ) -> None:
        """
        Internal callback function for sounddevice's InputStream.
        Buffers audio data while recording, and checks for max recording duration.

        Args:
            indata: Input audio data as numpy array
            frames: Number of frames
            time_info: Time info dictionary
            status: Status flags (may be None)
        """
        if not self.is_recording:
            return

        if status:
            # Add specific handling for common status flags
            if status.input_overflow:
                logger.error("Audio input overflow detected - data may be lost")
            if status.input_underflow:
                logger.warning("Audio input underflow detected - may cause gaps")
            if status.priming_output:
                logger.info("Audio system is priming output buffers")
            if status.output_underflow or status.output_overflow:
                logger.warning(f"Unexpected output status in input stream: {status}")
            logger.warning(f"Audio input status: {status}")

        # Validate audio quality (check for silence/distortion)
        if not self._validate_audio_quality(indata):
            self._quality_issue_count += 1
            current_time = time.time()
            # Only log warning if enough time has passed since last warning
            if current_time - self._last_quality_warning_time >= self._quality_warning_interval:
                logger.debug(f"Audio quality issues detected ({self._quality_issue_count} frames in last {self._quality_warning_interval}s)")
                self._last_quality_warning_time = current_time
                self._quality_issue_count = 0  # Reset counter after logging
        else:
            # Reset counter when quality is good
            self._quality_issue_count = 0

        # Append incoming frames to the buffer
        self.audio_buffer.append(indata.copy())

        # Check max recording duration
        elapsed_time = time.time() - self.recording_start_time
        if elapsed_time >= self.max_recording_duration:
            logger.info(f"Max recording duration reached ({elapsed_time:.2f} s). Auto-stopping.")
            # Schedule stop_recording to avoid calling from callback context
            threading.Thread(target=self.stop_recording).start()

    def get_available_devices(self) -> List[Dict[str, Any]]:
        """
        Get a list of available audio input devices with deduplication.
        
        On Windows, devices may appear multiple times through different APIs (MME, DirectSound, WASAPI).
        This method deduplicates devices and prefers the best API for each device.

        Returns:
            List of dictionaries with device information, sorted with physical devices first

        Raises:
            RuntimeError: If unable to query audio devices
        """
        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()
            
            # Build a list of all input devices with their host API info
            all_input_devices = []
            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    # Get host API name
                    host_api_idx = d["hostapi"]
                    host_api_name = host_apis[host_api_idx]["name"] if host_api_idx < len(host_apis) else "Unknown"
                    
                    device_info = {
                        "id": i,
                        "name": d["name"],
                        "channels": d["max_input_channels"],
                        "default": (i == sd.default.device[0]),
                        "host_api": host_api_name,
                        "host_api_index": host_api_idx,
                        "sample_rates": d.get("default_samplerate", 44100)
                    }
                    all_input_devices.append(device_info)
            
            # If no deduplication is needed (non-Windows), process devices anyway for categorization
            if platform.system() != "Windows":
                processed_devices = all_input_devices
            else:
                # Windows: Deduplicate devices
                processed_devices = self._deduplicate_windows_devices(all_input_devices)
            
            # Categorize and enhance device information
            categorized_devices = []
            for device in processed_devices:
                # Extract display name and check if virtual
                display_name = self._extract_display_name(device.get("display_name", device["name"]))
                device["display_name"] = display_name
                
                # Determine if device is virtual/system
                device["is_virtual"] = "(Virtual)" in display_name or "System Default" in display_name
                
                # Add device type for sorting
                if device["is_virtual"]:
                    device["device_type"] = "virtual"
                elif device["default"]:
                    device["device_type"] = "default"
                else:
                    device["device_type"] = "physical"
                
                categorized_devices.append(device)
            
            # Sort devices: physical devices first, then default, then virtual
            type_priority = {"default": 0, "physical": 1, "virtual": 2}
            categorized_devices.sort(key=lambda d: (type_priority.get(d["device_type"], 3), d["display_name"]))
            
            return categorized_devices
            
        except Exception as e:
            logger.error(f"Failed to query audio devices: {e}")
            raise RuntimeError(f"Failed to query audio devices: {e}") from e
    
    def _deduplicate_windows_devices(self, devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Deduplicate Windows audio devices by preferring the best API for each physical device.
        Uses fuzzy matching to handle truncated names and variations.
        
        Priority order (best to worst):
        1. Windows WASAPI (lowest latency, exclusive mode support)
        2. Windows DirectSound (good compatibility)
        3. MME (legacy, highest latency)
        4. Windows WDM-KS (kernel streaming, rarely used)
        
        Args:
            devices: List of all detected input devices
            
        Returns:
            List of deduplicated devices
        """
        # API priority mapping (lower number = higher priority) — single
        # truth source shared with the recording-time candidate order.
        api_priority = WINDOWS_API_PRIORITY

        # First pass: group devices by normalized names for better matching
        normalized_groups = {}
        
        for device in devices:
            # Get normalized name for grouping
            normalized = self._normalize_device_name(device["name"])
            
            # Skip generic Windows mapper devices
            if any(skip in device["name"].lower() for skip in ["sound mapper", "primary sound"]):
                continue
            
            # Debug logging for deduplication
            logger.debug(f"Device '{device['name']}' normalized to '{normalized}'")
            
            if normalized not in normalized_groups:
                normalized_groups[normalized] = []
            normalized_groups[normalized].append(device)
        
        # Second pass: merge groups that are likely the same device
        # This handles truncated names
        merged_groups = []
        processed = set()
        
        for norm_name, group in normalized_groups.items():
            if norm_name in processed:
                continue
                
            # Check if this group should be merged with any other group
            merged_group = list(group)
            processed.add(norm_name)
            
            for other_norm, other_group in normalized_groups.items():
                if other_norm in processed or other_norm == norm_name:
                    continue
                    
                # Check if one is a substring of the other (handles truncation)
                if norm_name in other_norm or other_norm in norm_name:
                    # Check if they're likely the same device
                    if self._are_likely_same_device(norm_name, other_norm):
                        merged_group.extend(other_group)
                        processed.add(other_norm)
            
            if merged_group:
                merged_groups.append(merged_group)
        
        # Now deduplicate within each group
        deduplicated_devices = []
        
        for group in merged_groups:
            if len(group) == 1:
                # Only one device, clean up its name and keep it
                device = group[0].copy()
                device["display_name"] = self._extract_display_name(device["name"])
                deduplicated_devices.append(device)
            else:
                # Multiple devices, choose the best API
                sorted_group = sorted(
                    group,
                    key=lambda d: api_priority.get(d["host_api"], 999)
                )
                
                # Use the highest priority device
                best_device = sorted_group[0].copy()

                # Extract clean display name
                best_device["display_name"] = self._extract_display_name(best_device["name"])
                best_device["original_name"] = best_device["name"]
                best_device["duplicate_count"] = len(group)

                # OSの既定フラグはMME変種に付くことが多く、採用される
                # WASAPI変種からは落ちる — グループ内のどれかが既定なら
                # 代表デバイスへ引き継ぐ(既定マーカー表示・並び順の正)。
                best_device["default"] = any(d.get("default") for d in group)
                
                deduplicated_devices.append(best_device)
                
                # Log deduplication info
                logger.info(
                    f"Deduplicated '{best_device['display_name']}': selected {best_device['host_api']} "
                    f"from {len(group)} available APIs"
                )
                
                # Log all devices in the group for debugging
                if len(group) > 1:
                    logger.debug(f"  Devices in group for '{best_device['display_name']}':")
                    for dev in group:
                        logger.debug(f"    - '{dev['name']}' ({dev['host_api']})")
        
        # Don't add back generic Windows devices - they're already handled by marking the actual default
        # This prevents duplicates like "Default" and "Windows Default (Sound Mapper)"
        
        # Final cleanup pass: check for any remaining duplicates based on display names
        # This catches cases where devices slipped through with similar names
        final_devices = []
        seen_display_names = {}
        
        for device in deduplicated_devices:
            display_name = device.get("display_name", device["name"])
            
            # Check if we've seen a very similar display name
            found_similar = False
            for seen_name, seen_device in seen_display_names.items():
                # Normalize both names for comparison
                norm1 = self._normalize_device_name(display_name)
                norm2 = self._normalize_device_name(seen_name)
                
                if norm1 == norm2 or self._are_likely_same_device(norm1, norm2):
                    # Found a duplicate - log it
                    logger.info(f"Final cleanup: '{display_name}' appears to be duplicate of '{seen_name}'")
                    found_similar = True
                    
                    # Keep the one with higher priority API
                    if api_priority.get(device["host_api"], 999) < api_priority.get(seen_device["host_api"], 999):
                        # Replace the previous device with this one
                        final_devices = [d for d in final_devices if d != seen_device]
                        final_devices.append(device)
                        seen_display_names[display_name] = device
                    break
            
            if not found_similar:
                final_devices.append(device)
                seen_display_names[display_name] = device
        
        return final_devices
    
    def _are_likely_same_device(self, name1: str, name2: str) -> bool:
        """
        Check if two normalized device names likely refer to the same device.
        
        Args:
            name1: First normalized name
            name2: Second normalized name
            
        Returns:
            bool: True if likely the same device
        """
        # If they're exactly the same, they're the same device
        if name1 == name2:
            return True
            
        # If one is a significant substring of the other
        if len(name1) > 5 and len(name2) > 5:
            shorter = min(name1, name2, key=len)
            longer = max(name1, name2, key=len)
            
            # Check if the shorter name is at least 70% of the longer name
            if len(shorter) / len(longer) > 0.7:
                return shorter in longer
        
        # Additional check: calculate string similarity using character overlap
        # This helps with names that are similar but not exact substrings
        if len(name1) > 5 and len(name2) > 5:
            # Simple character set overlap similarity
            set1 = set(name1)
            set2 = set(name2)
            intersection = len(set1 & set2)
            union = len(set1 | set2)
            
            if union > 0:
                similarity = intersection / union
                # If 80% of characters overlap, consider them the same
                if similarity > 0.8:
                    # Additional check: ensure the beginning matches
                    # This prevents false matches like "device1" and "device2"
                    min_len = min(len(name1), len(name2))
                    if min_len >= 5 and name1[:5] == name2[:5]:
                        return True
        
        return False

    def _extract_display_name(self, device_name: str) -> str:
        """
        Extract a clean display name by removing common prefixes and suffixes.
        
        Args:
            device_name: Device name (possibly with prefixes like "Microphone")
            
        Returns:
            Clean device name for display
        """
        import re
        
        # Windows system devices that should be simplified (multilingual support)
        system_device_mappings = {
            # English
            'sound mapper': 'System Default',
            'primary sound driver': 'System Default',
            'primary sound capture driver': 'System Default',
            'microsoft sound mapper': 'System Default',
            # Japanese
            'プライマリ サウンド キャプチャ ドライバー': 'System Default',
            'プライマリ サウンド ドライバー': 'System Default',
            'プライマリサウンドキャプチャドライバー': 'System Default',
            'サウンド マッパー': 'System Default',
            'マイクロソフト サウンド マッパー': 'System Default',
            # German
            'primärer soundaufnahmetreiber': 'System Default',
            'primärer soundtreiber': 'System Default',
            # French
            'pilote de capture audio principal': 'System Default',
            'pilote audio principal': 'System Default',
            # Spanish
            'controlador de captura de sonido principal': 'System Default',
            'controlador de sonido principal': 'System Default',
            # Chinese (Simplified)
            '主声音捕获驱动程序': 'System Default',
            '主声音驱动程序': 'System Default',
            # Korean
            '기본 사운드 캡처 드라이버': 'System Default',
            '기본 사운드 드라이버': 'System Default',
        }
        
        # Check if this is a system device (case-insensitive, handles Unicode)
        lower_name = device_name.lower()
        
        # First check exact matches
        for system_name, display_name in system_device_mappings.items():
            if system_name.lower() in lower_name:
                return display_name  # Don't add (Virtual) here, it will be detected later
        
        # Remove Windows API suffixes first (numbers in parentheses at the end)
        # e.g., "Microphone (NVIDIA Broadcast) (28)" -> "Microphone (NVIDIA Broadcast)"
        clean_name = re.sub(r'\s*\(\d+\)$', '', device_name)
        
        # Special handling for Stereo Mixer
        if clean_name.startswith('Stereo Mixer'):
            match = re.match(r'^Stereo\s+Mixer\s*\(([^)]+)\)', clean_name, re.IGNORECASE)
            if match:
                device_part = match.group(1).strip()
                # Skip if it's a generic name
                if device_part.lower() not in ['realtek', 'realtek hd audio', 'realtek high definition audio']:
                    return f"Stereo Mixer ({device_part})"
            return "Stereo Mixer"
        
        # Handle device names with parentheses
        # Pattern: "Prefix (Device Name)" - extract the device name from parentheses
        # Extended to support more languages
        prefix_patterns = [
            # English
            'Microphone', 'Mic', 'Line In', 'Line', 'Audio Input',
            # Japanese
            'マイク', 'マイクロフォン', 'ライン入力', 'オーディオ入力', '音声入力',
            # German
            'Mikrofon', 'Eingang',
            # French
            'Microphone', 'Entrée', 'Entrée ligne',
            # Spanish
            'Micrófono', 'Entrada', 'Entrada de línea',
            # Chinese
            '麦克风', '话筒', '音频输入', '线路输入',
            # Korean
            '마이크', '오디오 입력', '라인 입력'
        ]
        
        # Build regex pattern with all prefixes
        prefix_pattern = '|'.join(re.escape(prefix) for prefix in prefix_patterns)
        match = re.match(rf'^(?:{prefix_pattern})\s*\(([^)]+)\)$', clean_name, re.IGNORECASE | re.UNICODE)
        if match:
            device_part = match.group(1).strip()
            # Remove any remaining API names
            device_part = re.sub(r'\s*\((?:Windows\s+)?(?:WASAPI|DirectSound|MME|WDM-KS)\)$', '', device_part, flags=re.IGNORECASE)
            # Check if this extracted device name is not another virtual device indicator
            if not any(virt in device_part.lower() for virt in ['driver', 'ドライバー', 'treiber', 'pilote', '驱动']):
                return device_part
        
        # Handle "Line N - Device" pattern and variations like "N- Device"
        # This catches patterns like "Line 3- Yamaha AG03MK2" or "3- Yamaha AG03MK2"
        match = re.match(r'^(?:Line\s+)?(\d+)\s*-\s*(.+)$', clean_name, re.IGNORECASE)
        if match:
            device_part = match.group(2).strip()
            # Remove trailing version numbers like "-1"
            device_part = re.sub(r'-\d+$', '', device_part).strip()
            return device_part
        
        # Handle direct device names (no parentheses, no prefixes)
        if '(' not in clean_name:
            # First check if it starts with a number followed by dash (like "3- Yamaha")
            number_prefix_match = re.match(r'^\d+\s*-\s*(.+)$', clean_name)
            if number_prefix_match:
                clean_name = number_prefix_match.group(1).strip()
            
            # Remove common prefixes if they exist
            prefixes_to_remove = [
                r'^(?:Microphone|マイク|Mic|Line In|Line)\s+',
                r'^(?:Primary|Microsoft|Windows)\s+',
            ]
            
            for prefix in prefixes_to_remove:
                clean_name = re.sub(prefix, '', clean_name, flags=re.IGNORECASE)
            
            # Remove trailing numbers (like "-1", "-2")
            clean_name = re.sub(r'-\d+$', '', clean_name).strip()
            
            return clean_name
        
        # For other complex cases, try to extract the most meaningful part
        # Look for content in parentheses that looks like a device name
        paren_matches = re.findall(r'\(([^)]+)\)', clean_name)
        for match in paren_matches:
            match = match.strip()
            # Skip if it's just numbers, API names, or too short
            if (not match.isdigit() and 
                len(match) > 3 and 
                match.lower() not in ['wasapi', 'directsound', 'mme', 'wdm-ks', 'windows wasapi']):
                return match
        
        # Last resort: clean up the name as much as possible
        # Remove common prefixes
        prefixes = [
            r'^Microphone\s+',
            r'^マイク\s+',
            r'^Mic\s+',
            r'^Line In\s+',
            r'^Line\s+',
            r'^Primary\s+',
            r'^Microsoft\s+',
            r'^Windows\s+',
        ]
        
        for prefix in prefixes:
            clean_name = re.sub(prefix, '', clean_name, flags=re.IGNORECASE)
        
        # Remove trailing numbers and clean up
        clean_name = re.sub(r'-\d+$', '', clean_name)
        clean_name = re.sub(r'\s+', ' ', clean_name).strip()
        
        return clean_name
    
    def _normalize_device_name(self, device_name: str) -> str:
        """
        Normalize device name for comparison during deduplication.
        This helps match truncated or slightly different versions of the same device.
        
        Args:
            device_name: Device name to normalize
            
        Returns:
            Normalized name for comparison
        """
        import re
        
        # First, remove any numbered prefix (like "3-" or "Line 3-")
        normalized = re.sub(r'^(?:Line\s+)?\d+\s*-\s*', '', device_name, flags=re.IGNORECASE)
        
        # Remove trailing version numbers (like "-1", "-2")
        normalized = re.sub(r'-\d+$', '', normalized)
        
        # Now extract display name using the existing method
        normalized = self._extract_display_name(normalized)
        
        # Convert to lowercase for comparison
        normalized = normalized.lower()
        
        # Remove spaces and special characters for comparison, but keep alphanumeric (including Unicode)
        # This preserves Japanese characters while removing punctuation
        normalized = re.sub(r'[^\w]', '', normalized, flags=re.UNICODE)
        
        return normalized

    def check_device_availability(self) -> bool:
        """
        Check if audio input devices are still available and functioning.
        Used to detect OS-level audio system failures or device disconnection.

        Returns:
            bool: True if at least one input device is available, False otherwise
        """
        try:
            current_time = time.time()
            # Only check periodically to avoid excessive device queries
            if (current_time - self._last_device_check) < self._device_check_interval:
                return True  # Assume OK if checked recently

            self._last_device_check = current_time

            devices = self.get_available_devices()
            if not devices:
                logger.warning("No audio input devices currently available")
                return False

            # We're always using the default device, so just check if any device exists

            return True
        except Exception as e:
            logger.warning(f"Device availability check failed: {e}")
            return False

    def _validate_audio_quality(self, audio_data: np.ndarray, is_final: bool = False) -> bool:
        """
        Check if recorded audio has meaningful content and is not corrupt.

        Args:
            audio_data: Audio data as numpy array
            is_final: Whether this is the final check on complete recording

        Returns:
            bool: True if audio quality is acceptable, False otherwise
        """
        if audio_data.size == 0:
            return False

        # Check for NaN or Inf values in audio data
        if not np.all(np.isfinite(audio_data)):
            logger.error("Audio data contains NaN or Inf values - corrupted audio detected")
            if is_final:
                from .errors import STTError, STT_CORRUPTED_AUDIO
                raise STTError(STT_CORRUPTED_AUDIO,
                               "Corrupted audio data detected (contains NaN or Inf values)")
            return False

        # Calculate RMS amplitude
        rms = np.sqrt(np.mean(audio_data**2))

        # Check amplitude levels (silence detection)
        if rms < 0.001:  # Extremely quiet
            if is_final:
                logger.debug(f"Recorded audio appears to be silent (RMS: {rms:.6f})")
            return False

        # Check for clipping/distortion (values near extremes)
        clip_threshold = 0.98
        clipped_samples = np.sum(np.abs(audio_data) > clip_threshold)
        if clipped_samples > 0:
            clip_percent = (clipped_samples / audio_data.size) * 100
            if clip_percent > 1.0:  # More than 1% clipping is concerning
                logger.debug(f"Significant audio clipping detected ({clip_percent:.2f}% of samples) - recording may be distorted")
                return False
            elif clip_percent > 0.1:  # Minor clipping
                logger.info(f"Minor audio clipping detected ({clip_percent:.2f}% of samples)")

        # For final check on complete recording, perform additional analysis
        if is_final:
            # Check for consistent audio levels (detect gaps, dropouts)
            # Split into segments and check for dramatic level changes
            if audio_data.size > 16000:  # At least 1 second at 16kHz
                segment_length = 1600  # 100ms segments
                segments = [audio_data[i:i+segment_length] for i in range(0, len(audio_data), segment_length)]
                segment_rms = [np.sqrt(np.mean(s**2)) for s in segments if len(s) == segment_length]

                if segment_rms:
                    # Check for long silent gaps
                    silent_segments = sum(1 for s in segment_rms if s < 0.005)
                    if silent_segments > len(segment_rms) * 0.7:  # More than 70% silence
                        logger.debug("Recording contains mostly silence")
                        return False

                    # Check for sudden dramatic level changes (potential corruption)
                    epsilon = 1e-10  # Small value to prevent division issues
                    for i in range(1, len(segment_rms)):
                        if segment_rms[i] > 0 and segment_rms[i-1] > 0:
                            # Add epsilon to prevent division by extremely small values
                            ratio = max(segment_rms[i], segment_rms[i-1]) / (min(segment_rms[i], segment_rms[i-1]) + epsilon)
                            if ratio > 50:  # Dramatic 50x change in level
                                logger.debug(f"Suspicious audio level change detected at segment {i}")
                                return False

        return True

    def _check_system_resources(self) -> Dict[str, Any]:
        """
        Monitor system resources to detect potential issues before they cause failures.

        Returns:
            Dict with status information:
                "ok": bool - True if resources are sufficient, False if critical
                "message": str - Description of any resource issues
                "cpu": float - Current CPU usage percentage
                "memory": float - Current memory usage percentage
        """
        result = {
            "ok": True,
            "message": "",
            "cpu": 0.0,
            "memory": 0.0
        }

        try:
            # Get current process (persistent handle so cpu_percent deltas work)
            process = self._psutil_process or psutil.Process(os.getpid())

            # Check CPU usage (both system and process). interval=None is
            # non-blocking (usage since previous call) — the old interval=0.1
            # sampling blocked ~0.2s on every recording start and STT start.
            system_cpu = psutil.cpu_percent(interval=None)
            process_cpu = process.cpu_percent(interval=None)
            result["cpu"] = max(system_cpu, process_cpu)

            # Check memory usage
            system_memory = psutil.virtual_memory().percent
            process_memory = process.memory_percent()
            result["memory"] = max(system_memory, process_memory)

            # Check if any resources are above threshold
            warnings = []

            if result["cpu"] > self._resource_warning_threshold["cpu"]:
                warnings.append(f"CPU usage critical: {result['cpu']:.1f}%")

            if result["memory"] > self._resource_warning_threshold["memory"]:
                warnings.append(f"Memory usage critical: {result['memory']:.1f}%")

            if warnings:
                result["ok"] = False
                result["message"] = "; ".join(warnings)
                logger.warning(f"System resource warning: {result['message']}")
        except Exception as e:
            # Non-critical - just log and continue
            logger.info(f"Resource monitoring error: {e}")
            result["message"] = f"Unable to check resources: {e}"

        return result
        
    def _publish_mic_note(self, message_key: str, **fmt) -> None:
        """Best-effort note on the Audio Setting status line (WS, JS専有div).

        _publish_model_status と同じ飲み込み方針: UI 通知の失敗が録音を
        壊してはならないので、ローカライズも送達も全て swallow する。
        """
        try:
            from backend.shared.i18n import t
            from backend.shared.ui_events import publish_ui_update
            publish_ui_update("stt_model_status",
                              data={"state": "info", "message": t(message_key, **fmt)})
        except Exception as e:
            logger.debug(f"mic note publish failed: {e}")

    def _configured_input_name(self) -> Optional[str]:
        """設定済みマイク名(''=未設定・取得失敗は None=システム既定)。"""
        try:
            from backend.shared.settings_store import get_setting
            wanted = get_setting('audio', 'input_device', '')
        except Exception as e:
            logger.debug(f"Input device config unavailable, using default: {e}")
            return None
        return wanted or None

    def _input_candidate_ids(self, wanted: str) -> "List[tuple]":
        """保存名に対応する全API変種を (id, api名) の優先順リストで返す。

        名前で永続化しIDへは録音開始のたびに解決する — sounddevice のIDは
        表の作り直しで変動するため。API変種は生名が同一とは限らない
        (MMEは31文字で切断される実測)ので、重複排除と同じ正規化+類似判定で
        物理デバイス単位に束ね、WASAPI→DirectSound→MME→WDM-KS の順に並べる。
        1変種だけ試して諦めると、凍結表のズレで誤った変種を掴んだとき
        (WdmSyncIoctl -9999 実障害 2026-07-25)に即・既定へ劣化してしまう。
        """
        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()
        except Exception as e:
            logger.warning(f"Raw device enumeration failed: {e}")
            return []
        wanted_norm = self._normalize_device_name(wanted)
        candidates = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] <= 0:
                continue
            if any(skip in d["name"].lower()
                   for skip in ["sound mapper", "primary sound"]):
                continue
            norm = self._normalize_device_name(d["name"])
            if norm == wanted_norm or self._are_likely_same_device(norm, wanted_norm):
                host_idx = d["hostapi"]
                api = (host_apis[host_idx]["name"]
                       if host_idx < len(host_apis) else "Unknown")
                candidates.append((WINDOWS_API_PRIORITY.get(api, 999), i, api))
        candidates.sort()
        return [(i, api) for _prio, i, api in candidates]

    def refresh_device_table(self) -> bool:
        """PortAudio のデバイス表を再初期化して現実と同期する。

        表はプロセス起動時に凍結され抜き差しが反映されない(凍結表の古い
        IDが別API変種を指し録音が既定へ劣化した実障害 2026-07-25)。この
        プロセスの PortAudio ストリームは録音の InputStream 1本だけ
        (TTS/ビープはブラウザ側再生)なので、ストリーム実体が無ければ
        再初期化は安全。判定は is_recording でなく self.stream —
        start_recording はストリームを開く前に is_recording を立てるため、
        フラグで判定するとリカバリ経路(開く前)が常にスキップされる。
        失敗時は凍結表のまま継続(従来挙動への劣化)。
        """
        if self.stream is not None:
            logger.debug("Device table refresh skipped: stream is open")
            return False
        try:
            sd._terminate()
            sd._initialize()
            logger.info("PortAudio device table refreshed")
            return True
        except Exception as e:
            logger.warning(f"PortAudio device table refresh failed: {e}")
            return False

    def last_recording_fallback(self) -> Optional[str]:
        """直近の録音開始でのフォールバック理由。

        'not_found'(候補なし) | 'open_failed'(全変種で開けず) | None(正常)。
        録音開始のたびに上書きされる。UI層(録音フロー)がポップアップ判断に読む。
        """
        return self._recording_fallback

    def _wasapi_extra_settings(self, device: Optional[int]):
        """WASAPI デバイスに 16kHz を開かせる auto_convert 設定(他は None)。

        WASAPI 共有モードは既定でリサンプルせず 16kHz を拒否する
        (PaErrorCode -9997・実測 2026-07-25)。auto_convert=True で WASAPI 側が
        変換する。MME/DirectSound は元々自動変換のため不要。device=None
        (システム既定)は従来経路のまま触らない。
        """
        if device is None:
            return None
        try:
            host_idx = sd.query_devices(device)["hostapi"]
            if "WASAPI" in sd.query_hostapis(host_idx)["name"]:
                return sd.WasapiSettings(auto_convert=True)
        except Exception as e:
            logger.debug(f"WASAPI extra settings probe failed: {e}")
        return None

    def precheck_input_device(self, name: str) -> str:
        """UI保存時の事前検証: 'ok' | 'not_connected' | 'check_failed'。

        録音時と同一条件(サンプルレート/ch/dtype/WASAPI auto_convert)で
        開けるかを PortAudio に照会する。失敗しても保存は有効のまま=
        録音開始時の既定フォールバックが最後の受け皿。
        """
        try:
            candidates = self._input_candidate_ids(name)
            if not candidates:
                return 'not_connected'
            # 録音時に最初に試す変種(最優先候補)を検証する
            dev_id, _api = candidates[0]
            sd.check_input_settings(
                device=dev_id,
                samplerate=self.audio_config["sample_rate"],
                channels=self.audio_config["channels"],
                dtype=self.audio_config["dtype"],
                extra_settings=self._wasapi_extra_settings(dev_id),
            )
            return 'ok'
        except Exception as e:
            logger.warning(f"Input device precheck failed for '{name}': {e}")
            return 'check_failed'

    def _open_input_stream(self, device: Optional[int]) -> "sd.InputStream":
        """設定パラメータで InputStream を開いて start まで済ませて返す。"""
        stream = sd.InputStream(
            samplerate=self.audio_config["sample_rate"],
            channels=self.audio_config["channels"],
            dtype=self.audio_config["dtype"],
            device=device,  # None = use system default
            callback=self._audio_callback,
            extra_settings=self._wasapi_extra_settings(device)
        )
        stream.start()
        return stream

    def _try_candidates(self, candidates) -> "Optional[tuple]":
        """候補 (id, api名) を順に開き、最初の成功を (stream, 表記) で返す。"""
        for dev_id, api in candidates:
            try:
                stream = self._open_input_stream(dev_id)
                return stream, f"id={dev_id} [{api}]"
            except sd.PortAudioError as e:
                logger.warning(f"Input device id={dev_id} [{api}] failed to open: {e}")
        return None

    def _open_configured_stream(self, wanted: str) -> "tuple":
        """選択マイクを 全API変種→表の再初期化→既定 の順で開く。

        戻り値: (stream, ログ用の実使用デバイス表記)。既定への劣化時は
        _recording_fallback へ理由を記録し状態行へ可視化する(録音フロー=
        UI層がポップアップ判断に読む)。
        """
        candidates = self._input_candidate_ids(wanted)
        opened = self._try_candidates(candidates)
        if opened is None and self.refresh_device_table():
            # 凍結表が現実とズレている可能性(抜き差し/再列挙): 表を作り
            # 直して一度だけ再解決・再試行=挿し直しからの自己回復経路。
            candidates = self._input_candidate_ids(wanted)
            opened = self._try_candidates(candidates)
        if opened is not None:
            return opened
        if not candidates:
            self._recording_fallback = 'not_found'
            logger.warning(
                f"Configured input device '{wanted}' not found - "
                f"falling back to system default")
            self._publish_mic_note('hdl.audio_device.not_found_fallback',
                                   name=wanted)
        else:
            self._recording_fallback = 'open_failed'
            logger.warning(
                f"All API variants of '{wanted}' failed to open - "
                f"falling back to system default")
            self._publish_mic_note('hdl.audio_device.open_failed_fallback')
        return self._open_input_stream(None), "system default (fallback)"

    def start_recording(self) -> None:
        """
        Start capturing audio using the configured microphone
        (system default when none is configured).

        Raises:
            RuntimeError: If recording fails to start
        """
        # Rate limiting for rapid start/stop toggling
        current_time = time.time()
        if (current_time - self._last_toggle_time) < self._min_toggle_interval:
            logger.warning(f"Start/stop toggled too rapidly (within {self._min_toggle_interval} sec). Ignoring request.")
            return
        self._last_toggle_time = current_time

        if self.is_recording:
            logger.warning("start_recording() called but recording is already in progress.")
            return

        # Check system resources before starting
        resource_status = self._check_system_resources()
        if not resource_status["ok"]:
            resource_warning = f"System resources critical: {resource_status['message']}"
            logger.warning(resource_warning)
            # Continue anyway but warn user

        try:
            # Clear previous buffer and reset state
            self.audio_buffer = []
            self.recording_start_time = time.time()
            self.is_recording = True
            self.recording_event.clear()
            self._recording_fallback = None

            wanted = self._configured_input_name()
            if wanted is None:
                self.stream = self._open_input_stream(None)
                used = "system default"
            else:
                # NOTE: _open_configured_stream 内の表再初期化は
                # self.stream is None が条件 — stream への代入はこの呼び出し
                # の後でなければならない。
                self.stream, used = self._open_configured_stream(wanted)
            logger.info(
                f"Recording started: {self.audio_config['sample_rate']} Hz, "
                f"{self.audio_config['channels']} channel(s), device={used}"
            )
        except sd.PortAudioError as e:
            self.is_recording = False
            # Specifically identify "device in use" errors
            if "Device unavailable" in str(e) or "busy" in str(e):
                error_msg = f"Microphone is in use by another process: {e}"
                logger.error(error_msg)
                raise RuntimeError(error_msg) from e
            else:
                logger.error(f"Failed to start recording: {e}")
                raise RuntimeError(f"Failed to start recording: {e}") from e
        except Exception as e:
            self.is_recording = False
            logger.error(f"Failed to start recording: {e}")
            raise RuntimeError(f"Failed to start recording: {e}") from e

    def stop_recording(self) -> None:
        """
        Stop capturing audio.

        Closes the InputStream and keeps the audio buffer in memory for transcription.
        Logs a warning if not currently recording.
        """
        # Rate limiting for rapid start/stop toggling.
        # 停止要求を握り潰すとストリームが開いたまま(コールバックがバッファへ追記中)
        # UI は停止済み表示になり、直後の transcribe とバッファ競合する。
        # 無視ではなく、最小間隔の残り時間だけ待ってから確実に停止する。
        current_time = time.time()
        remaining = self._min_toggle_interval - (current_time - self._last_toggle_time)
        if remaining > 0:
            logger.info(f"stop_recording throttled: waiting {remaining:.2f}s before stopping")
            time.sleep(remaining)
        self._last_toggle_time = time.time()

        if not self.is_recording:
            logger.warning("stop_recording() called but recording is not active.")
            return

        self.is_recording = False

        if self.stream is not None:
            try:
                self.stream.stop()
                elapsed = time.time() - self.recording_start_time if self.recording_start_time else 0
                logger.info(f"Recording stopped after {elapsed:.2f} seconds.")

                # Validate the final recording for quality
                if self.audio_buffer:
                    full_audio = np.concatenate(self.audio_buffer, axis=0).flatten()
                    if not self._validate_audio_quality(full_audio, is_final=True):
                        logger.debug("Final audio recording has quality issues that may affect transcription")

            except sd.PortAudioError as e:
                logger.error(f"PortAudio error when stopping recording: {e}")
                # This could indicate a device disconnection during recording
                if "Invalid device" in str(e) or "device disconnected" in str(e):
                    logger.error("Audio device appears to have been disconnected during recording")
            except Exception as e:
                logger.error(f"Error stopping the recording stream: {e}")
            finally:
                # close() を finally に移動: stop() が例外を投げても PortAudio
                # ストリームを必ず解放する(旧実装は stop() 例外時に close 未到達で
                # プロセス終了までストリームがリークしていた)。
                try:
                    self.stream.close()
                except Exception as e:
                    logger.debug(f"Best-effort stream close failed: {e}")
                self.stream = None
                self.recording_event.set()

    def _stt_engine_config(self) -> "tuple[str, str]":
        """Resolve the configured STT engine and API model.

        Call-time import of backend.shared (audio_input stays a leaf at
        module load). Any failure degrades to the local engine.

        Returns:
            (engine, api_model): engine is 'faster_whisper' or 'openai'.
        """
        try:
            from backend.shared.settings_store import get_setting
            engine = get_setting('audio', 'stt_engine', 'faster_whisper')
            api_model = get_setting('audio', 'stt_api_model', 'whisper-1')
            if engine not in ("faster_whisper", "openai"):
                logger.warning(f"Unknown stt_engine '{engine}', falling back to faster_whisper")
                engine = "faster_whisper"
            return engine, api_model
        except Exception as e:
            logger.debug(f"STT engine config unavailable, defaulting to local: {e}")
            return "faster_whisper", "whisper-1"

    def _transcribe_via_api(self, audio_bytes: bytes, filename: str, api_model: str) -> str:
        """Shared OpenAI transcription call: resolve the key at call time.

        Raises:
            RuntimeError: If the OpenAI API key is not set or the API fails.
        """
        from backend.shared.api_settings import load_api_settings
        api_key = load_api_settings().get("openai", {}).get("api_key", "")
        if not api_key:
            from .errors import STTError, STT_API_KEY_MISSING
            raise STTError(STT_API_KEY_MISSING,
                           "OpenAI API key is not set. Configure it in the API Setting tab.")

        from . import openai_stt
        language = self.language if self.language in SUPPORTED_LANGUAGES else None
        text = openai_stt.transcribe_bytes(
            audio_bytes, filename, api_key, api_model, language=language
        )
        # Guarantee the local-route contract (stripped text) at this level too.
        return text.strip()

    def _transcribe_buffer_via_api(self, api_model: str) -> str:
        """API route for the in-memory recording buffer.

        Silence is checked locally BEFORE upload (same "No speech detected"
        message as the local route — identical UX, no wasted API calls).
        Empty API text returns "" like the local route (callers decide).
        """
        from . import openai_stt

        audio_data = np.concatenate(self.audio_buffer, axis=0).flatten()
        if audio_data.size == 0:
            logger.debug("Audio buffer is empty or contains no valid data. Cannot transcribe.")
            return ""
        if not np.all(np.isfinite(audio_data)):
            logger.warning("Audio buffer contains NaN/Inf values - sanitizing before upload")
            audio_data = np.nan_to_num(audio_data)

        rms = np.sqrt(np.mean(audio_data**2))
        if rms < 0.001:
            logger.error("Audio appears to be completely silent - no speech detected")
            from .errors import STTError, STT_NO_SPEECH
            raise STTError(STT_NO_SPEECH, "No speech detected in recording (silent audio)")

        wav_bytes = openai_stt.numpy_to_wav_bytes(audio_data, self.audio_config["sample_rate"])
        text = self._transcribe_via_api(wav_bytes, "audio.wav", api_model)

        # Match the local route: clear the buffer only after success.
        self.audio_buffer = []
        return text

    def transcribe_audio(self) -> str:
        """
        Transcribe the audio currently buffered, using the configured STT
        engine (local faster-whisper, or the OpenAI transcription API).

        Implements chunked processing for large audio buffers to avoid memory issues.

        Returns:
            The transcribed text from the buffered audio

        Raises:
            RuntimeError: If model is not initialized or transcription fails
        """
        if not self.audio_buffer:
            logger.debug("No audio data to transcribe. Returning empty string.")
            return ""

        engine, api_model = self._stt_engine_config()
        if engine == "openai":
            return self._transcribe_buffer_via_api(api_model)

        if not self.ensure_model_loaded():
            error_msg = "faster-whisper model is not available (load failed or timed out). See logs for details."
            logger.error(error_msg)
            from .errors import STTError, STT_MODEL_UNAVAILABLE
            raise STTError(STT_MODEL_UNAVAILABLE, error_msg)

        # Hold a local reference for the whole transcription: a concurrent
        # release (engine/model-size switch) sets self.model to None, but the
        # refcount keeps this instance alive until we are done with it.
        model = self.model
        if model is None:
            error_msg = "faster-whisper model was released before transcription could start."
            logger.error(error_msg)
            from .errors import STTError, STT_MODEL_RELEASED
            raise STTError(STT_MODEL_RELEASED, error_msg)

        # Check system resources before computationally intensive operation
        resource_status = self._check_system_resources()
        if not resource_status["ok"]:
            resource_warning = f"System resources critical before transcription: {resource_status['message']}"
            logger.warning(resource_warning)
            # Continue but with warning

        try:
            # Calculate total size of buffer
            total_samples = sum(chunk.size for chunk in self.audio_buffer)
            max_chunk_size = self.transcription_config["max_chunk_size"]

            if total_samples == 0:
                logger.debug("Audio buffer is empty or contains no valid data. Cannot transcribe.")
                return ""

            # Process in chunks if buffer is too large
            if total_samples > max_chunk_size:
                logger.info(f"Large audio buffer detected ({total_samples} samples). Processing in chunks.")

                results = []
                chunks = []
                current_size = 0
                all_silent = True  # Track if all chunks are silent

                for chunk in self.audio_buffer:
                    chunks.append(chunk)
                    current_size += chunk.size

                    if current_size >= max_chunk_size:
                        # Process this batch
                        audio_chunk = np.concatenate(chunks, axis=0).flatten()
                        
                        # Validate chunk data before processing
                        if not np.all(np.isfinite(audio_chunk)):
                            logger.error("Audio chunk contains NaN or Inf values - skipping corrupted chunk")
                            chunks = []
                            current_size = 0
                            continue
                        
                        # Quick silence check for this chunk
                        rms = np.sqrt(np.mean(audio_chunk**2))
                        if rms > 0.001:
                            all_silent = False
                        
                        try:
                            segment_results, _ = model.transcribe(
                                audio_chunk,
                                language=self.language,
                                task="transcribe",
                                beam_size=self.transcription_config["beam_size"],
                                vad_filter=self.transcription_config["vad_filter"],
                                hallucination_silence_threshold=self.transcription_config.get("hallucination_silence_threshold"),
                            )
                            results.extend(list(segment_results))
                        except Exception as chunk_error:
                            logger.error(f"Error transcribing audio chunk: {chunk_error}")
                            # Continue with other chunks instead of failing completely

                        chunks = []
                        current_size = 0

                # Process any remaining chunks
                if chunks:
                    audio_chunk = np.concatenate(chunks, axis=0).flatten()

                    # Validate final chunk data before processing
                    if not np.all(np.isfinite(audio_chunk)):
                        logger.error("Final audio chunk contains NaN or Inf values - skipping corrupted chunk")
                    else:
                        # Quick silence check for final chunk
                        rms = np.sqrt(np.mean(audio_chunk**2))
                        if rms > 0.001:
                            all_silent = False

                        try:
                            segment_results, _ = model.transcribe(
                                audio_chunk,
                                language=self.language,
                                task="transcribe",
                                beam_size=self.transcription_config["beam_size"],
                                vad_filter=self.transcription_config["vad_filter"],
                                hallucination_silence_threshold=self.transcription_config.get("hallucination_silence_threshold"),
                            )
                            results.extend(list(segment_results))
                        except Exception as chunk_error:
                            logger.error(f"Error transcribing final audio chunk: {chunk_error}")

                # Check if entire recording was silent
                if all_silent:
                    logger.error("Audio appears to be completely silent - no speech detected")
                    from .errors import STTError, STT_NO_SPEECH
                    raise STTError(STT_NO_SPEECH,
                                   "No speech detected in recording (silent audio)")

                # Combine results
                text = "".join(s.text for s in results)

                # Check if we got any text at all
                if not text.strip():
                    logger.debug("No text was transcribed from any audio chunks")

            else:
                # Original processing for small buffers
                audio_data = np.concatenate(self.audio_buffer, axis=0).flatten()

                # Validate audio quality before transcription
                if not self._validate_audio_quality(audio_data, is_final=True):
                    logger.debug("Audio quality issues detected - transcription may be unreliable")
                    
                    # Check for silent audio (extremely low amplitude) and provide specific message
                    rms = np.sqrt(np.mean(audio_data**2))
                    if rms < 0.001:
                        logger.error("Audio appears to be completely silent - no speech detected")
                        from .errors import STTError, STT_NO_SPEECH
                        raise STTError(STT_NO_SPEECH,
                                       "No speech detected in recording (silent audio)")
                
                segments, info = model.transcribe(
                    audio_data,
                    language=self.language,
                    task="transcribe",
                    beam_size=self.transcription_config["beam_size"],
                    vad_filter=self.transcription_config["vad_filter"],
                    hallucination_silence_threshold=self.transcription_config.get("hallucination_silence_threshold"),
                )
                text = "".join(s.text for s in segments)
                logger.info(f"Transcription successful. Detected language: '{info.language}'. Transcript: {text}")

            # Clear buffer after successful transcription
            self.audio_buffer = []
            return text.strip()

        except Exception as e:
            # STTError(ag_code付き)は包み直さず素通し — 包むとUI層の
            # コード照合(err.<code>翻訳)が壊れる。
            from .errors import STTError
            if isinstance(e, STTError):
                raise
            logger.error(f"Transcription failed: {e}")
            # Include more specific error message types
            if "CUDA" in str(e) or "GPU" in str(e) or "device" in str(e).lower():
                error_msg = f"GPU error during transcription: {e}. Try using CPU mode instead."
                logger.error(error_msg)
                raise RuntimeError(error_msg) from e
            elif "memory" in str(e).lower():
                error_msg = f"Memory error during transcription: {e}. Try reducing model size or audio chunk size."
                logger.error(error_msg)
                raise RuntimeError(error_msg) from e
            else:
                raise RuntimeError(f"Transcription failed: {e}") from e

    def transcribe_file(self, file_path: str) -> str:
        """Transcribe audio from a file path (WebM, WAV, etc.).

        The faster-whisper model accepts file paths and decodes them internally
        via ffmpeg, so WebM/Opus from browser MediaRecorder is supported.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text (stripped).

        Raises:
            RuntimeError: If the whisper model is not initialized.
        """
        engine, api_model = self._stt_engine_config()
        if engine == "openai":
            with open(file_path, "rb") as f:
                audio_bytes = f.read()
            filename = os.path.basename(file_path) or "audio.webm"
            text = self._transcribe_via_api(audio_bytes, filename, api_model)
            logger.info(f"[BrowserMic] API file transcription: text='{text}'")
            return text

        if not self.ensure_model_loaded():
            from .errors import STTError, STT_MODEL_UNAVAILABLE
            raise STTError(STT_MODEL_UNAVAILABLE,
                           "faster-whisper model is not available (load failed or timed out). See logs for details.")

        # Local reference: survives a concurrent release (engine/size switch).
        model = self.model
        if model is None:
            from .errors import STTError, STT_MODEL_RELEASED
            raise STTError(STT_MODEL_RELEASED,
                           "faster-whisper model was released before transcription could start.")

        segments, info = model.transcribe(
            file_path,
            language=self.language,
            task="transcribe",
            beam_size=self.transcription_config["beam_size"],
            vad_filter=self.transcription_config["vad_filter"],
            hallucination_silence_threshold=self.transcription_config.get("hallucination_silence_threshold"),
        )
        text = "".join(s.text for s in segments)
        logger.info(f"[BrowserMic] File transcription: lang='{info.language}', text='{text.strip()}'")
        return text.strip()

    def is_currently_recording(self) -> bool:
        """
        Check if currently recording.
        
        Public method to check recording state without accessing internal attributes.
        
        Returns:
            bool: True if recording is active, False otherwise
        """
        return self.is_recording

    def unload_model(self) -> None:
        """
        Release the local Whisper model (VRAM) when the STT engine switches
        to the API route. Narrower than cleanup(): recording state and the
        audio buffer are untouched, so an engine toggle mid-session is safe.
        """
        if self._model_loading:
            # A warm-up load is in flight and cannot be cancelled. Unload
            # after it lands — but only if the engine is still the API by
            # then (the user may have toggled back).
            def _deferred_unload():
                self._model_load_done.wait(timeout=600)
                if self._stt_engine_config()[0] == "openai":
                    self._release_model()

            threading.Thread(
                target=_deferred_unload,
                name="whisper-deferred-unload",
                daemon=True,
            ).start()
            logger.info("Whisper model load in flight - deferred unload scheduled")
            return
        self._release_model()

    def _release_model(self) -> None:
        """Drop the model reference and free GPU memory (idempotent)."""
        with self._model_lock:
            if self.model is None:
                return
            try:
                del self.model
            finally:
                self.model = None
                self._loaded_model_size = None

        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Error during GPU cleanup after model unload: {e}")
        logger.info("Whisper model unloaded (STT engine switched to API)")

    def cleanup(self) -> None:
        """
        Clean up audio input resources including the Whisper model and GPU memory.
        Should be called during application shutdown to ensure proper resource release.
        """
        logger.info("Cleaning up audio input resources...")

        # Stop any active recording first
        if self.is_recording:
            try:
                self.stop_recording()
            except Exception as e:
                logger.warning(f"Error stopping recording during cleanup: {e}")

        # Clear audio buffer to free memory
        self.audio_buffer = []

        # Unload Whisper model (CTranslate2 based)
        if self.model is not None:
            try:
                del self.model
                self.model = None
                self._loaded_model_size = None
                logger.info("Whisper model unloaded")
            except Exception as e:
                logger.warning(f"Error unloading Whisper model: {e}")

        # Force garbage collection (important for CTranslate2 cleanup)
        import gc
        gc.collect()

        # Also clear PyTorch cache if available (for consistency)
        # torch 未ロードなら空にすべき CUDA キャッシュも存在しない — 遅延import化
        # 後に「会話せず終了」した場合、シャットダウンで初 torch import(~5秒)を
        # 払わないためのガード(意味は従来と同一)
        if 'torch' in sys.modules:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                logger.info("Audio input GPU memory released")
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Error during GPU cleanup: {e}")

        logger.info("Audio input cleanup complete")


# Create a default instance for backwards compatibility with module-level functions
_audio_manager = AudioInputManager()

# Module-level functions that use the default instance
def init_audio_input(
    model_size: str = "turbo",
    device: Optional[str] = None,
    compute_type: str = "float16"
) -> None:
    """
    Initialize the audio input module.
    
    Args:
        model_size: Which faster-whisper model to load
        device: The device to run faster-whisper on
        compute_type: Precision/quantization for the model
    """
    _audio_manager.init_audio_input(model_size, device, compute_type)


def configure_stt(language: str) -> None:
    """
    Configure the language for speech-to-text (STT).

    Args:
        language: Language code, e.g. "en", "ja"
    """
    _audio_manager.configure_stt(language)


def start_model_load() -> None:
    """Kick off the Whisper model load in the background (non-blocking)."""
    _audio_manager.start_model_load()


def is_model_ready() -> bool:
    """True if the Whisper model is loaded and usable."""
    return _audio_manager.is_model_ready()


def is_model_loading() -> bool:
    """True while a Whisper model load/download worker is in flight."""
    return _audio_manager.is_model_loading()


def unload_model() -> None:
    """Release the local Whisper model (used when STT switches to the API engine)."""
    _audio_manager.unload_model()


def set_model_size(model_size: str) -> str:
    """Switch the local faster-whisper model size at runtime.

    Returns 'invalid' | 'already_loaded' | 'reloading' | 'queued' | 'deferred'
    (see AudioInputManager.set_model_size).
    """
    return _audio_manager.set_model_size(model_size)


def start_recording() -> None:
    """
    Start capturing audio from the system's default microphone.
    """
    _audio_manager.start_recording()


def stop_recording() -> None:
    """Stop capturing audio."""
    _audio_manager.stop_recording()


def transcribe_audio() -> str:
    """
    Transcribe the audio currently buffered.
    
    Returns:
        The transcribed text from the buffered audio
    """
    return _audio_manager.transcribe_audio()


def get_available_devices() -> List[Dict[str, Any]]:
    """
    Get a list of available audio input devices.

    AudioInputManager.get_available_devices(実列挙+Windows重複排除)へ委譲する。
    旧実装はダミー1件を固定で返しており、起動時の「No Microphone Detected」
    判定(ui/app.py)が実デバイス0件でも永遠に発火しなかった。

    Returns:
        List of device dictionaries (empty when no input device exists).
    """
    return _audio_manager.get_available_devices()


def precheck_input_device(name: str) -> str:
    """
    Precheck a saved input device name: 'ok' | 'not_connected' | 'check_failed'.
    """
    return _audio_manager.precheck_input_device(name)


def refresh_device_table() -> bool:
    """
    Re-initialize the PortAudio device table (safe only while no stream is open).
    """
    return _audio_manager.refresh_device_table()


def last_recording_fallback() -> Optional[str]:
    """
    Fallback reason of the latest recording start:
    'not_found' | 'open_failed' | None.
    """
    return _audio_manager.last_recording_fallback()


def set_max_recording_duration(seconds: int) -> None:
    """
    Set the maximum recording duration in seconds.
    
    Args:
        seconds: Maximum recording duration
    """
    _audio_manager.set_max_recording_duration(seconds)


def is_currently_recording() -> bool:
    """
    Check if currently recording.

    Returns:
        bool: True if recording is active, False otherwise
    """
    return _audio_manager.is_currently_recording()


def transcribe_file(file_path: str) -> str:
    """
    Transcribe audio from a file path (WebM, WAV, etc.).

    Args:
        file_path: Path to the audio file.

    Returns:
        Transcribed text (stripped).
    """
    return _audio_manager.transcribe_file(file_path)


def cleanup() -> None:
    """
    Clean up audio input resources including the Whisper model and GPU memory.
    Module-level cleanup function for audio input resources.
    Should be called during application shutdown.
    """
    _audio_manager.cleanup()