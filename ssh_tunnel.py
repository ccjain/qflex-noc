#!/usr/bin/env python3
"""
NOC Engine — SSH Tunnel Manager
===============================
Manages TCP connections to local SSHD on behalf of remote clients.

Each SSH tunnel:
  - WebSocket (from NOC Server) ↔ TCP (to localhost:22)
  - Data is Base64 encoded for JSON WebSocket transport
  - Tracks bytes transferred and connection duration

Architecture:
  NOC Server (SSH Proxy) ←→ WebSocket ←→ NOC Engine ←→ TCP ←→ SSHD
"""

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class LocalSSHTunnel:
    """
    Represents a single SSH tunnel from WebSocket to local SSHD.
    
    Attributes:
        tunnel_id: Unique identifier for this tunnel
        reader: asyncio StreamReader for TCP connection to SSHD
        writer: asyncio StreamWriter for TCP connection to SSHD
        connected_at: When the tunnel was established
        bytes_tx: Bytes sent to SSHD (from client)
        bytes_rx: Bytes received from SSHD (to client)
        closed: Whether the tunnel is closed
        _closed_event: Asyncio event set when tunnel closes
    """
    tunnel_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connected_at: datetime = field(default_factory=datetime.utcnow)
    bytes_tx: int = 0
    bytes_rx: int = 0
    closed: bool = False
    _closed_event: asyncio.Event = field(default_factory=asyncio.Event)
    
    def mark_closed(self):
        """Mark tunnel as closed and signal waiting coroutines."""
        self.closed = True
        self._closed_event.set()
    
    async def wait_closed(self):
        """Wait for tunnel to close."""
        await self._closed_event.wait()


