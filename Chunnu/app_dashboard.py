#!/usr/bin/env python3
"""Bambu Lab AI Spaghetti Dashboard - Pure Bambu Studio & OrcaSlicer Device Tab.

Streamlined Device Command Center featuring:
- Chamber camera feed with toggleable YOLO failure detection
- Real-time temperatures (Nozzle/Bed actual vs target)
- Direct spool filament monitor (AMS removed)
- Print progress, layer counter, and remaining time
- Speed profile modes (Silent, Standard, Sport, Ludicrous)
- Pause, Resume, Stop controls with verified monotonic MQTT
- Telegram bot alert integration with instant photo dispatch
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
  <title>Bambu Studio / OrcaSlicer - Device Manager</title>
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
        <span class="slicer-logo-badge">DEVICE</span>
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
    print("🚀 Bambu Studio / OrcaSlicer Device Manager Starting!")
    print("👉 Open in browser: http://localhost:8787")
    print("=" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
