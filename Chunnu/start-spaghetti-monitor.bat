@echo off
title Bambu Lab AI Spaghetti Detector
cd /d "%~dp0"

echo ============================================================
echo   Bambu Lab A1 AI Spaghetti Failure Detector
echo   Powered by YOLOv8 + Bambu Cloud Remote MQTT
echo ============================================================
echo.

python cloud_spaghetti_ai.py
pause
