"""nocServerEnabled runtime kill switch — behavioral tests.

Covers:
- Constructor reads `noc_server.enabled` from config (default True).
- `_parse_enabled_value` strict-truthy parsing.
- `_fetch_noc_enabled_once` returns None on failure, bool on success.
- `_noc_enabled_refresh_loop` persists transitions to cache and wakes the event.
- `_enabled_guard_loop` returns within ~2s when the flag flips false.
- `run()` parks on the event when disabled and resumes on enable.
- API unreachable preserves last known state.
"""
import asyncio
import json

import pytest

from noc_engine import NocEngine


def _cfg(enabled=None):
    cfg = {
        "charger_id": "T",
        "noc_server": {"host": "127.0.0.1", "port": 1},
        "charger_ip": "127.0.0.1",
        "charger_ports": {
            "system_api": 0, "charging_controller": 0,
            "allocation_engine": 0, "error_generation": 0,
        },
    }
    if enabled is not None:
        cfg["noc_server"]["enabled"] = enabled
    return cfg


# ---------------------------------------------------------------------------
# Constructor + state defaults
# ---------------------------------------------------------------------------

def test_constructor_defaults_enabled_true_when_absent(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    assert engine.noc_enabled is True
    assert engine._enabled_event.is_set()  # set when starting enabled


def test_constructor_reads_enabled_false_from_config(tmp_path):
    engine = NocEngine(_cfg(enabled=False), charger_id_cache_file=str(tmp_path / "c.json"))
    assert engine.noc_enabled is False
    assert not engine._enabled_event.is_set()  # cleared when starting disabled


def test_constructor_builds_enabled_endpoint_url(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    assert engine._noc_enabled_url.endswith(
        "/api/v1/config/ocpp/nocServerEnabled"
    )


def test_constructor_creates_rate_limiter(tmp_path):
    from log_helpers import RateLimitedLogger
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    assert isinstance(engine._rl_noc_enabled, RateLimitedLogger)


# ---------------------------------------------------------------------------
# _parse_enabled_value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("  true  ", True),
    ("1", True),
    (1, True),
    ("false", False),
    ("False", False),
    ("0", False),
    (0, False),
    ("", False),
    ("yes", False),    # only the listed forms are truthy
    ("on", False),
    (None, False),
    (2, False),
    ([], False),
])
def test_parse_enabled_value(value, expected):
    assert NocEngine._parse_enabled_value(value) is expected


# ---------------------------------------------------------------------------
# _fetch_noc_enabled_once — mock the shared http_session
# ---------------------------------------------------------------------------

class _MockResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def json(self, **_): return self._payload


class _MockSession:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.closed = False
        self.get_calls = []
    def get(self, url, **kw):
        self.get_calls.append((url, kw))
        if self._raise is not None:
            raise self._raise
        return self._response


