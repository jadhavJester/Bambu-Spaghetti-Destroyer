@echo off
title Bambu-Spaghetti-Destroyer AI Sentinel
color 0A

echo ============================================================
echo   BAMBU-SPAGHETTI-DESTROYER: AUTONOMOUS AI SENTINEL (MVP)
echo ============================================================
echo.

:: 1. Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3 is not installed or not in your PATH!
    echo Please install Python 3.10+ from https://python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

:: 2. Check Cloud Credentials
if not exist "cloud_credentials.json" (
    echo [*] First-time setup detected: Linking Bambu Cloud Account...
    echo.
    python cloud_bambu_auth.py
    if not exist "cloud_credentials.json" (
        echo [!] Cloud authentication was not completed.
        echo You can run 'python cloud_bambu_auth.py' at any time.
        pause
        exit /b 1
    )
)

:: 3. Check .env
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
    )
)

:: 4. Launch MVP Command Center
echo [*] Launching AI Sentinel Command Center...
echo [*] Opening http://localhost:8787 in your default browser...
echo.

python app_dashboard.py

pause
