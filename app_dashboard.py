#!/usr/bin/env python3
"""Bambu Lab AI Spaghetti Detector & Printer Command Center Dashboard.

Serves an ultra-sleek real-time web UI on http://localhost:8787 with:
- Live AI Camera Stream with toggleable YOLO detection overlay.
- Real-time Cloud Telemetry (Temperatures, Layer #, Progress %, Gcode state).
- Remote Controls (Emergency Pause, Resume, Stop).
- AI Failure Detection Event Log.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import cv2
import numpy as np
import requests
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from starlette.routing import Route
from ultralytics import YOLO
import uvicorn

from cloud_mqtt_control import BambuCloudController
try:
    import camera_stream
except ImportError:
    camera_stream = None

# Global detector state
CONTROLLER = None
YOLO_MODEL = None
AUTO_PAUSE = True
LATEST_FRAME_RAW = None
LATEST_FRAME_AI = None
LATEST_DETECTIONS = []
LATEST_FAIL_CONF = 0.0
AI_LOGS = []
LAST_CHECK_TS = 0.0

CONFIDENCE_THRESHOLD = 0.70


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


try:
    from cloud_camera_stream import get_cloud_streamer
except ImportError:
    get_cloud_streamer = None


def fetch_camera_frame() -> np.ndarray | None:
    """Fetch camera frame via Cloud TUTK or local go2rtc."""
    # 1. Primary: Cloud TUTK Streamer (Works everywhere over internet)
    if get_cloud_streamer is not None:
        try:
            streamer = get_cloud_streamer()
            frame = streamer.get_frame_cv2()
            if frame is not None:
                return frame
        except Exception:
            pass

    # 2. Local go2rtc stream (when on home LAN with go2rtc active)
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
                
                # Run YOLO
                results = model.predict(frame, conf=0.50, verbose=False)
                annotated = results[0].plot()
                LATEST_FRAME_AI = annotated
                
                detections = []
                is_failure = False
                max_conf = 0.0
                
                for box in results[0].boxes:
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    cls_name = results[0].names.get(cls_id, str(cls_id))
                    detections.append({"name": cls_name, "conf": round(conf * 100, 1)})
                    if conf > max_conf:
                        max_conf = conf
                    if conf >= CONFIDENCE_THRESHOLD:
                        is_failure = True

                LATEST_DETECTIONS = detections
                LATEST_FAIL_CONF = max_conf
                LAST_CHECK_TS = time.time()

                if is_failure and AUTO_PAUSE:
                    # Log event & Trigger Pause
                    ts_str = time.strftime("%H:%M:%S")
                    msg = f"Spaghetti/Defect detected ({max_conf:.1%}) -> Emergency Pause Triggered!"
                    AI_LOGS.insert(0, {"time": ts_str, "type": "danger", "msg": msg})
                    if len(AI_LOGS) > 30:
                        AI_LOGS.pop()
                    ctrl.pause_print()

        except Exception as e:
            # print(f"Monitor loop error: {e}")
            pass
            
        await asyncio.sleep(4)


# --- HTTP Handlers ---

async def index(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bambu A1 | AI Spaghetti Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: rgba(22, 27, 34, 0.85);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent: #10b981;
      --accent-glow: rgba(16, 185, 129, 0.25);
      --danger: #ef4444;
      --warning: #f59e0b;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Outfit', sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      background-image: radial-gradient(circle at 20% 15%, rgba(16, 185, 129, 0.08) 0%, transparent 40%),
                        radial-gradient(circle at 80% 85%, rgba(59, 130, 246, 0.06) 0%, transparent 40%);
    }
    header {
      padding: 1rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
      background: rgba(13, 17, 23, 0.8);
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .logo-group { display: flex; align-items: center; gap: 0.75rem; }
    .logo-icon {
      width: 36px; height: 36px;
      background: linear-gradient(135deg, #10b981, #059669);
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 12px var(--accent-glow);
    }
    .logo-title { font-size: 1.25rem; font-weight: 700; letter-spacing: -0.02em; }
    .logo-subtitle { font-size: 0.75rem; color: var(--text-muted); }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.8rem;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; background: #10b981;
      box-shadow: 0 0 10px #10b981;
      animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(0.85); } }

    main {
      padding: 1.5rem 2rem;
      flex: 1;
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 1.5rem;
      max-width: 1600px;
      width: 100%;
      margin: 0 auto;
    }
    @media (max-width: 1024px) { main { grid-template-columns: 1fr; } }

    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.25rem;
      backdrop-filter: blur(16px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
    }
    .card-title {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    /* Video Frame */
    .video-container {
      position: relative;
      width: 100%;
      aspect-ratio: 4 / 3;
      background: #000;
      border-radius: 12px;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }
    .video-container img {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
    .stream-overlay-tag {
      position: absolute;
      top: 12px;
      left: 12px;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(8px);
      padding: 0.35rem 0.75rem;
      border-radius: 8px;
      font-size: 0.75rem;
      font-family: 'JetBrains Mono', monospace;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    /* Controls Grid */
    .btn-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.75rem;
      margin-top: 1rem;
    }
    .btn {
      padding: 0.75rem 1rem;
      border-radius: 10px;
      font-weight: 600;
      font-size: 0.85rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      border: none;
      transition: all 0.2s;
    }
    .btn-pause { background: #f59e0b; color: #111; }
    .btn-pause:hover { background: #d97706; }
    .btn-resume { background: #10b981; color: #fff; }
    .btn-resume:hover { background: #059669; }
    .btn-stop { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
    .btn-stop:hover { background: #ef4444; color: #fff; }

    /* Telemetry Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 1rem;
      margin-bottom: 1.25rem;
    }
    .metric-box {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .metric-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }
    .metric-value { font-size: 1.5rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

    /* Progress Bar */
    .progress-wrap { margin-top: 0.5rem; }
    .progress-bar-bg {
      width: 100%;
      height: 10px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 999px;
      overflow: hidden;
      margin: 0.5rem 0;
    }
    .progress-bar-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #10b981, #3b82f6);
      transition: width 0.5s ease;
    }

    /* Logs */
    .logs-container {
      max-height: 220px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8rem;
    }
    .log-item {
      padding: 0.5rem 0.75rem;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.02);
      border-left: 3px solid #10b981;
    }
    .log-item.danger { border-left-color: #ef4444; background: rgba(239, 68, 68, 0.08); }

    .toggle-ai {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.8rem;
      cursor: pointer;
    }
  </style>
</head>
<body>

  <header>
    <div class="logo-group">
      <div class="logo-icon">🍝</div>
      <div>
        <div class="logo-title">Bambu AI Command Center</div>
        <div class="logo-subtitle">Real-time YOLOv8 Spaghetti & Failure Sentinel</div>
      </div>
    </div>
    <div id="printer-badge" class="status-badge">
      <div class="status-dot"></div>
      <span id="badge-text">CONNECTING</span>
    </div>
  </header>

  <main>
    <!-- Left Column: Video & Controls -->
    <div style="display: flex; flex-direction: column; gap: 1.5rem;">
      <div class="card">
        <div class="card-title">
          <span>📹 Live Chamber Camera</span>
          <label class="toggle-ai">
            <input type="checkbox" id="ai-toggle" checked onchange="toggleView()">
            Show YOLO AI Detections
          </label>
        </div>
        <div class="video-container">
          <img id="live-camera" src="/api/stream.jpg" alt="Camera Stream">
          <div class="stream-overlay-tag">
            <span id="fps-counter">● LIVE</span>
            <span id="detection-tag" style="color: #34d399;">| No Defects</span>
          </div>
        </div>

        <div class="btn-grid">
          <button class="btn btn-pause" onclick="sendCommand('pause')">⏸️ Pause Print</button>
          <button class="btn btn-resume" onclick="sendCommand('resume')">▶️ Resume</button>
          <button class="btn btn-stop" onclick="sendCommand('stop')">⏹️ Stop Print</button>
        </div>
      </div>

      <!-- Event Logs -->
      <div class="card">
        <div class="card-title">
          <span>🛡️ Sentinel Activity Logs</span>
          <span style="font-size: 0.75rem; color: #10b981;">Auto-Pause: ON</span>
        </div>
        <div id="logs-feed" class="logs-container">
          <div class="log-item">AI monitor initialized. Watching for print failures...</div>
        </div>
      </div>
    </div>

    <!-- Right Column: Telemetry & Progress -->
    <div style="display: flex; flex-direction: column; gap: 1.5rem;">
      <div class="card">
        <div class="card-title">
          <span>📊 Print Progress</span>
          <span id="percentage-label" style="font-family: 'JetBrains Mono'; color: #10b981;">0%</span>
        </div>
        <div class="progress-wrap">
          <div class="progress-bar-bg">
            <div id="progress-fill" class="progress-bar-fill"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted);">
            <span id="layer-label">Layer 0 / 0</span>
            <span id="state-label">State: IDLE</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>🔥 Thermal Telemetry</span>
        </div>
        <div class="metrics-grid">
          <div class="metric-box">
            <div class="metric-label">Nozzle Temp</div>
            <div class="metric-value"><span id="nozzle-temp">0</span>°C</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">Heatbed Temp</div>
            <div class="metric-value"><span id="bed-temp">0</span>°C</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">Print Speed</div>
            <div class="metric-value"><span id="speed-lvl">100</span>%</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">Spaghetti Risk</div>
            <div class="metric-value" id="risk-score" style="color: #34d399;">0%</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>🖨️ Printer Hardware</span>
        </div>
        <div style="display: flex; flex-direction: column; gap: 0.5rem; font-size: 0.85rem; color: var(--text-muted);">
          <div style="display: flex; justify-content: space-between;"><span>Model:</span><strong style="color: #fff;">Bambu Lab A1</strong></div>
          <div style="display: flex; justify-content: space-between;"><span>Serial:</span><code style="color: #38bdf8;">03919D591207239</code></div>
          <div style="display: flex; justify-content: space-between;"><span>Cloud Bridge:</span><span style="color: #10b981;">● Connected (us.mqtt.bambulab.com)</span></div>
        </div>
      </div>
    </div>
  </main>

  <script>
    let showAi = true;
    function toggleView() {
      showAi = document.getElementById('ai-toggle').checked;
    }

    // Double buffered image refresh
    function refreshFrame() {
      const img = document.getElementById('live-camera');
      const url = showAi ? '/api/stream_ai.jpg?t=' : '/api/stream_raw.jpg?t=';
      img.src = url + Date.now();
    }
    setInterval(refreshFrame, 1500);

    async function updateTelemetry() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        
        // Badge
        const badge = document.getElementById('printer-badge');
        const badgeText = document.getElementById('badge-text');
        const state = data.gcode_state || 'IDLE';
        badgeText.innerText = state;
        
        // Metrics
        document.getElementById('nozzle-temp').innerText = Math.round(data.nozzle_temp || 0);
        document.getElementById('bed-temp').innerText = Math.round(data.bed_temp || 0);
        document.getElementById('percentage-label').innerText = (data.mc_percent || 0) + '%';
        document.getElementById('progress-fill').style.width = (data.mc_percent || 0) + '%';
        document.getElementById('layer-label').innerText = `Layer ${data.layer_num || 0} / ${data.total_layers || 0}`;
        document.getElementById('state-label').innerText = `State: ${state}`;
        
        // Risk
        const risk = Math.round((data.fail_conf || 0) * 100);
        const riskElem = document.getElementById('risk-score');
        riskElem.innerText = risk + '%';
        riskElem.style.color = risk > 50 ? '#ef4444' : '#34d399';
        
        // Detection tag
        const tag = document.getElementById('detection-tag');
        if (data.detections && data.detections.length > 0) {
          tag.innerText = '| ' + data.detections.map(d => `${d.name} (${d.conf}%)`).join(', ');
          tag.style.color = risk > 60 ? '#ef4444' : '#f59e0b';
        } else {
          tag.innerText = '| No Defects';
          tag.style.color = '#34d399';
        }

        // Logs
        if (data.logs && data.logs.length > 0) {
          const logBox = document.getElementById('logs-feed');
          logBox.innerHTML = data.logs.map(l => 
            `<div class="log-item ${l.type}">[${l.time}] ${l.msg}</div>`
          ).join('');
        }
      } catch (e) {
        console.error("Telemetry error", e);
      }
    }
    setInterval(updateTelemetry, 2000);
    updateTelemetry();

    async function sendCommand(cmd) {
      if (!confirm(`Are you sure you want to ${cmd.toUpperCase()} the print?`)) return;
      try {
        await fetch(`/api/control/${cmd}`, { method: 'POST' });
        alert(`Sent ${cmd.toUpperCase()} command!`);
      } catch (e) {
        alert("Command failed: " + e);
      }
    }
  </script>
</body>
</html>"""
    return HTMLResponse(html)


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
    
    # Return placeholder
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(blank, "Connecting to A1 Camera...", (140, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
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
    if cmd == "pause":
        ctrl.pause_print()
    elif cmd == "resume":
        ctrl.resume_print()
    elif cmd == "stop":
        ctrl.stop_print()
    return JSONResponse({"status": "ok", "command": cmd})


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
    print("\n" + "=" * 60)
    print("🚀 Bambu Lab AI Spaghetti Dashboard Starting!")
    print("👉 Open in browser: http://localhost:8787")
    print("=" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
