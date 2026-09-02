#!/usr/bin/env python3
"""Bambu Lab Cloud Authentication & Device Discovery.

Authenticates with Bambu Cloud API (https://api.bambulab.com)
and retrieves user JWT token, user_id, and list of bound 3D printers.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import requests

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_credentials.json")
API_BASE = "https://api.bambulab.com"


def login(account: str | None = None, password: str | None = None) -> dict:
    """Authenticate with Bambu Lab Cloud and save credentials."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    if not account:
        account = input("Enter Bambu Cloud Email / Account: ").strip()
    if not password:
        password = getpass.getpass("Enter Bambu Cloud Password: ").strip()

    login_url = f"{API_BASE}/v1/user-service/user/login"
    payload = {"account": account, "password": password}

    print(f"[*] Authenticating {account} with Bambu Cloud...")
    resp = session.post(login_url, json=payload, timeout=15)
    
    if resp.status_code != 200:
        print(f"[!] Login failed (HTTP {resp.status_code}): {resp.text}")
        sys.exit(1)

    data = resp.json()
    token = data.get("accessToken") or data.get("token") or data.get("jwt")
    login_type = data.get("loginType")
    tfa_key = data.get("tfaKey", "")

    # Handle Email Verification Code / 2FA Challenge
    if not token and (login_type in ("verifyCode", "tfa") or tfa_key or data.get("code") == "verifyCode"):
        print("\n📧 Bambu Lab sent a verification code to your email (or 2FA app).")
        vcode = input("Enter 6-digit verification code: ").strip()
        
        # Try verify with code + tfaKey or code + account
        v_payload = {
            "account": account,
            "code": vcode,
        }
        if tfa_key:
            v_payload["tfaKey"] = tfa_key
        if password:
            v_payload["password"] = password

        print("[*] Submitting verification code...")
        v_resp = session.post(login_url, json=v_payload, timeout=15)
        
        if v_resp.status_code != 200:
            print(f"[!] Verification failed (HTTP {v_resp.status_code}): {v_resp.text}")
            sys.exit(1)

        data = v_resp.json()
        token = data.get("accessToken") or data.get("token") or data.get("jwt")

    if not token:
        print(f"[!] Could not extract access token from response: {data}")
        sys.exit(1)

    print("\n[+] Successfully logged in to Bambu Cloud!")
    
    # Fetch user profile for user_id
    user_id = data.get("userId") or data.get("uid") or ""
    if not user_id:
        try:
            p_resp = session.get(
                f"{API_BASE}/v1/user-service/my/profile",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if p_resp.status_code == 200:
                user_id = p_resp.json().get("uid") or p_resp.json().get("id", "")
        except Exception:
            pass

    # Fetch bound devices (printers)
    headers = {"Authorization": f"Bearer {token}"}
    bind_resp = session.get(f"{API_BASE}/v1/iot-service/api/user/bind", headers=headers, timeout=15)
    
    devices = []
    if bind_resp.status_code == 200:
        devices = bind_resp.json().get("devices", [])

    auth_data = {
        "account": account,
        "token": token,
        "user_id": str(user_id),
        "devices": devices,
    }

    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(auth_data, f, indent=2)

    print(f"[+] Saved credentials to {AUTH_FILE}")
    print(f"[+] Found {len(devices)} bound printer(s):")
    for d in devices:
        print(f"    - {d.get('name')} | Model: {d.get('dev_model_name')} | SN: {d.get('dev_id')} | Online: {d.get('online')}")

    return auth_data


def load_credentials() -> dict:
    """Load cached credentials or prompt for login if missing."""
    if os.path.isfile(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("token"):
                    return data
        except Exception:
            pass
    return login()


if __name__ == "__main__":
    load_credentials()
