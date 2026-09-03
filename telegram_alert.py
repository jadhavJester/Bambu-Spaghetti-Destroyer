#!/usr/bin/env python3
"""Telegram Alert Dispatcher for Bambu 3D Print Failure Detection.

Sends instant emergency alerts to your Telegram chat with an annotated photo
when spaghetti or other printing failures are detected.
"""
from __future__ import annotations

import io
import os
import sys
import time
import requests
import cv2
import numpy as np

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env_vars() -> dict[str, str]:
    """Parse .env file for configuration."""
    vals = {}
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        vals[k.strip()] = v.strip().strip("\"'")
        except Exception:
            pass
    # Override with system environment if present
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def save_telegram_config(token: str, chat_id: str):
    """Save or update Telegram credentials into .env."""
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    token_found = False
    chat_found = False
    new_lines = []

    for line in lines:
        if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
            new_lines.append(f"TELEGRAM_BOT_TOKEN={token}\n")
            token_found = True
        elif line.strip().startswith("TELEGRAM_CHAT_ID="):
            new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}\n")
            chat_found = True
        else:
            new_lines.append(line)

    if not token_found:
        new_lines.append(f"TELEGRAM_BOT_TOKEN={token}\n")
    if not chat_found:
        new_lines.append(f"TELEGRAM_CHAT_ID={chat_id}\n")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print("[+] Saved Telegram configuration to .env successfully!")


def send_telegram_alert(
    photo: bytes | np.ndarray | str,
    error_type: str = "Spaghetti Failure",
    confidence: float = 0.85,
    layer_num: int = 0,
    total_layers: int = 0,
    nozzle_temp: float = 0.0,
    bed_temp: float = 0.0,
    action_taken: str = "Print Paused Automatically via Cloud MQTT",
) -> bool:
    """Send Telegram notification with failure details and annotated JPEG photo.

    Parameters
    ----------
    photo : bytes | np.ndarray | str
        JPEG bytes, OpenCV BGR image, or filepath.
    error_type : str
        Name of defect (e.g. "Spaghetti", "Stringing", "Detached Blob").
    confidence : float
        AI confidence percentage (0.0 - 1.0).
    layer_num : int
        Current layer number.
    total_layers : int
        Total print layers.
    nozzle_temp : float
        Current nozzle temperature in °C.
    bed_temp : float
        Current bed temperature in °C.
    action_taken : str
        Action executed (e.g. "Print Paused").
    """
    cfg = load_env_vars()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[Telegram] Skipping alert: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured in .env", flush=True)
        return False

    # Convert photo to JPEG bytes
    jpeg_bytes: bytes | None = None
    if isinstance(photo, bytes):
        jpeg_bytes = photo
    elif isinstance(photo, str):
        if os.path.exists(photo):
            with open(photo, "rb") as f:
                jpeg_bytes = f.read()
    elif isinstance(photo, np.ndarray):
        ret, buf = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ret:
            jpeg_bytes = buf.tobytes()

    if not jpeg_bytes:
        print("[Telegram] Error: Failed to encode photo for alert.", flush=True)
        return False

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    caption = (
        f"🚨 <b>3D PRINT FAILURE DETECTED!</b>\n\n"
        f"⚠️ <b>Defect:</b> {error_type}\n"
        f"🎯 <b>AI Confidence:</b> {confidence:.1%}\n"
        f"📊 <b>Progress:</b> Layer {layer_num} / {total_layers}\n"
        f"🌡️ <b>Temperatures:</b> Nozzle {nozzle_temp:.1f}°C | Bed {bed_temp:.1f}°C\n"
        f"🛑 <b>Status:</b> {action_taken}\n"
        f"🕒 <b>Time:</b> {ts}\n\n"
        f"<i>Bambu-Spaghetti-Destroyer AI Sentinel</i>"
    )

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    files = {
        "photo": ("failure.jpg", io.BytesIO(jpeg_bytes), "image/jpeg"),
    }
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, data=data, files=files, timeout=15)
        if resp.status_code == 200:
            print(f"[+] Telegram alert successfully dispatched to chat {chat_id}!", flush=True)
            return True
        else:
            print(f"[!] Telegram API error ({resp.status_code}): {resp.text}", flush=True)
            return False
    except Exception as e:
        print(f"[!] Failed to send Telegram alert: {e}", flush=True)
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        print("--- Telegram Bot Configuration ---")
        token = input("Enter your Telegram Bot Token (from @BotFather): ").strip()
        chat_id = input("Enter your Telegram Chat ID (from @userinfobot): ").strip()
        if token and chat_id:
            save_telegram_config(token, chat_id)
            print("[*] Testing alert...")
            # Create a test sample image
            test_img = np.zeros((400, 600, 3), dtype=np.uint8)
            cv2.putText(test_img, "Bambu Sentinel Test Alert", (40, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            send_telegram_alert(test_img, error_type="Test Alert", confidence=0.99, layer_num=42, total_layers=100)
    else:
        cfg = load_env_vars()
        if not cfg.get("TELEGRAM_BOT_TOKEN") or not cfg.get("TELEGRAM_CHAT_ID"):
            print("Telegram is not configured yet!")
            print("Run: python telegram_alert.py setup")
        else:
            print(f"Telegram configured! Bot token: {cfg['TELEGRAM_BOT_TOKEN'][:6]}... | Chat ID: {cfg['TELEGRAM_CHAT_ID']}")
            print("Sending test alert...")
            test_img = np.zeros((400, 600, 3), dtype=np.uint8)
            cv2.putText(test_img, "Sentinel System Online", (70, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            send_telegram_alert(test_img, error_type="Diagnostic Test", confidence=1.0, layer_num=1, total_layers=100)
