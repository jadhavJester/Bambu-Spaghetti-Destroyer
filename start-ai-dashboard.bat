@echo off
title Bambu Lab AI Spaghetti Command Center Dashboard
cd /d "%~dp0"

echo ============================================================
echo   Bambu Lab A1 AI Spaghetti Command Center Dashboard
echo   Live Web UI: http://localhost:8787
echo ============================================================
echo.

start http://localhost:8787
python app_dashboard.py
pause
