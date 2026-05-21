# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and adhere to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] — 2026-05-21

### Added

#### `nocServerEnabled` runtime kill switch

- New boolean flag polled every 30s from `:8003/api/v1/config/ocpp/nocServerEnabled`. When `true`, the engine maintains its NOC WebSocket as before; when `false`, the engine stays alive but holds *no* NOC connectivity. A `false → true` transition wakes the engine and reconnects immediately; a `true → false` transition tears down the live WS within ~1s and parks the run loop on an `asyncio.Event`.
- **Startup (`main.py`).** `_fetch_noc_enabled()` mirrors `_fetch_noc_url()`: API → cache → fail-open default `True`. The resolved value is injected into `config["noc_server"]["enabled"]` and persisted in `charger_id_cache.json` under `noc_enabled`. New helpers: `_parse_enabled()`, `_load_cached_noc_enabled()`, `_save_cached_noc_enabled()`.
- **Engine (`noc_engine.py`).** New state: `self.noc_enabled`, `self._noc_enabled_url`, `self._enabled_event`, `self._enabled_refresh_task`, and the `self._rl_noc_enabled` rate-limited error logger. Three new methods: `_parse_enabled_value` (static, strict truthy parser — only `True`/`"true"`/`"1"`/`1` count), `_fetch_noc_enabled_once`, `_noc_enabled_refresh_loop` (engine-lifetime, 30s cadence), and `_enabled_guard_loop` (per-WS, 1s cadence). `run()` now gates on `self.noc_enabled` before each connect attempt and adds the guard to the per-WS task set so a `true → false` flip rides the existing `asyncio.wait(FIRST_COMPLETED)` teardown. `stop()` cancels the refresh task and sets the event so a parked `run()` exits.
- **Logging.** Steady-state polling is silent; only actual transitions and disable/resume entry log at INFO. Endpoint errors go through `RateLimitedLogger.exception(...)` matching the [[1.1.5]] pattern.
- **Behavior unchanged when API is absent.** Fail-open (default `True`) preserves all existing connectivity.
- **Tests.** New `tests/test_noc_engine_enabled_flag.py` (34 tests): constructor defaults, strict-truthy parsing, fetch happy/error paths, refresh-loop transition + steady-state + API-unreachable, guard-loop disable/disconnect/healthy behavior.

## [1.1.5] — 2026-05-16

### Changed

#### Logging noise reduction — `noc_engine.log` 95 MB/day → <10 MB/day

- **Why.** A 24h capture (`qflex_logs_full_20260515_114015/noc-engine/noc_engine.log`, 856 113 lines, 95 MB — the biggest log of the four QFlex modules) showed **86% was DEBUG noise** (732 905 DEBUG lines). The single worst contributor was the `websockets.client` library at 469 536 lines/day (≈55% of the file) — TEXT-frame dumps and keepalive ping/pong traces with no application-level signal. Application code added another ~250 K lines of per-tick INFO/DEBUG: heartbeat-sent (89 503), telemetry-sent (21 931 INFO), incoming-WS (21 932 INFO), per-endpoint telemetry OK (~128 K DEBUG), session-sync send-success (11 885 DEBUG), zero-history fetch (10 116 DEBUG), and refresh-loop connection errors (~7 000 DEBUG with no stack frame). Meanwhile genuine errors were tiny (45 ERROR / 62 WARNING in 856 K lines) — they were getting buried.
- **New helpers (`log_helpers.py`).** Three primitives plus a library-config call:
  - `log_on_change(logger, key, value, msg, threshold=None)` — emits only when a tracked value changes.
  - `RateLimitedLogger(logger, key, schedule=(1, 10, 100, 1000))` — backoff schedule + a single "recovered" INFO line on `.ok()`.
  - `PeriodicSummary(logger, key, window_seconds)` — buffers per-window counts and emits one INFO/window; also tracks OK/FAIL status and emits an immediate flip line on a transition.
  - `_configure_library_loggers()` — sets `websockets`, `websockets.client`, `websockets.server`, `websockets.protocol`, `httpx`, `asyncio` to `WARNING`. Called once from `main.py` at startup. **Removes ~470 000 lines/day on its own.**
- **Per-site changes:**
  - `main.py` — calls `_configure_library_loggers()` immediately after `logging.basicConfig(...)`. Once-per-process startup INFO lines (`✅ OCPP serial`, `✅ HW serial`, `✅ Charger ID`, `✅ NOC URL`, startup banner) are explicitly KEPT with inline `# KEEP` comments to deter future cleanups.
  - `noc_engine.py` — `_heartbeat_loop` log demoted below DEBUG (level 5) so even DEBUG mode skips it (-89 503 lines/day). `_telemetry_loop` per-tick INFO replaced by a 1-hour `PeriodicSummary` with immediate OK↔FAIL flip lines (-21 931 INFO/day). `_receive_loop` unconditional `📥 INCOMING WS MESSAGE` INFO demoted to DEBUG (-21 932 INFO/day); the `proxy_request` branch now also feeds a 60s `PeriodicSummary`. `_charger_id_refresh_loop` (OCPP + HW fetches) and `_noc_url_refresh_loop` now use per-endpoint `RateLimitedLogger.exception(...)` — first failure captures the stack frame; recovery is announced via `.ok()`.
  - `telemetry_collector.py` — `_fetch` per-call OK demoted to TRACE (level 5); per-endpoint status flips emitted via `log_on_change`. The bare `except Exception as e:` is now `logger.exception(...)`.
  - `session_sync.py` — `Fetched N history sessions` only emits when `N>0` or on a 0↔N transition (via `log_on_change`). `Sent session_sync to NOC Server` demoted to TRACE. All three `Failed to fetch …` ERROR sites use `logger.exception(...)`.
  - `ssh_tunnel.py`, `command_executor.py` — sweep of `except ... as e: logger.error(f"…{e}…")` → `logger.exception(...)`. Redundant `traceback.format_exc()` lines deleted.
