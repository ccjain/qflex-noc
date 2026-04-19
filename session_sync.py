#!/usr/bin/env python3
"""
NOC Engine — Session Sync Module
=================================
Pulls session data from charger local APIs and sends to NOC Server.

This module is designed to be integrated into the existing NOC Engine service
that runs on the charger.

Features:
  - Polls active sessions (Gun 1 & 2) every 30 seconds
  - Polls session history with incremental sync (using last_sync_time)
  - Persists sync state to local JSON file
  - Sends data to NOC Server via WebSocket

Usage:
    from session_sync import SessionSyncManager
    
    session_sync = SessionSyncManager(
        noc_ws_client=ws_client,
        charger_id="CP-001",
        local_api_base="http://localhost:8003"
    )
    await session_sync.start()
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class SessionSyncManager:
    """
    Manages session data synchronization from charger APIs to NOC Server.
    """
    
    STATE_FILE = Path(__file__).parent / "session_sync_state.json"
    
    def __init__(
        self,
        noc_ws_client,
        charger_id: str,
        local_api_base: str = "http://localhost:8003",
        poll_interval: int = 30,
    ):
        """
        Args:
            noc_ws_client: WebSocket client connected to NOC Server
            charger_id: Unique identifier for this charger
            local_api_base: Base URL for local charger APIs
            poll_interval: Seconds between polls (default: 30)
        """
        self.ws_client = noc_ws_client
        self.charger_id = charger_id
        self.api_base = local_api_base.rstrip("/")
        self.poll_interval = poll_interval
        
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # State tracking
        self._state = self._load_state()
        self._last_active_sessions: dict[int, Optional[str]] = {1: None, 2: None}
    
    def _load_state(self) -> dict:
        """Load sync state from local JSON file."""
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[SessionSync] Failed to load state: {e}")
        
        # Default state: sync last 24 hours on first run
        default_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return {
            "last_sync_time": default_time,
            "sent_session_ids": [],  # Track sent sessions to avoid duplicates
        }
    
    def _save_state(self):
        """Persist sync state to local JSON file."""
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"[SessionSync] Failed to save state: {e}")
    
    async def start(self):
        """Start the session sync loop."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())
        logger.info(f"[SessionSync] Started for charger {self.charger_id}")
    
    async def stop(self):
        """Stop the session sync loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"[SessionSync] Stopped for charger {self.charger_id}")
    
    async def _sync_loop(self):
        """Main sync loop - runs every 30 seconds."""
        while self._running:
            try:
                await self._sync_once()
            except Exception as e:
                logger.error(f"[SessionSync] Sync error: {e}")
            
            await asyncio.sleep(self.poll_interval)
    
    async def _sync_once(self):
        """Perform one sync cycle."""
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # 1. Fetch active sessions for both guns
        active_sessions = await self._fetch_active_sessions()
        
        # 2. Detect completed sessions (was active, now inactive)
        completed_session_ids = self._detect_completed_sessions(active_sessions)
        
        # 3. Fetch history (incremental - only after last_sync_time)
        history_sessions = await self._fetch_history()
        
        # 4. Filter: only send history sessions we haven't sent before
        new_history = self._filter_new_sessions(history_sessions)
        
        # 5. Send to NOC Server
        if active_sessions or new_history:
            message = {
                "type": "session_sync",
                "charger_id": self.charger_id,
                "timestamp": timestamp,
                "payload": {
                    "active_sessions": active_sessions,
                    "history_sessions": new_history,
                    "completed_detected": completed_session_ids,
                }
            }
            await self._send_to_noc(message)
            
            # 6. Update state
            self._update_state(active_sessions, new_history)
            self._save_state()
    
    async def _fetch_active_sessions(self) -> list[dict]:
        """Fetch active sessions from local APIs for both guns."""
        active_sessions = []
        
        async with aiohttp.ClientSession() as session:
            for gun_id in [1, 2]:
                try:
                    url = f"{self.api_base}/api/v1/session_details/active/{gun_id}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("success") and data.get("session"):
                                session_data = data["session"]
                                session_data["gun_id"] = gun_id
                                active_sessions.append(session_data)
                                self._last_active_sessions[gun_id] = session_data.get("session_id")
                            else:
                                # Empty session = not charging
                                self._last_active_sessions[gun_id] = None
                        else:
                            logger.warning(f"[SessionSync] Active API error for gun {gun_id}: {resp.status}")
                except Exception as e:
                    logger.error(f"[SessionSync] Failed to fetch active session gun {gun_id}: {e}")
        
        return active_sessions
    
    def _detect_completed_sessions(self, current_active: list[dict]) -> list[str]:
        """Detect sessions that were active but are now completed."""
        current_ids = {s.get("session_id") for s in current_active}
        completed = []
        
        for gun_id, last_id in self._last_active_sessions.items():
            if last_id and last_id not in current_ids:
                # This session was active, now inactive = completed
                completed.append(last_id)
                logger.info(f"[SessionSync] Detected completed session: {last_id} (gun {gun_id})")
        
        return completed
    
    async def _fetch_history(self) -> list[dict]:
        """Fetch session history from local API (incremental)."""
        try:
            # Use hours parameter to limit data transfer
            # On first run: 24 hours, then incremental based on last_sync_time
            last_sync = self._state.get("last_sync_time", "")
            hours = self._calculate_hours_window(last_sync)
            
            url = f"{self.api_base}/api/v1/session_details/history"
            params = {
                "hours": hours,
                "limit": 50,
                "offset": 0,
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            sessions = data.get("sessions", [])
                            logger.debug(f"[SessionSync] Fetched {len(sessions)} history sessions")
                            return sessions
                    else:
                        logger.warning(f"[SessionSync] History API error: {resp.status}")
        except Exception as e:
            logger.error(f"[SessionSync] Failed to fetch history: {e}")
        
        return []
    
    def _calculate_hours_window(self, last_sync_time: str) -> int:
        """Calculate hours parameter based on last sync time."""
        try:
            last_sync = datetime.fromisoformat(last_sync_time.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff_hours = int((now - last_sync).total_seconds() / 3600)
            # Cap at 24 hours to avoid too much data
            return min(max(diff_hours, 1), 24)
        except:
            return 24  # Default to 24 hours on error
    
    def _filter_new_sessions(self, sessions: list[dict]) -> list[dict]:
        """Filter out sessions we've already sent to NOC Server."""
        sent_ids = set(self._state.get("sent_session_ids", []))
        new_sessions = []
        
        for session in sessions:
            session_id = session.get("session_id")
            start_time = session.get("start_time", "")
            last_sync = self._state.get("last_sync_time", "")
            
            # Include if:
            # 1. Not in sent list, OR
            # 2. Has end_time (completed) - we want final update
            is_new = session_id not in sent_ids
            is_completed = session.get("end_time") is not None
            is_after_sync = start_time > last_sync if start_time and last_sync else True
            
            if is_new or (is_completed and session_id in sent_ids):
                new_sessions.append(session)
        
        return new_sessions
    
    async def _send_to_noc(self, message: dict):
        """Send session data to NOC Server via WebSocket."""
        try:
            # Handle different WebSocket client APIs
            if hasattr(self.ws_client, 'connected'):
                # WSClient from NocEngine - takes dict directly
                await self.ws_client.send(message)
            elif hasattr(self.ws_client, 'send_text'):
                # FastAPI WebSocket
                await self.ws_client.send_text(json.dumps(message))
            elif hasattr(self.ws_client, 'send_json'):
                # Other WebSocket clients
                await self.ws_client.send_json(message)
            else:
                # Generic send with json dumps
                await self.ws_client.send(json.dumps(message))
            
            logger.debug(f"[SessionSync] Sent session_sync to NOC Server")
        except Exception as e:
            logger.error(f"[SessionSync] Failed to send to NOC: {e}")
    
    def _update_state(self, active_sessions: list[dict], history_sessions: list[dict]):
        """Update internal state after successful sync."""
        # Update last_sync_time
        self._state["last_sync_time"] = datetime.now(timezone.utc).isoformat()
        
        # Track sent session IDs
        sent_ids = set(self._state.get("sent_session_ids", []))
        for session in active_sessions + history_sessions:
            session_id = session.get("session_id")
            if session_id:
                sent_ids.add(session_id)
        
        # Keep only last 1000 session IDs to prevent state file bloat
        self._state["sent_session_ids"] = list(sent_ids)[-1000:]


# -----------------------------------------------------------------------------
# Standalone usage example
# -----------------------------------------------------------------------------

async def main():
    """Example usage for testing."""
    import aiohttp
    
    # Mock WebSocket client for testing
    class MockWS:
        async def send(self, data):
            print(f"[MockWS] Would send: {data[:200]}...")
    
    sync_mgr = SessionSyncManager(
        noc_ws_client=MockWS(),
        charger_id="TEST-001",
        local_api_base="http://localhost:8003",
        poll_interval=30,
    )
    
    await sync_mgr.start()
    
    try:
        # Run for a few minutes
        await asyncio.sleep(120)
    finally:
        await sync_mgr.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
