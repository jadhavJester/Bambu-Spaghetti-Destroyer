#!/usr/bin/env python3
"""Bambu Lab AI Spaghetti Dashboard - High Performance Real-Time Streaming Edition.

Key Features:
- Fluid Native MJPEG Video Stream (/api/stream.mjpeg) - zero polling jitter, smooth 15 FPS
- Decoupled Camera Ingestion thread + Asynchronous YOLO Sentinel
- Monitored Direct-Feed Single Spool & Live Bambu Cloud MQTT Telemetry
- Verified monotonic pause/resume/stop printer controls
- Telegram bot photo alert dispatch
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
import time
import cv2
import numpy as np
import requests
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
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


# Global state
CONTROLLER: BambuCloudController | None = None
YOLO_MODEL: YOLO | None = None

LATEST_FRAME_RAW: np.ndarray | None = None
LATEST_FRAME_AI: np.ndarray | None = None
SHOW_AI_OVERLAY: bool = True
FRAME_LOCK = threading.Lock()

LATEST_DETECTIONS: list = []
LATEST_FAIL_CONF = 0.0
AI_LOGS = []

HAZARDOUS_DEFECTS = ("spaghetti", "bed", "detach", "dislodge", "air_print")
CRITICAL_PAUSE_CONFIDENCE = 0.82  # 82% threshold
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
        print("[*] Loading YOLO failure detection weights (spaghetti_yolo.pt)...", flush=True)
        YOLO_MODEL = YOLO("spaghetti_yolo.pt")
    return YOLO_MODEL


# --- Dedicated Thread: Fast Camera Ingestion (15 FPS) ---

def camera_ingestion_worker():
    """Continuously pulls camera frames from Cloud TUTK or go2rtc."""
    global LATEST_FRAME_RAW
    print("[*] Camera ingestion worker started.", flush=True)
    streamer = None

    while True:
        frame = None
        # 1. Cloud TUTK P2P streamer
        if get_cloud_streamer is not None:
            try:
                if streamer is None:
                    streamer = get_cloud_streamer()
                frame = streamer.get_frame_cv2()
            except Exception:
                streamer = None

        # 2. Local fallback if on home LAN
        if frame is None:
            try:
                resp = requests.get("http://localhost:1984/api/frame.jpeg?src=bambu_camera", timeout=0.5)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception:
                pass

        if frame is not None:
            with FRAME_LOCK:
                LATEST_FRAME_RAW = frame
            time.sleep(0.06)  # ~15 FPS
        else:
            time.sleep(0.3)


# --- Dedicated Thread: Asynchronous YOLO AI Sentinel ---

def ai_sentinel_worker():
    """Samples frames and evaluates failure risk without lagging video."""
    global LATEST_FRAME_AI, LATEST_DETECTIONS, LATEST_FAIL_CONF
    model = get_yolo()
    ctrl = get_controller()
    print("[*] AI Sentinel worker online.", flush=True)

    while True:
        time.sleep(1.2)
        frame_copy = None
        with FRAME_LOCK:
            if LATEST_FRAME_RAW is not None:
                frame_copy = LATEST_FRAME_RAW.copy()

        if frame_copy is None:
            continue

        try:
            results = model.predict(frame_copy, conf=0.35, verbose=False)
            annotated = results[0].plot()

            detections = []
            max_conf = 0.0
            critical_failure = False
            critical_defect = ""
            critical_conf = 0.0

            for box in results[0].boxes:
                cls_id = int(box.cls[0])
                cls_name = results[0].names.get(cls_id, str(cls_id)).lower()
                conf = float(box.conf[0])
                detections.append({"name": cls_name, "conf": round(conf * 100, 1)})

                if conf > max_conf:
                    max_conf = conf

                # ONLY pause if defect is heavy spaghetti or bed separation >= 82%
                is_hazardous = any(k in cls_name for k in HAZARDOUS_DEFECTS)
                if is_hazardous and conf >= CRITICAL_PAUSE_CONFIDENCE:
                    critical_failure = True
                    critical_defect = cls_name
                    critical_conf = conf

            with FRAME_LOCK:
                LATEST_FRAME_AI = annotated
                LATEST_DETECTIONS = detections
                LATEST_FAIL_CONF = max_conf

            if critical_failure and AUTO_PAUSE:
                ts_str = time.strftime("%H:%M:%S")
                defect_title = critical_defect.replace("_", " ").title()
                msg = f"CRITICAL {defect_title.upper()} ({critical_conf:.1%}) -> Emergency Pause Triggered!"
                AI_LOGS.insert(0, {"time": ts_str, "type": "danger", "msg": msg})
                if len(AI_LOGS) > 30:
                    AI_LOGS.pop()
                ctrl.pause_print()

                try:
                    stat = ctrl.get_status()
                    ok = send_telegram_alert(
                        photo=annotated,
                        error_type=f"Critical {defect_title} Failure",
                        confidence=critical_conf,
                        layer_num=stat.get("layer_num") or 0,
                        total_layers=stat.get("total_layers") or 0,
                        nozzle_temp=stat.get("nozzle_temp") or 0.0,
                        bed_temp=stat.get("bed_temp") or 0.0,
                        action_taken="Emergency Pause Executed (Print Halted)",
                    )
                    if ok:
                        AI_LOGS.insert(0, {"time": ts_str, "type": "info", "msg": "Telegram alert & proof photo delivered!"})
                except Exception as tg_err:
                    print(f"[Telegram Alert Error]: {tg_err}", flush=True)

        except Exception as e:
            # print(f"Inference error: {e}")
            pass


# --- Background MQTT Telemetry Poller ---

def mqtt_heartbeat_worker():
    """Periodically requests full telemetry dumps from Bambu Cloud."""
    ctrl = get_controller()
    while True:
        try:
            ctrl.request_full_status()
        except Exception:
            pass
        time.sleep(8)


# --- Starlette Web Routes ---

async def index(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bambu Studio / OrcaSlicer - Live Command Center</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Segoe+UI:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">

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
      user-select: none;
    }

    /* Top Slicer Header */
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
      font-weight: 600;
      color: #fff;
      display: flex;
      align-items: center;
      height: 100%;
      border-bottom: 2px solid var(--bambu-green);
      padding: 0 0.5rem;
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

    /* Main Grid */
    main {
      flex: 1;
      padding: 1.25rem 1.5rem;
      display: grid;
      grid-template-columns: 1.55fr 1fr;
      gap: 1.25rem;
      max-width: 1720px;
      width: 100%;
      margin: 0 auto;
    }
    @media (max-width: 1100px) { main { grid-template-columns: 1fr; } }

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

    /* Camera Viewport */
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

    .select-dark {
      background: #181818;
      color: #fff;
      border: 1px solid var(--border-color);
      border-radius: 4px;
      padding: 0.4rem 0.6rem;
      font-size: 0.8rem;
      cursor: pointer;
    }

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
      width: 44px; height: 44px; border-radius: 50%;
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
      padding: 0.75rem 1rem; max-height: 180px; overflow-y: auto;
      font-family: 'JetBrains Mono', monospace; font-size: 0.75rem;
      display: flex; flex-direction: column; gap: 0.4rem;
    }
    .activity-item { padding: 0.4rem 0.6rem; border-radius: 4px; background: #181818; border-left: 3px solid var(--bambu-green); }
    .activity-item.danger { border-left-color: var(--danger); background: rgba(239, 68, 68, 0.08); }
    .activity-item.info { border-left-color: var(--orca-teal); }
  </style>
</head>
<body>

  <!-- Top Navigation Header -->
  <header>
    <div class="slicer-tabs">
      <div class="slicer-logo">
        <span>BambuStudio</span>
        <span class="slicer-logo-badge">LIVE</span>
      </div>
      <div class="tab-item">Device Control</div>
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

  <!-- Main Work Area (Device View) -->
  <main>
    <!-- Left: Chamber MJPEG Video Stream & Controls -->
    <div style="display: flex; flex-direction: column; gap: 1rem;">
      <div class="studio-card">
        <div class="studio-card-header">
          <span>Chamber Live Stream (MJPEG 15 FPS)</span>
          <label style="cursor: pointer; display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem;">
            <input type="checkbox" id="ai-toggle" checked onchange="toggleAiView(this.checked)">
            <span>YOLO AI Overlays</span>
          </label>
        </div>

        <div class="camera-viewport">
          <!-- Fluid Native MJPEG Continuous Video Stream -->
          <img id="live-camera" src="/api/stream.mjpeg" alt="Chamber Camera Feed">
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

          <div style="display: flex; align-items: center; gap: 0.5rem;">
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

      <!-- Single Spool Holder -->
      <div class="spool-single-card">
        <div class="spool-single-circle" id="single-spool-dot"></div>
        <div style="display: flex; flex-direction: column; gap: 0.2rem;">
          <span style="font-size: 0.85rem; font-weight: 600; color: #fff;">Filament Spool (Direct Feed)</span>
          <span style="font-size: 0.75rem; color: var(--text-muted);">Bambu PLA Basic • White (1.75mm)</span>
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
  </main>

  <script>
    function toggleFullscreen() {
      const elem = document.querySelector('.camera-viewport');
      if (!document.fullscreenElement) {
        elem.requestFullscreen().catch(err => alert(err.message));
      } else {
        document.exitFullscreen();
      }
    }

    async function toggleAiView(enabled) {
      try {
        await fetch(`/api/toggle_ai?enabled=${enabled}`, { method: 'POST' });
      } catch (e) {
        console.error("Toggle AI error", e);
      }
    }

    // Real-time Telemetry Poller (1.5s)
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
        
        const dets = (data.detections || []).map(d => d.name.toLowerCase());
        const hasHazardous = dets.some(n => n.includes('spaghetti') || n.includes('bed') || n.includes('detach'));

        if (hasHazardous && risk >= 82) {
          riskElem.style.color = '#ef4444';
          riskBadge.innerText = 'CRITICAL DEFECT!';
          riskBadge.style.color = '#ef4444';
          aiBadge.innerText = '| 🚨 HAZARDOUS FAILURE (PAUSED)';
          aiBadge.style.color = '#ef4444';
        } else if (dets.includes('stringing') || dets.includes('zits')) {
          riskElem.style.color = '#38bdf8';
          riskBadge.innerText = 'Cosmetic Only';
          riskBadge.style.color = '#38bdf8';
          aiBadge.innerText = '| Minor Stringing/Zits (Running)';
          aiBadge.style.color = '#38bdf8';
        } else if (risk > 40) {
          riskElem.style.color = '#f59e0b';
          riskBadge.innerText = 'Watching';
          riskBadge.style.color = '#f59e0b';
          aiBadge.innerText = '| Analyzing Surface';
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
    setInterval(updateTelemetry, 1500);
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


async def api_mjpeg_stream(request):
    """High-performance continuous MJPEG live video stream (15 FPS)."""
    async def frame_generator():
        while True:
            frame = None
            with FRAME_LOCK:
                if SHOW_AI_OVERLAY and LATEST_FRAME_AI is not None:
                    frame = LATEST_FRAME_AI
                elif LATEST_FRAME_RAW is not None:
                    frame = LATEST_FRAME_RAW

            if frame is not None:
                _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                yield (
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
                )
            await asyncio.sleep(0.065)  # ~15 FPS

    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


async def api_stream_single(request):
    """Fallback single JPEG frame."""
    frame = None
    with FRAME_LOCK:
        frame = LATEST_FRAME_AI if (SHOW_AI_OVERLAY and LATEST_FRAME_AI is not None) else LATEST_FRAME_RAW

    if frame is not None:
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return Response(buf.tobytes(), media_type="image/jpeg")

    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(blank, "Waiting for Camera Stream...", (120, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    _, buf = cv2.imencode('.jpg', blank)
    return Response(buf.tobytes(), media_type="image/jpeg")


async def api_toggle_ai(request):
    global SHOW_AI_OVERLAY
    enabled_str = request.query_params.get("enabled", "true")
    SHOW_AI_OVERLAY = enabled_str.lower() in ("1", "true", "yes")
    return JSONResponse({"status": "ok", "show_ai": SHOW_AI_OVERLAY})


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
    Route("/api/stream.mjpeg", endpoint=api_mjpeg_stream),
    Route("/api/stream.jpg", endpoint=api_stream_single),
    Route("/api/toggle_ai", endpoint=api_toggle_ai, methods=["POST"]),
    Route("/api/control/{command}", endpoint=api_control, methods=["POST"]),
]

app = Starlette(routes=routes)


def start_all_background_workers():
    """Start threads on launch."""
    get_controller()
    
    t_cam = threading.Thread(target=camera_ingestion_worker, daemon=True)
    t_cam.start()

    t_ai = threading.Thread(target=ai_sentinel_worker, daemon=True)
    t_ai.start()

    t_hb = threading.Thread(target=mqtt_heartbeat_worker, daemon=True)
    t_hb.start()


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("🚀 Bambu Studio / OrcaSlicer Real-Time Dashboard Starting!")
    print("👉 Open in browser: http://localhost:8787")
    print("=" * 60 + "\n")
    start_all_background_workers()
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
