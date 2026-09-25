"""TTS再生中のタイムアウト時計停止（稜裁定 2026-08-12）のスモーク。

契約: TTS区間（playback_state フラグ）中は、LLMタスクの2つの時計
（呼出側 _enqueue_llm_task の future 待ち／キュー側
_execute_task_with_timeout のスライスループ）のどちらも残り時間を
減らさない。フラグ解除後・非TTSタスクは従来どおり計時して切る。
"""

import threading
import time

import pytest

from backend.shared import playback_state
from backend.shared.queue_manager import LLMTaskQueue, TaskTimeoutError


@pytest.fixture(autouse=True)
def _clean_flag():
    playback_state.clear_tts_active()
    yield
    playback_state.clear_tts_active()


@pytest.fixture
def llm_queue():
    q = LLMTaskQueue()
    yield q
    q.shutdown(cancel_pending=True, timeout=5.0, force=True)


def _tts_wrapped_task(duration, result=42):
    """TTS区間つきタスク（_play_tts_blocking のフラグ運用を再現）。"""
    def task():
        playback_state.mark_tts_active()
        try:
            time.sleep(duration)
        finally:
            playback_state.clear_tts_active()
        return result
    return task


def test_flag_basics():
    assert playback_state.is_tts_active() is False
    playback_state.mark_tts_active()
    assert playback_state.is_tts_active() is True
    playback_state.clear_tts_active()
    assert playback_state.is_tts_active() is False


def test_flag_cleared_on_tts_error(monkeypatch, make_state, make_cm):
    """例外経路の解除は製品実装（_play_tts_blocking の finally）で検証する。

    契約: 合成失敗は non-fatal（呼出元へ伝播せず会話は継続）・フラグは
    finally で必ず解除される。
    """
    import audio_output.audio_output as audio_output_mod

    state = make_state(speechless_enabled=False)
    cm = make_cm(state, {})

    seen = {}

    def _failing_synthesis(text):
        # TTS区間に入ってから失敗したこと（speechless 早期returnでない）を記録
        seen["flag_during_synthesis"] = playback_state.is_tts_active()
        raise RuntimeError("synthesis failed (injected)")

    monkeypatch.setattr(audio_output_mod, "text_to_speech", _failing_synthesis)

    cm._play_tts_blocking("こんにちは")  # 伝播したらこの行で落ちる

    assert seen["flag_during_synthesis"] is True
    assert playback_state.is_tts_active() is False


def test_queue_clock_pauses_during_tts(llm_queue):
    # timeout=0.5 のタスクが 1.2 秒かかっても、TTS中は切られない
    future = llm_queue.enqueue_llm_task(_tts_wrapped_task(1.2), timeout=0.5)
    assert future.result(timeout=10) == 42


def test_queue_clock_still_times_out_without_tts(llm_queue):
    def slow_task():
        time.sleep(1.2)
        return 42
    future = llm_queue.enqueue_llm_task(slow_task, timeout=0.3)
    with pytest.raises(TaskTimeoutError):
        future.result(timeout=10)


def test_caller_clock_pauses_during_tts(make_state, make_cm, llm_queue):
    state = make_state()
    state.queue_manager = llm_queue
    state.pending_requests_lock = threading.Lock()
    cm = make_cm(state, {})

    # 呼出側時計(future待ちのスライスループ)もTTS中は減算しない
    result = cm._enqueue_llm_task(_tts_wrapped_task(1.6), timeout=0.5)
    assert result == 42


def test_caller_clock_still_times_out_without_tts(make_state, make_cm, llm_queue):
    state = make_state()
    state.queue_manager = llm_queue
    state.pending_requests_lock = threading.Lock()
    cm = make_cm(state, {})

    def slow_task():
        time.sleep(2.0)
        return 42

    with pytest.raises(RuntimeError, match="timed out"):
        cm._enqueue_llm_task(slow_task, timeout=0.5)
