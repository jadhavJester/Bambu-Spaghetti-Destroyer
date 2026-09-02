@echo off
title Test Bambu A1 Port 6000
cd /d "%~dp0"

echo Dang kiem tra ket noi toi Bambu A1 tren cong 6000...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Test-NetConnection 192.168.1.2 -Port 6000"

echo.
pause
