"""
ui/audio_state.py

Audio-related state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the audio knobs the UI keeps around: the mic start/stop
beep settings (enabled flag, volume, pre-rendered beep waveforms), the TTS
playback volume, the audio-playback routing mode (browser vs local sounddevice),
and the recording-start timestamp used to measure recording duration. It came
out of the AppState god-object split.
These fields used to live directly on ``ui/state.py``'s ``AppState`` and are
read/written in place by ``ui/conversation.py`` (beep/TTS playback, recording
lifecycle), ``ui/settings_manager.py`` (load/save persistence),
``ui/components.py`` / ``ui/pages.py`` (control rendering) and the hotkey paths
(``ui/hotkey_service.py`` / ``ui/handlers/hotkey.py``, recording-state reads).

Placement: mirrors the sibling carve-outs (DataState; backend CommandState /
TokenState / MemoryCaches) but lives under ``ui/`` because this is UI-layer
state. It is a pure stdlib state container (numpy is imported only for the
beep-waveform type hint; numpy is already a hard AG dependency).

Layering: this module owns no behavior — only the state container. The audio
*behavior* (beep generation/playback, TTS routing, recording control, volume
update helpers) stays in ``ui/conversation.py`` and reaches these fields through
AppState's backward-compat properties, so the model-facing contract is
unaffected (audio is a UI/real-machine path, outside the harness — so the smoke
import + a real-AppState round-trip are the gate).
"""

from typing import Optional

import numpy as np


class AudioState:
    """Owns the audio knobs carved out of AppState.

    AppState holds a single instance as ``app_state.audio`` and keeps
    backward-compat properties for the legacy ``app_state.<field>`` access paths,
    so existing call sites are unchanged while ownership now lives here. Defaults
    mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.beep_enabled: bool = True
        self.beep_volume: float = 0.5  # Beep volume control (0.0 to 1.0)
        # TTS volume control (0.0 to 1.0) - default 70% to prevent distortion
        self.tts_volume: float = 0.7
        self.start_beep: Optional[np.ndarray] = None
        self.stop_beep: Optional[np.ndarray] = None
        # Track when recording started
        self.recording_start_time: Optional[float] = None
        # 文字起こし(STT)実行中フラグ。recording_start_time は文字起こし
        # 開始時に消える(=録音クレームが途切れる)ため、会話終了ガード
        # (toggle_start_end)が音声ターンを文字起こし完了まで見えるように
        # する。書き手は stop_and_transcribe_phase1 のみ(set/finally clear)。
        self.transcribing: bool = False
        # 直近に通知済みのマイクフォールバック理由('not_found'/'open_failed')。
        # 同じ理由での録音のたびにポップアップを連打しないための状態遷移
        # 検出用(選択マイクで開けたら None に戻る)。書き手は録音フロー
        # (ui/conversation/recording.py)のみ。
        self.mic_fallback_notified: Optional[str] = None
