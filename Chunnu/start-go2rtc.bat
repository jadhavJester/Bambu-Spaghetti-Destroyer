@echo off
title Bambu Lab A1 Camera Bridge (go2rtc)
cd /d "%~dp0"

if not exist ".env" (
    echo [!] File .env chua ton tai!
    echo Chep tu .env.example sang .env va nhap PRINTER_ACCESS_CODE cua ban.
    if exist ".env.example" (
        copy .env.example .env
        echo Da tao file .env mau. Vui long mo file .env de nhap Access Code.
    )
    pause
    exit /b 1
)

echo ============================================================
echo   Bambu Lab A1 Camera Bridge (go2rtc)
echo   Stream URL: http://localhost:1984/api/stream.mjpeg?src=bambu_camera
echo   RTSP Stream: rtsp://localhost:8554/bambu_camera
echo   Web UI:     http://localhost:1984
echo ============================================================
echo.

go2rtc.exe -config go2rtc.yaml
pause
