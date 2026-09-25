"""
tests/smoke/test_settings_roundtrip.py

save_app_state_settings (called on every shutdown/restart and on audio/font
changes) must not clobber settings keys it does not manage. display.language
is written directly via settings_store by the System page dropdown; replacing
the whole 'display' category on shutdown silently reverts the UI language to
'auto' on the next boot — the exact bug this test pins down.
"""

from types import SimpleNamespace

from backend.shared import settings_store


def _fake_app_state():
    return SimpleNamespace(
        auto_prompt_enabled=False,
        auto_prompt_timer_duration=60,
        auto_prompt_ja='ja-prompt',
        auto_prompt_en='en-prompt',
        beep_enabled=True,
        beep_volume=0.5,
        tts_volume=0.7,
        chat_font_size=16,
    )


def test_save_app_state_settings_preserves_ui_language(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, 'SETTINGS_FILE', tmp_path / 'user_settings.json')

    # The System page dropdown persists the language directly...
    assert settings_store.update_setting('display', 'language', 'en')

    # ...then the app shuts down and saves app_state-owned settings.
    from ui.settings_manager import save_app_state_settings
    assert save_app_state_settings(_fake_app_state())

    assert settings_store.get_setting('display', 'language') == 'en', (
        "shutdown save clobbered display.language"
    )
    assert settings_store.get_setting('display', 'chat_font_size') == 16


def test_save_app_state_settings_preserves_stt_engine(tmp_path, monkeypatch):
    """audio カテゴリの丸ごと置換が STT エンジン選択を毎シャットダウンで
    Faster-whisper に戻していた実機バグ(2026-07-12)のロック。"""
    monkeypatch.setattr(settings_store, 'SETTINGS_FILE', tmp_path / 'user_settings.json')

    # The STT engine selector persists directly via settings_store...
    assert settings_store.update_setting('audio', 'stt_engine', 'openai')
    assert settings_store.update_setting('audio', 'stt_api_model', 'gpt-4o-mini-transcribe')
    assert settings_store.update_setting('audio', 'stt_local_model', 'small')

    # ...then the app shuts down and saves app_state-owned settings.
    from ui.settings_manager import save_app_state_settings
    assert save_app_state_settings(_fake_app_state())

    assert settings_store.get_setting('audio', 'stt_engine') == 'openai', (
        "shutdown save clobbered audio.stt_engine"
    )
    assert settings_store.get_setting('audio', 'stt_api_model') == 'gpt-4o-mini-transcribe'
    assert settings_store.get_setting('audio', 'stt_local_model') == 'small'
    assert settings_store.get_setting('audio', 'tts_volume') == 0.7  # app_state-owned key still saved


def test_save_retries_replace_on_permission_error(tmp_path, monkeypatch):
    """Windows: os.replace fails with PermissionError while any handle is open
    on the target (AV scan, indexer, a racing reader) — the write must retry,
    not silently vanish (実機 2026-07-17: STTモデル選択の保存がこれで消えた)。"""
    import os as os_mod

    monkeypatch.setattr(settings_store, 'SETTINGS_FILE', tmp_path / 'user_settings.json')

    real_replace = os_mod.replace
    fails = {'left': 3}

    def flaky_replace(src, dst):
        if fails['left'] > 0:
            fails['left'] -= 1
            raise PermissionError(5, 'アクセスが拒否されました。', src)
        return real_replace(src, dst)

    monkeypatch.setattr(settings_store.os, 'replace', flaky_replace)
    monkeypatch.setattr(settings_store.time, 'sleep', lambda s: None)  # test speed

    assert settings_store.update_setting('audio', 'stt_local_model', 'large-v2')
    assert fails['left'] == 0  # the flaky window was actually exercised
    assert settings_store.get_setting('audio', 'stt_local_model') == 'large-v2'


def test_concurrent_reads_do_not_break_saves(tmp_path, monkeypatch):
    """読み(load_settings)と書き(update_setting)を並行連打しても書きが
    失われない(load をロック外で行っていた頃の Windows 実機競合のロック)。"""
    import threading

    monkeypatch.setattr(settings_store, 'SETTINGS_FILE', tmp_path / 'user_settings.json')

    stop = threading.Event()
    read_errors = []

    def reader():
        while not stop.is_set():
            try:
                settings_store.load_settings()
            except Exception as e:  # pragma: no cover - failure evidence
                read_errors.append(e)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for th in threads:
        th.start()
    try:
        results = [settings_store.update_setting('audio', 'stt_local_model', size)
                   for size in ('tiny', 'base', 'small', 'medium', 'large-v2') * 4]
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5)

    assert all(results), "a write was lost under concurrent reads"
    assert not read_errors
    assert settings_store.get_setting('audio', 'stt_local_model') == 'large-v2'
