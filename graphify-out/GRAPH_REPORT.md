# Graph Report - qflex-noc  (2026-05-21)

## Corpus Check
- 24 files · ~32,353 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 469 nodes · 1176 edges · 48 communities detected
- Extraction: 39% EXTRACTED · 61% INFERRED · 0% AMBIGUOUS · INFERRED: 714 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]

## God Nodes (most connected - your core abstractions)
1. `SessionSyncManager` - 135 edges
2. `WSClient` - 134 edges
3. `SSHTunnelManager` - 133 edges
4. `VersionAPIServer` - 127 edges
5. `NocEngine` - 117 edges
6. `RateLimitedLogger` - 83 edges
7. `PeriodicSummary` - 72 edges
8. `_cfg()` - 16 edges
9. `_MockResponse` - 11 edges
10. `_MockSession` - 10 edges

## Surprising Connections (you probably didn't know these)
- `Read the runtime-discovered cache file. Returns {} on any error.` --uses--> `NocEngine`  [INFERRED]
  main.py → noc_engine.py
- `Merge `updates` into the cache file (preserves other fields).` --uses--> `NocEngine`  [INFERRED]
  main.py → noc_engine.py
- `Load charger_id from persistent cache file.` --uses--> `NocEngine`  [INFERRED]
  main.py → noc_engine.py
- `NocEngine` --uses--> `Save NOC URL to persistent cache file.`  [INFERRED]
  noc_engine.py → main.py
