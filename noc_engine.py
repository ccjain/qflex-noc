#!/usr/bin/env python3
"""
NocEngine Core
==============
Main orchestrator. Manages the WebSocket connection to the NOC server,
drives telemetry pushes and heartbeats, and dispatches incoming commands.

Architecture (asyncio tasks running concurrently on one connection):
   ┌─────────────────────────────────────┐
   │  NocEngine.run()                    │
   │    ├── _receive_loop()  ← server    │
   │    ├── _telemetry_loop() → server   │
   │    └── _heartbeat_loop() → server   │
   └─────────────────────────────────────┘
   If any loop exits (connection lost), all are cancelled and
   the engine reconnects with exponential backoff.
"""

import asyncio
import json
import logging
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path

from websockets.exceptions import ConnectionClosed, WebSocketException

from ws_client import WSClient, WebSocketException
from telemetry_collector import collect as collect_telemetry
from command_executor import execute as execute_command
from ssh_tunnel import SSHTunnelManager

# Import session sync - will be initialized if available
try:
    from session_sync import SessionSyncManager
    _SESSION_SYNC_AVAILABLE = True
except ImportError:
    _SESSION_SYNC_AVAILABLE = False
    logger = logging.getLogger(__name__)

# Store active chunked uploads for large file transfers
# Key: upload_id, Value: {chunks: dict, total_chunks: int, file_name: str, target: str}
_chunked_uploads: dict = {}

logger = logging.getLogger(__name__)


