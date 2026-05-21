# nocServerEnabled — Runtime Gate for NOC Server Connectivity

**Date:** 2026-05-21
**Status:** Approved (design)
**Owner:** kmittal-quenchchargers

## Problem

Today the NOC engine *always* attempts to connect to the NOC server. There is no way to runtime-disable NOC connectivity from the charger without stopping the process. We want a remotely-toggleable kill switch.

## Goal

Introduce a runtime boolean `nocServerEnabled`, polled from the local charging_controller API at `:8003`, that gates whether `NocEngine` opens or maintains a WebSocket connection to the NOC server.

## Source of truth

- **Endpoint:** `GET http://{charger_ip}:{cc_port}/api/v1/config/ocpp/nocServerEnabled`
- **Response shape:** identical to the existing `NocURL` endpoint — `{"success": bool, "value": <truthy/falsy>}`.
- **Truthiness parsing:** `value` is enabled iff it is one of `True`, `"true"`, `"True"`, `"1"`, `1` (case-insensitive string compare). Anything else → disabled.
- **Poll cadence:** every 30 seconds.

## Behavior

|Condition|Engine action|
|---------|-------------|
|`enabled = true`|Full NOC connectivity (current behavior). All per-WS loops run.|
|`enabled = false`|No NOC connectivity. If a WS is open, tear it down within the next watchdog cycle. The engine loop is alive but does not attempt to connect.|
|`false → true` transition|Wake the engine loop and connect immediately (no extra backoff).|
|`true → false` transition|Cancel the live WS task set; the next iteration of `run()` finds `enabled=false` and waits.|
|API unreachable, cache available|Keep last cached value.|
|API unreachable, no cache (cold start)|Default to **enabled** (fail-open; preserves current behavior).|

## State

Added to `NocEngine`:

- `self.noc_enabled: bool` — current effective state. Initialised in `__init__` from `config["noc_server"]["enabled"]`, defaulting to `True` if missing.
- `self._noc_enabled_url: str` — `f"http://{charger_ip}:{cc_port}/api/v1/config/ocpp/nocServerEnabled"`.
- `self._enabled_event: asyncio.Event` — set on every transition so a disabled `run()` wakes immediately on enable.
- `self._rl_noc_enabled: RateLimitedLogger` — rate-limited error logger for endpoint fetch failures, mirroring `_rl_noc_url`.
- `self._enabled_refresh_task: asyncio.Task | None` — engine-lifetime task handle for `_noc_enabled_refresh_loop`, owned by `run()`/`stop()`.

Persisted in `charger_id_cache.json` under key `noc_enabled` (alongside `noc_url`).

Injected at startup into `config["noc_server"]["enabled"]`.

## Components

### 1. Startup fetch — `main.py`

Add `_fetch_noc_enabled() -> bool | None` parallel to `_fetch_noc_url()`. Returns `True`/`False` if the API answered, `None` on any failure.

In `_main()`, after `_fetch_noc_url()`:

```python
enabled = await _fetch_noc_enabled()
if enabled is not None:
    _write_cache({"noc_enabled": enabled})
    config.setdefault("noc_server", {})["enabled"] = enabled
else:
    cached = _read_cache().get("noc_enabled")
    if cached is not None:
        config.setdefault("noc_server", {})["enabled"] = bool(cached)
    else:
        config.setdefault("noc_server", {})["enabled"] = True  # fail-open
```

Update the startup banner to log the resolved `enabled` state.

### 2. Engine lifetime task — `_noc_enabled_refresh_loop()`

A new task spawned **once** in `NocEngine.run()` alongside `_chunked_upload_sweeper_loop` (NOT per-WS). It outlives reconnects.

```python
async def _noc_enabled_refresh_loop(self):
    REFRESH_INTERVAL = 30
    while self._running:
        await asyncio.sleep(REFRESH_INTERVAL)
        new_enabled = await self._fetch_noc_enabled_once()
        if new_enabled is None:
            continue  # API down — keep last state
        self._save_cache({"noc_enabled": new_enabled})
        if new_enabled != self.noc_enabled:
            logger.info(
                f"[NOC-Engine] nocServerEnabled changed: "
                f"{self.noc_enabled} → {new_enabled}"
            )
            self.noc_enabled = new_enabled
            self._enabled_event.set()  # wake run() if waiting
```

`_fetch_noc_enabled_once()` is a small helper using `self.http_session` and `self._rl_noc_enabled` for error rate-limiting. Returns `True | False | None`.

### 3. Per-WS guard task — `_enabled_guard_loop(ws)`

