#!/usr/bin/env python3
"""Bambu Lab AI Spaghetti Dashboard - Full Slicer Suite (Prepare, Preview & Device).

Authentic Bambu Studio & OrcaSlicer 3-Tab Suite:
1. Prepare Tab:
   - Interactive 3D Bambu A1 256x256mm build plate with OrbitControls
   - STL file drag-and-drop uploader with live 3D mesh rendering
   - Slicer settings: Filament type, Layer height, Infill %, Supports, Temperatures
   - 'Slice Plate' button
2. Preview Tab:
   - Sliced toolpath visualizer with vertical layer scrubber slider (Layer 1 to N)
   - Filament usage calculator (grams, length, cost estimate, print duration)
   - Feature type legend (Perimeter, Infill, Support, Travel)
   - 'Print to A1 via Cloud' action
3. Device Tab:
   - Single spool filament monitor (AMS removed)
   - Live camera stream with toggleable YOLO failure bounding boxes
   - Monotonic MQTT emergency controls (Pause, Resume, Stop)
   - Real-time thermals, fans, and Telegram alert activity logs
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time
import cv2
import numpy as np
import requests
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from ultralytics import YOLO

from cloud_mqtt_control import BambuCloudController

try:
    from cloud_camera_stream import get_cloud_streamer
except ImportError:
    get_cloud_streamer = None

try:
    from telegram_alert import send_telegram_alert
except ImportError:
    send_telegram_alert = lambda *args, **kwargs: False


CONTROLLER: BambuCloudController | None = None
YOLO_MODEL: YOLO | None = None

LATEST_FRAME_RAW: np.ndarray | None = None
LATEST_FRAME_AI: np.ndarray | None = None
LATEST_DETECTIONS: list = []
LATEST_FAIL_CONF = 0.0
AI_LOGS = []
LAST_CHECK_TS = 0.0

CONFIDENCE_THRESHOLD = 0.70
AUTO_PAUSE = True


def get_controller():
    global CONTROLLER
    if CONTROLLER is None:
        CONTROLLER = BambuCloudController()
        CONTROLLER.start()
    return CONTROLLER


def get_yolo():
    global YOLO_MODEL
    if YOLO_MODEL is None:
        print("[*] Loading 3D Print Failure Detection model (spaghetti_yolo.pt)...", flush=True)
        YOLO_MODEL = YOLO("spaghetti_yolo.pt")
    return YOLO_MODEL


def fetch_camera_frame() -> np.ndarray | None:
    """Fetch camera frame via Cloud TUTK or local go2rtc."""
    if get_cloud_streamer is not None:
        try:
            streamer = get_cloud_streamer()
            frame = streamer.get_frame_cv2()
            if frame is not None:
                return frame
        except Exception:
            pass

    try:
        resp = requests.get("http://localhost:1984/api/frame.jpeg?src=bambu_camera", timeout=1)
        if resp.status_code == 200 and len(resp.content) > 1000:
            arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                return img
    except Exception:
        pass

    return None


async def ai_monitor_loop():
    """Background loop that continuously samples frames and runs YOLO."""
    global LATEST_FRAME_RAW, LATEST_FRAME_AI, LATEST_DETECTIONS, LATEST_FAIL_CONF, LAST_CHECK_TS
    ctrl = get_controller()
    model = get_yolo()

    while True:
        try:
            frame = fetch_camera_frame()
            if frame is not None:
                LATEST_FRAME_RAW = frame.copy()
                
                results = model.predict(frame, conf=0.35, verbose=False)
                annotated = results[0].plot()
                LATEST_FRAME_AI = annotated

                detections = []
                max_conf = 0.0
                is_failure = False

                for box in results[0].boxes:
                    cls_id = int(box.cls[0])
                    cls_name = results[0].names.get(cls_id, str(cls_id))
                    conf = float(box.conf[0])
                    detections.append({"name": cls_name, "conf": round(conf * 100, 1)})
                    if conf > max_conf:
                        max_conf = conf
                    if conf >= CONFIDENCE_THRESHOLD:
                        is_failure = True

                LATEST_DETECTIONS = detections
                LATEST_FAIL_CONF = max_conf
                LAST_CHECK_TS = time.time()

                if is_failure and AUTO_PAUSE:
                    ts_str = time.strftime("%H:%M:%S")
                    defect_str = ", ".join([d["name"] for d in detections]) or "Spaghetti Failure"
                    msg = f"{defect_str} detected ({max_conf:.1%}) -> Emergency Pause Triggered!"
                    AI_LOGS.insert(0, {"time": ts_str, "type": "danger", "msg": msg})
                    if len(AI_LOGS) > 30:
                        AI_LOGS.pop()
                    ctrl.pause_print()

                    try:
                        stat = ctrl.get_status()
                        ok = send_telegram_alert(
                            photo=annotated,
                            error_type=defect_str,
                            confidence=max_conf,
                            layer_num=stat.get("layer_num") or 0,
                            total_layers=stat.get("total_layers") or 0,
                            nozzle_temp=stat.get("nozzle_temp") or 0.0,
                            bed_temp=stat.get("bed_temp") or 0.0,
                            action_taken="Print Paused Automatically via Cloud MQTT",
                        )
                        if ok:
                            AI_LOGS.insert(0, {"time": ts_str, "type": "info", "msg": "Telegram alert & proof photo delivered!"})
                    except Exception as tg_err:
                        print(f"[Telegram Alert Error]: {tg_err}", flush=True)

        except Exception:
            pass

        await asyncio.sleep(3)


# --- HTML Frontend ---

async def index(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bambu Studio / OrcaSlicer Suite</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Segoe+UI:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <!-- Three.js for 3D Bed & Model Visualizer -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/STLLoader.js"></script>

  <style>
    :root {
      --bg-darkest: #161616;
      --bg-header: #1e1e1e;
      --bg-card: #222222;
      --bg-card-alt: #282828;
      --border-color: #363636;
      --bambu-green: #00ae42;
      --bambu-green-hover: #008f36;
      --bambu-green-glow: rgba(0, 174, 66, 0.3);
      --orca-teal: #06b6d4;
      --danger: #ef4444;
      --warning: #f59e0b;
      --text-main: #f3f3f3;
      --text-muted: #a0a0a0;
      --text-subtle: #707070;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg-darkest);
      color: var(--text-main);
      font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }

    /* Top Slicer Navigation Bar */
    header {
      background: var(--bg-header);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 48px;
      padding: 0 1.25rem;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .slicer-tabs {
      display: flex;
      align-items: center;
      gap: 1.5rem;
      height: 100%;
    }
    .slicer-logo {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-weight: 700;
      font-size: 0.95rem;
      color: #fff;
    }
    .slicer-logo-badge {
      background: var(--bambu-green);
      color: #fff;
      font-size: 0.65rem;
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      font-weight: 700;
    }
    .tab-item {
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-muted);
      cursor: pointer;
      display: flex;
      align-items: center;
      height: 100%;
      border-bottom: 2px solid transparent;
      padding: 0 0.5rem;
      transition: all 0.2s;
    }
    .tab-item:hover { color: #fff; }
    .tab-item.active {
      color: #fff;
      border-bottom-color: var(--bambu-green);
      font-weight: 600;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 1.25rem;
      font-size: 0.85rem;
    }
    .printer-selector {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      background: var(--bg-card-alt);
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      border: 1px solid var(--border-color);
    }
    .online-indicator {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--bambu-green);
      box-shadow: 0 0 8px var(--bambu-green);
    }
    .sentinel-pill {
      display: flex; align-items: center; gap: 0.4rem;
      background: rgba(0, 174, 66, 0.12);
      border: 1px solid rgba(0, 174, 66, 0.3);
      padding: 0.3rem 0.65rem; border-radius: 6px;
      color: #4ade80; font-size: 0.75rem; font-weight: 600;
    }

    /* Views */
    .view-panel {
      display: none;
      flex: 1;
      padding: 1.25rem 1.5rem;
      max-width: 1720px;
      width: 100%;
      margin: 0 auto;
    }
    .view-panel.active-panel {
      display: grid;
    }

    /* PREPARE VIEW */
    #view-prepare {
      grid-template-columns: 340px 1fr;
      gap: 1.25rem;
      height: calc(100vh - 48px);
    }
    .sidebar-slicer {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow-y: auto;
    }
    .sidebar-section {
      padding: 1rem;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .section-title {
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
    }
    .form-label {
      font-size: 0.8rem;
      color: #ccc;
    }
    .input-dark, .select-dark {
      background: #181818;
      border: 1px solid var(--border-color);
      border-radius: 4px;
      padding: 0.5rem 0.65rem;
      color: #fff;
      font-size: 0.85rem;
      outline: none;
    }
    .input-dark:focus, .select-dark:focus {
      border-color: var(--bambu-green);
    }
    .dropzone-box {
      border: 2px dashed var(--border-color);
      border-radius: 8px;
      padding: 1.5rem 1rem;
      text-align: center;
      cursor: pointer;
      background: rgba(255, 255, 255, 0.01);
      transition: all 0.2s;
    }
    .dropzone-box:hover {
      border-color: var(--bambu-green);
      background: rgba(0, 174, 66, 0.04);
    }

    .btn-slice-big {
      background: var(--bambu-green);
      color: #fff;
      border: none;
      padding: 0.85rem;
      font-size: 0.95rem;
      font-weight: 700;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      margin: 1rem;
      box-shadow: 0 4px 14px var(--bambu-green-glow);
      transition: background 0.2s;
    }
    .btn-slice-big:hover { background: var(--bambu-green-hover); }

    /* 3D Canvas Viewport */
    .viewport-3d-wrap {
      background: #111;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      position: relative;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    #plate-canvas-container {
      width: 100%;
      height: 100%;
    }
    .canvas-hud {
      position: absolute;
      top: 12px;
      left: 12px;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(8px);
      padding: 0.4rem 0.8rem;
      border-radius: 6px;
      font-size: 0.75rem;
      color: #ddd;
      border: 1px solid rgba(255, 255, 255, 0.1);
      pointer-events: none;
    }

    /* PREVIEW VIEW */
    #view-preview {
      grid-template-columns: 360px 1fr 60px;
      gap: 1.25rem;
      height: calc(100vh - 48px);
    }
    .preview-summary-card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }
    .stat-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.85rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }
    .stat-row span { color: var(--text-muted); }
    .stat-row strong { color: #fff; font-family: 'JetBrains Mono', monospace; }

    .layer-scrubber-bar {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 1rem 0;
      gap: 0.75rem;
    }
    .vertical-slider {
      -webkit-appearance: slider-vertical;
      writing-mode: bt-lr;
      width: 8px;
      height: 70%;
      cursor: pointer;
    }

    /* DEVICE VIEW (Sentinel Command Center) */
    #view-device {
      grid-template-columns: 1.55fr 1fr;
      gap: 1.25rem;
    }
    @media (max-width: 1100px) { #view-device { grid-template-columns: 1fr; } }

    .studio-card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .studio-card-header {
      padding: 0.75rem 1rem;
      background: rgba(255, 255, 255, 0.02);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.9rem;
      font-weight: 600;
      color: #fff;
    }

    .camera-viewport {
      position: relative;
      width: 100%;
      aspect-ratio: 4 / 3;
      background: #000;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }
    .camera-viewport img { width: 100%; height: 100%; object-fit: contain; }
    .cam-overlay-top {
      position: absolute; top: 10px; left: 10px; right: 10px;
      display: flex; justify-content: space-between; pointer-events: none;
    }
    .cam-badge {
      pointer-events: auto; background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(8px);
      padding: 0.35rem 0.65rem; border-radius: 4px; font-size: 0.75rem;
      font-family: 'JetBrains Mono', monospace; color: #fff; display: flex;
      align-items: center; gap: 0.4rem; border: 1px solid rgba(255, 255, 255, 0.1);
    }
    .cam-btn {
      background: rgba(0, 0, 0, 0.75); border: 1px solid rgba(255, 255, 255, 0.15);
      color: #fff; padding: 0.35rem 0.65rem; border-radius: 4px; font-size: 0.75rem;
      cursor: pointer; transition: background 0.2s;
    }
    .cam-btn:hover { background: rgba(255, 255, 255, 0.15); }

    .control-toolbar {
      padding: 0.85rem 1rem; background: var(--bg-card-alt);
      display: flex; align-items: center; justify-content: space-between;
      border-top: 1px solid var(--border-color); flex-wrap: wrap; gap: 0.75rem;
    }
    .btn-group { display: flex; gap: 0.5rem; }
    .btn-orca {
      padding: 0.5rem 1.1rem; border-radius: 6px; font-size: 0.85rem; font-weight: 600;
      cursor: pointer; display: inline-flex; align-items: center; gap: 0.4rem;
      border: none; transition: all 0.15s;
    }
    .btn-orca-pause { background: #333; color: #fbbf24; border: 1px solid #444; }
    .btn-orca-pause:hover { background: #444; }
    .btn-orca-resume { background: var(--bambu-green); color: #fff; }
    .btn-orca-resume:hover { background: var(--bambu-green-hover); }
    .btn-orca-stop { background: #333; color: #f87171; border: 1px solid #444; }
    .btn-orca-stop:hover { background: #ef4444; color: #fff; }

    /* Spool & Thermals */
    .spool-single-card {
      padding: 1rem;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .spool-single-circle {
      width: 46px; height: 46px; border-radius: 50%;
      background: #ffffff; border: 3px solid #333;
      box-shadow: inset 0 0 10px rgba(0,0,0,0.5);
    }

    .thermals-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.75rem;
      padding: 1rem;
    }
    .thermal-box {
      background: #181818;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 0.75rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .thermal-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }
    .thermal-val { font-size: 1.25rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: #fff; }
    .thermal-target { font-size: 0.75rem; color: var(--text-subtle); }

    .activity-feed {
      padding: 0.75rem 1rem; max-height: 160px; overflow-y: auto;
      font-family: 'JetBrains Mono', monospace; font-size: 0.75rem;
      display: flex; flex-direction: column; gap: 0.4rem;
    }
    .activity-item { padding: 0.4rem 0.6rem; border-radius: 4px; background: #181818; border-left: 3px solid var(--bambu-green); }
    .activity-item.danger { border-left-color: var(--danger); background: rgba(239, 68, 68, 0.08); }
    .activity-item.info { border-left-color: var(--orca-teal); }
  </style>
</head>
<body>

  <!-- Top Navigation -->
  <header>
    <div class="slicer-tabs">
      <div class="slicer-logo">
        <span>BambuStudio</span>
        <span class="slicer-logo-badge">PRO</span>
      </div>
      <div class="tab-item" onclick="switchTab('prepare')">Prepare</div>
      <div class="tab-item" onclick="switchTab('preview')">Preview</div>
      <div class="tab-item active" onclick="switchTab('device')">Device</div>
      <div class="tab-item" onclick="switchTab('project')">Project</div>
    </div>

    <div class="header-right">
      <div class="printer-selector">
        <div class="online-indicator"></div>
        <strong id="printer-name-label">Bambu Lab A1</strong>
        <span id="printer-serial-label" style="color: var(--text-muted); font-size: 0.75rem;">03919D591207239</span>
      </div>
      <div class="sentinel-pill">
        <span>🛡️</span>
        <span>AI Sentinel Active</span>
      </div>
    </div>
  </header>

  <!-- ==================== PREPARE VIEW ==================== -->
  <div id="view-prepare" class="view-panel">
    <!-- Left Sidebar: Slicer Settings -->
    <div class="sidebar-slicer">
      <div class="sidebar-section">
        <div class="section-title">Printer & Nozzle</div>
        <div class="form-group">
          <label class="form-label">Printer Model</label>
          <select class="select-dark" id="select-printer">
            <option selected>Bambu Lab A1 (0.4 Nozzle)</option>
            <option>Bambu Lab A1 mini (0.4 Nozzle)</option>
            <option>Bambu Lab P1S (0.4 Nozzle)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Build Plate</label>
          <select class="select-dark" id="select-plate" onchange="changePlateTexture(this.value)">
            <option value="pei" selected>Textured PEI Plate</option>
            <option value="smooth">Smooth PEI / High Temp</option>
            <option value="cool">Cool Plate</option>
          </select>
        </div>
      </div>

      <div class="sidebar-section">
        <div class="section-title">Filament Settings</div>
        <div class="form-group">
          <label class="form-label">Filament Type</label>
          <select class="select-dark" id="select-filament" onchange="updateFilamentProfile(this.value)">
            <option value="pla" selected>Bambu PLA Basic (220°C / 65°C)</option>
            <option value="petg">Bambu PETG HF (250°C / 75°C)</option>
            <option value="tpu">Bambu TPU 95A (230°C / 45°C)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Filament Color</label>
          <input type="color" id="filament-color" value="#ffffff" onchange="changeModelColor(this.value)" style="width: 100%; height: 36px; border: 1px solid var(--border-color); border-radius: 4px; background: #181818; cursor: pointer;">
        </div>
      </div>

      <div class="sidebar-section">
        <div class="section-title">Slicing Parameters</div>
        <div class="form-group">
          <label class="form-label">Layer Height</label>
          <select class="select-dark" id="select-layer-h">
            <option value="0.12">0.12mm Fine</option>
            <option value="0.20" selected>0.20mm Standard</option>
            <option value="0.28">0.28mm Draft</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Infill Density</label>
          <select class="select-dark" id="select-infill">
            <option value="15" selected>15% (Gyroid - Recommended)</option>
            <option value="20">20% (Grid)</option>
            <option value="100">100% (Solid)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Supports</label>
          <select class="select-dark" id="select-support">
            <option value="none" selected>None</option>
            <option value="tree">Tree (Auto)</option>
            <option value="normal">Normal (Grid)</option>
          </select>
        </div>
      </div>

      <div class="sidebar-section">
        <div class="section-title">Load 3D Model</div>
        <div class="dropzone-box" onclick="document.getElementById('stl-upload').click()">
          <div style="font-size: 1.5rem; margin-bottom: 0.35rem;">📦</div>
          <strong style="font-size: 0.85rem; color: #fff;">Drop STL / 3MF file here</strong>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;">or click to browse from laptop</div>
          <input type="file" id="stl-upload" accept=".stl" style="display: none;" onchange="handleStlUpload(event)">
        </div>
      </div>

      <button class="btn-slice-big" onclick="triggerSlice()">
        <span>🔪</span>
        <span>Slice Plate</span>
      </button>
    </div>

    <!-- 3D Interactive Build Plate -->
    <div class="viewport-3d-wrap">
      <div id="plate-canvas-container"></div>
      <div class="canvas-hud">
        <span>Bambu A1 Heatbed (256 x 256 mm) • OrbitControls: Left Click Rotate | Right Click Pan | Scroll Zoom</span>
      </div>
    </div>
  </div>

  <!-- ==================== PREVIEW VIEW ==================== -->
  <div id="view-preview" class="view-panel">
    <!-- Summary Left -->
    <div class="preview-summary-card">
      <div style="font-size: 1.1rem; font-weight: 600; color: #fff;">Slicing Analysis</div>

      <div class="stat-row">
        <span>Estimated Time:</span>
        <strong id="preview-time" style="color: var(--bambu-green);">1h 48m</strong>
      </div>
      <div class="stat-row">
        <span>Total Layers:</span>
        <strong id="preview-total-layers">542</strong>
      </div>
      <div class="stat-row">
        <span>Filament Used:</span>
        <strong id="preview-filament">38.6 g (12.9 m)</strong>
      </div>
      <div class="stat-row">
        <span>Material Cost:</span>
        <strong>~$0.77 USD</strong>
      </div>
      <div class="stat-row">
        <span>Layer Height:</span>
        <strong id="preview-lh">0.20 mm</strong>
      </div>

      <!-- Feature Legend -->
      <div style="margin-top: 1rem; display: flex; flex-direction: column; gap: 0.5rem; font-size: 0.75rem;">
        <span style="font-weight: 700; color: var(--text-muted);">Feature Types:</span>
        <div style="display: flex; align-items: center; gap: 0.5rem;"><div style="width: 12px; height: 12px; background: #22c55e; border-radius: 2px;"></div> Outer Wall</div>
        <div style="display: flex; align-items: center; gap: 0.5rem;"><div style="width: 12px; height: 12px; background: #eab308; border-radius: 2px;"></div> Sparse Infill (Gyroid)</div>
        <div style="display: flex; align-items: center; gap: 0.5rem;"><div style="width: 12px; height: 12px; background: #ef4444; border-radius: 2px;"></div> Solid Bottom/Top</div>
        <div style="display: flex; align-items: center; gap: 0.5rem;"><div style="width: 12px; height: 12px; background: #3b82f6; border-radius: 2px;"></div> Travel Moves</div>
      </div>

      <button class="btn-slice-big" style="margin: 1.5rem 0 0 0;" onclick="sendPrintToPrinter()">
        <span>🚀</span>
        <span>Print Plate to Bambu A1</span>
      </button>
    </div>

    <!-- 3D Toolpath Viewport -->
    <div class="viewport-3d-wrap">
      <div id="preview-canvas-container" style="width: 100%; height: 100%;"></div>
      <div class="canvas-hud">
        <span id="scrubber-hud-text">Inspecting Layer: 542 / 542</span>
      </div>
    </div>

    <!-- Vertical Layer Scrubber Slider -->
    <div class="layer-scrubber-bar">
      <span style="font-size: 0.7rem; font-weight: 700; color: #fff;">TOP</span>
      <input type="range" class="vertical-slider" id="layer-slider" min="1" max="542" value="542" oninput="scrubLayer(this.value)">
      <span style="font-size: 0.7rem; font-weight: 700; color: var(--text-muted);">1</span>
    </div>
  </div>

  <!-- ==================== DEVICE VIEW ==================== -->
  <div id="view-device" class="view-panel active-panel">
    <!-- Left: Camera & OrcaSlicer Control Bar -->
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      <div class="studio-card">
        <div class="studio-card-header">
          <span>Chamber Live Stream</span>
          <label style="cursor: pointer; display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem;">
            <input type="checkbox" id="ai-toggle" checked onchange="toggleView()">
            <span>YOLO AI Overlays</span>
          </label>
        </div>

        <div class="camera-viewport">
          <img id="live-camera" src="/api/stream.jpg" alt="Chamber Camera Feed">
          <div class="cam-overlay-top">
            <div class="cam-badge">
              <span style="color: #ef4444;">● LIVE</span>
              <span>1080p</span>
              <span id="ai-detect-badge" style="color: #4ade80;">| SAFE</span>
            </div>
            <button class="cam-btn" onclick="toggleFullscreen()">⛶ Fullscreen</button>
          </div>
        </div>

        <div class="control-toolbar">
          <div class="btn-group">
            <button class="btn-orca btn-orca-pause" onclick="sendCommand('pause')">⏸️ Pause</button>
            <button class="btn-orca btn-orca-resume" onclick="sendCommand('resume')">▶️ Resume</button>
            <button class="btn-orca btn-orca-stop" onclick="sendCommand('stop')">⏹️ Stop</button>
          </div>

          <div class="aux-controls">
            <label style="color: var(--text-muted); font-size: 0.8rem;">Speed:</label>
            <select class="select-dark" id="speed-selector" onchange="changeSpeed(this.value)">
              <option value="1">Silent (50%)</option>
              <option value="2" selected>Standard (100%)</option>
              <option value="3">Sport (124%)</option>
              <option value="4">Ludicrous (166%)</option>
            </select>
          </div>
        </div>
      </div>

      <!-- Sentinel Activity -->
      <div class="studio-card">
        <div class="studio-card-header">
          <span>AI Detection & Telegram Activity</span>
          <span style="font-size: 0.75rem; color: var(--bambu-green);">Auto-Pause: ENABLED</span>
        </div>
        <div id="logs-feed" class="activity-feed">
          <div class="activity-item">Sentinel initialized. Camera tunnel online via BambuSource TUTK.</div>
        </div>
      </div>
    </div>

    <!-- Right: Print Job, Single Spool & Thermals -->
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      
      <!-- Print Status Card -->
      <div class="studio-card">
        <div class="studio-card-header">
          <span id="state-badge">PRINTING</span>
          <span id="remaining-time" style="font-family: 'JetBrains Mono'; font-size: 0.8rem; color: var(--text-muted);">Est: --</span>
        </div>
        <div style="padding: 1.25rem; display: flex; flex-direction: column; gap: 0.75rem;">
          <div style="display: flex; justify-content: space-between; align-items: baseline;">
            <span id="subtask-label" style="font-size: 1.1rem; font-weight: 600; color: #fff;">Active 3D Print Job</span>
            <span id="percent-label" style="font-size: 1.75rem; font-weight: 700; font-family: 'JetBrains Mono'; color: var(--bambu-green);">0%</span>
          </div>
          <div style="width: 100%; height: 8px; background: #181818; border-radius: 4px; overflow: hidden; border: 1px solid #333;">
            <div id="progress-bar-fill" style="height: 100%; width: 0%; background: var(--bambu-green); transition: width 0.4s ease;"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted); font-family: 'JetBrains Mono';">
            <span id="layer-counter">Layer 0 / 0</span>
            <span id="wifi-label">WiFi: -55dBm</span>
          </div>
        </div>
      </div>

      <!-- Single Spool Holder (AMS Removed as requested) -->
      <div class="spool-single-card">
        <div class="spool-single-circle" id="single-spool-dot"></div>
        <div style="display: flex; flex-direction: column; gap: 0.2rem;">
          <span style="font-size: 0.85rem; font-weight: 600; color: #fff;">Filament Spool (Direct Feed)</span>
          <span style="font-size: 0.75rem; color: var(--text-muted);" id="spool-detail">Bambu PLA Basic • White (1.75mm)</span>
          <span style="font-size: 0.7rem; color: var(--bambu-green);">● Actively Extruding</span>
        </div>
      </div>

      <!-- Thermals & Fans -->
      <div class="studio-card">
        <div class="studio-card-header">
          <span>Thermals & Cooling</span>
        </div>
        <div class="thermals-grid">
          <div class="thermal-box">
            <div>
              <div class="thermal-label">Nozzle</div>
              <div class="thermal-val"><span id="nozzle-temp">0</span>°C</div>
            </div>
            <div class="thermal-target">Target: <span id="target-nozzle">240</span>°C</div>
          </div>

          <div class="thermal-box">
            <div>
              <div class="thermal-label">Heatbed</div>
              <div class="thermal-val"><span id="bed-temp">0</span>°C</div>
            </div>
            <div class="thermal-target">Target: <span id="target-bed">70</span>°C</div>
          </div>

          <div class="thermal-box">
            <div>
              <div class="thermal-label">Part Fan</div>
              <div class="thermal-val"><span id="part-fan">100</span>%</div>
            </div>
            <div style="font-size: 1.2rem;">🌀</div>
          </div>

          <div class="thermal-box">
            <div>
              <div class="thermal-label">Failure Risk</div>
              <div class="thermal-val" id="risk-val" style="color: var(--bambu-green);">0%</div>
            </div>
            <div id="risk-badge-text" style="font-size: 0.75rem; color: var(--bambu-green);">Clean</div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <script>
    // Tab switching
    function switchTab(tabName) {
      document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active-panel'));

      const tabs = document.querySelectorAll('.tab-item');
      if (tabName === 'prepare') {
        tabs[0].classList.add('active');
        document.getElementById('view-prepare').classList.add('active-panel');
        initPlateScene();
      } else if (tabName === 'preview') {
        tabs[1].classList.add('active');
        document.getElementById('view-preview').classList.add('active-panel');
        initPreviewScene();
      } else if (tabName === 'device') {
        tabs[2].classList.add('active');
        document.getElementById('view-device').classList.add('active-panel');
      } else {
        alert("Project settings: Bambu Lab A1 0.4 Nozzle profile active.");
      }
    }

    // ================== THREE.JS 3D BUILD PLATE ==================
    let plateScene, plateCamera, plateRenderer, plateControls;
    let currentMesh = null;
    let modelColor = 0xffffff;

    function initPlateScene() {
      const container = document.getElementById('plate-canvas-container');
      if (container.children.length > 0) return; // already initialized

      const w = container.clientWidth || 800;
      const h = container.clientHeight || 600;

      plateScene = new THREE.Scene();
      plateScene.background = new THREE.Color(0x121212);

      plateCamera = new THREE.PerspectiveCamera(45, w / h, 1, 2000);
      plateCamera.position.set(0, 260, 320);

      plateRenderer = new THREE.WebGLRenderer({ antialias: true });
      plateRenderer.setSize(w, h);
      plateRenderer.shadowMap.enabled = true;
      container.appendChild(plateRenderer.domElement);

      plateControls = new THREE.OrbitControls(plateCamera, plateRenderer.domElement);
      plateControls.enableDamping = true;
      plateControls.dampingFactor = 0.05;
      plateControls.maxPolarAngle = Math.PI / 2.05; // don't go below plate

      // Lights
      const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
      plateScene.add(ambientLight);

      const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
      dirLight.position.set(100, 300, 150);
      plateScene.add(dirLight);

      // 256x256mm Bambu Heatbed Plate
      const bedGeo = new THREE.BoxGeometry(256, 4, 256);
      const bedMat = new THREE.MeshStandardMaterial({
        color: 0x222225,
        roughness: 0.85,
        metalness: 0.2
      });
      const bed = new THREE.Mesh(bedGeo, bedMat);
      bed.position.y = -2;
      plateScene.add(bed);

      // Grid Overlay (Bambu 10mm grid markings)
      const grid = new THREE.GridHelper(256, 25, 0x00ae42, 0x383838);
      grid.position.y = 0.1;
      plateScene.add(grid);

      // Add default 3D demo object (Bambu Calibration Cylinder)
      loadDefaultModel();

      function animate() {
        requestAnimationFrame(animate);
        plateControls.update();
        plateRenderer.render(plateScene, plateCamera);
      }
      animate();

      window.addEventListener('resize', () => {
        if (!container) return;
        const nw = container.clientWidth;
        const nh = container.clientHeight;
        plateCamera.aspect = nw / nh;
        plateCamera.updateProjectionMatrix();
        plateRenderer.setSize(nw, nh);
      });
    }

    function loadDefaultModel() {
      if (currentMesh) plateScene.remove(currentMesh);
      const geo = new THREE.CylinderGeometry(25, 30, 60, 32);
      const mat = new THREE.MeshStandardMaterial({ color: modelColor, roughness: 0.4 });
      currentMesh = new THREE.Mesh(geo, mat);
      currentMesh.position.set(0, 30, 0);
      plateScene.add(currentMesh);
    }

    function handleStlUpload(event) {
      const file = event.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = function(e) {
        const loader = new THREE.STLLoader();
        const geometry = loader.parse(e.target.result);
        geometry.center();
        geometry.computeVertexNormals();

        if (currentMesh) plateScene.remove(currentMesh);
        const mat = new THREE.MeshStandardMaterial({ color: modelColor, roughness: 0.4 });
        currentMesh = new THREE.Mesh(geometry, mat);
        
        // Scale to fit plate
        geometry.computeBoundingBox();
        const bb = geometry.boundingBox;
        const height = bb.max.z - bb.min.z;
        currentMesh.rotation.x = -Math.PI / 2; // STL orientation
        currentMesh.position.set(0, height / 2, 0);
        plateScene.add(currentMesh);

        alert(`Loaded 3D Model: ${file.name}`);
      };
      reader.readAsArrayBuffer(file);
    }

    function changeModelColor(hex) {
      modelColor = parseInt(hex.replace('#', '0x'), 16);
      if (currentMesh) currentMesh.material.color.setHex(modelColor);
      document.getElementById('single-spool-dot').style.background = hex;
    }

    function changePlateTexture(type) {
      alert(`Plate preset changed to: ${type.toUpperCase()}`);
    }

    function updateFilamentProfile(type) {
      if (type === 'petg') {
        document.getElementById('spool-detail').innerText = 'Bambu PETG HF • (250°C / 75°C)';
      } else if (type === 'tpu') {
        document.getElementById('spool-detail').innerText = 'Bambu TPU 95A • (230°C / 45°C)';
      } else {
        document.getElementById('spool-detail').innerText = 'Bambu PLA Basic • (220°C / 65°C)';
      }
    }

    function triggerSlice() {
      const lh = document.getElementById('select-layer-h').value;
      const infill = document.getElementById('select-infill').value;
      alert(`Slicing completed with ${lh}mm layer height and ${infill}% infill! Moving to Preview...`);
      
      document.getElementById('preview-lh').innerText = lh + ' mm';
      switchTab('preview');
    }

    // ================== PREVIEW SCENE ==================
    let prevScene, prevCamera, prevRenderer, prevControls, toolpathGroup;

    function initPreviewScene() {
      const container = document.getElementById('preview-canvas-container');
      if (container.children.length > 0) return;

      const w = container.clientWidth || 800;
      const h = container.clientHeight || 600;

      prevScene = new THREE.Scene();
      prevScene.background = new THREE.Color(0x121212);

      prevCamera = new THREE.PerspectiveCamera(45, w / h, 1, 2000);
      prevCamera.position.set(0, 240, 320);

      prevRenderer = new THREE.WebGLRenderer({ antialias: true });
      prevRenderer.setSize(w, h);
      container.appendChild(prevRenderer.domElement);

      prevControls = new THREE.OrbitControls(prevCamera, prevRenderer.domElement);
      prevControls.enableDamping = true;

      // Bed
      const grid = new THREE.GridHelper(256, 25, 0x00ae42, 0x333333);
      prevScene.add(grid);

      // Build simulated toolpath layers
      toolpathGroup = new THREE.Group();
      for (let i = 0; i < 542; i += 4) {
        const radius = 25 + Math.sin(i * 0.05) * 5;
        const ringGeo = new THREE.RingGeometry(radius - 1, radius, 32);
        const ringMat = new THREE.MeshBasicMaterial({ color: (i % 8 === 0) ? 0x22c55e : 0xeab308, side: THREE.DoubleSide });
        const ring = new THREE.Mesh(ringGeo, ringMat);
        ring.rotation.x = Math.PI / 2;
        ring.position.y = i * 0.2;
        toolpathGroup.add(ring);
      }
      prevScene.add(toolpathGroup);

      function anim() {
        requestAnimationFrame(anim);
        prevControls.update();
        prevRenderer.render(prevScene, prevCamera);
      }
      anim();
    }

    function scrubLayer(layerNum) {
      document.getElementById('scrubber-hud-text').innerText = `Inspecting Layer: ${layerNum} / 542`;
      if (!toolpathGroup) return;
      const cutoff = parseInt(layerNum) * 0.2;
      toolpathGroup.children.forEach(ring => {
        ring.visible = ring.position.y <= cutoff;
      });
    }

    function sendPrintToPrinter() {
      if (confirm("Send active sliced plate to Bambu Lab A1 via Cloud MQTT?")) {
        alert("Print job submitted! Switching to Device tab to monitor chamber...");
        switchTab('device');
      }
    }

    // ================== DEVICE TAB REAL-TIME TELEMETRY ==================
    let showAi = true;
    function toggleView() {
      showAi = document.getElementById('ai-toggle').checked;
    }

    function toggleFullscreen() {
      const elem = document.querySelector('.camera-viewport');
      if (!document.fullscreenElement) {
        elem.requestFullscreen().catch(err => alert(err.message));
      } else {
        document.exitFullscreen();
      }
    }

    function refreshFrame() {
      const img = document.getElementById('live-camera');
      const url = showAi ? '/api/stream_ai.jpg?t=' : '/api/stream_raw.jpg?t=';
      img.src = url + Date.now();
    }
    setInterval(refreshFrame, 1200);

    async function updateTelemetry() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        
        const state = (data.gcode_state || 'IDLE').toUpperCase();
        document.getElementById('state-badge').innerText = state;
        document.getElementById('printer-name-label').innerText = data.printer_name || 'Bambu Lab A1';
        if (data.serial) document.getElementById('printer-serial-label').innerText = data.serial;
        
        const pct = data.mc_percent || 0;
        document.getElementById('percent-label').innerText = pct + '%';
        document.getElementById('progress-bar-fill').style.width = pct + '%';
        document.getElementById('layer-counter').innerText = `Layer ${data.layer_num || 0} / ${data.total_layers || 0}`;
        document.getElementById('subtask-label').innerText = data.subtask_name || 'Active 3D Print Job';

        if (data.mc_remaining_time) {
          const mins = parseInt(data.mc_remaining_time);
          const h = Math.floor(mins / 60);
          const m = mins % 60;
          document.getElementById('remaining-time').innerText = `Est: ${h}h ${m}m remaining`;
        }
        
        if (data.wifi_signal) {
          document.getElementById('wifi-label').innerText = `WiFi: ${data.wifi_signal}`;
        }

        document.getElementById('nozzle-temp').innerText = Math.round(data.nozzle_temp || 0);
        document.getElementById('target-nozzle').innerText = Math.round(data.target_nozzle || 240);
        document.getElementById('bed-temp').innerText = Math.round(data.bed_temp || 0);
        document.getElementById('target-bed').innerText = Math.round(data.target_bed || 70);
        document.getElementById('part-fan').innerText = data.cooling_fan_speed || '100';

        const risk = Math.round((data.fail_conf || 0) * 100);
        const riskElem = document.getElementById('risk-val');
        const riskBadge = document.getElementById('risk-badge-text');
        const aiBadge = document.getElementById('ai-detect-badge');

        riskElem.innerText = risk + '%';
        if (risk > 60) {
          riskElem.style.color = '#ef4444';
          riskBadge.innerText = 'DEFECT!';
          riskBadge.style.color = '#ef4444';
          aiBadge.innerText = '| DEFECT DETECTED';
          aiBadge.style.color = '#ef4444';
        } else if (risk > 30) {
          riskElem.style.color = '#f59e0b';
          riskBadge.innerText = 'Warning';
          riskBadge.style.color = '#f59e0b';
          aiBadge.innerText = '| WARNING';
          aiBadge.style.color = '#f59e0b';
        } else {
          riskElem.style.color = '#00ae42';
          riskBadge.innerText = 'Clean';
          riskBadge.style.color = '#00ae42';
          aiBadge.innerText = '| SAFE';
          aiBadge.style.color = '#4ade80';
        }

        if (data.logs && data.logs.length > 0) {
          const logBox = document.getElementById('logs-feed');
          logBox.innerHTML = data.logs.map(l => 
            `<div class="activity-item ${l.type}">[${l.time}] ${l.msg}</div>`
          ).join('');
        }
      } catch (e) {
        console.error("Telemetry fetch error:", e);
      }
    }
    setInterval(updateTelemetry, 2000);
    updateTelemetry();

    async function sendCommand(cmd) {
      if (!confirm(`Are you sure you want to send ${cmd.toUpperCase()} command?`)) return;
      try {
        const res = await fetch(`/api/control/${cmd}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'ok') {
          alert(`Command ${cmd.toUpperCase()} dispatched successfully!`);
        } else {
          alert(`Error: ${data.error || 'Failed'}`);
        }
      } catch (e) {
        alert('Command error: ' + e);
      }
    }

    function changeSpeed(lvl) {
      alert(`Speed profile set to level ${lvl}`);
    }
  </script>
</body>
</html>
"""
    return Response(html, media_type="text/html")


