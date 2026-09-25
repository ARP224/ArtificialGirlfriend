"""
tests/smoke/test_local_takeover.py

ST-F: ローカルモード「後勝ち」セッション制御のスモーク。
実 WebSocketManager をローカルモードで起動し、desktop クライアント2本で
「新規が生存・旧が force_disconnected + close 4004」を検証する。
サーバーモードの裁定 (session_manager) はこの経路を通らない＝不変。
"""

import asyncio
import json

import websockets

from backend.server.websocket_server import WebSocketManager


async def _identify(ws, client_type="desktop"):
    """welcome 等を読み飛ばして identify → identify_response を返す。"""
    await ws.send(json.dumps({"type": "identify", "client_type": client_type}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if msg.get("type") == "identify_response":
            return msg


def test_local_desktop_takeover():
    manager = WebSocketManager(server_mode=False)
    port = manager.start()
    assert port, "WS server failed to start"

    async def scenario():
        uri = f"ws://localhost:{port}"

        ws1 = await websockets.connect(uri)
        resp1 = await _identify(ws1)
        assert resp1["status"] == "accepted"

        # 2本目の desktop が後勝ちする
        ws2 = await websockets.connect(uri)
        resp2 = await _identify(ws2)
        assert resp2["status"] == "accepted"

        # 旧タブ: force_disconnected 受信 → 4004 で閉じられる
        got_force_disconnect = False
        close_code = None
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
                if msg.get("type") == "force_disconnected":
                    got_force_disconnect = True
        except websockets.exceptions.ConnectionClosed as e:
            close_code = e.rcvd.code if e.rcvd else None

        assert got_force_disconnect
        assert close_code == 4004

        # 新タブは生存・desktop は常に1接続
        await asyncio.sleep(0.3)
        assert manager.get_client_counts().get("desktop") == 1

        # リロード相当（正常クローズ→再接続）は素通り
        await ws2.close()
        await asyncio.sleep(0.3)
        assert manager.get_client_counts().get("desktop", 0) == 0
        ws3 = await websockets.connect(uri)
        resp3 = await _identify(ws3)
        assert resp3["status"] == "accepted"
        await ws3.close()

    try:
        asyncio.run(scenario())
    finally:
        manager.stop()
