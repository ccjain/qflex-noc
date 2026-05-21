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
import hashlib
import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from websockets.exceptions import ConnectionClosed, WebSocketException

from ws_client import WSClient, WebSocketException
from telemetry_collector import collect as collect_telemetry
from command_executor import execute as execute_command
from ssh_tunnel import SSHTunnelManager
from version_api import VersionAPIServer

# Import session sync - will be initialized if available
try:
    from session_sync import SessionSyncManager
    _SESSION_SYNC_AVAILABLE = True
except ImportError:
    _SESSION_SYNC_AVAILABLE = False
    logger = logging.getLogger(__name__)

# Store active chunked uploads for large file transfers
# Key: upload_id, Value: {chunks: dict, total_chunks: int, file_name: str, target: str, expected_hash: str}
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
        self.noc_url: str | None = noc_cfg.get("url")
        if self.noc_url:
            self.noc_host = self.noc_url
            self.noc_port = 443 if self.noc_url.startswith("wss://") else 80
        else:
            self.noc_host = noc_cfg.get("host", "localhost")
            self.noc_port = noc_cfg.get("port", 8080)
        self.ws_uri = self._build_ws_uri()

        # Runtime kill switch — polled every 30s from local API. When False,
        # the engine stays alive but does not maintain a WS connection.
        self.noc_enabled: bool = bool(noc_cfg.get("enabled", True))
        self._enabled_event: asyncio.Event = asyncio.Event()
        if self.noc_enabled:
            self._enabled_event.set()
        self._enabled_refresh_task: asyncio.Task | None = None

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
        self._firmware_version_url = base_cc + "/api/v1/config/ocpp/firmware_version"

        # Charger ID refresh URLs and cache file
        self._ocpp_serial_url = (
            f"http://{self.charger_ip}:{cc_port}"
            f"/api/v1/config/ocpp/charge_box_serial_number"
        )
        self._hw_serial_url = f"http://{self.charger_ip}:{sys_port}/api/v1/system/hardware"
        self._noc_url_url   = f"http://{self.charger_ip}:{cc_port}/api/v1/config/ocpp/NocURL"
        self._noc_enabled_url = (
            f"http://{self.charger_ip}:{cc_port}/api/v1/config/ocpp/nocServerEnabled"
        )
        self._charger_id_cache_file = Path(charger_id_cache_file) if charger_id_cache_file else Path(__file__).parent / "charger_id_cache.json"

        self._running = False

        # Watchdog — force reconnect if NO WS activity (inbound, outbound,
        # or ping/pong) for this many seconds.
        self.activity_idle_timeout: float = 120.0
        self.reconnect_delay: float = 5.0

        # Bound the firmware-version fetch during auth (was 5s aiohttp timeout, now best-effort)
        self.firmware_fetch_timeout: float = 1.5

        # Chunked upload state (per-engine, with TTL sweep)
        self._chunked_uploads: dict = {}
        self.chunked_upload_ttl: float = 300.0  # seconds before an idle upload is dropped
        self._sweeper_task: asyncio.Task | None = None

        # Tracked fire-and-forget background tasks (commands, proxy, ssh, uploads)
        self._background_tasks: set[asyncio.Task] = set()

        # Shared aiohttp session for outbound HTTP (telemetry, executor, session_sync,
        # version polls, charger-id refresh, NOC URL refresh, proxy, devtools upload).
        self.http_session: aiohttp.ClientSession | None = None

        # SSH Tunnel manager for remote SSH access
        self._ssh_manager = SSHTunnelManager()

        # Periodic summaries for high-frequency events. Constructed once;
        # safe to use across reconnects (state is per-process, not per-WS).
        from log_helpers import PeriodicSummary, RateLimitedLogger
        self._telemetry_summary = PeriodicSummary(
            logger, key="Telemetry", window_seconds=3600.0,
        )
        self._proxy_request_summary = PeriodicSummary(
            logger, key="proxy_request", window_seconds=60.0,
        )

        # Rate-limit per-endpoint refresh failures. Attempts 1, 10, 100, 1000
        # each log a stack-frame; subsequent silent until .ok() emits a single
        # "recovered" INFO when the endpoint comes back.
        self._rl_ocpp_serial = RateLimitedLogger(logger, key="charger_id_refresh/ocpp")
        self._rl_hw_serial   = RateLimitedLogger(logger, key="charger_id_refresh/hw")
        self._rl_noc_url     = RateLimitedLogger(logger, key="noc_url_refresh")
        self._rl_noc_enabled = RateLimitedLogger(logger, key="noc_enabled_refresh")

        # Local HTTP API for version/health introspection
        engine_version = self._read_engine_version()
        self._version_api = VersionAPIServer(
            version=engine_version,
            host="127.0.0.1",
            port=int(ports.get("noc_engine_api", 8009)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _read_engine_version() -> str:
        """Read the engine's own package version from the VERSION file."""
        try:
            return Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
        except Exception:
            return "0.0.0+unknown"

    def _build_ws_uri(self) -> str:
        """Build the WebSocket URI from current noc_url (or host/port) + charger_id."""
        if self.noc_url:
            return f"{self.noc_url.rstrip('/')}/ws/{self.charger_id}"
        scheme = "wss" if self.noc_port == 443 else "ws"
        return f"{scheme}://{self.noc_host}:{self.noc_port}/ws/{self.charger_id}"

    def _save_cache(self, updates: dict) -> None:
        """Merge `updates` into the runtime cache file (preserves other fields)."""
        try:
            data: dict = {}
            if self._charger_id_cache_file.exists():
                with open(self._charger_id_cache_file, "r") as f:
                    data = json.load(f) or {}
        except Exception as e:
            logger.debug(f"[NOC-Engine] Cache read failed (will overwrite): {e}")
            data = {}
        data.update(updates)
        data["timestamp"] = self._now_iso()
        try:
            with open(self._charger_id_cache_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"[NOC-Engine] Failed to save cache: {e}")

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

    async def _fetch_firmware_version(self) -> str | None:
        """
        Fetch firmware version from the charger's OCPP config endpoint.
        Returns the version string on success, or None on any failure.
        """
        try:
            session = await self._ensure_http_session()
            async with session.get(
                self._firmware_version_url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        f"[NOC-Engine] Firmware version fetch: HTTP {resp.status}"
                    )
                    return None
                data = await resp.json(content_type=None)
                value = str(data.get("value", "")).replace("\x00", "").strip()
                if data.get("success") and value:
                    return value
        except Exception as e:
            logger.debug(f"[NOC-Engine] Firmware version fetch failed: {e}")
        return None

    async def _send_auth(self, ws: WSClient):
        try:
            fetched = await asyncio.wait_for(
                self._fetch_firmware_version(),
                timeout=self.firmware_fetch_timeout,
            )
        except asyncio.TimeoutError:
            fetched = None
            logger.info(
                f"[NOC-Engine] Firmware version fetch exceeded "
                f"{self.firmware_fetch_timeout}s — using config value"
            )

        if fetched:
            self.firmware_version = fetched
            logger.info(
                f"[NOC-Engine] Firmware version refreshed from "
                f"{self._firmware_version_url}: {fetched}"
            )
        else:
            logger.info(
                f"[NOC-Engine] Firmware version fetch unavailable — "
                f"using config value: {self.firmware_version}"
            )

        msg = self._make_msg("auth", {
            "charger_id":       self.charger_id,
            "firmware_version": self.firmware_version,
            "model":            self.model,
        })
        await ws.send(msg)
        logger.info(f"[NOC-Engine] Auth → charger_id={self.charger_id} fw={self.firmware_version}")

    async def _version_poll_loop(self, ws: WSClient):
        """
        Poll :8003/api/v1/system/version five times at 20-second intervals
        immediately after connecting, then stop.

        Each result is sent to the NOC server as a 'version_info' message.
        This gives the server a snapshot of all service versions on startup.
        """
        import aiohttp

        POLLS      = 5
        INTERVAL_S = 20

        logger.info(
            f"[NOC-Engine] 🔖 Starting version poll "
            f"({POLLS}x every {INTERVAL_S}s) from {self.version_url}"
        )

        for poll_num in range(1, POLLS + 1):
            if not ws.connected:
                logger.debug("[NOC-Engine] Version poll stopped — WS disconnected")
                return

            try:
                session = await self._ensure_http_session()
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
                logger.log(5, "[NOC-Engine] ♥ Heartbeat sent")  # below DEBUG (10)
            except Exception as e:
                logger.warning(f"[NOC-Engine] Heartbeat failed: {e}")
                break

    async def _telemetry_loop(self, ws: WSClient):
        """Collect and push charger telemetry every N seconds.

        Per-tick INFO line was dropped — see ``_telemetry_summary`` for a 1h
        windowed summary and immediate OK↔FAIL flip lines.
        """
        while True:
            await asyncio.sleep(self.telemetry_interval)
            if not ws.connected:
                break
            try:
                payload = await collect_telemetry(
                    self.api_urls,
                    session=await self._ensure_http_session(),
                )
                await ws.send(self._make_msg("telemetry", payload))
                self._telemetry_summary.success()
            except Exception as e:
                self._telemetry_summary.failure(str(e))
                logger.warning(f"[NOC-Engine] Telemetry push failed: {e}")
                break

    async def _charger_id_refresh_loop(self, ws: WSClient):
        """
        Poll local APIs every 10s to refresh charger ID and update cache.

        On change: persist to cache, rebuild ws_uri, then RETURN. Returning
        wakes the asyncio.wait(FIRST_COMPLETED) in run(), which cancels the
        sibling tasks and reconnects with the new charger_id.
        """
        import aiohttp

        REFRESH_INTERVAL = 10

        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            if not ws.connected:
                return

            ocpp_serial: str | None = None
            hw_serial: str | None = None

            try:
                session = await self._ensure_http_session()
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
                                self._rl_ocpp_serial.ok()
                except Exception:
                    self._rl_ocpp_serial.exception(
                        "OCPP serial fetch failed (URL: %s)", self._ocpp_serial_url,
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
                                self._rl_hw_serial.ok()
                except Exception:
                    self._rl_hw_serial.exception(
                        "HW serial fetch failed (URL: %s)", self._hw_serial_url,
                    )
            except Exception:
                # Outer error: e.g. _ensure_http_session() blew up. Bucket under OCPP
                # since that's the first endpoint we'd have hit.
                self._rl_ocpp_serial.exception("Charger ID refresh outer error")

            if not (ocpp_serial and hw_serial):
                continue

            # Always refresh cache so the latest values are the fallback
            self._save_cache({"ocpp_serial": ocpp_serial, "hw_serial": hw_serial})

            new_charger_id = f"{ocpp_serial}-{hw_serial}"
            if new_charger_id != self.charger_id:
                # KEEP — fires only on actual change (≈ zero/day in steady state).
                logger.info(
                    f"[NOC-Engine] Charger ID changed: {self.charger_id} → {new_charger_id} — reconnecting"
                )
                self.charger_id = new_charger_id
                self.ws_uri = self._build_ws_uri()
                return  # triggers reconnect in run()

    async def _noc_url_refresh_loop(self, ws: WSClient):
        """
        Poll local API every 10s to refresh the NOC server URL and update cache.

        On change: persist to cache, rebuild ws_uri, then RETURN to trigger a
        reconnect to the new URL.
        """
        import aiohttp

        REFRESH_INTERVAL = 10

        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            if not ws.connected:
                return

            new_url: str | None = None
            try:
                session = await self._ensure_http_session()
                async with session.get(
                    self._noc_url_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        url = str(data.get("value", "")).strip()
                        if data.get("success") and url:
                            new_url = url
                            self._rl_noc_url.ok()
            except Exception:
                self._rl_noc_url.exception(
                    "NOC URL refresh failed (URL: %s)", self._noc_url_url,
                )
                continue

            if not new_url:
                continue

            # Always refresh cache so the latest URL is the fallback
            self._save_cache({"noc_url": new_url})

            if new_url != self.noc_url:
                # KEEP — fires only on actual change (≈ zero/day in steady state).
                logger.info(
                    f"[NOC-Engine] NOC URL changed: {self.noc_url} → {new_url} — reconnecting"
                )
                self.noc_url  = new_url
                self.noc_host = new_url
                self.noc_port = 443 if new_url.startswith("wss://") else 80
                self.ws_uri   = self._build_ws_uri()
                return  # triggers reconnect in run()

    @staticmethod
    def _parse_enabled_value(value) -> bool:
        """Parse the nocServerEnabled API value into a strict bool.

        Truthy: True, "true"/"True"/"TRUE", "1", 1. Everything else → False.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1")
        return False

    async def _fetch_noc_enabled_once(self) -> bool | None:
        """One-shot fetch of nocServerEnabled. Returns True/False or None on failure."""
        try:
            session = await self._ensure_http_session()
            async with session.get(
                self._noc_enabled_url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    self._rl_noc_enabled.exception(
                        "nocServerEnabled fetch HTTP %s (URL: %s)",
                        resp.status, self._noc_enabled_url,
                    )
                    return None
                data = await resp.json(content_type=None)
                if not data.get("success"):
                    self._rl_noc_enabled.exception(
                        "nocServerEnabled returned success=False (URL: %s, body=%s)",
                        self._noc_enabled_url, data,
                    )
                    return None
                self._rl_noc_enabled.ok()
                return self._parse_enabled_value(data.get("value"))
        except Exception:
            self._rl_noc_enabled.exception(
                "nocServerEnabled refresh failed (URL: %s)", self._noc_enabled_url,
            )
            return None

    async def _noc_enabled_refresh_loop(self):
        """
        Engine-lifetime loop. Polls the local nocServerEnabled flag every 30s.

        Unlike per-WS refresh loops, this task is spawned ONCE in run() and
        outlives reconnects. On transition: persist to cache, update state,
        and set the event so a disabled run() wakes immediately on enable.
        """
        REFRESH_INTERVAL = 30
        while self._running:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
            except asyncio.CancelledError:
                return
            if not self._running:
                return

            new_enabled = await self._fetch_noc_enabled_once()
            if new_enabled is None:
                continue  # API down — keep last state

            self._save_cache({"noc_enabled": new_enabled})

            if new_enabled != self.noc_enabled:
                # KEEP — fires only on actual change (≈ zero/day in steady state).
                logger.info(
                    f"[NOC-Engine] nocServerEnabled changed: "
                    f"{self.noc_enabled} → {new_enabled}"
                )
                self.noc_enabled = new_enabled
                # Wake run() if it's waiting for re-enable, and let the
                # per-WS guard loop notice on its next tick.
                self._enabled_event.set()

    async def _enabled_guard_loop(self, ws: WSClient):
        """Per-WS guard: if noc_enabled flips false, return to trigger reconnect.

        Exiting feeds asyncio.wait(FIRST_COMPLETED) in run(), which cancels
        sibling tasks and tears the connection down cleanly. The next iteration
        of run() finds noc_enabled=False and waits on _enabled_event.
        """
        while True:
            await asyncio.sleep(1)
            if not ws.connected:
                return
            if not self.noc_enabled:
                logger.info("[NOC-Engine] nocServerEnabled=false — closing WS")
                return

    async def _session_sync_loop(self, ws: WSClient):
        """Collect and push session data every 30 seconds."""
        if not _SESSION_SYNC_AVAILABLE:
            logger.debug("[NOC-Engine] Session sync not available, skipping loop")
            return
        
        # Initialize session sync manager (shared HTTP session)
        session_sync = SessionSyncManager(
            noc_ws_client=ws,
            charger_id=self.charger_id,
            local_api_base=f"http://{self.charger_ip}:8003",
            poll_interval=30,
            http_session=await self._ensure_http_session(),
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

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        """Lazily create the shared aiohttp.ClientSession (re-creates if it was closed)."""
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
            logger.debug("[NOC-Engine] aiohttp.ClientSession opened")
        return self.http_session

    async def _close_http_session(self):
        """Close the shared aiohttp.ClientSession (idempotent)."""
        if self.http_session is not None:
            try:
                await self.http_session.close()
            except Exception as e:
                logger.warning(f"[NOC-Engine] Error closing HTTP session: {e}")
            self.http_session = None

    def _spawn_tracked(self, coro, name: str | None = None) -> asyncio.Task:
        """Schedule a fire-and-forget coroutine so we can cancel it later
        and log any exception instead of silently swallowing it."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    f"[NOC-Engine] Background task {t.get_name()!r} raised: "
                    f"{type(exc).__name__}: {exc}"
                )

        task.add_done_callback(_on_done)
        return task

    async def _watchdog_loop(self, ws: WSClient):
        """Force a reconnect if no WS activity at all for ``activity_idle_timeout`` seconds.

        Activity is any of: inbound message, outbound message, or ping/pong.
        The WSClient tracks this via ``last_activity_at`` (monotonic).

        Returning from this coroutine wakes the asyncio.wait(FIRST_COMPLETED) in
        run() and triggers the standard reconnect flow.
        """
        import time as _time
        # Poll cadence: check frequently but cap at 5 s so stalls are caught quickly.
        poll = min(max(self.activity_idle_timeout / 4, 1.0), 5.0)
        while True:
            await asyncio.sleep(poll)
            if not ws.connected:
                return
            idle = _time.monotonic() - ws.last_activity_at
            if idle >= self.activity_idle_timeout:
                logger.warning(
                    f"[NOC-Engine] \U0001f436 Watchdog: no WS activity for {idle:.1f}s "
                    f"(threshold={self.activity_idle_timeout}s) — forcing reconnect"
                )
                return  # exit triggers FIRST_COMPLETED → reconnect

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

                # Per-message DEBUG (lifecycle trace); the 60s summary below
                # is the canonical INFO-level signal.
                logger.debug(
                    f"[NOC-Engine] 📥 WS msg type={msg_type} keys={list(message.keys())}"
                )

                if msg_type == "command":
                    # Dispatch to a separate task so we don't block receive loop
                    payload = message.get("payload", {})
                    cmd_id = payload.get("command_id", "?")
                    logger.info(f"[NOC-Engine] ⚡ Dispatching command task for cmd={cmd_id}")
                    self._spawn_tracked(self._handle_command(ws, message), name=f"cmd-{cmd_id}")

                elif msg_type == "proxy_request":
                    # Aggregate via 60s windowed summary; keep per-message DEBUG
                    # for forensic troubleshooting.
                    payload = message.get("payload", {})
                    request_id = payload.get("request_id", "?")
                    logger.debug(
                        f"[NOC-Engine] 🔀 Proxy request {request_id}: "
                        f"{payload.get('method')} {payload.get('path')}"
                    )
                    self._proxy_request_summary.increment()
                    self._spawn_tracked(self._handle_proxy_request(ws, message), name=f"proxy-{request_id}")

                elif msg_type == "upload_chunk":
                    # Handle chunked file upload from NOC Server
                    payload = message.get("payload", {})
                    upload_id = payload.get("upload_id", "?")
                    chunk_index = payload.get("chunk_index", 0)
                    total_chunks = payload.get("total_chunks", 0)
                    logger.info(f"[NOC-Engine] 📦 Upload chunk {chunk_index + 1}/{total_chunks} for {upload_id}")
                    self._spawn_tracked(self._handle_upload_chunk(ws, message), name=f"upload-{upload_id}")

                elif msg_type == "ssh_tunnel_open":
                    # Handle SSH tunnel open request from NOC Server
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.info(f"[NOC-Engine] 🔐 SSH tunnel open request: {tunnel_id}")
                    self._spawn_tracked(self._handle_ssh_tunnel_open(ws, message), name=f"ssh-open-{tunnel_id}")
                
                elif msg_type == "ssh_data":
                    # Handle SSH data from client (via NOC Server)
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.debug(f"[NOC-Engine] 🔐 SSH data for tunnel: {tunnel_id}")
                    self._spawn_tracked(self._handle_ssh_data(ws, message), name=f"ssh-data-{tunnel_id}")

                elif msg_type == "ssh_tunnel_close":
                    # Handle SSH tunnel close request
                    payload = message.get("payload", {})
                    tunnel_id = payload.get("tunnel_id", "?")
                    logger.info(f"[NOC-Engine] 🔐 SSH tunnel close request: {tunnel_id}")
                    self._spawn_tracked(self._handle_ssh_tunnel_close(ws, message), name=f"ssh-close-{tunnel_id}")

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
            # Pass charger_ip so executor targets the right host; share the engine's session.
            result = await execute_command(
                command,
                charger_ip=self.charger_ip,
                session=await self._ensure_http_session(),
            )
            result_msg = self._make_msg("command_result", result)
            await ws.send(result_msg)
            logger.info(
                f"[NOC-Engine] ✔ Command result sent: "
                f"cmd={cmd_id} status={result.get('status_code')}"
            )
        except Exception:
            logger.exception(f"[NOC-Engine] Failed to send command result for cmd={cmd_id}")

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
        except Exception:
            logger.exception("[NOC-Engine] 🔐 Failed to send SSH tunnel ack")
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
            except Exception:
                logger.exception("[NOC-Engine] 🔐 Failed to send tunnel closed")

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
        except Exception:
            logger.exception("[NOC-Engine] 🔐 Failed to send tunnel closed")

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
            "headers": {"Accept": "application/json"},
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        logger.info(f"[NOC-Engine] Proxy {method} {target_url}")
        
        # Add body and headers for POST/PUT requests
        if body and method in ["POST", "PUT"]:
            if body_is_binary:
                # Decode base64 binary data
                decoded_body = base64.b64decode(body)
                request_kwargs["data"] = decoded_body
                request_kwargs["headers"] = {"Accept": "application/json", "Content-Type": content_type}
                logger.info(f"[NOC-Engine] Proxy binary POST: target={target_url}, content_type={content_type}, body_size={len(decoded_body)}")
                # Log first 200 bytes of body for debugging boundary issues
                logger.info(f"[NOC-Engine] Body preview: {decoded_body[:200]}")
            else:
                # Text data
                request_kwargs["data"] = body.encode('utf-8')
                request_kwargs["headers"] = {"Accept": "application/json", "Content-Type": content_type}
                logger.info(f"[NOC-Engine] Proxy text POST: target={target_url}, content_type={content_type}")
        
        response_msg: dict | None = None
        error: Exception | None = None
        status_code: int | None = None
        try:
            session = await self._ensure_http_session()
            async with session.request(**request_kwargs) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    data = {"raw_response": text}
                status_code = resp.status
                response_msg = self._make_msg("proxy_response", {
                    "request_id": request_id,
                    "success": True,
                    "status_code": resp.status,
                    "data": data,
                })
        except Exception as e:
            error = e
            response_msg = self._make_msg("proxy_response", {
                "request_id": request_id,
                "success": False,
                "error": str(e),
            })

        # Drop the response if the WS closed while we were doing I/O — the server
        # has no correlation slot for this request_id anymore, and ws.send would
        # raise ConnectionError as an unhandled task exception.
        if not ws.connected:
            logger.warning(
                f"[NOC-Engine] 🔀 Proxy response dropped (WS closed): {request_id}"
            )
            return
        try:
            await ws.send(response_msg)
        except Exception as send_err:
            logger.warning(
                f"[NOC-Engine] 🔀 Proxy response send failed: {request_id} - {send_err}"
            )
            return

        if error is not None:
            logger.error(f"[NOC-Engine] 🔀 Proxy request failed: {request_id} - {error}")
        else:
            logger.info(f"[NOC-Engine] 🔀 Proxy response sent: {request_id} (HTTP {status_code})")

    async def _sweep_chunked_uploads_once(self):
        """Drop any chunked-upload entry that hasn't completed within the TTL."""
        now = time.monotonic()
        stale = [
            uid for uid, u in self._chunked_uploads.items()
            if now - u.get("started_at", now) > self.chunked_upload_ttl
        ]
        for uid in stale:
            entry = self._chunked_uploads.pop(uid, None)
            if entry is not None:
                logger.warning(
                    f"[NOC-Engine] 🧹 Dropped stale chunked upload {uid} "
                    f"(age={now - entry.get('started_at', now):.0f}s, "
                    f"chunks={len(entry.get('chunks', {}))}/{entry.get('total_chunks')})"
                )

    async def _chunked_upload_sweeper_loop(self):
        """Background sweeper — runs at TTL/4 intervals as long as the engine is running."""
        while self._running:
            await asyncio.sleep(max(self.chunked_upload_ttl / 4, 5))
            await self._sweep_chunked_uploads_once()

    async def _handle_upload_chunk(self, ws: WSClient, message: dict):
        """Handle file upload chunk from NOC Server. Accumulates chunks and posts to port 8005."""
        import base64

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
        if upload_id not in self._chunked_uploads:
            self._chunked_uploads[upload_id] = {
                "chunks": {},
                "total_chunks": total_chunks,
                "file_name": file_name,
                "target": target,
                "expected_hash": "",
                "started_at": time.monotonic(),
            }
            logger.info(f"[NOC-Engine] Started chunked upload: {upload_id}, {total_chunks} chunks")
        
        upload = self._chunked_uploads[upload_id]
        
        # Capture file_hash from any chunk (later chunks override if non-empty)
        file_hash = payload.get("file_hash", "")
        if file_hash:
            upload["expected_hash"] = file_hash
        
        # Store chunk
        try:
            chunk_bytes = base64.b64decode(chunk_data_b64)
            upload["chunks"][chunk_index] = chunk_bytes
            logger.info(f"[NOC-Engine] Stored chunk {chunk_index + 1}/{total_chunks} for {upload_id}")
        except Exception as e:
            logger.exception(f"[NOC-Engine] Failed to decode chunk {chunk_index}")
            error_msg = self._make_msg("chunked_upload_response", {
                "upload_id": upload_id,
                "success": False,
                "error": f"Invalid chunk data: {e}",
            })
            await ws.send(error_msg)
            self._chunked_uploads.pop(upload_id, None)
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
            file_size = len(file_data)
            logger.info(f"[Upload] Reassembly complete: {upload_id}, size={file_size} bytes")
            
            # Verify SHA-256 hash if provided
            expected_hash = upload.get("expected_hash", "")
            actual_hash = ""
            if expected_hash:
                actual_hash = hashlib.sha256(file_data).hexdigest()
                logger.info(f"[Upload] Expected hash: {expected_hash}")
                logger.info(f"[Upload] Actual hash:   {actual_hash}")
                
                if actual_hash != expected_hash:
                    logger.error(f"[Upload] HASH MISMATCH for {upload_id} — refusing extraction")
                    error_msg = self._make_msg("chunked_upload_response", {
                        "upload_id": upload_id,
                        "success": False,
                        "error": (
                            f"File hash mismatch — upload corrupted. "
                            f"Expected {expected_hash} but got {actual_hash}. "
                            f"Please retry the upload when the connection is stable."
                        ),
                    })
                    await ws.send(error_msg)
                    self._chunked_uploads.pop(upload_id, None)
                    return
                else:
                    logger.info(f"[Upload] Hash verified for {upload_id} — proceeding with extraction")
            
            logger.info(f"[NOC-Engine] Reassembled file {file_name} ({file_size} bytes), target={upload['target']}, posting to port 8005")
            
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
            self._chunked_uploads.pop(upload_id, None)

    async def _post_file_to_devtools(self, target: str, file_name: str, file_data: bytes) -> dict:
        """Post complete file to dev-tools service on port 8005."""
        url = f"http://{self.charger_ip}:8005/api/v1/devtools/upload/{target}"
        logger.info(f"[NOC-Engine] Posting to {url}, file={file_name}, size={len(file_data)} bytes")

        # Use aiohttp's FormData for proper multipart encoding
        form = aiohttp.FormData()
        form.add_field('file', file_data, filename=file_name, content_type='application/octet-stream')

        try:
            session = await self._ensure_http_session()
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
            logger.exception("[NOC-Engine] Exception posting to port 8005")
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

        logger.info(f"[NOC-Engine] Starting → {self.ws_uri}")
        logger.info(f"[NOC-Engine] Charger IP   : {self.charger_ip}")
        logger.info(f"[NOC-Engine] Telemetry URLs:")
        for k, v in self.api_urls.items():
            logger.info(f"[NOC-Engine]   {k}: {v}")

        # Open the shared HTTP session (used by telemetry, executor, session_sync, etc.)
        await self._ensure_http_session()

        # Start the local version API (best-effort; port may be busy)
        try:
            await self._version_api.start()
        except OSError as e:
            logger.warning(f"[NOC-Engine] Version API failed to start: {e}")

        # Background sweeper for orphaned chunked uploads (engine-lifetime)
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(
                self._chunked_upload_sweeper_loop(), name="chunk_sweeper"
            )

        # Engine-lifetime poll of the nocServerEnabled kill switch
        if self._enabled_refresh_task is None or self._enabled_refresh_task.done():
            self._enabled_refresh_task = asyncio.create_task(
                self._noc_enabled_refresh_loop(), name="noc_enabled_refresh"
            )

        while self._running:
            # Kill-switch gate: if disabled, sit on the event until re-enabled.
            if not self.noc_enabled:
                logger.info(
                    "[NOC-Engine] nocServerEnabled=false — sleeping until enabled"
                )
                self._enabled_event.clear()
                await self._enabled_event.wait()
                if not self._running:
                    break
                logger.info("[NOC-Engine] nocServerEnabled=true — resuming")
                continue

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
                    asyncio.create_task(self._noc_url_refresh_loop(ws),     name="noc_url_refresh"),
                    asyncio.create_task(self._watchdog_loop(ws),            name="watchdog"),
                    asyncio.create_task(self._enabled_guard_loop(ws),       name="enabled_guard"),
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

                # Cancel and drain background tasks tied to the dead connection
                for t in list(self._background_tasks):
                    t.cancel()
                if self._background_tasks:
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
                    self._background_tasks.clear()

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

            logger.info(f"[NOC-Engine] Reconnecting in {self.reconnect_delay}s ...")
            await asyncio.sleep(self.reconnect_delay)

        try:
            await self._version_api.stop()
        except Exception as e:
            logger.warning(f"[NOC-Engine] Version API stop error: {e}")
        await self._close_http_session()
        logger.info("[NOC-Engine] Stopped.")


    def stop(self):
        """Stop the NOC engine and cleanup resources."""
        self._running = False

        # Cancel the chunked-upload sweeper
        if self._sweeper_task is not None and not self._sweeper_task.done():
            self._sweeper_task.cancel()

        # Cancel the engine-lifetime nocServerEnabled refresh task and wake
        # any run() that's parked on the disabled-state event.
        if self._enabled_refresh_task is not None and not self._enabled_refresh_task.done():
            self._enabled_refresh_task.cancel()
        self._enabled_event.set()

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
