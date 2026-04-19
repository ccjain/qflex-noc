# NocEngine — PowerShell Startup Script
# =========================================
# Run from the NocEngine directory:
#   cd A-core\NocEngine
#   .\start.ps1
#
# If execution policy blocks this, run once:
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "   NocEngine - Remote Charger Client" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
try {
    $pyVer = python --version 2>&1
    Write-Host "  Python : $pyVer" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Python not found in PATH." -ForegroundColor Red
    Write-Host "        Install Python 3.10+ from https://python.org" -ForegroundColor Yellow
    exit 1
}

# Create venv if it doesn't exist
if (-Not (Test-Path "venv")) {
    Write-Host "[Setup] Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
}

# Activate venv
$activateScript = "venv\Scripts\Activate.ps1"
if (Test-Path $activateScript) {
    . $activateScript
} else {
    Write-Host "[WARN] Could not activate venv — using system Python" -ForegroundColor Yellow
}

# Install/update dependencies
Write-Host "[Setup] Checking dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet

Write-Host ""
Write-Host "  [NocEngine] Starting client..." -ForegroundColor Green
Write-Host "  [NocEngine] Press Ctrl+C to stop" -ForegroundColor Gray
Write-Host ""

# Run
python main.py
