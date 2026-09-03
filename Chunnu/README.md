# 🍝 Bambu-Spaghetti-Destroyer

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![YOLOv8](https://img.shields.io/badge/YOLO-v8%20%2F%20v11-green.svg)](https://github.com/ultralytics/ultralytics)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Bambu Lab](https://img.shields.io/badge/Bambu%20Lab-A1%20%7C%20P1%20%7C%20X1-red.svg)](https://bambulab.com)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20Alerts-blue.svg)](https://telegram.org)

**Autonomous AI-Powered 3D Print Failure & Spaghetti Sentinel with Remote Cloud Command Center and Telegram Bot Photo Alerts for Bambu Lab 3D Printers.**

---

## 🌟 Overview

**Bambu-Spaghetti-Destroyer** is an autonomous failure detection system for Bambu Lab 3D printers (**A1, A1 mini, P1P, P1S, X1C**).

Unlike local-only camera bridges that stop working the moment your laptop leaves home or connects to a phone hotspot, **Bambu-Spaghetti-Destroyer** uses the **exact same proprietary Cloud P2P (TUTK/Kalay) tunnel engine that OrcaSlicer and Bambu Studio use (`BambuSource.dll`)**. 

This allows you to monitor, inspect, and protect your prints from **anywhere in the world** — on cellular hotspot, 5G, or office Wi-Fi — with zero router port forwarding, zero static IP, and zero home server or mini PC requirements.

---

## ✨ Features

- 🛰️ **Remote Cloud P2P Camera (Hotspot / 5G / Remote Wi-Fi)**:
  - Establishes an encrypted P2P media tunnel (`BambuSource.dll`) with your printer across the internet.
  - Bypasses ISP Carrier-Grade NAT (CGNAT) and firewalls without requiring router port-forwarding or a VPN.
- 🍝 **Real-Time YOLO AI Failure Detection**:
  - Powered by fine-tuned **YOLO** weights (`spaghetti_yolo.pt`) trained on **9,000+ real 3D print failures**.
  - Identifies **Spaghetti** (detached filament / air printing), **Stringing** (whisker webs), and **Zits** (blob defects).
- 🛑 **Autonomous Cloud Emergency Pause**:
  - Automatically issues a monotonic MQTT control packet to `us.mqtt.bambulab.com:8883` to pause extrusion immediately when failure confidence exceeds threshold (e.g., >70%).
  - Saves an annotated proof photo (`failure_detected_<timestamp>.jpg`) with defect bounding boxes.
- 📱 **Instant Telegram Failure Alerts with Proof Photos**:
  - Sends immediate notifications to your Telegram when an error occurs.
  - Formats defect name, AI confidence %, layer #, nozzle/bed temperatures, and emergency pause confirmation.
  - Attaches the single annotated camera frame with bounding boxes directly in the chat.
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
 └─────────────────────────────────┘  Encrypted └───────┬───────────────────────┘
                                      P2P Tunnel        │
                                                        ├── cloud_camera_stream.py (TUTK)
                                                        ├── cloud_mqtt_control.py (MQTT)
                                                        ├── cloud_spaghetti_ai.py (YOLO)
                                                        ├── app_dashboard.py (:8787 UI)
                                                        │
                                                        ▼
                                           ┌───────────────────────────┐
                                           │   TELEGRAM BOT ALERTS     │
                                           │   (Photos + Status Msg)   │
                                           └───────────────────────────┘
```

---

## 📋 Step-by-Step Installation & Setup

### Step 1: Install Prerequisites
1. **Python 3.10+**: Download from [python.org](https://www.python.org/downloads/). (Make sure to check *"Add Python to PATH"* during installation).
2. **Bambu Studio** or **OrcaSlicer**: Must be installed on your Windows PC (provides `BambuSource.dll` for camera streaming).
3. **Install Python Dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```

---

### Step 2: Connect Bambu Cloud (One-Time Setup)
Run the cloud authentication helper:
```powershell
python cloud_bambu_auth.py
```
1. Enter your Bambu Cloud email and password.
2. Enter the 6-digit verification code sent to your email.
3. Your access token and printer Serial Number are saved securely to `cloud_credentials.json` (gitignored).

---

### Step 3: Enable Developer Mode on Your Printer (Crucial!)
To allow custom software to send Pause and Stop commands without encountering `MQTT Command verification failed`:
1. On your **Bambu A1 Touchscreen**, tap **Settings ⚙️** (bottom-left gear icon).
2. Tap **WLAN / Network 📶**.
3. Locate **"LAN Mode"** or **"Developer Mode"** (under *Security / Access Control* on some firmware versions).
4. Switch **Developer Mode: ON** (or *Allow LAN / Third-party Control*).
5. Tap **Confirm**.

---

### Step 4: Configure Telegram Bot Alerts
1. Open Telegram and search for **[@BotFather](https://t.me/botfather)**:
   - Send `/newbot` and follow the prompts to name your bot.
   - Copy the generated **HTTP API Token** (e.g. `8603881379:AAH...`).
2. Search for **[@userinfobot](https://t.me/userinfobot)**:
   - Send `/start` and copy your numeric **Id** (e.g. `5321627099`).
3. Open your bot in Telegram and tap **START** (or send `/start`).
4. Run the setup launcher:
   👉 Double-click **`setup-telegram.bat`** *(or run `python telegram_alert.py`)*
   - It will save your credentials to `.env` and immediately dispatch a test alert to your phone!

---

### Step 5: Test the Failure Simulation (Optional)
To verify what a real failure alert looks like with real camera frames and annotated YOLO bounding boxes:
```powershell
python simulate_failure_alert.py
```
Check your Telegram: you will receive a notification with a high-resolution JPEG showing the exact bounding box and defect diagnosis!

---

### Step 6: Launch the Web Command Center!
Double-click:
👉 **`start-ai-dashboard.bat`**  
*(or run `python app_dashboard.py` in your terminal)*

Open your browser to:
👉 **`http://localhost:8787`**

You now have a real-time command center:
- Live camera stream.
- Toggleable YOLO failure overlays.
- Real-time nozzle/bed temperature gauges.
- Print progress & layer counter.
- Remote **Pause**, **Resume**, and **Stop** buttons.

---

## 📁 Repository Structure

| File | Description |
| :--- | :--- |
| **`app_dashboard.py`** | Real-time Web UI dashboard with live camera, telemetry, and manual controls. |
| **`cloud_camera_stream.py`** | Direct `BambuSource.dll` TUTK P2P streamer for cloud camera frames. |
| **`cloud_mqtt_control.py`** | Remote Bambu Cloud MQTT controller for telemetry and Emergency Pause. |
| **`cloud_spaghetti_ai.py`** | Standalone AI failure detection background worker. |
| **`telegram_alert.py`** | Telegram notification dispatcher with photo upload. |
| **`simulate_failure_alert.py`**| Test script to simulate a failure and test Telegram alerts. |
| **`cloud_bambu_auth.py`** | Cloud API authentication and device discovery helper. |
| **`spaghetti_yolo.pt`** | Fine-tuned YOLO weights for 3D printing failures (9,000+ prints). |
| **`train_custom_spaghetti.py`**| Pipeline to fine-tune YOLO models on custom datasets. |
| **`start-ai-dashboard.bat`** | One-click launcher for the Web Command Center. |
| **`start-spaghetti-monitor.bat`** | One-click launcher for the CLI AI monitor. |
| **`setup-telegram.bat`** | One-click setup launcher for Telegram alerts. |

---

## 🛠️ Training Your Own Custom Model

If you want to train or fine-tune YOLO on specific filaments or camera angles:
1. Place your dataset in `dataset/` (with `data.yaml`, `train/`, and `val/` directories).
2. Run:
   ```powershell
   python train_custom_spaghetti.py
   ```
3. Best weights will be saved in `runs/detect/bambu_spaghetti_detector/weights/best.pt`.

---

## ❓ Frequently Asked Questions & Troubleshooting

#### Q: The camera feed shows "Connecting to A1 Camera..." or a black screen. Why?
> **Answer:** Bambu Lab printers only allow **ONE active camera connection at a time**. If **Bambu Studio** or **Bambu Handy** is open on your PC/phone with the live camera active, the printer locks the stream. Switch to the *Prepare* tab in Bambu Studio or close the live view in the mobile app, and the dashboard will immediately acquire the stream.

#### Q: I see `ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 8787)`.
> **Answer:** Another instance of `app_dashboard.py` is already running in a terminal. Close any other open dashboard terminals or kill existing Python processes in Task Manager, then restart `start-ai-dashboard.bat`.

#### Q: Telegram returns `Bad Request: chat not found` (Error 400).
> **Answer:** Telegram privacy rules prevent bots from messaging users first. Open your bot in Telegram (e.g. `@YourBotName`) and tap **START** (or send `/start`). Then re-run `python telegram_alert.py`.

#### Q: My printer showed `MQTT Command verification failed`.
> **Answer:** Recent Bambu firmware requires control commands to be signed unless **Developer Mode** is enabled. On your printer touchscreen, go to **Settings > WLAN > LAN Mode / Developer Mode** and switch **Developer Mode: ON**.

---

## 🔒 Security & Privacy

- **No Secrets in Git**: Account credentials (`cloud_credentials.json`), API keys, and `.env` files are strictly excluded via `.gitignore`.
- **End-to-End Encryption**: Control packets use TLS 1.3 MQTT (:8883), and video streams use encrypted P2P tunnels directly between your machine and the printer.

---

## 🤝 Acknowledgments

- **[Ultralytics YOLO](https://github.com/ultralytics/ultralytics)** for the object detection architecture.
- **[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)** & **[open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking)** for protocol reverse-engineering research.
- **[OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)** & **[BambuStudio](https://github.com/bambulab/BambuStudio)** for native driver binaries.

---

## 📄 License
MIT License. Open source and free for personal and commercial 3D printing operations!
