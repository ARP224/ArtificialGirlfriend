"""
tests/smoke/test_session_release_paths.py

セッション解放経路のスモーク。

- reason="client_close"（ページJSの意図的 close(1000)）は即時解放
- reason="disconnect"（ネットワーク断・close code 不明）はグレース入り
- StarletteWSAdapter が websocket.disconnect の close code を捕捉する
  （recv / __anext__ の両経路。実ハンドラは async for = __anext__ を通る）
"""

import asyncio

import pytest
import websockets.exceptions

from backend.server.session_manager import SessionManager
from backend.server.ws_starlette_adapter import StarletteWSAdapter


@pytest.fixture
def sm(monkeypatch):
    """サーバーモードの SessionManager（タイムアウトスレッド・whois なし）。"""
    monkeypatch.setattr(
        SessionManager, "_resolve_device_name_async",
        lambda self, token, ip: None,
    )
    manager = SessionManager()
    manager._server_mode = True  # configure() はスレッドを起こすので直接セット
    return manager


def test_client_close_releases_immediately(sm):
    ws = object()
    ok, mode, _token = sm.try_claim_session(ws, "100.64.0.1", "desktop", None)
    assert ok and mode == "primary"

    sm.release_session(websocket=ws, reason="client_close")

    assert not sm.has_primary_session()


def test_network_disconnect_enters_grace(sm):
    ws = object()
    ok, _mode, _token = sm.try_claim_session(ws, "100.64.0.1", "desktop", None)
    assert ok

    sm.release_session(websocket=ws, reason="disconnect")

    # グレース中: セッションは維持・pending_reconnect が立つ
    assert sm.has_primary_session()
    assert sm.get_status()["session"]["pending_reconnect"] is True


class _FakeStarletteWS:
    def __init__(self, messages):
        self._msgs = list(messages)

    async def receive(self):
        return self._msgs.pop(0)


def test_starlette_adapter_captures_close_code():
    async def scenario():
        # recv 経路
        a = StarletteWSAdapter(
            _FakeStarletteWS([{"type": "websocket.disconnect", "code": 1000}])
        )
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await a.recv()
        assert a.close_code == 1000

        # __anext__ 経路（実ハンドラのメッセージループはこちら）
        b = StarletteWSAdapter(_FakeStarletteWS([
            {"type": "websocket.message", "text": "hello"},
            {"type": "websocket.disconnect", "code": 1001},
        ]))
        assert await b.__anext__() == "hello"
        with pytest.raises(StopAsyncIteration):
            await b.__anext__()
        assert b.close_code == 1001

    asyncio.run(scenario())
