#!/usr/bin/env python3
"""
NocEngine — Entry Point
========================
Run this as a standalone process alongside other A-core modules.

Usage:
    cd A-core/NocEngine
    python main.py

    # Or with a custom config path:
    python main.py --config /path/to/config.json

Configuration:
    Edit config.json to set your NOC server host/port and charger identity.
    The charger_id is resolved automatically at startup by polling the local
    charging_controller API (charge_box_serial_number) every 5 seconds until
    it succeeds — no fallback to config.json is used.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = os.environ.get(
    "NOC_ENGINE_CONFIG", "/app/qflex/config/noc_engine/config.json"
)

parser = argparse.ArgumentParser(description="NocEngine — QFlex Remote Management")
parser.add_argument(
    "--config",
    default=_DEFAULT_CONFIG_PATH,
    help=f"Path to config.json (default: {_DEFAULT_CONFIG_PATH})",
)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------

config_path = Path(args.config)
if not config_path.exists():
    print(f"[NocEngine] ERROR: Config file not found: {config_path}", file=sys.stderr)
    sys.exit(1)

with open(config_path, "r") as f:
    config = json.load(f)

# ---------------------------------------------------------------------------
# Configure logging  (done early so all subsequent log calls work)
# ---------------------------------------------------------------------------

log_cfg = config.get("logging", {})
logging.basicConfig(
    level=getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO),
    format=log_cfg.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"),
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import engine (after sys.path is set up)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
from noc_engine import NocEngine

# ---------------------------------------------------------------------------
# Charger ID cache  (stored alongside config.json)
# ---------------------------------------------------------------------------

CHARGER_ID_CACHE_FILE = config_path.parent / "charger_id_cache.json"


def _read_cache() -> dict:
    """Read the runtime-discovered cache file. Returns {} on any error."""
    if not CHARGER_ID_CACHE_FILE.exists():
        return {}
    try:
        with open(CHARGER_ID_CACHE_FILE, "r") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning(f"[NocEngine] Failed to read cache: {e}")
        return {}


def _write_cache(updates: dict) -> None:
    """Merge `updates` into the cache file (preserves other fields)."""
    data = _read_cache()
    data.update(updates)
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(CHARGER_ID_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"[NocEngine] Failed to write cache: {e}")


def _load_cached_charger_id() -> str | None:
    """Load charger_id from persistent cache file."""
    data = _read_cache()
    ocpp = str(data.get("ocpp_serial", "")).strip()
    hw   = str(data.get("hw_serial", "")).strip()
    if ocpp and hw:
        return f"{ocpp}-{hw}"
    return None


def _save_cached_charger_id(ocpp_serial: str, hw_serial: str) -> None:
    """Save charger ID components to persistent cache file."""
    _write_cache({"ocpp_serial": ocpp_serial, "hw_serial": hw_serial})


def _load_cached_noc_url() -> str | None:
    """Load NOC URL from persistent cache file."""
    url = str(_read_cache().get("noc_url", "")).strip()
    return url or None


def _save_cached_noc_url(noc_url: str) -> None:
    """Save NOC URL to persistent cache file."""
    _write_cache({"noc_url": noc_url})


# ---------------------------------------------------------------------------
# Charger ID resolution — tries APIs once, falls back to cache, no blocking
# ---------------------------------------------------------------------------

async def _fetch_noc_url() -> str | None:
    """
    Fetch NOC WebSocket URL from the local charging_controller API.

    Endpoint: GET /api/v1/config/ocpp/NocURL
    Returns the 'value' field if successful, otherwise None.
    """
    import aiohttp

    charger_ip = config.get("charger_ip", "localhost")
    cc_port    = config.get("charger_ports", {}).get("charging_controller", 8003)
    url        = f"http://{charger_ip}:{cc_port}/api/v1/config/ocpp/NocURL"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data    = await resp.json()
                    noc_url = str(data.get("value", "")).strip()
                    if data.get("success") and noc_url:
                        logger.info(f"[NocEngine] ✅ NOC URL    : {noc_url}")
                        return noc_url
                    else:
                        logger.warning(f"[NocEngine] NOC URL empty or unsuccessful: {data}")
                else:
                    logger.warning(f"[NocEngine] NOC URL fetch failed: HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"[NocEngine] NOC URL fetch error: {e}")

    return None


async def _fetch_charger_id() -> str:
    """
    Resolve charger_id by polling local APIs once.

    If BOTH APIs respond, the IDs are cached to disk and returned.
    If one or both APIs fail, the last cached IDs are used as fallback
    so NocEngine can start immediately.

    A background task in NocEngine continues retrying every 10s.

    Final charger_id = "<charge_box_serial_number>-<hw_serial_number>"
    """
    import aiohttp

    charger_ip = config.get("charger_ip", "localhost")
    ports = config.get("charger_ports", {})
    cc_port = ports.get("charging_controller", 8003)
    sys_port = ports.get("system_api", 8000)

    ocpp_url = (
        f"http://{charger_ip}:{cc_port}"
        f"/api/v1/config/ocpp/charge_box_serial_number"
    )
    hw_url = f"http://{charger_ip}:{sys_port}/api/v1/system/hardware"

    logger.info(f"[NocEngine] 🔍 Resolving charger ID ...")
    logger.info(f"[NocEngine]    OCPP serial : {ocpp_url}")
    logger.info(f"[NocEngine]    HW serial   : {hw_url}")

    ocpp_serial: str | None = None
    hw_serial: str | None = None

    async with aiohttp.ClientSession() as session:
        # --- fetch OCPP charge_box_serial_number ---
        try:
            async with session.get(
                ocpp_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    serial = data.get("value", "").replace("\x00", "").strip()
                    if data.get("success") and serial:
                        ocpp_serial = serial
                        logger.info(f"[NocEngine] ✅ OCPP serial: {ocpp_serial}")
                else:
                    logger.warning(f"[NocEngine] OCPP serial fetch: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"[NocEngine] OCPP serial fetch error: {e}")

        # --- fetch hardware serial_number ---
        try:
            async with session.get(
                hw_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    serial = str(data.get("serial_number", "")).replace("\x00", "").strip()
                    if data.get("success") and serial:
                        hw_serial = serial
                        logger.info(f"[NocEngine] ✅ HW serial  : {hw_serial}")
                else:
                    logger.warning(f"[NocEngine] HW serial fetch: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"[NocEngine] HW serial fetch error: {e}")

    # Both APIs succeeded — cache and return
    if ocpp_serial and hw_serial:
        _save_cached_charger_id(ocpp_serial, hw_serial)
        charger_id = f"{ocpp_serial}-{hw_serial}"
        logger.info(f"[NocEngine] ✅ Charger ID  : {charger_id}")
        return charger_id

    # One or both failed — try cache
    cached = _load_cached_charger_id()
    if cached:
        logger.warning(
            f"[NocEngine] ⚠️ APIs unavailable — using cached charger ID: {cached}"
        )
        return cached

    # Nothing available — last resort: config fallback
    config_id = config.get("charger_id", "UNKNOWN-CHARGER")
    logger.error(
        f"[NocEngine] ❌ Could not resolve charger ID from APIs or cache — "
        f"using config fallback: {config_id}"
    )
    return config_id

# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------

_engine: NocEngine | None = None


async def _main():
    global _engine

    # 1. Block here until we have the real hardware serial number
    charger_id = await _fetch_charger_id()
    config["charger_id"] = charger_id

    # 1b. Fetch dynamic NOC URL from local API.
    #     Fallback chain: API → cache → config.json (host/port).
    noc_url = await _fetch_noc_url()
    if noc_url:
        _save_cached_noc_url(noc_url)
        config.setdefault("noc_server", {})["url"] = noc_url
    else:
        cached_url = _load_cached_noc_url()
        if cached_url:
            logger.warning(
                f"[NocEngine] ⚠️ NOC URL API unavailable — using cached URL: {cached_url}"
            )
            config.setdefault("noc_server", {})["url"] = cached_url
        else:
            logger.warning("[NocEngine] Falling back to NOC server from config.json")

    # 2. Startup banner (printed after charger_id is confirmed)
    logger.info("=" * 60)
    logger.info("  NocEngine — QFlex Remote Charger Management")
    logger.info("=" * 60)
    logger.info(f"  Charger ID   : {charger_id}")
    logger.info(f"  Model        : {config.get('model', '-')}")
    logger.info(f"  Firmware     : {config.get('firmware_version', '-')}")
    noc = config.get("noc_server", {})
    noc_url = noc.get("url")
    if noc_url:
        logger.info(f"  NOC Server   : {noc_url} (from API)")
    else:
        logger.info(f"  NOC Server   : {noc.get('host', 'localhost')}:{noc.get('port', 8080)} (from config)")
    charger_ip = config.get("charger_ip", "localhost")
    mode = "ON-CHARGER (production)" if charger_ip == "localhost" else f"REMOTE TEST → {charger_ip}"
    logger.info(f"  Charger IP   : {charger_ip}  [{mode}]")
    ports = config.get("charger_ports", {})
    logger.info(
        f"  APIs target  : {charger_ip}:{ports.get('charging_controller', 8003)}"
        f" / :{ports.get('allocation_engine', 8002)}"
        f" / :{ports.get('error_generation', 8006)}"
    )
    logger.info(f"  Telemetry    : every {config.get('telemetry', {}).get('interval_seconds', 30)}s")
    logger.info("=" * 60)

    # 3. Start the engine
    _engine = NocEngine(config, charger_id_cache_file=str(CHARGER_ID_CACHE_FILE))
    await _engine.run()


def _shutdown(sig, frame):
    logger.info(f"\n[NocEngine] Signal {sig} received — shutting down...")
    if _engine:
        _engine.stop()
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("[NocEngine] Interrupted by user")