class NocEngine:
    """
    Core NOC engine class.

    Args:
        config: Parsed config.json dict
    """

    def __init__(self, config: dict, charger_id_cache_file: str | None = None):
        # Identity
        self.charger_id       = config.get("charger_id", "UNKNOWN-CHARGER")
        self.firmware_version = config.get("firmware_version", "unknown")
        self.model            = config.get("model", "QFlex")

        # NOC server connection
        noc_cfg = config.get("noc_server", {})
        noc_url = noc_cfg.get("url")
        if noc_url:
            self.noc_host = noc_url
            self.noc_port = 443 if noc_url.startswith("wss://") else 80
            self.ws_uri   = f"{noc_url.rstrip('/')}/ws/{self.charger_id}"
        else:
            self.noc_host = noc_cfg.get("host", "localhost")
            self.noc_port = noc_cfg.get("port", 8080)
            scheme        = "wss" if self.noc_port == 443 else "ws"
            self.ws_uri   = f"{scheme}://{self.noc_host}:{self.noc_port}/ws/{self.charger_id}"

        # Intervals
        self.telemetry_interval  = config.get("telemetry", {}).get("interval_seconds", 30)
        self.heartbeat_interval  = config.get("heartbeat", {}).get("interval_seconds", 10)
        self.max_reconnect_delay = config.get("reconnect", {}).get("max_delay_seconds", 60)

        # Charger IP — 'localhost' when ON the charger, real IP for remote testing
        self.charger_ip = config.get("charger_ip", "localhost")

        # Service ports (rarely need changing)
        ports    = config.get("charger_ports", {})
        cc_port  = ports.get("charging_controller", 8003)
        ae_port  = ports.get("allocation_engine",   8002)
        eg_port  = ports.get("error_generation",    8006)
        sys_port = ports.get("system_api",          8000)

        # Build telemetry API URLs from charger_ip + ports
        base_cc  = f"http://{self.charger_ip}:{cc_port}"
        base_ae  = f"http://{self.charger_ip}:{ae_port}"
        base_eg  = f"http://{self.charger_ip}:{eg_port}"
        base_sys = f"http://{self.charger_ip}:{sys_port}"
        self.api_urls = {
            "charging_status":   base_cc  + "/api/v1/charging/status",
            "allocation_status": base_ae  + "/api/state/with_history",
            "network_status":    base_sys + "/api/v1/system/hardware",
            "active_errors":     base_eg  + "/error-generator/errors/pending",
            "session_1":         base_cc  + "/api/v1/session_details/1",
            "session_2":         base_cc  + "/api/v1/session_details/2",
        }
        self.version_url = base_cc + "/api/v1/system/version"

        # Charger ID refresh URLs and cache file
        self._ocpp_serial_url = (
            f"http://{self.charger_ip}:{cc_port}"
            f"/api/v1/config/ocpp/charge_box_serial_number"
        )
        self._hw_serial_url = f"http://{self.charger_ip}:{sys_port}/api/v1/system/hardware"
        self._charger_id_cache_file = Path(charger_id_cache_file) if charger_id_cache_file else Path(__file__).parent / "charger_id_cache.json"

        self._running = False
        
        # SSH Tunnel manager for remote SSH access
        self._ssh_manager = SSHTunnelManager()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ssl_context(self):
        """Return an SSL context for WSS connections."""
        if not self.ws_uri.startswith("wss://"):
            return None
        if os.environ.get("NOC_SKIP_SSL_VERIFY", "").lower() in ("1", "true", "yes"):
            logger.warning("[NOC-Engine] SSL certificate verification is DISABLED (NOC_SKIP_SSL_VERIFY=1)")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        ctx = ssl.create_default_context()
        ca_file = Path(__file__).with_name("certs").joinpath("AmazonRootCA1.pem")
        if ca_file.exists():
            ctx.load_verify_locations(str(ca_file))
            logger.info(f"[NOC-Engine] Loaded root CA: {ca_file}")
        else:
            logger.warning(f"[NOC-Engine] Root CA not found at {ca_file} — using system defaults")
        return ctx

    def _make_msg(self, msg_type: str, payload: dict) -> dict:
        return {
            "type":       msg_type,
            "charger_id": self.charger_id,
            "timestamp":  self._now_iso(),
            "payload":    payload,
        }

    # ------------------------------------------------------------------
    # Message senders
    # ------------------------------------------------------------------

    async def _send_auth(self, ws: WSClient):
        msg = self._make_msg("auth", {
            "charger_id":       self.charger_id,
            "firmware_version": self.firmware_version,
            "model":            self.model,
        })
        await ws.send(msg)
        logger.info(f"[NOC-Engine] Auth → charger_id={self.charger_id} fw={self.firmware_version}")

    async def _version_poll_loop(self, ws: WSClient):
        """
        Poll :8003/api/v1/system/version five times at 10-second intervals
        immediately after connecting, then stop.

        Each result is sent to the NOC server as a 'version_info' message.
        This gives the server a snapshot of all service versions on startup.
        """
        import aiohttp

        POLLS      = 5
        INTERVAL_S = 10

        logger.info(
            f"[NOC-Engine] 🔖 Starting version poll "
            f"({POLLS}x every {INTERVAL_S}s) from {self.version_url}"
        )

        for poll_num in range(1, POLLS + 1):
            if not ws.connected:
                logger.debug("[NOC-Engine] Version poll stopped — WS disconnected")
                return

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        self.version_url,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            logger.info(
                                f"[NOC-Engine] 🔖 Version poll {poll_num}/{POLLS}: "
                                f"{data.get('versions', {})}"
                            )
                            msg = self._make_msg("version_info", {
                                "poll":     poll_num,
                                "of":       POLLS,
                                "versions": data.get("versions", {}),
                                "source":   self.version_url,
                            })
                            await ws.send(msg)
                        else:
                            logger.warning(
                                f"[NOC-Engine] 🔖 Version poll {poll_num}/{POLLS}: "
                                f"HTTP {resp.status}"
                            )
            except Exception as e:
                logger.warning(
                    f"[NOC-Engine] 🔖 Version poll {poll_num}/{POLLS} failed: {e}"
                )

            # Wait before next poll (skip wait after the last one)
            if poll_num < POLLS:
                await asyncio.sleep(INTERVAL_S)

        logger.info(f"[NOC-Engine] 🔖 Version polling complete ({POLLS} polls done)")

    # ------------------------------------------------------------------
    # Concurrent loops (run inside one WS connection)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws: WSClient):
        """Send heartbeat every N seconds to keep the connection alive."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            if not ws.connected:
                break
            try:
                await ws.send(self._make_msg("heartbeat", {}))
                logger.debug("[NOC-Engine] ♥ Heartbeat sent")
            except Exception as e:
                logger.warning(f"[NOC-Engine] Heartbeat failed: {e}")
                break

    async def _telemetry_loop(self, ws: WSClient):
        """Collect and push charger telemetry every N seconds."""
        while True:
            await asyncio.sleep(self.telemetry_interval)
            if not ws.connected:
                break
            try:
                payload = await collect_telemetry(self.api_urls)
                await ws.send(self._make_msg("telemetry", payload))
                logger.info("[NOC-Engine] 📡 Telemetry sent")
            except Exception as e:
                logger.warning(f"[NOC-Engine] Telemetry push failed: {e}")
                break

    async def _charger_id_refresh_loop(self, ws: WSClient):
        """Poll local APIs every 10s to refresh charger ID and update cache."""
        import aiohttp

        REFRESH_INTERVAL = 10

        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            if not ws.connected:
                break

            ocpp_serial: str | None = None
            hw_serial: str | None = None

            try:
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.get(
                            self._ocpp_serial_url,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                serial = data.get("value", "").replace("\x00", "").strip()
                                if data.get("success") and serial:
                                    ocpp_serial = serial
                    except Exception as e:
                        logger.debug(
                            f"[NOC-Engine] Charger ID refresh: OCPP fetch failed: {e}"
                        )

                    try:
                        async with session.get(
                            self._hw_serial_url,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                serial = str(data.get("serial_number", "")).replace("\x00", "").strip()
                                if data.get("success") and serial:
                                    hw_serial = serial
                    except Exception as e:
                        logger.debug(
                            f"[NOC-Engine] Charger ID refresh: HW fetch failed: {e}"
                        )
            except Exception as e:
                logger.debug(f"[NOC-Engine] Charger ID refresh error: {e}")

            if ocpp_serial and hw_serial:
                # Save to cache
                try:
                    with open(self._charger_id_cache_file, "w") as f:
                        json.dump(
                            {
                                "ocpp_serial": ocpp_serial,
                                "hw_serial": hw_serial,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                            f,
                        )
                except Exception as e:
                    logger.warning(
                        f"[NOC-Engine] Failed to save charger ID cache: {e}"
                    )

                new_charger_id = f"{ocpp_serial}-{hw_serial}"
                if new_charger_id != self.charger_id:
                    logger.info(
                        f"[NOC-Engine] Charger ID updated: {self.charger_id} → {new_charger_id}"
                    )
                    self.charger_id = new_charger_id
                    # Rebuild WS URI so next reconnect uses the new ID
                    if self.ws_uri.startswith("wss://"):
                        base = self.ws_uri.rsplit("/", 1)[0]
                        self.ws_uri = f"{base}/{self.charger_id}"
                    else:
                        base = self.ws_uri.rsplit("/", 1)[0]
                        self.ws_uri = f"{base}/{self.charger_id}"

    async def _session_sync_loop(self, ws: WSClient):
        """Collect and push session data every 30 seconds."""
        if not _SESSION_SYNC_AVAILABLE:
            logger.debug("[NOC-Engine] Session sync not available, skipping loop")
            return
        
        # Initialize session sync manager
        session_sync = SessionSyncManager(
            noc_ws_client=ws,
            charger_id=self.charger_id,
            local_api_base=f"http://{self.charger_ip}:8003",
            poll_interval=30,
        )
        
        await session_sync.start()
        logger.info("[NOC-Engine] 📝 Session sync started")
        
        try:
            # Keep running until connection closes
            while ws.connected:
                await asyncio.sleep(1)
        finally:
            # Ensure session sync background task doesn't leak on cancellation
            await session_sync.stop()
            logger.info("[NOC-Engine] 📝 Session sync stopped")

    async def _receive_loop(self, ws: WSClient):
        """
        Receive messages from NOC server and dispatch them.
        Handles:
          - 'command'  → execute proxy command, return result
          - 'ack'      → log server acknowledgement
          - others     → log and ignore
        """
        while True:
            try:
                message = await ws.receive()
                msg_type = message.get("type", "UNKNOWN")

                # Log EVERYTHING we receive at INFO level for debugging
                logger.info(f"[NOC-Engine] 📥 INCOMING WS MESSAGE | type={msg_type} | keys={list(message.keys())}")

                if msg_type == "command":
                    # Dispatch to a separate task so we don't block receive loop
                    payload = message.get("payload", {})
                    cmd_id = payload.get("command_id", "?")
                    logger.info(f"[NOC-Engine] ⚡ Dispatching command task for cmd={cmd_id}")
                    asyncio.create_task(self._handle_command(ws, message))

                elif msg_type == "proxy_request":
                    # Handle web tool proxy request (synchronous response)
                    payload = message.get("payload", {})
                    request_id = payload.get("request_id", "?")
                    logger.info(f"[NOC-Engine] 🔀 Proxy request {request_id}: {payload.get('method')} {payload.get('path')}")
                    asyncio.create_task(self._handle_proxy_request(ws, message))

                elif msg_type == "upload_chunk":
                    # Handle chunked file upload from NOC Server
                    payload = message.get("payload", {})
                    upload_id = payload.get("upload_id", "?")
                    chunk_index = payload.get("chunk_index", 0)
                    total_chunks = payload.get("total_chunks", 0)
                    logger.info(f"[NOC-Engine] 📦 Upload chunk {chunk_index + 1}/{total_chunks} for {upload_id}")
                    asyncio.create_task(self._handle_upload_chunk(ws, message))

                elif msg_type == "ssh_tunnel_open":
                    # Handle SSH tunnel open request from NOC Server
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.info(f"[NOC-Engine] 🔐 SSH tunnel open request: {tunnel_id}")
                    asyncio.create_task(self._handle_ssh_tunnel_open(ws, message))
                
                elif msg_type == "ssh_data":
                    # Handle SSH data from client (via NOC Server)
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.debug(f"[NOC-Engine] 🔐 SSH data for tunnel: {tunnel_id}")
                    asyncio.create_task(self._handle_ssh_data(ws, message))
                
                elif msg_type == "ssh_tunnel_close":
                    # Handle SSH tunnel close request
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.info(f"[NOC-Engine] 🔐 SSH tunnel close request: {tunnel_id}")
                    asyncio.create_task(self._handle_ssh_tunnel_close(ws, message))

                elif msg_type == "ack":
                    logger.info(
                        f"[NOC-Engine] ✅ Server ACK: "
                        f"{message.get('payload', {}).get('message', '')}"
                    )
                else:
                    logger.info(f"[NOC-Engine] ⚠️ Ignored message type={msg_type!r}")

            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"[NOC-Engine] WS closed in receive loop: {e}")
                break
            except Exception as e:
                logger.error(f"[NOC-Engine] Receive error: {e}", exc_info=True)
                break

    async def _handle_command(self, ws: WSClient, message: dict):
        """Execute a proxy command, then push the result back to the server."""
        command = message.get("payload", {})
        cmd_id  = command.get("command_id", "?")
        logger.info(
            f"[NOC-Engine] ⚡ Command received: "
            f"{command.get('method')} port={command.get('target_port')} "
            f"path={command.get('path')} → target={self.charger_ip}"
        )
        try:
            # Pass charger_ip so executor targets the right host
            result     = await execute_command(command, charger_ip=self.charger_ip)
            result_msg = self._make_msg("command_result", result)
            await ws.send(result_msg)
            logger.info(
                f"[NOC-Engine] ✔ Command result sent: "
                f"cmd={cmd_id} status={result.get('status_code')}"
            )
        except Exception as e:
            logger.error(f"[NOC-Engine] Failed to send command result for cmd={cmd_id}: {e}")

    # ------------------------------------------------------------------
    # SSH Tunnel Handlers
    # ------------------------------------------------------------------

    async def _handle_ssh_tunnel_open(self, ws: WSClient, message: dict):
        """
        Handle SSH tunnel open request from NOC Server.
        Creates TCP connection to localhost:22 and starts forwarding.
        """
        payload = message.get("payload", {})
        tunnel_id = payload.get("tunnel_id")
        target_host = payload.get("target_host", "localhost")
        target_port = payload.get("target_port", 22)

        if not tunnel_id:
            logger.error("[NOC-Engine] 🔐 SSH tunnel open: missing tunnel_id")
            logger.error(f"[NOC-Engine] 🔐 Full message: {message}")
            return

        logger.info(
            f"[NOC-Engine] 🔐 Opening SSH tunnel {tunnel_id} to "
            f"{target_host}:{target_port}"
        )
        logger.debug(f"[NOC-Engine] 🔐 Full payload: {payload}")
        logger.debug(f"[NOC-Engine] 🔐 WS connected: {ws.connected}")
        
        # Create callback for sending SSH data back to server
        async def send_to_server(msg: dict):
            # Add charger_id and timestamp
            msg["charger_id"] = self.charger_id
            msg["timestamp"] = self._now_iso()
            await ws.send(msg)
        
        # Open the tunnel
        logger.info(f"[NOC-Engine] 🔐 Calling ssh_manager.open_tunnel for {tunnel_id}")
        success = await self._ssh_manager.open_tunnel(
            tunnel_id=tunnel_id,
            ws_send_callback=send_to_server,
            target_host=target_host,
            target_port=target_port
        )

        # Send acknowledgment back to server
        ack_payload = {
            "tunnel_id": tunnel_id,
            "status": "ready" if success else "failed",
        }
        if not success:
            ack_payload["error"] = f"Failed to connect to SSHD at {target_host}:{target_port}"
            logger.error(f"[NOC-Engine] 🔐 Tunnel {tunnel_id} failed to open - SSHD connection failed")
        else:
            # Include SSH server version if we can detect it
            ack_payload["ssh_server_version"] = "SSH-2.0-OpenSSH_8.9"
            logger.info(f"[NOC-Engine] 🔐 Tunnel {tunnel_id} opened successfully")
        
        ack_msg = self._make_msg("ssh_tunnel_ack", ack_payload)
        
        try:
            await ws.send(ack_msg)
            logger.info(
                f"[NOC-Engine] 🔐 SSH tunnel {tunnel_id} ack sent: "
                f"status={'ready' if success else 'failed'}"
            )
        except Exception as e:
            logger.error(f"[NOC-Engine] 🔐 Failed to send SSH tunnel ack: {e}")
            # Clean up tunnel if we can't communicate
            if success:
                await self._ssh_manager.close_tunnel(tunnel_id, reason="comm_error")

    async def _handle_ssh_data(self, ws: WSClient, message: dict):
        """
        Handle SSH data from client (via NOC Server).
        Forward to local SSHD.
        """
        payload = message.get("payload", {})
        tunnel_id = payload.get("tunnel_id")
        data_b64 = payload.get("data")
        
        if not tunnel_id or not data_b64:
            logger.warning("[NOC-Engine] 🔐 SSH data: missing tunnel_id or data")
            return

        # Log first data chunk for debugging
        import base64
        try:
            decoded = base64.b64decode(data_b64)
            logger.debug(f"[NOC-Engine] 🔐 SSH data from client for {tunnel_id}: {len(decoded)} bytes")
        except Exception as e:
            logger.warning(f"[NOC-Engine] 🔐 Failed to decode SSH data for logging: {e}")

        # Forward to SSHD
        success = await self._ssh_manager.forward_to_ssh(tunnel_id, data_b64)
        
        if not success:
            # Tunnel might be closed, notify server
            closed_msg = self._make_msg("ssh_tunnel_closed", {
                "tunnel_id": tunnel_id,
                "reason": "forward_failed",
            })
            try:
                await ws.send(closed_msg)
            except Exception as e:
                logger.error(f"[NOC-Engine] 🔐 Failed to send tunnel closed: {e}")

    async def _handle_ssh_tunnel_close(self, ws: WSClient, message: dict):
        """
        Handle SSH tunnel close request from NOC Server.
        Clean up the tunnel and report final statistics.
        """
        payload = message.get("payload", {})
        tunnel_id = payload.get("tunnel_id")
        reason = payload.get("reason", "client_request")
        
        if not tunnel_id:
            logger.warning("[NOC-Engine] 🔐 SSH tunnel close: missing tunnel_id")
            return
        
        # Close tunnel and get final stats
        stats = await self._ssh_manager.close_tunnel(tunnel_id, reason=reason)
        
        # Send closed notification with stats
        closed_payload = {
            "tunnel_id": tunnel_id,
            "reason": reason,
        }
        if stats:
            closed_payload.update({
                "bytes_tx": stats.get("bytes_tx", 0),
                "bytes_rx": stats.get("bytes_rx", 0),
                "duration_seconds": stats.get("duration_seconds", 0),
            })
        
        closed_msg = self._make_msg("ssh_tunnel_closed", closed_payload)
        
        try:
            await ws.send(closed_msg)
            logger.info(f"[NOC-Engine] 🔐 SSH tunnel {tunnel_id} closed notification sent")
        except Exception as e:
            logger.error(f"[NOC-Engine] 🔐 Failed to send tunnel closed: {e}")

    async def _handle_proxy_request(self, ws: WSClient, message: dict):
        """Handle web tool proxy request and return response immediately."""
        import aiohttp
        import base64
        
        payload = message.get("payload", {})
        request_id = payload.get("request_id", "?")
        method = payload.get("method", "GET")
        port = payload.get("port", 8003)
        path = payload.get("path", "/")
        body = payload.get("body")
        content_type = payload.get("content_type", "application/json")
        body_is_binary = payload.get("body_is_binary", False)
        
        # Build target URL
        target_url = f"http://{self.charger_ip}:{port}{path}"
        
        # Prepare request kwargs
        request_kwargs = {
            "method": method,
            "url": target_url,
            "timeout": aiohttp.ClientTimeout(total=60)  # Longer timeout for uploads
        }
        
        # Add body and headers for POST/PUT requests
        if body and method in ["POST", "PUT"]:
            if body_is_binary:
                # Decode base64 binary data
                decoded_body = base64.b64decode(body)
                request_kwargs["data"] = decoded_body
                request_kwargs["headers"] = {"Content-Type": content_type}
                logger.info(f"[NOC-Engine] Proxy binary POST: target={target_url}, content_type={content_type}, body_size={len(decoded_body)}")
                # Log first 200 bytes of body for debugging boundary issues
                logger.info(f"[NOC-Engine] Body preview: {decoded_body[:200]}")
            else:
                # Text data
                request_kwargs["data"] = body.encode('utf-8')
                request_kwargs["headers"] = {"Content-Type": content_type}
                logger.info(f"[NOC-Engine] Proxy text POST: target={target_url}, content_type={content_type}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(**request_kwargs) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except:
                        text = await resp.text()
                        data = {"raw_response": text}
                    
                    response_msg = self._make_msg("proxy_response", {
                        "request_id": request_id,
                        "success": True,
                        "status_code": resp.status,
                        "data": data,
                    })
                    await ws.send(response_msg)
                    logger.info(f"[NOC-Engine] 🔀 Proxy response sent: {request_id} (HTTP {resp.status})")
                    
        except Exception as e:
            error_msg = self._make_msg("proxy_response", {
                "request_id": request_id,
                "success": False,
                "error": str(e),
            })
            await ws.send(error_msg)
            logger.error(f"[NOC-Engine] 🔀 Proxy request failed: {request_id} - {e}")

    async def _handle_upload_chunk(self, ws: WSClient, message: dict):
        """Handle file upload chunk from NOC Server. Accumulates chunks and posts to port 8005."""
        import aiohttp
        import base64
        import uuid
        
        global _chunked_uploads
        
        payload = message.get("payload", {})
        upload_id = payload.get("upload_id")
        chunk_index = payload.get("chunk_index")
        total_chunks = payload.get("total_chunks")
        file_name = payload.get("file_name")
        target = payload.get("target")
        chunk_data_b64 = payload.get("chunk_data", "")
        is_last = payload.get("is_last", False)
        
        logger.info(f"[NOC-Engine] _handle_upload_chunk: upload_id={upload_id}, chunk={chunk_index}/{total_chunks}, target={target}, file={file_name}, is_last={is_last}")
        
        # Initialize upload storage on first chunk
        if upload_id not in _chunked_uploads:
            _chunked_uploads[upload_id] = {
                "chunks": {},
                "total_chunks": total_chunks,
                "file_name": file_name,
                "target": target,
            }
            logger.info(f"[NOC-Engine] Started chunked upload: {upload_id}, {total_chunks} chunks")
        
        upload = _chunked_uploads[upload_id]
        
        # Store chunk
        try:
            chunk_bytes = base64.b64decode(chunk_data_b64)
            upload["chunks"][chunk_index] = chunk_bytes
            logger.info(f"[NOC-Engine] Stored chunk {chunk_index + 1}/{total_chunks} for {upload_id}")
        except Exception as e:
            logger.error(f"[NOC-Engine] Failed to decode chunk {chunk_index}: {e}")
            error_msg = self._make_msg("chunked_upload_response", {
                "upload_id": upload_id,
                "success": False,
                "error": f"Invalid chunk data: {e}",
            })
            await ws.send(error_msg)
            _chunked_uploads.pop(upload_id, None)
            return
        
        # If not last chunk, just acknowledge (NOC Server waits for all chunks)
        if not is_last:
            return
        
        # Last chunk - verify all chunks received and post to port 8005
        try:
            if len(upload["chunks"]) != total_chunks:
                missing = set(range(total_chunks)) - set(upload["chunks"].keys())
                raise ValueError(f"Missing chunks: {missing}")
            
            # Reassemble file
            file_data = b"".join(upload["chunks"][i] for i in range(total_chunks))
            logger.info(f"[NOC-Engine] Reassembled file {file_name} ({len(file_data)} bytes), target={upload['target']}, posting to port 8005")
            
            # Post to port 8005 (dev-tools service)
            result = await self._post_file_to_devtools(upload["target"], upload["file_name"], file_data)
            
            # Send response back to NOC Server
            response_payload = {
                "upload_id": upload_id,
                "success": result["success"],
                "data": result.get("data", {}),
                "error": result.get("error", ""),
            }
            logger.info(f"[NOC-Engine] Sending chunked_upload_response: success={result['success']}, error={result.get('error', 'none')}")
            response_msg = self._make_msg("chunked_upload_response", response_payload)
            await ws.send(response_msg)
            logger.info(f"[NOC-Engine] Chunked upload complete: {upload_id}, success={result['success']}")
            
        except Exception as e:
            import traceback
            error_str = f"{type(e).__name__}: {str(e)}"
            logger.error(f"[NOC-Engine] Chunked upload failed: {error_str}")
            logger.error(f"[NOC-Engine] Traceback: {traceback.format_exc()}")
            error_msg = self._make_msg("chunked_upload_response", {
                "upload_id": upload_id,
                "success": False,
                "error": error_str or "Unknown error in chunked upload",
            })
            await ws.send(error_msg)
        finally:
            _chunked_uploads.pop(upload_id, None)

    async def _post_file_to_devtools(self, target: str, file_name: str, file_data: bytes) -> dict:
        """Post complete file to dev-tools service on port 8005."""
        import aiohttp
        import uuid
        
        url = f"http://{self.charger_ip}:8005/api/v1/devtools/upload/{target}"
        logger.info(f"[NOC-Engine] Posting to {url}, file={file_name}, size={len(file_data)} bytes")
        
        # Use aiohttp's FormData for proper multipart encoding
        form = aiohttp.FormData()
        form.add_field('file', file_data, filename=file_name, content_type='application/octet-stream')
        
        try:
            async with aiohttp.ClientSession() as session:
                logger.info(f"[NOC-Engine] Sending POST request to port 8005 using FormData...")
                async with session.post(
                    url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    logger.info(f"[NOC-Engine] Got response from port 8005: status={resp.status}")
                    response_text = await resp.text()
                    logger.info(f"[NOC-Engine] Response body length: {len(response_text)}, content_type={resp.content_type}")
                    logger.info(f"[NOC-Engine] Response body preview: {response_text[:1000] if response_text else '(empty)'}")
                    
                    # Parse JSON if possible
                    data = None
                    if resp.content_type == 'application/json' or response_text.strip().startswith('{'):
                        try:
                            data = json.loads(response_text)
                            logger.info(f"[NOC-Engine] Parsed JSON response: {data}")
                        except Exception as json_err:
                            logger.warning(f"[NOC-Engine] Failed to parse JSON: {json_err}")
                            data = {"raw_response": response_text}
                    else:
                        data = {"raw_response": response_text}
                    
                    if resp.status < 400:
                        logger.info(f"[NOC-Engine] Upload to port 8005 succeeded")
                        return {"success": True, "data": data}
                    else:
                        # Extract error message with fallbacks
                        error_msg = None
                        if isinstance(data, dict):
                            error_msg = data.get("detail") or data.get("message") or data.get("error") or str(data)
                        if not error_msg:
                            error_msg = response_text or f"HTTP {resp.status}"
                        logger.error(f"[NOC-Engine] Upload to port 8005 failed: status={resp.status}, error={error_msg}")
                        return {"success": False, "error": error_msg}
        except asyncio.TimeoutError as te:
            logger.error(f"[NOC-Engine] Timeout posting to port 8005: {te}")
            return {"success": False, "error": f"Timeout connecting to port 8005: {te}"}
        except Exception as e:
            logger.error(f"[NOC-Engine] Exception posting to port 8005: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[NOC-Engine] Traceback: {traceback.format_exc()}")
            return {"success": False, "error": f"{type(e).__name__}: {str(e) or 'Unknown error'}"}

    # ------------------------------------------------------------------
    # Main run loop with reconnect
    # ------------------------------------------------------------------

    async def run(self):
        """
        Main engine loop. Connects to NOC server and starts all sub-loops.
        On disconnect or any error, cleans up the socket and retries after 5s.
        """
        self._running = True
        RECONNECT_DELAY = 5  # Fixed 5-second retry — no backoff

        logger.info(f"[NOC-Engine] Starting → {self.ws_uri}")
        logger.info(f"[NOC-Engine] Charger IP   : {self.charger_ip}")
        logger.info(f"[NOC-Engine] Telemetry URLs:")
        for k, v in self.api_urls.items():
            logger.info(f"[NOC-Engine]   {k}: {v}")

        while self._running:
            ws = WSClient(self.ws_uri, ssl_context=self._ssl_context())
            try:
                await ws.connect()

                # Authenticate immediately after connecting
                await self._send_auth(ws)

                # Run essential loops that dictate connection health
                tasks = [
                    asyncio.create_task(self._receive_loop(ws),             name="receive"),
                    asyncio.create_task(self._telemetry_loop(ws),           name="telemetry"),
                    asyncio.create_task(self._heartbeat_loop(ws),           name="heartbeat"),
                    asyncio.create_task(self._session_sync_loop(ws),        name="session_sync"),
                    asyncio.create_task(self._charger_id_refresh_loop(ws),  name="charger_id_refresh"),
                ]

                # Fire and forget version poll (it self-terminates after 5 polls)
                version_task = asyncio.create_task(self._version_poll_loop(ws), name="version_poll")

                # Wait until any one loop exits (= connection lost or error)
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel surviving loops
                for task in pending:
                    task.cancel()
                version_task.cancel()
                await asyncio.gather(*pending, version_task, return_exceptions=True)

                logger.warning("[NOC-Engine] Connection closed — will reconnect")

            except (ConnectionRefusedError, OSError) as e:
                logger.warning(f"[NOC-Engine] Cannot reach NOC server at {self.ws_uri}: {e}")

            except WebSocketException as e:
                # Catches InvalidStatus (HTTP 200 instead of 101) and similar.
                # HTTP 200 usually means the server still has a live session for
                # this charger_id and is returning an HTTP page instead of
                # upgrading.  Closing and reconnecting resolves it once the
                # server-side session expires.
                logger.warning(
                    f"[NOC-Engine] WS handshake failed: {e} — "
                    f"server may still have a stale session for {self.charger_id}"
                )

            except Exception as e:
                logger.error(f"[NOC-Engine] Unexpected error: {e}", exc_info=True)

            finally:
                # Always close and nullify the socket before the next attempt,
                # even if connect() itself threw (e.g. InvalidStatus).
                await ws.disconnect()

            if not self._running:
                break

            logger.info(f"[NOC-Engine] Reconnecting in {RECONNECT_DELAY}s ...")
            await asyncio.sleep(RECONNECT_DELAY)

        logger.info("[NOC-Engine] Stopped.")


    def stop(self):
        """Stop the NOC engine and cleanup resources."""
        self._running = False
        
        # Close all SSH tunnels
        if hasattr(self, '_ssh_manager'):
            # Use asyncio.run_coroutine_threadsafe if called from sync context
            # or schedule it if from async context
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._ssh_manager.close_all())
                else:
                    loop.run_until_complete(self._ssh_manager.close_all())
            except Exception as e:
                logger.warning(f"[NOC-Engine] Error closing SSH tunnels: {e}")
        
        logger.info("[NOC-Engine] Stop signal sent")
