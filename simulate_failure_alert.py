#!/usr/bin/env python3
"""Simulate an intentional print failure and dispatch an alert to Telegram."""
from __future__ import annotations

import time
import cv2
import numpy as np
import requests

from cloud_mqtt_control import BambuCloudController
from telegram_alert import send_telegram_alert


def get_live_or_simulated_frame() -> np.ndarray:
    """Fetch live camera frame or generate high-detail print bed frame."""
    # 1. Try local dashboard endpoint if running
    try:
        resp = requests.get("http://localhost:8787/api/stream.jpg", timeout=2)
        if resp.status_code == 200 and len(resp.content) > 15000:
            arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                return img
    except Exception:
        pass

    # 2. Try direct cloud streamer
    try:
        from cloud_camera_stream import get_cloud_streamer
        s = get_cloud_streamer()
        t0 = time.time()
        while time.time() - t0 < 5:
            f = s.get_frame_cv2()
            if f is not None:
                return f
            time.sleep(0.5)
    except Exception:
        pass

    # 3. Fallback: High-resolution simulated printer bed with model
    h, w = 720, 1280
    frame = np.full((h, w, 3), (35, 35, 35), dtype=np.uint8)
    # Textured PEI bed plate
    cv2.rectangle(frame, (180, 100), (1100, 680), (55, 55, 58), -1)
    cv2.rectangle(frame, (180, 100), (1100, 680), (80, 80, 85), 2)
    # Printer head / gantry
    cv2.rectangle(frame, (450, 40), (830, 220), (70, 70, 75), -1)
    cv2.putText(frame, "BAMBU LAB A1", (540, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (220, 220, 220), 2)
    # Printed part
    cv2.circle(frame, (640, 420), 120, (180, 180, 185), -1)
    return frame


def annotate_failure(frame: np.ndarray, label: str = "spaghetti", conf: float = 0.942) -> np.ndarray:
    """Draw realistic YOLO bounding box and high-visibility alert banner."""
    annotated = frame.copy()
    h, w = annotated.shape[:2]

    # Bounding box coordinates near the print center
    x1, y1 = int(w * 0.42), int(h * 0.45)
    x2, y2 = int(w * 0.65), int(h * 0.72)

    # Tangled filament effect if drawing on synthetic frame
    for i in range(15):
        pt1 = (int(x1 + (x2 - x1) * (0.1 + 0.05 * i)), int(y1 + (y2 - y1) * (0.2 + 0.04 * i)))
        pt2 = (int(x1 + (x2 - x1) * (0.8 - 0.04 * i)), int(y2 - (y2 - y1) * (0.1 + 0.05 * i)))
        cv2.line(annotated, pt1, pt2, (200, 220, 240), 2)

    # Red Bounding Box
    box_color = (0, 0, 235)  # Bright Red BGR
    cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 3)

    # Label Badge with text
    badge_text = f" {label} {conf:.1%} "
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.75
    font_th = 2
    (tw, th), baseline = cv2.getTextSize(badge_text, font, font_scale, font_th)

    cv2.rectangle(annotated, (x1, y1 - th - 12), (x1 + tw, y1), box_color, -1)
    cv2.putText(annotated, badge_text, (x1, y1 - 6), font, font_scale, (255, 255, 255), font_th)

    # Emergency Alert banner across the top
    banner_bg = (18, 18, 180)
    cv2.rectangle(annotated, (0, 0), (w, 55), banner_bg, -1)
    cv2.putText(
        annotated,
        f"CRITICAL DEFECT DETECTED - EMERGENCY PAUSE TRIGGERED [{label.upper()}]",
        (int(w * 0.12), 38),
        font,
        0.85,
        (255, 255, 255),
        2,
    )

    return annotated


def run_mimic_test():
    print("[*] Gathering printer telemetry from Bambu Cloud...", flush=True)
    ctrl = BambuCloudController()
    ctrl.start()
    time.sleep(1.5)

    status = ctrl.get_status()
    layer = status.get("layer_num", 142)
    total_l = status.get("total_layers", 1005)
    nozzle = status.get("nozzle_temp", 240.0)
    bed = status.get("bed_temp", 70.0)
    ctrl.stop()

    print(f"[*] Live Telemetry: Layer {layer}/{total_l} | Nozzle {nozzle}°C | Bed {bed}°C", flush=True)
    print("[*] Fetching camera frame...", flush=True)
    frame = get_live_or_simulated_frame()

    print("[*] Applying YOLO Failure Annotation (Spaghetti 94.2%)...", flush=True)
    annotated = annotate_failure(frame, label="spaghetti", conf=0.942)

    # Save local copy
    cv2.imwrite("failure_simulation.jpg", annotated)
    print("[+] Saved local preview to failure_simulation.jpg", flush=True)

    print("[*] Dispatching Telegram Alert with single annotated JPEG...", flush=True)
    layer_val = layer if isinstance(layer, (int, float)) and layer > 0 else 142
    total_val = total_l if isinstance(total_l, (int, float)) and total_l > 0 else 1005
    nozzle_val = nozzle if isinstance(nozzle, (int, float)) and nozzle > 0 else 240.0
    bed_val = bed if isinstance(bed, (int, float)) and bed > 0 else 70.0

    ok = send_telegram_alert(
        photo=annotated,
        error_type="Spaghetti Defect (Detached Extrusion)",
        confidence=0.942,
        layer_num=int(layer_val),
        total_layers=int(total_val),
        nozzle_temp=float(nozzle_val),
        bed_temp=float(bed_val),
        action_taken="SIMULATED FAILURE: Emergency Pause Verified",
    )

    if ok:
        print("\n[+] SUCCESS! Telegram alert and failure JPEG delivered to your phone!", flush=True)
    else:
        print("\n[!] Could not send alert to Telegram. Check your .env configuration.", flush=True)


if __name__ == "__main__":
    run_mimic_test()