- `Save NOC URL to persistent cache file.` --uses--> `NocEngine`  [INFERRED]
  main.py → noc_engine.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (77): Post complete file to dev-tools service on port 8005., Post complete file to dev-tools service on port 8005., Main engine loop. Connects to NOC server and starts all sub-loops.         On d, Drop any chunked-upload entry that hasn't completed within the TTL., Background sweeper — runs at TTL/4 intervals as long as the engine is running., Main engine loop. Connects to NOC server and starts all sub-loops.         On d, Main engine loop. Connects to NOC server and starts all sub-loops.         On d, Stop the NOC engine and cleanup resources. (+69 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (61): Load charger_id from persistent cache file., Save charger ID components to persistent cache file., Load NOC URL from persistent cache file., Load NOC URL from persistent cache file., Fetch NOC WebSocket URL from the local charging_controller API.      Endpoint:, Fetch NOC WebSocket URL from the local charging_controller API.      Endpoint:, Resolve charger_id by polling local APIs once.      If BOTH APIs respond, the, Resolve charger_id by polling local APIs once.      If BOTH APIs respond, the (+53 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (41): PeriodicSummary, RateLimitedLogger, Like ``error`` but always captures the current exception's traceback., Buffer per-window counts and flush one INFO line per ``window_seconds``.      Tw, Emit errors on a backoff schedule (default 1, 10, 100, 1000) when a     conditio, Post complete file to dev-tools service on port 8005., Main engine loop. Connects to NOC server and starts all sub-loops.         On d, Stop the NOC engine and cleanup resources. (+33 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (17): Handle file upload chunk from NOC Server. Accumulates chunks and posts to port 8, Post complete file to dev-tools service on port 8005., Main engine loop. Connects to NOC server and starts all sub-loops.         On d, Stop the NOC engine and cleanup resources., Fetch firmware version from the charger's OCPP config endpoint.         Returns, Poll :8003/api/v1/system/version five times at 20-second intervals         imme, Send heartbeat every N seconds to keep the connection alive., Collect and push charger telemetry every N seconds.          Per-tick INFO lin (+9 more)

### Community 4 - "Community 4"
Cohesion: 0.12
Nodes (25): _fetch_charger_id(), _fetch_noc_enabled(), _fetch_noc_url(), _load_cached_charger_id(), _load_cached_noc_enabled(), _load_cached_noc_url(), _main(), _parse_enabled() (+17 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (14): LocalSSHTunnel, Create a new TCP connection to local SSHD.                  Args:, Decode Base64 data and write to SSHD connection.                  Args:, Background task: Read from SSHD, Base64 encode, send via WebSocket., Represents a single SSH tunnel from WebSocket to local SSHD.          Attribut, Close tunnel and cleanup resources.          Args:             tunnel_id: Tun, Calculate tunnel statistics., Return statistics for all active tunnels. (+6 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (20): _configure_library_loggers(), log_on_change(), Reusable logging helpers for keeping noc_engine.log signal-rich.  Primitives ---, Silence third-party libraries that flood DEBUG output.      Idempotent — safe to, Marker used by ``log_on_change`` to distinguish "never logged" from a     real p, Wipe all module-level caches. Test-only — never call at runtime., Emit ``msg`` at ``level`` only when ``value`` differs from the previously     lo, reset_log_caches() (+12 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (12): main(), Start the session sync loop., Stop the session sync loop., Main sync loop - runs every 30 seconds., Perform one sync cycle., Fetch active sessions from local APIs for both guns., Detect sessions that were active but are now completed., Filter out sessions we've already sent to NOC Server. (+4 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (8): Behavioral tests for the qflex-noc logging-noise reduction sites., [NOC-Engine] 📡 Telemetry sent — should be a 1h-windowed summary, not per-tick IN, TestIncomingWSMessageSummary, TestRefreshLoopsRateLimited, TestSessionSyncExceptionLogging, TestSessionSyncQuiet, TestTelemetryFetchPerEndpointFlip, TestTelemetrySummary

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (8): _now_iso(), _parse_enabled_value(), Build the WebSocket URI from current noc_url (or host/port) + charger_id., Merge `updates` into the runtime cache file (preserves other fields)., Poll local APIs every 10s to refresh charger ID and update cache.          On, Force a reconnect if no inbound WS message has arrived recently.          Retu, Receive messages from NOC server and dispatch them.         Handles:, _read_engine_version()

### Community 10 - "Community 10"
Cohesion: 0.16
Nodes (7): If the underlying close() hangs, disconnect must still return within ~6s., A send() that drains slower than send_timeout must raise asyncio.TimeoutError., ping_timeout MUST be a finite value so the websockets library can detect a froze, test_connect_passes_open_timeout(), test_connect_passes_ping_timeout_20(), test_disconnect_is_bounded_when_close_hangs(), test_send_raises_on_drain_stall()

### Community 11 - "Community 11"
Cohesion: 0.2
Nodes (5): Called when a pong frame arrives from the server., Serialize and send a JSON message, bounded by ``send_timeout``.          Raise, Receive the next JSON message.          Raises:             ConnectionError:, Record that WS-level activity just occurred., Open WebSocket connection to the NOC server.

### Community 12 - "Community 12"
Cohesion: 0.31
Nodes (8): copy_to_folder(), iter_matching_files(), main(), Export the qflex-charging project, keeping only .py, .json, .js, and .html files, Write ``files`` to ``zip_path`` under a top-level directory ``arc_root``., Yield files under ``root`` whose suffix is in ``include_exts``.      Skips any, Copy each file under ``folder_root`` preserving its path relative to PROJECT_ROO, write_zip()

### Community 13 - "Community 13"
Cohesion: 0.29
Nodes (5): echo_ws_server(), Shared pytest fixtures: free-port allocator, controllable WS servers., A WS server that echoes every message back., A WS server that accepts the connection but never reads/writes — simulates a fro, silent_ws_server()

### Community 14 - "Community 14"
Cohesion: 0.5
Nodes (2): Args:             noc_ws_client: WebSocket client connected to NOC Server, Load sync state from local JSON file.

### Community 15 - "Community 15"
Cohesion: 0.67
Nodes (2): execute(), Execute a proxy command received from the NOC server.      Args:         comm

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): NocEngine — Remote Charger Management Module ===================================

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Monotonic timestamp of the most recent WS activity (send/recv/pong).

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Manages session data synchronization from charger APIs to NOC Server.

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Args:             noc_ws_client: WebSocket client connected to NOC Server

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Load sync state from local JSON file.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Persist sync state to local JSON file.

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Start the session sync loop.

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Stop the session sync loop.

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): Main sync loop - runs every 30 seconds.

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): Perform one sync cycle.

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): Fetch active sessions from local APIs for both guns.

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): Detect sessions that were active but are now completed.

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (1): Fetch session history from local API (incremental).

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (1): Calculate hours parameter based on last sync time.

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (1): Send session data to NOC Server via WebSocket.

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (1): Update internal state after successful sync.

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (1): Example usage for testing.

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (1): Decode Base64 data and write to SSHD connection.                  Args:

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (1): Background task: Read from SSHD, Base64 encode, send via WebSocket.

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): Close tunnel and cleanup resources.          Args:             tunnel_id: Tun

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): Calculate tunnel statistics.

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): Return number of active tunnels.

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (1): Return list of active tunnel IDs.

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Return statistics for all active tunnels.

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): Close all tunnels (used during shutdown).

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (1): Fetch a single JSON endpoint.      Returns:         (key, data_or_None, healt

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (1): Collect telemetry from all configured local API sources.      Args:         a

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (1): Thin WebSocket wrapper used by NocEngine.      Usage:         client = WSClie

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Open WebSocket connection to the NOC server.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Close the WebSocket connection gracefully (bounded).

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Serialize and send a JSON message, bounded by ``send_timeout``.          Raise

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Receive the next JSON message.          Raises:             ConnectionError:

## Knowledge Gaps
- **100 isolated node(s):** `Execute a proxy command received from the NOC server.      Args:         comm`, `Reusable logging helpers for keeping noc_engine.log signal-rich.  Primitives ---`, `Marker used by ``log_on_change`` to distinguish "never logged" from a     real p`, `Wipe all module-level caches. Test-only — never call at runtime.`, `Emit ``msg`` at ``level`` only when ``value`` differs from the previously     lo` (+95 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (4 nodes): `Args:             noc_ws_client: WebSocket client connected to NOC Server`, `Load sync state from local JSON file.`, `.__init__()`, `._load_state()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 15`** (3 nodes): `command_executor.py`, `execute()`, `Execute a proxy command received from the NOC server.      Args:         comm`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `__init__.py`, `NocEngine — Remote Charger Management Module ===================================`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `Monotonic timestamp of the most recent WS activity (send/recv/pong).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Manages session data synchronization from charger APIs to NOC Server.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Args:             noc_ws_client: WebSocket client connected to NOC Server`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Load sync state from local JSON file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Persist sync state to local JSON file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `Start the session sync loop.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `Stop the session sync loop.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `Main sync loop - runs every 30 seconds.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `Perform one sync cycle.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `Fetch active sessions from local APIs for both guns.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `Detect sessions that were active but are now completed.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `Fetch session history from local API (incremental).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `Calculate hours parameter based on last sync time.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `Send session data to NOC Server via WebSocket.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `Update internal state after successful sync.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `Example usage for testing.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `Decode Base64 data and write to SSHD connection.                  Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `Background task: Read from SSHD, Base64 encode, send via WebSocket.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `Close tunnel and cleanup resources.          Args:             tunnel_id: Tun`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `Calculate tunnel statistics.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `Return number of active tunnels.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `Return list of active tunnel IDs.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `Return statistics for all active tunnels.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `Close all tunnels (used during shutdown).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `Fetch a single JSON endpoint.      Returns:         (key, data_or_None, healt`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `Collect telemetry from all configured local API sources.      Args:         a`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `Thin WebSocket wrapper used by NocEngine.      Usage:         client = WSClie`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Open WebSocket connection to the NOC server.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Close the WebSocket connection gracefully (bounded).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Serialize and send a JSON message, bounded by ``send_timeout``.          Raise`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Receive the next JSON message.          Raises:             ConnectionError:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `NocEngine` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.396) - this node is a cross-community bridge._
- **Why does `SessionSyncManager` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 6`, `Community 7`, `Community 9`, `Community 14`?**
  _High betweenness centrality (0.173) - this node is a cross-community bridge._
- **Why does `SSHTunnelManager` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 9`?**
  _High betweenness centrality (0.163) - this node is a cross-community bridge._
- **Are the 118 inferred relationships involving `SessionSyncManager` (e.g. with `NocEngine` and `Core NOC engine class.      Args:         config: Parsed config.json dict`) actually correct?**
  _`SessionSyncManager` has 118 INFERRED edges - model-reasoned connections that need verification._
- **Are the 125 inferred relationships involving `WSClient` (e.g. with `NocEngine` and `Core NOC engine class.      Args:         config: Parsed config.json dict`) actually correct?**
  _`WSClient` has 125 INFERRED edges - model-reasoned connections that need verification._
- **Are the 121 inferred relationships involving `SSHTunnelManager` (e.g. with `NocEngine` and `Core NOC engine class.      Args:         config: Parsed config.json dict`) actually correct?**
  _`SSHTunnelManager` has 121 INFERRED edges - model-reasoned connections that need verification._
- **Are the 121 inferred relationships involving `VersionAPIServer` (e.g. with `NocEngine` and `Core NOC engine class.      Args:         config: Parsed config.json dict`) actually correct?**
  _`VersionAPIServer` has 121 INFERRED edges - model-reasoned connections that need verification._