Added to the per-WS task set in `run()` so the existing `FIRST_COMPLETED` machinery handles teardown:

```python
async def _enabled_guard_loop(self, ws: WSClient):
    """If noc_enabled flips false while connected, exit to trigger reconnect/wait."""
    while True:
        await asyncio.sleep(1)
        if not ws.connected:
            return
        if not self.noc_enabled:
            logger.info("[NOC-Engine] nocServerEnabled=false — closing WS")
            return  # FIRST_COMPLETED → cancels siblings → run() loop iterates
```

1s poll keeps the response time tight without burning CPU; matches existing `_watchdog_loop` cadence floor.

### 4. `run()` loop changes

Before each connect attempt:

```python
while self._running:
    if not self.noc_enabled:
        # Clear before waiting so a stale set() doesn't immediately wake us
        self._enabled_event.clear()
        logger.info("[NOC-Engine] nocServerEnabled=false — sleeping until enabled")
        await self._enabled_event.wait()
        if not self._running:
            break
        continue
    # existing connect/auth/task-set logic
```

Spawn the engine-lifetime tasks once (before the `while`):

```python
self._enabled_refresh_task = asyncio.create_task(
    self._noc_enabled_refresh_loop(), name="noc_enabled_refresh"
)
```

Add the guard to the per-WS task set:

```python
tasks = [
    ...existing tasks...,
    asyncio.create_task(self._enabled_guard_loop(ws), name="enabled_guard"),
]
```

In `stop()`, cancel `_enabled_refresh_task` and set `_enabled_event` so any waiting `run()` exits.

## Data flow

```text
charging_controller :8003
        │
        │ GET /api/v1/config/ocpp/nocServerEnabled  (every 30s)
        ▼
_noc_enabled_refresh_loop ──► self.noc_enabled ──► self._enabled_event.set()
        │                            │
        │                            ├──► run() while-loop gate (connect/wait)
        │                            └──► _enabled_guard_loop (per-WS, 1s poll)
        ▼
charger_id_cache.json {"noc_enabled": bool}
```

## Logging

- **INFO** only on actual transitions and on enabled=false sleep entry/exit. Steady-state polling is silent.
- **Errors** routed through `self._rl_noc_enabled` (RateLimitedLogger) — matches the [[feedback]] pattern from the recent logging-noise-reduction work.

## Error handling

- Endpoint 4xx/5xx, network error, JSON parse error → treat as "API unreachable" → keep last state; log via rate-limited exception.
- `_enabled_guard_loop` exiting is normal (either disable detected or WS closed). It does NOT need a try/except.

## Testing

New test file `tests/test_noc_engine_enabled_flag.py`:

1. `test_cold_start_defaults_enabled_when_no_api_and_no_cache`
2. `test_cold_start_uses_cache_when_api_down`
3. `test_truthy_parsing` — covers `True`, `"true"`, `"1"`, `1`, `"false"`, `0`, `""`, `None`.
4. `test_transition_true_to_false_closes_ws` — fake WS, flip flag, assert guard loop returns within ~2s.
5. `test_transition_false_to_true_wakes_run_loop` — set `_enabled_event`, assert `run()` exits the wait.
6. `test_api_unreachable_preserves_last_state` — set `noc_enabled=True`, simulate fetch error, confirm unchanged.
7. `test_persists_to_cache` — confirm cache file written on transitions.

Mock `aiohttp.ClientSession.get` using the same patterns in [tests/test_noc_engine_uploads.py](../../../tests/test_noc_engine_uploads.py).

## Out of scope

- Server-pushed enable/disable (we poll, we don't subscribe).
- Multiple flags / a generic feature-flag framework — this is one specific kill switch.
- Disabling local API servers (`VersionAPIServer`) or SSH tunnel manager when disabled — only the NOC WebSocket connection is gated.

## Versioning & artifacts

Per global instructions:

- Bump `VERSION` (1.1.5 → 1.2.0; new feature, minor bump).
- Update `CHANGELOG.md`.
- Regenerate `graphify-out/` with `graphify update <repo>`.

## Files touched

- `main.py` — add `_fetch_noc_enabled`, call from `_main`, banner update.
- `noc_engine.py` — new state, `_fetch_noc_enabled_once`, `_noc_enabled_refresh_loop`, `_enabled_guard_loop`, `run()` gate, `stop()` cleanup.
- `config.json` — no required changes; `noc_server.enabled` is injected at runtime.
- `tests/test_noc_engine_enabled_flag.py` — new.
- `VERSION`, `CHANGELOG.md`, `graphify-out/` — versioning artifacts.
