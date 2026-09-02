#!/usr/bin/env python3
"""Bambu Lab Camera Streamer -> stdout for go2rtc bridge.

Connects to Bambu 3D printer camera service over TLS (TCP port 6000)
and writes raw JPEG frames directly to standard output.
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import sys
import time

# Ensure binary mode on Windows stdout
if sys.platform == "win32":
    import msvcrt
    try:
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    except Exception:
        pass


def _load_dotenv() -> None:
    """Load variables from .env if present in current or parent directories."""
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
    ]
    for env_path in candidates:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


_load_dotenv()

username = "bblp"
hostname = (
    os.environ.get("PRINTER_ADDRESS")
    or os.environ.get("BAMBU_HOST")
    or os.environ.get("PRINTER_IP")
    or ""
)
access_code = (
    os.environ.get("PRINTER_ACCESS_CODE")
    or os.environ.get("BAMBU_ACCESS_CODE")
    or os.environ.get("PRINTER_CODE")
    or ""
)
port = int(os.environ.get("PRINTER_PORT", "6000"))

if not hostname or not access_code:
    sys.stderr.write(
        "[camera-stream] ERROR: Missing PRINTER_ADDRESS or PRINTER_ACCESS_CODE in environment or .env file!\n"
    )
    sys.stderr.flush()
    time.sleep(5)
    sys.exit(1)

MAX_CONNECT_ATTEMPTS = 12

auth_data = bytearray()
auth_data += struct.pack("<I", 0x40)   # '@'\0\0\0
auth_data += struct.pack("<I", 0x3000) # \0'0'\0\0
auth_data += struct.pack("<I", 0)      # \0\0\0\0
auth_data += struct.pack("<I", 0)      # \0\0\0\0
for i in range(0, len(username)):
    auth_data += struct.pack("<c", username[i].encode("ascii"))
for i in range(0, 32 - len(username)):
    auth_data += struct.pack("<x")
for i in range(0, len(access_code)):
    auth_data += struct.pack("<c", access_code[i].encode("ascii"))
for i in range(0, 32 - len(access_code)):
    auth_data += struct.pack("<x")

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

jpeg_start = bytearray([0xFF, 0xD8, 0xFF, 0xE0])
jpeg_end = bytearray([0xFF, 0xD9])

read_chunk_size = 4096

connect_attempts = 0

while connect_attempts < MAX_CONNECT_ATTEMPTS:
    try:
        with socket.create_connection((hostname, port), timeout=10) as sock:
            try:
                connect_attempts += 1
                ssl_sock = ctx.wrap_socket(sock, server_hostname=hostname)
                ssl_sock.write(auth_data)
                img: bytearray | None = None
                payload_size = 0
            except socket.error as e:
                sys.stderr.write(f"[camera-stream] Socket error during handshake: {e}\n")
                sys.stderr.flush()
                time.sleep(3)
                continue

            ssl_sock.setblocking(False)
            while True:
                try:
                    dr = ssl_sock.recv(read_chunk_size)
                except ssl.SSLWantReadError:
                    time.sleep(0.05)
                    continue
                except Exception as e:
                    sys.stderr.write(f"[camera-stream] Recv error: {e}\n")
                    sys.stderr.flush()
                    time.sleep(1)
                    break

                if img is not None and len(dr) > 0:
                    img += dr
                    if len(img) > payload_size:
                        # Unexpected payload length, reset
                        img = None
                    elif len(img) == payload_size:
                        # Full image received
                        if img[:4] == jpeg_start and img[-2:] == jpeg_end:
                            os.write(1, img)
                        elif img[:2] == b"\xff\xd8" and img[-2:] == b"\xff\xd9":
                            os.write(1, img)
                        img = None
                elif len(dr) == 16:
                    connect_attempts = 0
                    img = bytearray()
                    payload_size = int.from_bytes(dr[0:4], byteorder="little")
                elif len(dr) == 0:
                    sys.stderr.write(
                        "[camera-stream] Connection closed by printer. Check IP & Access Code.\n"
                    )
                    sys.stderr.flush()
                    time.sleep(5)
                    break
                else:
                    time.sleep(0.05)

    except Exception as e:
        sys.stderr.write(f"[camera-stream] Connection attempt failed: {e}\n")
        sys.stderr.flush()
        time.sleep(3)
