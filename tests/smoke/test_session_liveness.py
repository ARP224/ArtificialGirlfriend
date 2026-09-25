"""
tests/smoke/test_session_liveness.py

セッション死活監視（server→client ping / pong）のスモーク。
awake時計を注入して _tick() を直接駆動する（スレッド・sleepなし）。

- 45秒無音でグレース入り（即時解放ではない）・44秒では維持
- pong相当（note_ws_alive）が入れば無音時計はリセットされる
- グレース中に同一tokenで再接続すると復帰する
- 復帰しないままグレース60秒満了で完全解放
"""

import pytest

import backend.server.session_manager as smod
from backend.server.session_manager import SessionManager


@pytest.fixture
def clock(monkeypatch):
    """session_manager モジュールの awake_seconds を凍結時計に差し替える。"""
    state = {"now": 10_000.0}
    monkeypatch.setattr(smod, "awake_seconds", lambda: state["now"])
    return state


@pytest.fixture
def sm(monkeypatch):
    monkeypatch.setattr(
        SessionManager, "_resolve_device_name_async",
        lambda self, token, ip: None,
    )
    manager = SessionManager()
    manager._server_mode = True  # configure() はスレッドを起こすので直接セット
    return manager


def _primary(sm):
    return sm.get_status()["session"]


def test_liveness_silence_enters_grace_then_resumes(sm, clock):
    ws = object()
    ok, mode, token = sm.try_claim_session(ws, "100.64.0.2", "mobile", None)
    assert ok and mode == "primary" and token

    # 44秒無音: 閾値未満 → 維持
    clock["now"] += 44
    sm._tick()
    st = _primary(sm)
    assert st["active"] and st["pending_reconnect"] is False

    # pong が届いた → 無音時計リセット → さらに44秒でも維持
    sm.note_ws_alive(ws)
    clock["now"] += 44
    sm._tick()
    st = _primary(sm)
    assert st["active"] and st["pending_reconnect"] is False

    # 45秒無音: グレース入り（即時解放ではない）
    clock["now"] += 1
    sm._tick()
    st = _primary(sm)
    assert st["active"] and st["pending_reconnect"] is True

    # グレース中に同一tokenで再接続 → 復帰
    ws2 = object()
    ok2, mode2, token2 = sm.try_claim_session(ws2, "100.64.0.2", "mobile", token)
    assert ok2 and mode2 == "primary" and token2 == token
    st = _primary(sm)
    assert st["active"] and st["pending_reconnect"] is False


def test_liveness_grace_expiry_fully_releases(sm, clock):
    ws = object()
    ok, _mode, _token = sm.try_claim_session(ws, "100.64.0.3", "mobile", None)
    assert ok

    # 死活切れ → グレース入り
    clock["now"] += smod.LIVENESS_TIMEOUT_SECONDS
    sm._tick()
    assert _primary(sm)["pending_reconnect"] is True

    # グレース満了 → 完全解放
    clock["now"] += smod.GRACE_PERIOD_SECONDS
    sm._tick()
    assert not sm.has_primary_session()
