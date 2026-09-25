"""
backend/server/ws_starlette_adapter.py

Starlette/FastAPI WebSocket transport adapter (B12 split from websocket_server).

Wraps a Starlette ``WebSocket`` so the single ``WebSocketManager.handler()`` can
drive both ``websockets.serve()`` clients (local mode) and FastAPI ``/ws``
endpoint clients (server mode) through the same interface. Depends only on the
``websockets`` library for its ConnectionClosed sentinel — no backend/ui imports.
"""

import websockets


class StarletteWSAdapter:
    """Wraps a Starlette WebSocket to match the websockets library interface.

    Allows the same handler() to work with both websockets.serve() clients
    (local mode) and FastAPI WebSocket endpoint clients (server mode).
    """

    def __init__(self, starlette_ws):
        self._ws = starlette_ws
        self._closed = False
        # Client-sent close code, captured from the websocket.disconnect message.
        # Mirrors the websockets library's protocol attribute of the same name so
        # handler() can read it uniformly in both modes (1000 = intentional close
        # from the page JS; None = never received a disconnect message).
        self.close_code = None

    @property
    def remote_address(self):
        if self._ws.client:
            return (self._ws.client.host, self._ws.client.port)
        return ("unknown", 0)

    async def send(self, data: str):
        try:
            await self._ws.send_text(data)
        except Exception:
            raise websockets.exceptions.ConnectionClosed(None, None)

    async def recv(self):
        """Receive the next message (text as str, binary as bytes)."""
        try:
            msg = await self._ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.close_code = msg.get("code")
                raise websockets.exceptions.ConnectionClosed(None, None)
            if "text" in msg:
                return msg["text"]    # str
            if "bytes" in msg:
                return msg["bytes"]   # bytes
            raise websockets.exceptions.ConnectionClosed(None, None)
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception:
            raise websockets.exceptions.ConnectionClosed(None, None)

    async def close(self, code=1000, reason=""):
        if not self._closed:
            self._closed = True
            try:
                await self._ws.close(code)
            except Exception:
                pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        """Yield the next message (text as str, binary as bytes)."""
        try:
            msg = await self._ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.close_code = msg.get("code")
                raise StopAsyncIteration
            if "text" in msg:
                return msg["text"]
            if "bytes" in msg:
                return msg["bytes"]
            raise StopAsyncIteration
        except StopAsyncIteration:
            raise
        except Exception:
            raise StopAsyncIteration