class SSHTunnelManager:
    """
    Manages multiple SSH tunnels for this charger.
    
    Each tunnel connects WebSocket ↔ localhost:22 (SSHD)
    Maximum 10 concurrent tunnels per charger.
    
    Usage:
        manager = SSHTunnelManager()
        
        # Open tunnel (called from _handle_ssh_tunnel_open)
        success = await manager.open_tunnel(
            tunnel_id="t-uuid",
            ws_send_callback=send_to_server,
            target_host="localhost",
            target_port=22
        )
        
        # Forward data (called from _handle_ssh_data)
        await manager.forward_to_ssh(tunnel_id, base64_data)
        
        # Close tunnel (called from _handle_ssh_tunnel_close)
        await manager.close_tunnel(tunnel_id)
    """
    
    MAX_TUNNELS = 10
    SSHD_DEFAULT_HOST = "localhost"
    SSHD_DEFAULT_PORT = 22
    
    def __init__(self):
        """Initialize the SSH tunnel manager."""
        self._tunnels: Dict[str, LocalSSHTunnel] = {}
        self._lock = asyncio.Lock()
        self._reader_tasks: Dict[str, asyncio.Task] = {}  # Track reader tasks
        logger.info("[SSH-Tunnel] Manager initialized")
    
    async def open_tunnel(
        self, 
        tunnel_id: str,
        ws_send_callback: Callable[[dict], asyncio.Future],
        target_host: str = SSHD_DEFAULT_HOST,
        target_port: int = SSHD_DEFAULT_PORT
    ) -> bool:
        """
        Create a new TCP connection to local SSHD.
        
        Args:
            tunnel_id: Unique identifier for this tunnel
            ws_send_callback: Async function to send messages back to NOC Server
            target_host: Target SSHD host (default: localhost)
            target_port: Target SSHD port (default: 22)
            
        Returns:
            True if tunnel opened successfully, False otherwise
        """
        async with self._lock:
            # Check if tunnel already exists
            if tunnel_id in self._tunnels:
                logger.warning(f"[SSH-Tunnel] Tunnel {tunnel_id} already exists")
                return False
            
            # Check max tunnels limit
            if len(self._tunnels) >= self.MAX_TUNNELS:
                logger.error(f"[SSH-Tunnel] Max tunnels ({self.MAX_TUNNELS}) reached")
                return False
        
        try:
            # Establish TCP connection to SSHD
            logger.info(
                f"[SSH-Tunnel] Opening tunnel {tunnel_id} to "
                f"{target_host}:{target_port}"
            )
            logger.debug(
                f"[SSH-Tunnel] Connection params: host={target_host}, "
                f"port={target_port}, timeout=10.0s"
            )

            # Get event loop info for debugging
            loop = asyncio.get_event_loop()
            logger.debug(f"[SSH-Tunnel] Event loop: {loop}")

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=10.0
            )

            # Log successful TCP connection details
            sock = writer.get_extra_info('socket')
            local_addr = writer.get_extra_info('sockname')
            remote_addr = writer.get_extra_info('peername')
            logger.info(f"[SSH-Tunnel] TCP connection established for {tunnel_id}")
            logger.info(f"[SSH-Tunnel] Local endpoint: {local_addr}, Remote: {remote_addr}")
            logger.debug(f"[SSH-Tunnel] Socket options: {sock.getsockopt if sock else 'N/A'}")
            
            # Create tunnel object
            tunnel = LocalSSHTunnel(
                tunnel_id=tunnel_id,
                reader=reader,
                writer=writer
            )
            
            async with self._lock:
                self._tunnels[tunnel_id] = tunnel
            
            # Start background reader task
            # This reads from SSHD and sends to WebSocket
            task = asyncio.create_task(
                self._ssh_reader_loop(tunnel_id, ws_send_callback),
                name=f"ssh_reader_{tunnel_id}"
            )
            self._reader_tasks[tunnel_id] = task

            # Try to read initial SSH banner (first data from SSHD)
            # This helps verify SSHD is actually responding
            logger.debug(f"[SSH-Tunnel] Waiting for SSH banner from SSHD for {tunnel_id}...")

            logger.info(
                f"[SSH-Tunnel] Tunnel {tunnel_id} opened successfully. "
                f"Active tunnels: {len(self._tunnels)}"
            )
            return True
            
        except asyncio.TimeoutError:
            logger.error(
                f"[SSH-Tunnel] Timeout connecting to SSHD at "
                f"{target_host}:{target_port} (10s timeout exceeded)"
            )
            logger.error(f"[SSH-Tunnel] Possible causes: SSHD not running, firewall blocking port {target_port}, or network unreachable")
            return False
        except ConnectionRefusedError as e:
            logger.error(
                f"[SSH-Tunnel] SSHD refused connection at "
                f"{target_host}:{target_port}. Is SSHD running?"
            )
            logger.error(f"[SSH-Tunnel] Check: systemctl status sshd / service ssh status")
            logger.error(f"[SSH-Tunnel] Check: netstat -tlnp | grep {target_port}")
            return False
        except OSError as e:
            logger.error(f"[SSH-Tunnel] OS error opening tunnel {tunnel_id}: {e}")
            logger.error(f"[SSH-Tunnel] Error code: {e.errno if hasattr(e, 'errno') else 'N/A'}")
            logger.error(f"[SSH-Tunnel] Target: {target_host}:{target_port}")
            return False
        except Exception as e:
            logger.error(f"[SSH-Tunnel] Failed to open tunnel {tunnel_id}: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[SSH-Tunnel] Traceback: {traceback.format_exc()}")
            return False
    
    async def forward_to_ssh(self, tunnel_id: str, data_b64: str) -> bool:
        """
        Decode Base64 data and write to SSHD connection.
        
        Args:
            tunnel_id: Tunnel identifier
            data_b64: Base64-encoded data from SSH client
            
        Returns:
            True if data was written successfully
        """
        tunnel = self._tunnels.get(tunnel_id)
        if not tunnel:
            logger.warning(f"[SSH-Tunnel] Cannot forward: tunnel {tunnel_id} not found")
            return False
        
        if tunnel.closed:
            logger.warning(f"[SSH-Tunnel] Cannot forward: tunnel {tunnel_id} is closed")
            return False
        
        try:
            # Decode Base64
            data = base64.b64decode(data_b64)

            # Log first few bytes for debugging SSH protocol issues
            if tunnel.bytes_tx == 0:
                logger.info(f"[SSH-Tunnel] First data from client for {tunnel_id}: {data[:50]}")
                if b'SSH-' in data:
                    logger.info(f"[SSH-Tunnel] Client sent SSH version string")

            # Write to SSHD
            tunnel.writer.write(data)
            await tunnel.writer.drain()
            
            # Update stats
            tunnel.bytes_tx += len(data)
            
            logger.debug(
                f"[SSH-Tunnel] Forwarded {len(data)} bytes to SSHD "
                f"for tunnel {tunnel_id}"
            )
            return True
            
        except Exception as e:
            logger.error(f"[SSH-Tunnel] Failed to forward to SSHD: {e}")
            await self.close_tunnel(tunnel_id, reason="write_error")
            return False
    
    async def _ssh_reader_loop(
        self, 
        tunnel_id: str,
        ws_send_callback: Callable[[dict], asyncio.Future]
    ):
        """
        Background task: Read from SSHD, Base64 encode, send via WebSocket.
        
        This runs in a separate task for each tunnel.
        """
        tunnel = self._tunnels.get(tunnel_id)
        if not tunnel:
            logger.error(f"[SSH-Tunnel] Reader loop: tunnel {tunnel_id} not found")
            return
        
        logger.info(f"[SSH-Tunnel] Reader loop started for {tunnel_id}")
        banner_logged = False

        try:
            while not tunnel.closed:
                # Read data from SSHD
                try:
                    data = await asyncio.wait_for(
                        tunnel.reader.read(8192),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if not data:
                    # Connection closed by SSHD
                    logger.info(
                        f"[SSH-Tunnel] SSHD closed connection for {tunnel_id}"
                    )
                    break

                # Log first data chunk (SSH banner) for debugging
                if not banner_logged:
                    try:
                        banner_preview = data[:100].decode('utf-8', errors='replace').strip()
                        logger.info(f"[SSH-Tunnel] First data from SSHD for {tunnel_id}: {banner_preview}")
                        if b'SSH-' in data:
                            logger.info(f"[SSH-Tunnel] Valid SSH banner detected for {tunnel_id}")
                        else:
                            logger.warning(f"[SSH-Tunnel] Unexpected data from SSHD (not SSH banner): {banner_preview[:50]}")
                    except Exception as e:
                        logger.warning(f"[SSH-Tunnel] Could not decode banner for {tunnel_id}: {e}")
                    banner_logged = True
                
                # Update stats
                tunnel.bytes_rx += len(data)
                
                # Base64 encode
                data_b64 = base64.b64encode(data).decode('ascii')
                
                # Send to NOC Server via WebSocket
                message = {
                    "type": "ssh_data",
                    "payload": {
                        "tunnel_id": tunnel_id,
                        "data": data_b64
                    }
                }
                
                try:
                    await ws_send_callback(message)
                    logger.debug(
                        f"[SSH-Tunnel] Sent {len(data)} bytes from SSHD "
                        f"for tunnel {tunnel_id}"
                    )
                except Exception as e:
                    logger.error(f"[SSH-Tunnel] Failed to send to WebSocket: {e}")
                    break
                    
        except asyncio.CancelledError:
            logger.info(f"[SSH-Tunnel] Reader loop cancelled for {tunnel_id}")
            raise
        except Exception as e:
            logger.error(f"[SSH-Tunnel] Reader loop error for {tunnel_id}: {e}")
        finally:
            # Clean up tunnel
            await self.close_tunnel(tunnel_id, reason="connection_lost")
    
    async def close_tunnel(self, tunnel_id: str, reason: str = "closed") -> dict:
        """
        Close tunnel and cleanup resources.

        Args:
            tunnel_id: Tunnel identifier
            reason: Why the tunnel is being closed

        Returns:
            Dict with tunnel statistics (for reporting to server)
        """
        task_to_cancel = None
        async with self._lock:
            tunnel = self._tunnels.pop(tunnel_id, None)

            if not tunnel:
                return {}

            if tunnel.closed:
                return self._get_tunnel_stats(tunnel)

            # Mark as closed
            tunnel.mark_closed()

            # Pop reader task inside the lock to prevent races,
            # but DO NOT await it here — awaiting a cancelled task
            # inside the lock causes a deadlock because the task's
            # finally block calls close_tunnel(), which tries to
            # re-acquire this same lock.
            current_task = asyncio.current_task()
            task = self._reader_tasks.pop(tunnel_id, None)
            if task and task is not current_task and not task.done():
                task.cancel()
                task_to_cancel = task  # await it after releasing the lock

            # Calculate stats while still holding data
            stats = self._get_tunnel_stats(tunnel)
            stats["reason"] = reason

        # Await the cancelled task OUTSIDE the lock.
        # The task's finally block calls close_tunnel(), which will
        # find no entry (already popped above) and return {} immediately.
        if task_to_cancel is not None:
            try:
                await task_to_cancel
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[SSH-Tunnel] Reader task error during close: {e}")

        # Close TCP connection OUTSIDE the lock to avoid deadlocking
        # other operations if wait_closed() hangs.
        try:
            tunnel.writer.close()
            await asyncio.wait_for(tunnel.writer.wait_closed(), timeout=2.0)
        except Exception as e:
            logger.debug(f"[SSH-Tunnel] Error closing TCP: {e}")

        logger.info(
            f"[SSH-Tunnel] Tunnel {tunnel_id} closed. "
            f"Reason: {reason}, "
            f"Duration: {stats.get('duration_seconds', 0)}s, "
            f"TX: {stats.get('bytes_tx', 0)}, "
            f"RX: {stats.get('bytes_rx', 0)}"
        )

        return stats
    
    def _get_tunnel_stats(self, tunnel: LocalSSHTunnel) -> dict:
        """Calculate tunnel statistics."""
        duration = (datetime.utcnow() - tunnel.connected_at).total_seconds()
        return {
            "tunnel_id": tunnel.tunnel_id,
            "bytes_tx": tunnel.bytes_tx,
            "bytes_rx": tunnel.bytes_rx,
            "duration_seconds": int(duration),
            "connected_at": tunnel.connected_at.isoformat(),
        }
    
    def get_tunnel_count(self) -> int:
        """Return number of active tunnels."""
        return len(self._tunnels)
    
    def get_tunnel_ids(self) -> list:
        """Return list of active tunnel IDs."""
        return list(self._tunnels.keys())
    
    def get_all_stats(self) -> list:
        """Return statistics for all active tunnels."""
        return [self._get_tunnel_stats(t) for t in self._tunnels.values()]
    
    async def close_all(self):
        """Close all tunnels (used during shutdown)."""
        tunnel_ids = list(self._tunnels.keys())
        for tunnel_id in tunnel_ids:
            await self.close_tunnel(tunnel_id, reason="shutdown")
        logger.info("[SSH-Tunnel] All tunnels closed")
