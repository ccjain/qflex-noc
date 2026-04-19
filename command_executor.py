#!/usr/bin/env python3
"""
NocEngine Command Executor
===========================
Executes proxy commands received from the NOC server.

When the NOC server sends a command like:
    {
        "command_id": "cmd-001",
        "method": "GET",
        "target_port": 8003,
        "path": "/api/v1/charging/status",
        "headers": {"accept": "application/json"},
        "body": null
    }

This module forwards the request to the target charger IP + port.

Modes:
  On-charger   (charger_ip='localhost') → hits local services
  Remote test  (charger_ip='172.16.14.123') → hits real charger over LAN/WiFi

Security:
  - Only whitelisted ports are allowed (8003, 8002, 8006)
  - Maximum 10s timeout per command
"""

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Security: only these service ports are allowed
ALLOWED_PORTS = {8003, 8002, 8006}

# Port → service name (for logging)
PORT_NAMES = {
    8003: "charging_controller",
    8002: "allocation_engine",
    8006: "error_generation",
}

COMMAND_TIMEOUT_S = 10.0


async def execute(command: dict, charger_ip: str = "localhost") -> dict:
    """
    Execute a proxy command received from the NOC server.

    Args:
        command:    Dict with keys: command_id, method, target_port, path,
                    headers (optional), body (optional)
        charger_ip: IP/hostname of the target charger.
                    'localhost' when running on the charger (default).
                    Real IP like '172.16.14.123' for remote/test mode.

    Returns:
        Dict with keys: command_id, status_code, response, execution_time_ms
    """
    command_id     = command.get("command_id", "unknown")
    method         = command.get("method", "GET").upper()
    target_port    = command.get("target_port", 8003)
    path           = command.get("path", "/")
    headers        = command.get("headers", {"accept": "application/json"})
    body           = command.get("body")

    # ---------------------------------------------------------------
    # Security check: reject disallowed ports
    # ---------------------------------------------------------------
    if target_port not in ALLOWED_PORTS:
        logger.warning(
            f"[Executor] cmd={command_id} REJECTED — port {target_port} not in whitelist "
            f"{sorted(ALLOWED_PORTS)}"
        )
        return {
            "command_id": command_id,
            "status_code": 403,
            "response": {
                "error": f"Port {target_port} is not allowed.",
                "allowed_ports": sorted(ALLOWED_PORTS),
            },
            "execution_time_ms": 0,
        }

    url     = f"http://{charger_ip}:{target_port}{path}"
    service = PORT_NAMES.get(target_port, f"port-{target_port}")

    logger.info(f"[Executor] cmd={command_id} → {method} {url} [{service}]")

    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            kwargs: dict[str, Any] = {
                "url": url,
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_S),
            }

            # Attach request body for mutating methods
            if body is not None and method in ("POST", "PUT", "PATCH"):
                kwargs["json"] = body

            async with session.request(method, **kwargs) as resp:
                elapsed_ms = int((time.monotonic() - start) * 1000)

                # Try JSON first; fall back to raw text
                try:
                    response_body = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    response_body = {"raw": text}

                logger.info(
                    f"[Executor] cmd={command_id} status={resp.status} "
                    f"time={elapsed_ms}ms"
                )
                return {
                    "command_id": command_id,
                    "status_code": resp.status,
                    "response": response_body,
                    "execution_time_ms": elapsed_ms,
                }

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(f"[Executor] cmd={command_id} TIMEOUT after {elapsed_ms}ms")
        return {
            "command_id": command_id,
            "status_code": 504,
            "response": {"error": f"Command timed out after {COMMAND_TIMEOUT_S}s"},
            "execution_time_ms": elapsed_ms,
        }

    except aiohttp.ClientConnectorError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"[Executor] cmd={command_id} connection error: {e}")
        return {
            "command_id": command_id,
            "status_code": 503,
            "response": {"error": f"Could not connect to {service} on port {target_port}"},
            "execution_time_ms": elapsed_ms,
        }

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"[Executor] cmd={command_id} unexpected error: {e}", exc_info=True)
        return {
            "command_id": command_id,
            "status_code": 500,
            "response": {"error": str(e)},
            "execution_time_ms": elapsed_ms,
        }