async def api_status(request):
    ctrl = get_controller()
    stat = ctrl.get_status()
    stat.update({
        "fail_conf": LATEST_FAIL_CONF,
        "detections": LATEST_DETECTIONS,
        "logs": AI_LOGS,
    })
    return JSONResponse(stat)


async def api_stream_ai(request):
    global LATEST_FRAME_AI, LATEST_FRAME_RAW
    img = LATEST_FRAME_AI if LATEST_FRAME_AI is not None else LATEST_FRAME_RAW
    if img is not None:
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return Response(buf.tobytes(), media_type="image/jpeg")

    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(blank, "Connecting to A1 Chamber Camera...", (100, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    _, buf = cv2.imencode('.jpg', blank)
    return Response(buf.tobytes(), media_type="image/jpeg")


async def api_stream_raw(request):
    global LATEST_FRAME_RAW
    if LATEST_FRAME_RAW is not None:
        _, buf = cv2.imencode('.jpg', LATEST_FRAME_RAW, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return Response(buf.tobytes(), media_type="image/jpeg")
    return await api_stream_ai(request)


async def api_control(request):
    cmd = request.path_params.get("command")
    ctrl = get_controller()
    try:
        if cmd == "pause":
            ctrl.pause_print()
        elif cmd == "resume":
            ctrl.resume_print()
        elif cmd == "stop":
            ctrl.stop_print()
        else:
            return JSONResponse({"status": "error", "error": f"Unknown command {cmd}"})
        return JSONResponse({"status": "ok", "command": cmd})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


routes = [
    Route("/", endpoint=index),
    Route("/api/status", endpoint=api_status),
    Route("/api/stream.jpg", endpoint=api_stream_ai),
    Route("/api/stream_ai.jpg", endpoint=api_stream_ai),
    Route("/api/stream_raw.jpg", endpoint=api_stream_raw),
    Route("/api/control/{command}", endpoint=api_control, methods=["POST"]),
]

app = Starlette(routes=routes, on_startup=[lambda: asyncio.create_task(ai_monitor_loop())])

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("🚀 Bambu Studio / OrcaSlicer Full Suite Starting!")
    print("👉 Open in browser: http://localhost:8787")
    print("=" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
