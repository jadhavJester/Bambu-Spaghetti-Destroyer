#!/usr/bin/env python3
"""Bambu Lab Cloud MQTT Controller.

Connects to Bambu Global Cloud MQTT Broker (us.mqtt.bambulab.com:8883)
over TLS to monitor telemetry and send remote print commands (pause/resume/stop).
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import paho.mqtt.client as mqtt
from cloud_bambu_auth import load_credentials

CLOUD_MQTT_HOST = "us.mqtt.bambulab.com"
CLOUD_MQTT_PORT = 8883


class BambuCloudController:
    def __init__(self, serial_number: str | None = None):
        self.creds = load_credentials()
        self.token = self.creds["token"]
        self.user_id = self.creds.get("user_id", "")
        
        devices = self.creds.get("devices", [])
        if not devices:
            raise ValueError("No bound Bambu printers found in this account!")
            
        if serial_number:
            self.device = next((d for d in devices if d.get("dev_id") == serial_number), devices[0])
        else:
            self.device = devices[0]
            
        self.serial = self.device.get("dev_id")
        self.report_topic = f"device/{self.serial}/report"
        self.request_topic = f"device/{self.serial}/request"
        
        self.state = {}
        self.connected = False
        self._seq = int(time.time() % 100000)
        
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"BambuStudio_{int(time.time())}"
        )
        username = f"u_{self.user_id}" if self.user_id else "u_client"
        self.client.username_pw_set(username, self.token)
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(ctx)
        
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            print(f"[+] Successfully connected to Bambu Cloud MQTT for {self.serial} ({self.device.get('name')})", flush=True)
            self.client.subscribe(self.report_topic)
            # Request immediate telemetry dump
            self.request_full_status()
        else:
            print(f"[!] MQTT connection failed with code {rc}", flush=True)

    def _on_disconnect(self, client, userdata, disconnect_flags, rc=None, properties=None):
        self.connected = False
        print("[!] Disconnected from Bambu Cloud MQTT", flush=True)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if "print" in payload:
                self.state.update(payload["print"])
                # Debug report
                gcode = self.state.get("gcode_state", "IDLE")
                nozzle = self.state.get("nozzle_temper", 0)
                bed = self.state.get("bed_temper", 0)
                pct = self.state.get("mc_percent", 0)
                layer = self.state.get("layer_num", 0)
                total_l = self.state.get("total_layer_num", 0)
                print(f"[Telemetry] Status: {gcode} | Nozzle: {nozzle}°C | Bed: {bed}°C | Layer: {layer}/{total_l} ({pct}%)", flush=True)
        except Exception:
            pass

    def start(self):
        """Start non-blocking MQTT listener loop."""
        self.client.connect_async(CLOUD_MQTT_HOST, CLOUD_MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self):
        """Stop MQTT client."""
        self.client.loop_stop()
        self.client.disconnect()

    def send_command(self, cmd_dict: dict):
        """Publish a command to the printer's cloud request topic."""
        payload = json.dumps(cmd_dict)
        print(f"[*] Publishing MQTT to {self.request_topic}: {payload}", flush=True)
        res = self.client.publish(self.request_topic, payload, qos=1)
        try:
            res.wait_for_publish(timeout=2)
        except Exception:
            pass

    def request_full_status(self):
        """Ask the printer to immediately dump complete status."""
        self._seq += 1
        self.send_command({
            "pushing": {
                "sequence_id": str(self._seq),
                "command": "pushall"
            }
        })

    def pause_print(self):
        """🚨 EMERGENCY PAUSE PRINT."""
        self._seq += 1
        print(f"[!] Sending PAUSE command (seq={self._seq}) to {self.serial}...", flush=True)
        self.send_command({
            "print": {
                "sequence_id": str(self._seq),
                "command": "pause",
                "param": ""
            }
        })

    def resume_print(self):
        """Resume paused print."""
        self._seq += 1
        print(f"[+] Sending RESUME command (seq={self._seq}) to {self.serial}...", flush=True)
        self.send_command({
            "print": {
                "sequence_id": str(self._seq),
                "command": "resume",
                "param": ""
            }
        })

    def stop_print(self):
        """Cancel print."""
        self._seq += 1
        print(f"[!] Sending STOP/CANCEL command (seq={self._seq}) to {self.serial}...", flush=True)
        self.send_command({
            "print": {
                "sequence_id": str(self._seq),
                "command": "stop",
                "param": ""
            }
        })

    def get_status(self) -> dict:
        """Return latest printer telemetry."""
        return {
            "nozzle_temp": self.state.get("nozzle_temper"),
            "bed_temp": self.state.get("bed_temper"),
            "layer_num": self.state.get("layer_num"),
            "total_layers": self.state.get("total_layer_num"),
            "mc_percent": self.state.get("mc_percent"),
            "gcode_state": self.state.get("gcode_state"),
            "spd_lvl": self.state.get("spd_lvl"),
            "stage": self.state.get("stg_cur"),
        }


if __name__ == "__main__":
    ctrl = BambuCloudController()
    ctrl.start()
    print("[*] Listening for printer reports over Bambu Cloud (press Ctrl+C to exit)...", flush=True)
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        ctrl.stop()
