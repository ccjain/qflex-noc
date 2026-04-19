# NocEngine — QFlex Remote Charger Management

## Project Overview

**NocEngine** is a Python-based remote management module for QFlex EV chargers. It runs as a standalone service on the charger hardware (or on a development PC for testing) and maintains a persistent WebSocket connection to a central Network Operations Center (NOC) server.

### Key Responsibilities

1. **Telemetry Collection**: Periodically fetches charging status, power allocation, hardware info, and active errors from local APIs
2. **Command Proxying**: Receives commands from the NOC server and forwards them to local charger services
3. **Session Synchronization**: Tracks active charging sessions and syncs session history to the NOC server
4. **SSH Tunneling**: Provides remote SSH access to the charger through WebSocket-based tunneling
5. **File Uploads**: Handles chunked file uploads from the NOC server to the charger's dev-tools service
6. **Version Reporting**: Polls service versions on startup and reports them to the NOC server

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        NOC Server                               │
│  (WebSocket endpoint: ws://host:8080/ws/{charger_id})          │
└───────────────────────┬─────────────────────────────────────────┘
                        │ WebSocket
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                      NocEngine (this module)                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   main.py   │  │ noc_engine  │  │      WSClient           │  │
│  │  (entry)    │──│  (core)     │──│   (ws_client.py)        │  │
│  └─────────────┘  └──────┬──────┘  └─────────────────────────┘  │
│                          │                                      │
│     ┌────────────────────┼────────────────────┐                 │
│     ▼                    ▼                    ▼                 │
│ ┌──────────┐    ┌────────────────┐   ┌──────────────┐          │
│ │Telemetry │    │ Command Exec   │   │ Session Sync │          │
│ │Collector │    │(command_exec)  │   │(session_sync)│          │
│ └────┬─────┘    └───────┬────────┘   └──────┬───────┘          │
│      │                  │                    │                  │
│      ▼                  ▼                    ▼                  │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │              Local Charger APIs (via HTTP)                 │  │
│ │  Port 8000: system_api    Port 8002: allocation_engine     │  │
│ │  Port 8003: charging_controller (also session APIs)        │  │
│ │  Port 8005: dev-tools (file uploads)  Port 8006: errors    │  │
│ └────────────────────────────────────────────────────────────┘  │
│                                                                 │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │     SSH Tunnel Manager (ssh_tunnel.py) ←→ localhost:22     │  │
│ └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| Async Framework | asyncio |
| HTTP Client | aiohttp >= 3.9.0 |
| WebSocket Client | websockets >= 12.0 |
| Configuration | JSON (config.json) |
| State Persistence | JSON file (session_sync_state.json) |

## Project Structure

```
A-core/NocEngine/
├── main.py                  # Entry point - argument parsing, charger ID resolution
├── noc_engine.py            # Core orchestrator - WS connection, message loops
├── ws_client.py             # Thin WebSocket client wrapper
├── telemetry_collector.py   # Fetches data from local APIs
├── command_executor.py      # Executes proxy commands from NOC server
├── session_sync.py          # Syncs charging session data to NOC server
├── ssh_tunnel.py            # Manages SSH tunnels for remote access
├── config.json              # Runtime configuration
├── requirements.txt         # Python dependencies
├── start.bat                # Windows batch startup script
├── start.ps1                # PowerShell startup script
└── session_sync_state.json  # Persisted sync state (auto-generated)
```

### Module Descriptions

#### `main.py` (Entry Point)
- Parses command-line arguments (`--config`)
- Loads and validates configuration
- **Resolves charger ID** by polling local APIs for:
  - OCPP `charge_box_serial_number` (from charging_controller port 8003)
  - Hardware `serial_number` (from system_api port 8000)
- Constructs final `charger_id` as: `{ocpp_serial}-{hw_serial}`
- Sets up logging with configurable level/format
- Handles graceful shutdown (SIGINT/SIGTERM)

#### `noc_engine.py` (Core Engine)
- `NocEngine` class - main orchestrator
- Manages WebSocket connection lifecycle with auto-reconnect (5-second fixed delay)
- Runs concurrent asyncio tasks:
  - `_receive_loop()`: Receives and dispatches messages from NOC server
  - `_telemetry_loop()`: Sends telemetry every 30 seconds (configurable)
  - `_heartbeat_loop()`: Sends keep-alive every 10 seconds (configurable)
  - `_session_sync_loop()`: Syncs session data every 30 seconds
  - `_version_poll_loop()`: Polls service versions 5 times at startup
- Handles incoming message types:
  - `command`: Proxy API commands to local services
  - `proxy_request`: Web tool proxy (immediate response)
  - `upload_chunk`: Chunked file upload handling
  - `ssh_tunnel_open/close/data`: SSH tunnel management
  - `ack`: Server acknowledgements

#### `telemetry_collector.py`
- `collect(api_urls)` function - fetches all telemetry sources concurrently
- Sources (all via HTTP GET):
  - `charging_status` → charging_controller (port 8003)
  - `allocation_status` → allocation_engine (port 8002)
  - `network_status` → system_api (port 8000)
  - `active_errors` → error_generation (port 8006)
  - `session_1`, `session_2` → session details (port 8003)
- Returns payload with `service_health` block for each source

#### `command_executor.py`
- `execute(command, charger_ip)` function
- Security: Only allows whitelisted ports (8002, 8003, 8006)
- Supports HTTP methods: GET, POST, PUT, PATCH
- 10-second timeout per command
- Returns standardized result with `command_id`, `status_code`, `response`, `execution_time_ms`

#### `session_sync.py`
- `SessionSyncManager` class
- Polls active sessions for Gun 1 & 2 every 30 seconds
- Fetches session history incrementally (using `last_sync_time`)
- Detects completed sessions (was active, now inactive)
- Persists state to `session_sync_state.json`:
  - `last_sync_time`: Timestamp of last successful sync
  - `sent_session_ids`: Last 1000 session IDs (deduplication)
- Sends `session_sync` messages to NOC server

#### `ssh_tunnel.py`
- `SSHTunnelManager` class
- Manages TCP connections from WebSocket to local SSHD (port 22)
- Maximum 10 concurrent tunnels per charger
- Data flow: WebSocket ←Base64→ NocEngine ←Raw TCP→ SSHD
- Tracks bytes transferred and connection duration
- Background reader task per tunnel (reads from SSHD, sends via WebSocket)

#### `ws_client.py`
- `WSClient` class - thin wrapper around `websockets` library
- Features:
  - Auto ping/pong (20s interval, 10s timeout)
  - 2MB max message size
  - JSON message serialization

## Configuration (config.json)

```json
{
  "charger_id": "QNCH-BOX-001",          // Fallback (overridden by API lookup)
  "firmware_version": "1.0.0",           // Firmware version reported to NOC
  "model": "DCFastCharger-v1",           // Charger model
  "noc_server": {
    "host": "172.16.18.166",             // NOC server IP/hostname
    "port": 8080                          // NOC server WebSocket port
  },
  "charger_ip": "localhost",             // "localhost" = on-charger mode
                                           // IP address = remote test mode
  "charger_ports": {
    "system_api": 8000,                  // Hardware/system info
    "charging_controller": 8003,         // Charging control & sessions
    "allocation_engine": 8002,           // Power allocation
    "error_generation": 8006             // Error/alarm generation
  },
  "telemetry": {
    "interval_seconds": 30               // Telemetry push interval
  },
  "heartbeat": {
    "interval_seconds": 10                // Keep-alive interval
  },
  "reconnect": {
    "max_delay_seconds": 60               // Exponential backoff cap
  },
  "logging": {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  }
}
```

### Configuration Modes

| Mode | `charger_ip` | Use Case |
|------|--------------|----------|
| **On-Charger (Production)** | `"localhost"` | NocEngine runs on the actual charger hardware |
| **Remote Test** | `"172.16.14.123"` | NocEngine runs on dev PC, targets charger over LAN |

## Build and Run Commands

### Prerequisites
- Python 3.10 or higher
- pip package manager

### Setup (One-time)

```bash
# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate.bat

# Activate (PowerShell)
venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Run

```bash
# Standard run (uses ./config.json)
python main.py

# With custom config
python main.py --config /path/to/config.json

# Using Windows batch script
start.bat

# Using PowerShell script
.\start.ps1
```

### Dependencies

```
aiohttp>=3.9.0
websockets>=12.0
```

## Code Style Guidelines

1. **Async/Await**: All I/O operations are async using `asyncio`
2. **Type Hints**: Use type annotations where practical (`str | None`, `dict`, etc.)
3. **Logging**: Use module-level logger (`logging.getLogger(__name__)`)
4. **Docstrings**: Google-style docstrings for all public functions/classes
5. **Constants**: UPPER_CASE for module-level constants
6. **Prefixes**: Log messages use prefixes like `[NOC-Engine]`, `[WS-Client]`, `[SSH-Tunnel]`

### Error Handling Patterns

```python
# Always return result dict, never raise in public APIs
try:
    result = await some_operation()
    return {"success": True, "data": result}
except SpecificException as e:
    logger.warning(f"[Module] Operation failed: {e}")
    return {"success": False, "error": str(e)}
```

## Testing Strategy

### Testing Modes

1. **Remote Test Mode** (Recommended for Development)
   - Set `charger_ip` to the charger's IP address in `config.json`
   - Run NocEngine on your development PC
   - Targets real charger APIs over the network

2. **Mock Server Mode** (Unit Testing)
   - Create mock NOC server that accepts WebSocket connections
   - Test individual modules in isolation

### Manual Testing Checklist

- [ ] Charger ID resolution succeeds (polls both OCPP and HW serials)
- [ ] WebSocket connects to NOC server
- [ ] Authentication message sent on connect
- [ ] Telemetry received at configured interval
- [ ] Heartbeats keep connection alive
- [ ] Commands execute and return results
- [ ] Session sync messages sent
- [ ] SSH tunnel opens and data flows
- [ ] File upload completes successfully
- [ ] Reconnects after connection loss
- [ ] Graceful shutdown on SIGINT

## Security Considerations

1. **Port Whitelisting**: Command executor only allows ports 8002, 8003, 8006
2. **SSH Access**: Tunnels only to localhost:22 (SSHD)
3. **Max Tunnels**: Limited to 10 concurrent SSH tunnels per charger
4. **Timeouts**: All HTTP operations have timeouts (5-10 seconds)
5. **No Secrets in Code**: Credentials/config should be injected, not hardcoded

## WebSocket Message Protocol

### Outgoing (NocEngine → NOC Server)

| Message Type | Description | Payload Fields |
|--------------|-------------|----------------|
| `auth` | Initial authentication | `charger_id`, `firmware_version`, `model` |
| `heartbeat` | Keep-alive ping | `{}` |
| `telemetry` | Status update | `charging_status`, `allocation_status`, `network_status`, `active_errors`, `service_health` |
| `command_result` | Command response | `command_id`, `status_code`, `response`, `execution_time_ms` |
| `proxy_response` | Web tool response | `request_id`, `success`, `status_code`, `data`/`error` |
| `version_info` | Service versions | `poll`, `of`, `versions`, `source` |
| `session_sync` | Session data | `active_sessions`, `history_sessions`, `completed_detected` |
| `ssh_tunnel_ack` | Tunnel opened | `tunnel_id`, `status`, `ssh_server_version` |
| `ssh_data` | SSH data to client | `tunnel_id`, `data` (base64) |
| `ssh_tunnel_closed` | Tunnel closed | `tunnel_id`, `reason`, `bytes_tx`, `bytes_rx`, `duration_seconds` |
| `chunked_upload_response` | Upload complete | `upload_id`, `success`, `data`/`error` |

### Incoming (NOC Server → NocEngine)

| Message Type | Description | Payload Fields |
|--------------|-------------|----------------|
| `command` | Execute API command | `command_id`, `method`, `target_port`, `path`, `headers`, `body` |
| `proxy_request` | Web tool proxy | `request_id`, `method`, `port`, `path`, `body`, `content_type`, `body_is_binary` |
| `upload_chunk` | File chunk | `upload_id`, `chunk_index`, `total_chunks`, `file_name`, `target`, `chunk_data`, `is_last` |
| `ssh_tunnel_open` | Open SSH tunnel | `tunnel_id`, `target_host`, `target_port` |
| `ssh_data` | SSH data from client | `tunnel_id`, `data` (base64) |
| `ssh_tunnel_close` | Close SSH tunnel | `tunnel_id`, `reason` |
| `ack` | Acknowledgement | `message` |

## Deployment Notes

- NocEngine is designed to run as a systemd service on the charger
- Ensure `config.json` is properly configured before deployment
- The service should auto-start on boot
- Monitor logs for connection issues or API failures
- `session_sync_state.json` persists across restarts - do not delete in production
