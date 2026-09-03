# 🍝 Bambu-Spaghetti-Destroyer

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![YOLOv8](https://img.shields.io/badge/YOLO-v8%20%2F%20v11-green.svg)](https://github.com/ultralytics/ultralytics)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Bambu Lab](https://img.shields.io/badge/Bambu%20Lab-A1%20%7C%20P1%20%7C%20X1-red.svg)](https://bambulab.com)

**Autonomous AI-Powered 3D Print Failure & Spaghetti Sentinel with Remote Cloud Command Center for Bambu Lab 3D Printers.**

---

## 🌟 Overview

**Bambu-Spaghetti-Destroyer** is a zero-configuration, cloud-connected autonomous failure detection system for Bambu Lab printers (A1, A1 mini, P1P, P1S, X1C).

Unlike local-only camera bridges that stop working the moment your laptop leaves home or connects to a phone hotspot, **Bambu-Spaghetti-Destroyer** uses the **exact same proprietary Cloud P2P (TUTK/Kalay) tunnel engine that OrcaSlicer and Bambu Studio use (`BambuSource.dll`)**. This allows you to monitor and protect your prints from **anywhere in the world** — on cellular hotspot, 5G, or office Wi-Fi — with zero port forwarding, zero static IP, and zero home server requirements!

---

## ✨ Features

- 🛰️ **Works from Anywhere (Hotspot / 5G / Remote Wi-Fi)**:
  - Automatically establishes an encrypted P2P media tunnel (`BambuSource.dll`) with your printer across the internet.
  - Bypasses ISP Carrier-Grade NAT (CGNAT) and firewalls without requiring router port-forwarding or a VPN.
- 🍝 **Real-Time YOLO AI Failure Detection**:
  - Powered by fine-tuned **YOLO** weights (`spaghetti_yolo.pt`) trained on **9,000+ real 3D print failures**.
  - Detects **Spaghetti** (detached filament / air printing), **Stringing** (whisker webs), and **Zits** (blob defects).
- 🛑 **Autonomous Cloud Emergency Pause**:
  - Automatically sends a monotonic MQTT control packet to `us.mqtt.bambulab.com:8883` to pause extrusion immediately when failure confidence exceeds threshold (e.g. >70%).
  - Saves an annotated proof photo (`failure_detected_<timestamp>.jpg`) with bounding boxes.
- 📱 **Instant Telegram Failure Alerts with Proof Photos**:
  - Delivers real-time notifications to your Telegram when an error occurs.
  - Includes defect name, AI confidence %, layer #, nozzle/bed temperatures, and emergency pause confirmation.
  - Attaches the exact annotated camera frame with failure bounding boxes.
- 🎛️ **Live Web Command Center Dashboard (`http://localhost:8787`)**:
  - Modern glassmorphic dark UI.
  - Live chamber camera stream with toggleable YOLO detection bounding boxes.
  - Live thermal gauges (Nozzle & Heatbed °C).
  - Print progress bar, current layer / total layers, and print state (`RUNNING`, `PAUSE`, `IDLE`).
  - Remote manual controls: **⏸️ Pause**, **▶️ Resume**, and **⏹️ Stop Print**.

---

## 🏗️ System Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │               BAMBU LAB CLOUD                │
                    │         (api.bambulab.com / us.mqtt)         │
                    └───────▲──────────────────────────────▲───────┘
                            │                              │
         TUTK P2P Signaling │                              │ Cloud MQTT :8883
         & Session Token    │                              │ Telemetry & Commands
                            │                              │
 ┌──────────────────────────▼──────┐            ┌──────────▼────────────────────┐
 │       BAMBU A1 AT HOME          │            │       YOUR LAPTOP / SERVER    │
 │       (Behind Router / CGNAT)   │◀──────────▶│     (Hotspot / 5G / Remote)   │
 └─────────────────────────────────┘  Encrypted └───────────────────────────────┘
                                      P2P Tunnel  │
                                                  ├── cloud_camera_stream.py (TUTK)
                                                  ├── cloud_mqtt_control.py (MQTT)
                                                  ├── cloud_spaghetti_ai.py (YOLO)
                                                  └── app_dashboard.py (:8787 Web UI)
```

---

## 🚀 Quickstart Guide

### 1. Requirements
- Python 3.10 or newer.
- Windows with **Bambu Studio** or **OrcaSlicer** installed (provides `BambuSource.dll`).
- Dependencies:
  ```bash
  pip install -r requirements.txt
  ```

### 2. Connect Your Bambu Cloud Account (One-Time Setup)
Run the authentication script to fetch your cloud session and link your printer:
```powershell
python cloud_bambu_auth.py
```
1. Enter your Bambu Cloud email & password.
2. Enter the 6-digit verification code sent to your email.
3. Your printer's Serial Number and access token will be saved securely to `cloud_credentials.json` (gitignored).

### 3. Launch the Live Web Command Center
Double-click:
👉 **`start-ai-dashboard.bat`**  
*(or run `python app_dashboard.py` in your terminal)*

Open your browser to:
👉 **`http://localhost:8787`**

---

## 📁 Repository Structure

| File | Description |
| :--- | :--- |
| **`app_dashboard.py`** | Real-time Web UI dashboard with live camera, telemetry, and controls. |
| **`cloud_camera_stream.py`** | Direct `BambuSource.dll` TUTK P2P streamer for cloud camera frames. |
| **`cloud_mqtt_control.py`** | Remote Bambu Cloud MQTT controller for telemetry and Emergency Pause. |
| **`cloud_spaghetti_ai.py`** | Standalone AI failure detection background worker. |
| **`cloud_bambu_auth.py`** | Cloud API authentication and device discovery helper. |
| **`spaghetti_yolo.pt`** | Fine-tuned YOLO weights for 3D printing failures. |
| **`train_custom_spaghetti.py`**| Pipeline to fine-tune YOLO models on custom datasets. |
| **`start-ai-dashboard.bat`** | One-click launcher for the Web Command Center. |
| **`start-spaghetti-monitor.bat`** | One-click launcher for the CLI AI monitor. |
| **`Chunnu/`** | Optional local-only LAN bridge (`bambu-go2rtc` for port 6000). |

---

## 🛠️ Training Your Own Custom Model

If you want to train or fine-tune YOLO on your own custom filament colors or camera angles:
1. Place your dataset in `dataset/` (with `data.yaml`, `train/`, and `val/` directories).
2. Run:
   ```bash
   python train_custom_spaghetti.py
   ```
3. Best weights will be saved in `runs/detect/bambu_spaghetti_detector/weights/best.pt`.

---

## 🔒 Security & Privacy

- **No Secrets in Repo**: Account credentials and tokens are saved locally to `cloud_credentials.json` and `.env` which are strictly excluded via `.gitignore`.
- **Encrypted Communication**: All control signals use TLS 1.3 MQTT (:8883), and video streams use encrypted P2P tunnels.

---

## 🤝 Acknowledgments

- **[Ultralytics YOLO](https://github.com/ultralytics/ultralytics)** for the object detection architecture.
- **[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)** & **[open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking)** for reverse engineering research on Bambu networking protocols.
- **[OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)** & **[BambuStudio](https://github.com/bambulab/BambuStudio)** for the native `BambuSource.dll` media driver.

---

## 📄 License
MIT License. Feel free to use and modify for personal or commercial 3D printing operations!
