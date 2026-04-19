"""
NocEngine — Remote Charger Management Module
============================================
Independent module under A-core. Runs as a standalone process
alongside charging_controller, evAllocationEngine, and errorGenerationEngine.

Usage:
    python main.py

Communication:
    - Collects telemetry from local APIs (ports 8003, 8002, 8006)
    - Connects to NOC server via WebSocket (configured in config.json)
"""
