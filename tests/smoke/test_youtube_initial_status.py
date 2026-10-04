"""
tests/smoke/test_youtube_initial_status.py

The WS welcome sequence must carry the current YouTube reply status.
Without it the YouTube tab keeps its placeholder ("状態取得中...") after every
page load until the next scheduler tick — up to 60 seconds (実機 2026-10-04).
ELYTH has had the same initial push since its status display was added.
"""

import asyncio
import json
from types import SimpleNamespace

import websockets

from backend.server.websocket_server import WebSocketManager
from backend.youtube import youtube_session_manager as ysm_mod


def test_welcome_sequence_includes_youtube_status(monkeypatch):
    fake_manager = SimpleNamespace(
        get_status=lambda event="status": {"event": event, "state": "paused"})
    monkeypatch.setattr(ysm_mod, "get_youtube_session_manager",
                        lambda state=None: fake_manager)

    manager = WebSocketManager(server_mode=False)
    port = manager.start()
    assert port, "WS server failed to start"

    async def scenario():
        ws = await websockets.connect(f"ws://localhost:{port}")
        try:
            # Sent on connect, before identify — read until it shows up.
            for _ in range(5):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if msg.get("action") == "youtube_status":
                    return msg
        finally:
            await ws.close()
        return None

    try:
        try:
            msg = asyncio.run(scenario())
        except asyncio.TimeoutError:
            msg = None
    finally:
        manager.stop()

    assert msg is not None, "no youtube_status in the welcome sequence"
    assert msg["data"] == {"event": "initial", "state": "paused"}
