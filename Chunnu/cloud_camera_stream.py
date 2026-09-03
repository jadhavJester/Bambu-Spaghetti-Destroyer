#!/usr/bin/env python3
"""Bambu Lab Cloud Camera Streamer via BambuSource TUTK P2P.

Connects to Bambu Cloud relay across any network (Hotspot, 5G, Wi-Fi)
and yields full-resolution live camera frames using BambuSource.dll.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import cv2
import numpy as np
import requests

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_credentials.json")


def _find_bambu_source_dll() -> str:
    """Find installed BambuSource.dll on Windows."""
    candidates = [
        os.path.expandvars(r"%APPDATA%\BambuStudio\plugins\BambuSource.dll"),
        os.path.expandvars(r"%APPDATA%\OrcaSlicer\plugins\BambuSource.dll"),
        r"C:\Program Files\Bambu Studio\plugins\BambuSource.dll",
        r"C:\Program Files\OrcaSlicer\plugins\BambuSource.dll",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError("Could not locate BambuSource.dll. Please ensure Bambu Studio or OrcaSlicer is installed.")


class Bambu_Sample(ctypes.Structure):
    _fields_ = [
        ("itrack", ctypes.c_int),
        ("size", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
        ("decode_time", ctypes.c_ulonglong),
    ]


class CloudCameraStreamer:
    def __init__(self):
        self.dll_path = _find_bambu_source_dll()
        self.lib = ctypes.CDLL(self.dll_path)
        
        # Setup function prototypes
        self.lib.Bambu_Create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        self.lib.Bambu_Create.restype = ctypes.c_int
        self.lib.Bambu_Open.argtypes = [ctypes.c_void_p]
        self.lib.Bambu_Open.restype = ctypes.c_int
        self.lib.Bambu_StartStream.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        self.lib.Bambu_StartStream.restype = ctypes.c_int
        self.lib.Bambu_ReadSample.argtypes = [ctypes.c_void_p, ctypes.POINTER(Bambu_Sample)]
        self.lib.Bambu_ReadSample.restype = ctypes.c_int
        self.lib.Bambu_Close.argtypes = [ctypes.c_void_p]
        self.lib.Bambu_Close.restype = ctypes.c_void_p
        self.lib.Bambu_Destroy.argtypes = [ctypes.c_void_p]
        self.lib.Bambu_Destroy.restype = ctypes.c_void_p

        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_cv2: np.ndarray | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def _mint_ttcode(self) -> str:
        """Call Bambu Cloud to get live TUTK session code."""
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        token = creds["token"]
        dev_id = creds["devices"][0]["dev_id"]

        url = "https://api.bambulab.com/v1/iot-service/api/user/ttcode"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(url, headers=headers, json={"dev_id": dev_id}, timeout=10)
        
        if resp.status_code != 200:
            raise ConnectionError(f"Failed to fetch ttcode: {resp.text}")
            
        data = resp.json()
        ttcode = data.get("ttcode")
        authkey = data.get("authkey")
        passwd = data.get("passwd")
        region = data.get("region", "us")
        
        if not ttcode:
            raise ValueError(f"Invalid ttcode response: {data}")

        return f"bambu:///tutk?uid={ttcode}&authkey={authkey}&passwd={passwd}&region={region}&device={dev_id}&user=bblp"

    def _stream_worker(self):
        """Worker loop that continuously fetches samples via TUTK."""
        while self._running:
            tunnel = ctypes.c_void_p()
            try:
                url = self._mint_ttcode()
                ret = self.lib.Bambu_Create(ctypes.byref(tunnel), url.encode("utf-8"))
                if ret != 0 or not tunnel.value:
                    time.sleep(3)
                    continue

                # Poll open
                t0 = time.time()
                connected = False
                last_err = 0
                while self._running and time.time() - t0 < 8:
                    r = self.lib.Bambu_Open(tunnel)
                    last_err = r
                    if r == 0:
                        connected = True
                        break
                    elif r == 2:  # would block
                        time.sleep(0.3)
                    else:
                        break

                if not connected:
                    if last_err == -90:
                        print("[Camera] Note: Printer camera is currently in use by Bambu Studio / Handy (error -90). Retrying...", flush=True)
                    time.sleep(3)
                    continue

                self.lib.Bambu_StartStream(tunnel, True)
                sample = Bambu_Sample()

                # Read loop
                while self._running:
                    r = self.lib.Bambu_ReadSample(tunnel, ctypes.byref(sample))
                    if r == 0 and sample.size > 1000:
                        buf = bytes(ctypes.cast(sample.buffer, ctypes.POINTER(ctypes.c_ubyte * sample.size)).contents)
                        if buf[:2] == b"\xff\xd8" and buf[-2:] == b"\xff\xd9":
                            arr = np.asarray(bytearray(buf), dtype=np.uint8)
                            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            with self._lock:
                                self._latest_jpeg = buf
                                self._latest_cv2 = img
                    elif r not in (0, 2):  # 2 is would_block
                        break
                    time.sleep(0.05)

            except Exception as e:
                time.sleep(3)
            finally:
                if tunnel.value:
                    try:
                        self.lib.Bambu_Close(tunnel)
                    except Exception:
                        pass
                    try:
                        self.lib.Bambu_Destroy(tunnel)
                    except Exception:
                        pass
                    tunnel.value = None

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._stream_worker, daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False

    def get_frame_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def get_frame_cv2(self) -> np.ndarray | None:
        with self._lock:
            if self._latest_cv2 is not None:
                return self._latest_cv2.copy()
            return None


# Global singleton
_STREAMER: CloudCameraStreamer | None = None


def get_cloud_streamer() -> CloudCameraStreamer:
    global _STREAMER
    if _STREAMER is None:
        _STREAMER = CloudCameraStreamer()
        _STREAMER.start()
    return _STREAMER


if __name__ == "__main__":
    print("[*] Starting Cloud TUTK Streamer...")
    s = get_cloud_streamer()
    for _ in range(20):
        time.sleep(1)
        frame = s.get_frame_jpeg()
        if frame:
            print(f"[+] Received live frame: {len(frame)} bytes!")
            break
        print("Waiting for first frame...")
    s.stop()
