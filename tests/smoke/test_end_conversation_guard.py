"""
tests/smoke/test_end_conversation_guard.py

生成中・音声ターン中の会話終了ガード(稜裁定 2026-08-21)のスモーク。

走行中ターンにキャンセル機構はないため、生成中(is_generating=True=TTS再生
完了まで)と音声ターン中(録音中/文字起こし中)の Start/End トグルはサーバ側で
拒否する(toggle_start_end 冒頭)。ガードの隙間に End が通った場合は、生成側
(handle_generate_reply)が会話終了を検知して中止する。録音・文字起こし条件は
会話中のみ効かせる(フラグの異常残留で会話開始まで死なないように)。
"""

from types import SimpleNamespace

from ui.constants import ERROR_MESSAGE_PREFIX
from ui.conversation.generation import handle_generate_reply, is_error_response
from ui.conversation.lifecycle import toggle_start_end
from ui.state import app_state


def _is_noop_update(value) -> bool:
    """gr.update()=無変更update(valueキーなし)かどうか。"""
    return isinstance(value, dict) and value.get("__type__") == "update" and "value" not in value


def test_toggle_refused_while_generating(monkeypatch):
    """is_generating 中の toggle は状態を変えず無変更updateを返し、警告を出す。"""
    saved_started = app_state.conversation_started
    saved_backend = app_state.backend_available
    popups = []
    try:
        app_state.backend_available = True
        app_state.conversation_started = True
        monkeypatch.setattr(
            "backend.backend._backend_state",
            SimpleNamespace(is_generating=True),
        )
        monkeypatch.setattr(
            "ui.conversation.lifecycle.show_warning_popup",
            lambda title, msg: popups.append((title, msg)),
        )

        btn, voice = toggle_start_end()

        # 会話状態は不変(stopは走っていない)
        assert app_state.conversation_started is True
        # ラベルを返すと再レンダがJSのdisabledを剥がすため、両方無変更update
        assert _is_noop_update(btn)
        assert _is_noop_update(voice)
        # 拒否は警告ポップアップ(WSトースト)で告知される
        assert len(popups) == 1
    finally:
        app_state.conversation_started = saved_started
        app_state.backend_available = saved_backend


def test_toggle_refused_while_recording(monkeypatch):
    """録音中(recording_start_time あり)の toggle は生成中でなくても拒否。"""
    saved_started = app_state.conversation_started
    saved_backend = app_state.backend_available
    saved_rec = app_state.recording_start_time
    popups = []
    try:
        app_state.backend_available = True
        app_state.conversation_started = True
        app_state.recording_start_time = 12345.0
        monkeypatch.setattr(
            "backend.backend._backend_state",
            SimpleNamespace(is_generating=False),
        )
        monkeypatch.setattr(
            "ui.conversation.lifecycle.show_warning_popup",
            lambda title, msg: popups.append((title, msg)),
        )

        btn, voice = toggle_start_end()

        assert app_state.conversation_started is True
        assert _is_noop_update(btn)
        assert _is_noop_update(voice)
        assert len(popups) == 1
    finally:
        app_state.conversation_started = saved_started
        app_state.backend_available = saved_backend
        app_state.recording_start_time = saved_rec


def test_toggle_refused_while_transcribing(monkeypatch):
    """文字起こし中(audio.transcribing)の toggle も拒否。"""
    saved_started = app_state.conversation_started
    saved_backend = app_state.backend_available
    saved_tr = app_state.audio.transcribing
    popups = []
    try:
        app_state.backend_available = True
        app_state.conversation_started = True
        app_state.audio.transcribing = True
        monkeypatch.setattr(
            "backend.backend._backend_state",
            SimpleNamespace(is_generating=False),
        )
        monkeypatch.setattr(
            "ui.conversation.lifecycle.show_warning_popup",
            lambda title, msg: popups.append((title, msg)),
        )

        btn, voice = toggle_start_end()

        assert app_state.conversation_started is True
        assert _is_noop_update(btn)
        assert _is_noop_update(voice)
        assert len(popups) == 1
    finally:
        app_state.conversation_started = saved_started
        app_state.backend_available = saved_backend
        app_state.audio.transcribing = saved_tr


def test_start_not_blocked_by_stale_voice_flags(monkeypatch):
    """会話未開始なら録音フラグの異常残留でも Start 分岐へ到達する
    (ガードが voice 条件を会話中に限定していることの検証)。"""
    saved_started = app_state.conversation_started
    saved_backend = app_state.backend_available
    saved_rec = app_state.recording_start_time
    saved_char = app_state.active_character_id
    popups = []
    try:
        app_state.backend_available = True
        app_state.conversation_started = False
        app_state.recording_start_time = 12345.0  # stale claim
        app_state.active_character_id = None
        monkeypatch.setattr(
            "backend.backend._backend_state",
            SimpleNamespace(is_generating=False),
        )
        monkeypatch.setattr(
            "ui.conversation.lifecycle.show_warning_popup",
            lambda title, msg: popups.append((title, msg)),
        )

        btn, voice = toggle_start_end()

        # ガードで拒否されず Start 分岐の「キャラ未選択」警告に到達している
        # (=ガード拒否なら無変更update×2 だが、こちらはラベルを返す)
        assert len(popups) == 1
        assert not _is_noop_update(voice)
    finally:
        app_state.conversation_started = saved_started
        app_state.backend_available = saved_backend
        app_state.recording_start_time = saved_rec
        app_state.active_character_id = saved_char


def test_generation_aborts_when_conversation_ended():
    """会話終了後に届いた生成要求は、backendに触れずエラーマーカー付きで中止。"""
    saved_started = app_state.conversation_started
    try:
        app_state.conversation_started = False

        result = handle_generate_reply("hello after end")

        assert result.startswith(ERROR_MESSAGE_PREFIX)
        assert is_error_response(result)
    finally:
        app_state.conversation_started = saved_started