- **Behavior unchanged.** No control-flow edits. Existing tests pass unchanged.
- **New tests.** `tests/test_log_helpers.py` (7 tests) covers the helpers and library-config call. `tests/test_logging_noise_reduction.py` covers each touched call site (`TestTelemetrySummary`, `TestIncomingWSMessageSummary`, `TestTelemetryFetchPerEndpointFlip`, `TestSessionSyncQuiet`, `TestRefreshLoopsRateLimited`, `TestSessionSyncExceptionLogging`).
- **Estimated reduction.** Library silencing (-470 K) + heartbeat demote (-89 K) + telemetry summary (-21 K) + incoming-WS demote (-21 K) + telemetry-OK to TRACE (-128 K) + session-sync demotes (-22 K) + refresh-loop rate-limit (-7 K) ≈ **758 K lines/day removed** (≈88% of the original 856 K).

## [1.1.4] — 2026-05-14

### Changed
- Watchdog rewritten to monitor **all** WS activity (inbound messages, outbound messages, and ping/pong) instead of only inbound messages. Idle threshold raised from 60s to 120s. Activity tracking moved into `WSClient` via `last_activity_at` (monotonic), which is updated on every `send()`, `receive()`, and pong callback. The watchdog now only forces a reconnect when the connection is truly dead in both directions.

### Fixed
- Restored the watchdog task in `run()` — it was accidentally removed in `d374292`.

## [1.1.3] — 2026-05-13

### Added
- Chunked upload now verifies a SHA-256 hash of the fully-reassembled payload before persisting. The client sends `sha256` alongside the final chunk; on mismatch the upload is rejected with a `hash_mismatch` error and the in-memory chunk state is discarded. Closes the gap where a silently corrupted multi-chunk transfer (truncated chunk, dropped middle frame) could be accepted as valid.

## [1.1.2] — 2026-05-10

### Added
- `scripts/zip_source.py` — dev utility that exports the project as a filtered folder (`<module>_opentest/`) and a timestamped zip (`<module>_dtd_DDMMYY_HHMMSS.zip`). Honors an exclusion list (caches, virtualenvs, graphify-out, tests, docs) and an extension allow-list (`.py .json .js .html .css`). Supports `--dry-run`, `--skip-folder`, `--skip-zip`, custom `--out-dir`, and `--include-ext`.

## [1.1.1] — 2026-05-10

### Fixed
- `_handle_proxy_request` no longer raises `ConnectionError: WebSocket not connected` as an unhandled task exception. When the WS closes mid-request, the response is dropped (logged as a warning) instead of attempting two doomed `ws.send` calls. The previous code re-sent into the same closed socket inside the except handler, producing `Task exception was never retrieved`.

### Changed
- Version poll cadence relaxed from 10s → 20s between the 5 startup polls of `:8003/api/v1/system/version`. Reduces local API load right after every reconnect.

## [1.1.0] — 2026-05-10

### Added
- Test suite (pytest + pytest-asyncio) under `tests/`.
- `VERSION` file and this changelog.

### Changed
_TBD per task_

### Fixed
- WS `ping_timeout` was `None`; a frozen server left the engine hung indefinitely. Now `20s`.
- `WSClient.connect` now uses `open_timeout=15` so an unreachable server fails fast.
- `WSClient.disconnect` is now hard-bounded at 6s and logs unexpected close errors instead of swallowing them.
- `WSClient.send` now wraps the underlying drain with `asyncio.wait_for(send_timeout=10s)`. A stuck server can no longer freeze every concurrent loop.

### Added
- New inbound-message watchdog: the engine forces a reconnect if it hasn't received any WS message within `inbound_idle_timeout` (default 60s). Closes the gap left by one-way application heartbeats and `asyncio.wait(FIRST_COMPLETED)` blocking. Polling cadence is capped at 5s so disconnects are noticed quickly even with high thresholds.
- Background sweeper for chunked-upload state with a 5-minute TTL.
- Local HTTP API on port 8009: `GET /api/v1/noc_engine/version` and `/api/v1/noc_engine/health`.
- `charger_ports.noc_engine_api` (default 8009) configurable in `config.json`.

### Changed
- `_chunked_uploads` moved off the module global onto the `NocEngine` instance; orphaned upload state (server dropped before final chunk) is now garbage-collected instead of leaking forever.
- Fire-and-forget background tasks (command/proxy/upload/ssh handlers) are now tracked, cancelled on disconnect, and their exceptions logged.
- Engine now holds a single shared `aiohttp.ClientSession` and passes it to telemetry, command executor, and session sync. Eliminates per-call session churn (FD/ephemeral-port pressure under load).
- SSH tunnel reader now bounds each `ws_send_callback` call at 5s. WS back-pressure can no longer pin the tunnel forever; tunnels are closed cleanly instead.
- `_send_auth` no longer blocks for the full 5s aiohttp timeout when the local OCPP firmware endpoint is slow; firmware fetch is bounded at 1.5s and is best-effort.
