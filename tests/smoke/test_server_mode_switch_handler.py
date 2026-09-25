"""
tests/smoke/test_server_mode_switch_handler.py

System ページ「サーバーモード」ハンドラ(ジェネレータ)の検証:
  - 準備中 → 証明書取得の試行イベント(1/3, 2/3...)がステータス欄HTMLとして
    順次 yield され、最終結果(エラー/成功)で閉じること
  - 失敗時は execute_shutdown が呼ばれない・成功時は呼ばれること
言語非依存のアサーション({attempt}/{total} の数字・CSSクラス・注入した
エラー文字列)のみを使う=ja/en どちらの実行環境でも緑。
"""

import threading

from ui.handlers.server_mode import make_switch_to_server_handler

_EV = {"total": 3, "wait_sec": 5}


def _fake_prepare_failing(progress=None):
    progress({"phase": "attempt", "attempt": 1, **_EV})
    progress({"phase": "retry_wait", "attempt": 1, **_EV})
    progress({"phase": "attempt", "attempt": 2, **_EV})
    return False, "SSL証明書の取得に失敗しました: boom"


def test_failure_stream_shows_attempts_then_error(monkeypatch):
    monkeypatch.setattr(
        "backend.server.mode_switch.prepare_switch_to_server", _fake_prepare_failing
    )
    shutdown_calls = []
    handler = make_switch_to_server_handler(lambda *a: shutdown_calls.append(a))

    outs = list(handler())

    # 準備中 → 試行1 → リトライ待ち(黄+カウントダウンspan) → 試行2 → 確定エラー(赤)
    assert len(outs) == 5
    assert "1/3" in outs[1]
    assert "1/3" in outs[2] and "#ff9800" in outs[2]
    assert "ag-countdown" in outs[2]
    assert "2/3" in outs[3]
    assert "SSL証明書の取得に失敗しました: boom" in outs[-1]
    assert "shutdown-initiated" not in outs[-1]
    assert shutdown_calls == []


def test_success_stream_starts_shutdown(monkeypatch):
    monkeypatch.setattr(
        "backend.server.mode_switch.prepare_switch_to_server",
        lambda progress=None: (True, ""),
    )
    shutdown_calls = []
    handler = make_switch_to_server_handler(lambda flag: shutdown_calls.append(flag))

    outs = list(handler())

    # 再起動スレッドの完了を待ってから検証(handler は起動するだけ)
    for th in threading.enumerate():
        if th.name == "switch-to-server":
            th.join(timeout=5)
    assert shutdown_calls == [True]
    # 準備中 → 成功div(既存の .then js が拾う .shutdown-initiated を含む)
    assert len(outs) == 2
    assert "shutdown-initiated" in outs[-1]


def test_prepare_crash_yields_error_div(monkeypatch):
    def _boom(progress=None):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("backend.server.mode_switch.prepare_switch_to_server", _boom)
    shutdown_calls = []
    handler = make_switch_to_server_handler(lambda *a: shutdown_calls.append(a))

    outs = list(handler())

    assert "unexpected" in outs[-1]
    assert "shutdown-initiated" not in outs[-1]
    assert shutdown_calls == []
