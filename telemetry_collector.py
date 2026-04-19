#!/usr/bin/env python3
"""
NocEngine Telemetry Collector
==============================
Fetches telemetry data from all local charging service APIs concurrently.

Sources:
  - charging_controller  (port 8003) → charging status, gun states, session data
  - allocation_engine    (port 8002) → power allocation status
  - network_api          (port 8000) → hardware / system info
  - error_generation     (port 8006) → active hardware alarms

All fetches run in parallel via asyncio.gather().

Every telemetry packet includes a top-level ``service_health`` block so the
NOC server always knows which modules are reachable, regardless of whether the
WS connection itself is healthy.

service_health format:
  {
    "charging_controller": { "status": "ok",          "url": "http://...", "http_code": 200 },
    "allocation_engine":   { "status": "unreachable",  "url": "http://...", "error": "..." },
    "network_api":         { "status": "http_error",   "url": "http://...", "http_code": 400 },
    "error_generation":    { "status": "timeout",      "url": "http://..." },
  }

status values:
  "ok"          — HTTP 200 received and parsed successfully
  "unreachable" — TCP connection refused / service not running
  "timeout"     — service reachable but did not respond within timeout
  "http_error"  — reachable but returned non-200 HTTP status
  "error"       — unexpected exception
"""

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service registry
# Maps a human-readable service name → default API URL for telemetry
# The noc_engine.py overrides these with charger_ip-aware URLs.
# ---------------------------------------------------------------------------

DEFAULT_APIS = {
    "charging_status":   "http://localhost:8003/api/v1/charging/status",
    "allocation_status": "http://localhost:8002/api/state/with_history",
    "network_status":    "http://localhost:8000/api/v1/system/hardware",
    "active_errors":     "http://localhost:8006/error-generator/errors/pending",
}

# Maps each telemetry key → which service_health entry it belongs to
_KEY_TO_SERVICE = {
    "charging_status":   "charging_controller",
    "allocation_status": "allocation_engine",
    "network_status":    "network_api",
    "active_errors":     "error_generation",
}


# ---------------------------------------------------------------------------
# Internal fetch helper — always returns a result dict (never raises)
# ---------------------------------------------------------------------------

async def _fetch(
    session: aiohttp.ClientSession,
    key: str,
    url: str,
    timeout_s: float = 5.0,
) -> tuple[str, Optional[dict], dict]:
    """
    Fetch a single JSON endpoint.

    Returns:
        (key, data_or_None, health_entry)

    health_entry is always populated with at minimum:
        { "status": <str>, "url": <str> }
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                logger.debug(f"[Telemetry] {key} ok ({url})")
                health = {"status": "ok", "url": url, "http_code": 200}
                return key, data, health

            # Non-200 response — service is up but returning an error
            logger.warning(f"[Telemetry] {key} → HTTP {resp.status} from {url}")
            health = {"status": "http_error", "url": url, "http_code": resp.status}
            return key, None, health

    except asyncio.TimeoutError:
        logger.warning(f"[Telemetry] {key} timed out ({timeout_s}s): {url}")
        health = {"status": "timeout", "url": url}
        return key, None, health

    except aiohttp.ClientConnectorError as e:
        logger.debug(f"[Telemetry] {key} service not reachable: {url}")
        health = {"status": "unreachable", "url": url, "error": str(e)}
        return key, None, health

    except Exception as e:
        logger.warning(f"[Telemetry] {key} unexpected error: {e}")
        health = {"status": "error", "url": url, "error": str(e)}
        return key, None, health


# ---------------------------------------------------------------------------
# Public collect() — called by noc_engine on every telemetry interval
# ---------------------------------------------------------------------------

async def collect(api_urls: Optional[dict] = None) -> dict:
    """
    Collect telemetry from all configured local API sources.

    Args:
        api_urls: Optional dict overriding the default source URLs.

    Returns:
        dict with:
          - One key per source with its data (or None on failure)
          - A top-level "service_health" key with per-service status
    """
    urls = {**DEFAULT_APIS, **(api_urls or {})}

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch(session, key, url) for key, url in urls.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    payload: dict = {}
    service_health: dict = {}

    for result in results:
        if isinstance(result, Exception):
            logger.error(f"[Telemetry] Unexpected gather exception: {result}")
            continue

        key, data, health = result
        payload[key] = data

        # Map telemetry key → well-known service name for the health block
        service_name = _KEY_TO_SERVICE.get(key, key)
        service_health[service_name] = health

    # Always include service_health so NOC server can detect partial failures
    payload["service_health"] = service_health

    # Log a summary of any unhealthy services at INFO level
    failed = [
        f"{svc}={info['status']}"
        for svc, info in service_health.items()
        if info.get("status") != "ok"
    ]
    if failed:
        logger.info(f"[Telemetry] ⚠ Service issues: {', '.join(failed)}")

    return payload
