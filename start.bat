@echo off
:: NocEngine — Windows Startup Script
:: =====================================
:: Run this from the NocEngine directory:
::   cd A-core\NocEngine
::   start.bat

echo.
echo  ============================================
echo   NocEngine - Remote Charger Client
echo  ============================================
echo.

:: Check Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: Check if venv exists, create if not
if not exist "venv\" (
    echo [Setup] Creating virtual environment...
    python -m venv venv
    echo [Setup] Installing dependencies...
    venv\Scripts\pip install -r requirements.txt
    echo.
)

:: Activate venv
call venv\Scripts\activate.bat

:: Check if deps are installed (fast check)
python -c "import aiohttp, websockets" >nul 2>&1
if %errorlevel% neq 0 (
    echo [Setup] Installing missing dependencies...
    pip install -r requirements.txt
    echo.
)

echo [NocEngine] Starting client...
echo [NocEngine] Press Ctrl+C to stop
echo.

:: Run the engine
python main.py

pause