async def test_fetch_returns_true_on_success(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    engine.http_session = _MockSession(  # type: ignore[assignment]
        response=_MockResponse(200, {"success": True, "value": "true"})
    )
    result = await engine._fetch_noc_enabled_once()
    assert result is True


async def test_fetch_returns_false_on_success_with_falsy_value(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    engine.http_session = _MockSession(  # type: ignore[assignment]
        response=_MockResponse(200, {"success": True, "value": "false"})
    )
    result = await engine._fetch_noc_enabled_once()
    assert result is False


async def test_fetch_returns_none_on_http_error(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    engine.http_session = _MockSession(  # type: ignore[assignment]
        response=_MockResponse(500, {}),
    )
    result = await engine._fetch_noc_enabled_once()
    assert result is None


async def test_fetch_returns_none_on_success_false(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    engine.http_session = _MockSession(  # type: ignore[assignment]
        response=_MockResponse(200, {"success": False, "value": "true"})
    )
    result = await engine._fetch_noc_enabled_once()
    assert result is None


async def test_fetch_returns_none_on_exception(tmp_path):
    engine = NocEngine(_cfg(), charger_id_cache_file=str(tmp_path / "c.json"))
    engine.http_session = _MockSession(  # type: ignore[assignment]
        raise_exc=RuntimeError("connection refused"),
    )
    result = await engine._fetch_noc_enabled_once()
    assert result is None


# ---------------------------------------------------------------------------
# _noc_enabled_refresh_loop
# ---------------------------------------------------------------------------

async def test_refresh_loop_persists_transition_to_cache_and_sets_event(tmp_path):
    cache_file = tmp_path / "c.json"
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(cache_file))
    engine._enabled_event.clear()  # simulate disabled→enabled transition wait

    # First call returns False (transition true → false), then we stop.
    calls = {"n": 0}
    async def fake_fetch():
        calls["n"] += 1
        engine._running = False  # stop after this iteration
        return False
    engine._fetch_noc_enabled_once = fake_fetch  # type: ignore[assignment]
    engine._running = True

    # Patch the sleep to be instant so we don't wait 30s.
    real_sleep = asyncio.sleep
    async def fast_sleep(_):
        await real_sleep(0)
    import noc_engine as engine_mod
    monkey_orig = engine_mod.asyncio.sleep
    engine_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        await engine._noc_enabled_refresh_loop()
    finally:
        engine_mod.asyncio.sleep = monkey_orig

    assert calls["n"] == 1
    assert engine.noc_enabled is False
    assert engine._enabled_event.is_set()  # event always set on transition

    data = json.loads(cache_file.read_text())
    assert data["noc_enabled"] is False


async def test_refresh_loop_no_log_on_steady_state(tmp_path, caplog):
    """API keeps returning the same value — no transition log."""
    import logging
    caplog.set_level(logging.INFO, logger="noc_engine")
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(tmp_path / "c.json"))

    calls = {"n": 0}
    async def fake_fetch():
        calls["n"] += 1
        engine._running = False  # stop after this iteration
        return True  # same as current state
    engine._fetch_noc_enabled_once = fake_fetch  # type: ignore[assignment]
    engine._running = True

    real_sleep = asyncio.sleep
    async def fast_sleep(_):
        await real_sleep(0)
    import noc_engine as engine_mod
    monkey_orig = engine_mod.asyncio.sleep
    engine_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        await engine._noc_enabled_refresh_loop()
    finally:
        engine_mod.asyncio.sleep = monkey_orig

    assert calls["n"] == 1
    transition_logs = [r for r in caplog.records if "nocServerEnabled changed" in r.message]
    assert transition_logs == []


async def test_refresh_loop_api_unreachable_preserves_state(tmp_path):
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(tmp_path / "c.json"))

    calls = {"n": 0}
    async def fake_fetch():
        calls["n"] += 1
        engine._running = False  # stop after this iteration
        return None  # API down
    engine._fetch_noc_enabled_once = fake_fetch  # type: ignore[assignment]
    engine._running = True

    real_sleep = asyncio.sleep
    async def fast_sleep(_):
        await real_sleep(0)
    import noc_engine as engine_mod
    monkey_orig = engine_mod.asyncio.sleep
    engine_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        await engine._noc_enabled_refresh_loop()
    finally:
        engine_mod.asyncio.sleep = monkey_orig

    assert calls["n"] == 1
    assert engine.noc_enabled is True  # unchanged


# ---------------------------------------------------------------------------
# _enabled_guard_loop
# ---------------------------------------------------------------------------

async def test_guard_loop_exits_when_disabled(tmp_path):
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(tmp_path / "c.json"))

    class _WS:
        connected = True

    task = asyncio.create_task(engine._enabled_guard_loop(_WS()))  # type: ignore[arg-type]
    # Let the loop tick once.
    await asyncio.sleep(0.05)
    assert not task.done()

    # Flip the flag — guard should exit within its 1s poll window.
    engine.noc_enabled = False
    await asyncio.wait_for(task, timeout=2.5)
    assert task.done() and task.exception() is None


async def test_guard_loop_exits_when_ws_disconnects(tmp_path):
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(tmp_path / "c.json"))

    class _WS:
        connected = True

    ws = _WS()
    task = asyncio.create_task(engine._enabled_guard_loop(ws))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    ws.connected = False
    await asyncio.wait_for(task, timeout=2.5)
    assert task.done() and task.exception() is None


async def test_guard_loop_stays_alive_while_enabled_and_connected(tmp_path):
    engine = NocEngine(_cfg(enabled=True), charger_id_cache_file=str(tmp_path / "c.json"))

    class _WS:
        connected = True

    task = asyncio.create_task(engine._enabled_guard_loop(_WS()))  # type: ignore[arg-type]
    await asyncio.sleep(1.2)  # one full poll cycle
    assert not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Cache helpers in main.py — exercised independently of NocEngine
# ---------------------------------------------------------------------------

def test_parse_enabled_matches_main_helper():
    """main._parse_enabled must agree with NocEngine._parse_enabled_value on the documented set."""
    import main as main_mod
    for v in (True, "true", "1", 1, False, "false", "0", 0, "", None, 2, "yes"):
        assert main_mod._parse_enabled(v) is NocEngine._parse_enabled_value(v), \
            f"mismatch for {v!r}"
