#!/usr/bin/env python3
"""Autonomous Spaghetti Failure Detection Engine for Bambu Lab.

Features:
- Samples camera snapshots periodically.
- Runs YOLOv8 failure detection model on print bed frames.
- If failure / spaghetti is detected with high confidence:
  1. Automatically triggers PAUSE command to printer via MQTT.
  2. Saves the evidence photo.
"""
from __future__ import annotations

import os
import sys
import time
import cv2
import requests
import numpy as np
from ultralytics import YOLO

# Import cloud controller & local camera stream fallback
from cloud_mqtt_control import BambuCloudController
try:
    import camera_stream
except ImportError:
    camera_stream = None

CONFIDENCE_THRESHOLD = 0.70   # Detection confidence (70%)
CHECK_INTERVAL_SECONDS = 6     # Time between checks
MODEL_PATH = "spaghetti_yolo.pt"  # 3D Print Failure model (spaghetti, stringing, zits)


class SpaghettiDetector:
    def __init__(self, model_path: str = MODEL_PATH, stream_url: str | None = None):
        print(f"[*] Initializing YOLO Model ({model_path})...", flush=True)
        self.model = YOLO(model_path)
        self.stream_url = stream_url or "http://localhost:1984/api/frame.jpeg?src=bambu_camera"
        self.cloud_ctrl = BambuCloudController()
        self.cloud_ctrl.start()
        self.paused_by_ai = False
        self.last_check_time = 0.0

    def fetch_frame(self) -> np.ndarray | None:
        """Fetch current camera frame via Cloud TUTK, go2rtc, or direct port 6000."""
        # 1. Primary: Cloud TUTK Streamer (Works across Hotspot & 5G)
        try:
            from cloud_camera_stream import get_cloud_streamer
            streamer = get_cloud_streamer()
            frame = streamer.get_frame_cv2()
            if frame is not None:
                return frame
        except Exception:
            pass

        # 2. Try local go2rtc stream
        try:
            resp = requests.get(self.stream_url, timeout=3)
            if resp.status_code == 200 and len(resp.content) > 1000:
                arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    return frame
        except Exception:
            pass

        # 3. Try direct LAN port 6000 fallback
        if camera_stream is not None:
            try:
                host = os.environ.get("PRINTER_ADDRESS", "192.168.1.2")
                code = os.environ.get("PRINTER_ACCESS_CODE", "ffd8e1e5")
                raw = camera_stream.get_frame(host, code, wait_s=2.0)
                if raw:
                    arr = np.asarray(bytearray(raw), dtype=np.uint8)
                    return cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception:
                pass

        return None

    def analyze_frame(self, frame: np.ndarray) -> tuple[bool, float, np.ndarray, list]:
        """Run YOLO inference and return (is_failure, max_conf, annotated_frame, detections)."""
        results = self.model.predict(frame, conf=CONFIDENCE_THRESHOLD, verbose=False)
        annotated = results[0].plot()
        
        is_failure = False
        max_conf = 0.0
        detections = []
        
        for box in results[0].boxes:
            conf = float(box.conf[0])
            cls_id = int(box.cls[0])
            cls_name = results[0].names.get(cls_id, str(cls_id))
            detections.append(f"{cls_name} ({conf:.0%})")
            if conf > max_conf:
                max_conf = conf
            # Custom 3D print failure threshold
            if conf >= CONFIDENCE_THRESHOLD:
                is_failure = True
                
        return is_failure, max_conf, annotated, detections

    def run(self):
        print(f"[+] AI Failure Monitor ACTIVE (checking every {CHECK_INTERVAL_SECONDS}s)...", flush=True)
        print("=" * 65, flush=True)
        try:
            while True:
                time.sleep(CHECK_INTERVAL_SECONDS)
                
                status = self.cloud_ctrl.get_status()
                state = status.get("gcode_state", "UNKNOWN")
                nozzle = status.get("nozzle_temp", 0)
                bed = status.get("bed_temp", 0)
                layer = status.get("layer_num", 0)
                total_l = status.get("total_layers", 0)
                pct = status.get("mc_percent", 0)

                # Skip inference if printer is idle/finished
                if state in ("IDLE", "FINISH"):
                    print(f"[{time.strftime('%H:%M:%S')}] Printer {state} | Standing by...", flush=True)
                    continue

                frame = self.fetch_frame()
                if frame is None:
                    print(f"[{time.strftime('%H:%M:%S')}] Waiting for camera frame... (State: {state} | Layer: {layer}/{total_l})", flush=True)
                    continue

                is_fail, conf, annotated, detections = self.analyze_frame(frame)
                ts = time.strftime('%H:%M:%S')

                if is_fail and not self.paused_by_ai:
                    print(f"\n[{ts}] 🚨 SPAGHETTI FAILURE DETECTED! (Confidence: {conf:.1%})", flush=True)
                    print(f"[{ts}] 🛑 SENDING EMERGENCY PAUSE COMMAND TO PRINTER...", flush=True)
                    
                    # 1. Emergency Pause Print via Cloud MQTT
                    self.cloud_ctrl.pause_print()
                    self.paused_by_ai = True
                    
                    # 2. Save proof image
                    fail_img_path = f"failure_detected_{int(time.time())}.jpg"
                    cv2.imwrite(fail_img_path, annotated)
                    print(f"[+] Proof image saved to {fail_img_path}", flush=True)
                    
                    # 3. Send Telegram Alert with Annotated Proof Photo
                    try:
                        from telegram_alert import send_telegram_alert
                        defect_str = ", ".join(detections) or "Spaghetti Failure"
                        send_telegram_alert(
                            photo=annotated,
                            error_type=defect_str,
                            confidence=conf,
                            layer_num=layer,
                            total_layers=total_l,
                            nozzle_temp=nozzle,
                            bed_temp=bed,
                            action_taken="Print Paused Automatically via Cloud MQTT",
                        )
                    except Exception as e:
                        print(f"[!] Telegram notification error: {e}", flush=True)
                    
                else:
                    det_str = ", ".join(detections) if detections else "No defects"
                    print(f"[{ts}] [✓ PASS] Layer: {layer}/{total_l} ({pct}%) | Nozzle: {nozzle}°C | Bed: {bed}°C | {det_str}", flush=True)
                    if not is_fail and self.paused_by_ai:
                        self.paused_by_ai = False

        except KeyboardInterrupt:
            print("\n[*] Stopping Spaghetti Detector...", flush=True)
            self.cloud_ctrl.stop()


if __name__ == "__main__":
    detector = SpaghettiDetector()
    detector.run()
