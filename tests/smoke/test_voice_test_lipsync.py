"""
tests/smoke/test_voice_test_lipsync.py

音声テストの声でも MotionPNGPlayer の口を動かす契約(稜指示 2026-09-19)のスモーク。
会話の声と同じ経路(_send_audio_to_browser)で、口パクのフレームを付けて送る。
セリフの文字は渡さない(吹き出しは text があるときだけ出る)。通知音(beep)は対象外。
"""

import numpy as np


def test_voice_test_audio_carries_lipsync_frames(monkeypatch):
    """音声テストは会話と同じく口パクのフレームを付ける。text は渡さない(吹き出しなし)。"""
    from ui.conversation import tts_playback

    captured = {}

    def fake_send(audio_data, sr, volume, include_lipsync=True, block=True, text=None):
        captured.update(include_lipsync=include_lipsync, block=block, text=text)

    monkeypatch.setattr(tts_playback, "_send_audio_to_browser", fake_send)

    tts_playback._play_test_audio_browser(np.zeros(2400, dtype=np.float32), 24000)

    assert captured == {"include_lipsync": True, "block": True, "text": None}
