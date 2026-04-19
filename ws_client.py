#!/usr/bin/env python3
"""
NocEngine WebSocket Client
===========================
Manages a persistent WebSocket connection to the NOC server.

Features:
  - connect / disconnect
  - send JSON messages
  - receive JSON messages
  - reports connection state
"""

import json
import logging
import ssl

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)


class WSClient:
    """
    Thin WebSocket wrapper used by NocEngine.

    Usage:
        client = WSClient("ws://server:8080/ws/CHARGER-001")
        await client.connect()
        await client.send({"type": "heartbeat", ...})
        msg = await client.receive()
        await client.disconnect()
    """

    def __init__(self, uri: str, ssl_context=None):
        self.uri = uri
        self._ssl = ssl_context
        self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection state
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        """Open WebSocket connection to the NOC server."""
        kwargs = dict(
            ping_interval=20,       # Keep-alive pings every 20s
            ping_timeout=10,        # Fail connection if no pong within 10s
            close_timeout=5,
            max_size=2 * 1024 * 1024,  # 2 MB max message size
        )
        if self._ssl is not None:
            kwargs["ssl"] = self._ssl

        self._ws = await websockets.connect(self.uri, **kwargs)
        self._connected = True
        logger.info(f"[WS-Client] Connected to {self.uri}")

    async def disconnect(self):
        """Close the WebSocket connection gracefully."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected = False
        self._ws = None
        logger.info(f"[WS-Client] Disconnected from {self.uri}")

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def send(self, message: dict):
        """
        Serialize and send a JSON message.

        Raises:
            ConnectionError: If not connected.
            ConnectionClosed: If connection dropped mid-send.
        """
        if not self.connected:
            raise ConnectionError("WebSocket not connected")
        await self._ws.send(json.dumps(message))

    async def receive(self) -> dict:
        """
        Receive the next JSON message.

        Raises:
            ConnectionError: If not connected.
            ConnectionClosed: If connection dropped.
        """
        if not self.connected:
            raise ConnectionError("WebSocket not connected")
        raw = await self._ws.recv()
        return json.loads(raw)
