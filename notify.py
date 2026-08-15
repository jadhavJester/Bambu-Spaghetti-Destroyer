#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bao chuong ve dien thoai khi may in XONG / LOI — ntfy + Telegram + Discord.

Cau hinh trong file .env (canh bambu_web.py — cung cho voi BAMBU_HOST), chi can
dien kenh nao muon dung, bo trong = tat kenh do:

  # ntfy (don gian nhat — cai app ntfy tren iPhone/Android, subscribe topic)
  NTFY_TOPIC=lp-bambu-a1-abc123        # tu dat, cang kho doan cang kin
  NTFY_SERVER=https://ntfy.sh          # mac dinh, tu host duoc thi doi

  # Telegram (tao bot qua @BotFather -> token; chat_id lay qua @userinfobot)
  TELEGRAM_BOT_TOKEN=123456:ABC-xyz
  TELEGRAM_CHAT_ID=123456789

  # Discord (Server Settings > Integrations > Webhooks > New Webhook > Copy URL)
  DISCORD_WEBHOOK=https://discord.com/api/webhooks/...

Gui "fire-and-forget" trong thread rieng — mat mang cung KHONG lam hub cham/treo.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

import printer_config


def _env() -> dict:
    """Doc .env moi lan goi (sua .env khong can restart hub) + os.environ de len."""
    data = {}
    try:
        data = printer_config._parse_dotenv(printer_config.env_path())  # noqa: SLF001
    except Exception:                                     # noqa: BLE001
        pass
    for k in ("NTFY_TOPIC", "NTFY_SERVER", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK", "HUB_URL"):
        if os.environ.get(k):
            data[k] = os.environ[k]
    return data


def hub_url() -> str:
    """Link dashboard (Tailscale) — dinh kem vao tin bao loi de user mo camera ngay."""
    return (_env().get("HUB_URL") or "https://administrator.tail2d2fb4.ts.net/").strip()


def channels() -> list[str]:
    """Kenh dang bat (de hien tren UI cho user biet da cau hinh chua)."""
    e = _env()
    out = []
    if e.get("NTFY_TOPIC"):
        out.append("ntfy")
    if e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID") and _on("TELEGRAM"):
        out.append("telegram")
    if e.get("DISCORD_WEBHOOK"):
        out.append("discord")
    if e.get("SLACK_BOT_TOKEN") and _slack_target() and _on("SLACK"):
        out.append("slack")
    return out


def _on(name: str) -> bool:
    """Bat/tat 1 kenh qua .env ENABLE_<NAME> (mac dinh '1'=bat; '0'=tat) — cho phep
    'tam dung Telegram de test Slack' ma KHONG xoa token."""
    return (_env().get(f"ENABLE_{name}") or "1").strip() != "0"


def _slack_target() -> str:
    """Kenh Slack de day CANH BAO: slack_target.txt (kenh DM user nhan gan nhat, do
    slack_bot ghi) > SLACK_NOTIFY_CHANNEL (.env). Rong = user chua nhan bot lan nao."""
    try:
        c = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "slack_target.txt"), encoding="utf-8").read().strip()
        if c:
            return c
    except OSError:
        pass
    return (_env().get("SLACK_NOTIFY_CHANNEL") or "").strip()


def _slack_text(title: str, body: str) -> None:
    """Gui text vao Slack qua chat.postMessage (urllib). BAY: Slack tra HTTP 200 kem
    {ok:false, error} khi loi (bot bi kick / kenh sai) -> phai kiem `ok`, khong thi
    log bao 'slack' GIA. 1 request, khong retry (tranh dang trung tin)."""
    e = _env()
    tok, chat = e.get("SLACK_BOT_TOKEN"), _slack_target()
    if not (tok and chat):
        return
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": chat, "text": f"{title}\n{body}"[:3900]}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {tok}"}, method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if not r.get("ok"):
        raise RuntimeError(f"slack API: {r.get('error')}")   # -> _send_all log 'slack:LOI'


def _slack_photos(jpgs: list, caption: str = "") -> bool:
    """Gui ANH vao Slack (files_upload_v2 — dung slack_sdk da cai san cho don gian;
    upload Slack moi la 3 buoc, urllib se rat dai)."""
    e = _env()
    tok, chat = e.get("SLACK_BOT_TOKEN"), _slack_target()
    jpgs = [j for j in (jpgs or []) if j]
    if not (tok and chat and jpgs) or not _on("SLACK"):
        return False
    try:
        from slack_sdk.web import WebClient
        ups = [{"file": j, "filename": f"cam{i}.jpg"} for i, j in enumerate(jpgs[:10])]
        WebClient(token=tok).files_upload_v2(channel=chat, file_uploads=ups,
                                             initial_comment=caption[:1500])
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _ntfy_photos(jpgs: list, caption: str = "") -> bool:
    """Gui ANH bao cao vao ntfy — dinh kem qua PUT (body = file, `Message` header =
    caption). Header phai latin-1 nen smuggle UTF-8 (nhu _send_all). Loat nhieu frame ->
    chon frame NET NHAT (JPEG lon nhat) de khoi spam nhieu thong bao."""
    e = _env()
    topic = e.get("NTFY_TOPIC")
    jpgs = [j for j in (jpgs or []) if j]
    if not (topic and jpgs):
        return False
    server = (e.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    cap = _plain(caption)[:300]
    try:
        req = urllib.request.Request(
            f"{server}/{topic}", data=max(jpgs, key=len), method="PUT",
            headers={"Filename": "cam.jpg", "Priority": "high",
                     "Title": "Bambu A1".encode("utf-8").decode("latin-1"),
                     "Message": cap.encode("utf-8").decode("latin-1")})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _post(url: str, data: bytes, headers: dict, tries: int = 3) -> None:
    """POST co RETRY — mang VN hay bop/chan api.telegram.org chap chon (do that:
    luc duoc luc 'handshake timed out'), thu lai 2-3 lan la qua duoc phan lon."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
            return
        except Exception as e:                          # noqa: BLE001
            last = e
    raise last if last else RuntimeError("post fail")


def _log(line: str) -> None:
    """Nhat ky gui tin -> notify.log (canh bambu_web) — soi duoc vi sao tin khong den."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "notify.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


_TAG = re.compile(r"<[^>]+>")


def _plain(s: str) -> str:
    """Bo the HTML cho kenh khong hieu HTML (ntfy header / Discord) + tra lai
    ky tu da escape. Telegram thi giu nguyen the."""
    return (_TAG.sub("", s or "").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def _send_all(title: str, body: str, urgent: bool, html: bool = True) -> list[str]:
    """Gui 1 tin di moi kenh. body co the chua the HTML (ui_tg.card) — Telegram
    render, ntfy/Discord tu lược the (user: 'moc tien do phai dep nhu tinh hinh in')."""
    e = _env()
    sent: list[str] = []
    t_plain, b_plain = _plain(title), _plain(body)
    if e.get("NTFY_TOPIC"):
        try:
            _post(f"{e.get('NTFY_SERVER') or 'https://ntfy.sh'}/{e['NTFY_TOPIC']}",
                  b_plain.encode("utf-8"),
                  {"Title": t_plain.encode("utf-8").decode("latin-1"),  # header latin-1
                   "Priority": "urgent" if urgent else "high",
                   "Tags": "rotating_light" if urgent else "white_check_mark"})
            sent.append("ntfy")
        except Exception as ex:                            # noqa: BLE001
            sent.append(f"ntfy:LOI {ex}")
    if e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID") and _on("TELEGRAM"):
        for attempt in (True, False):        # HTML loi (the la) -> gui lai dang tho
            use_html = html and attempt
            p = {"chat_id": e["TELEGRAM_CHAT_ID"],
                 "text": (f"{title}\n{body}" if use_html
                          else f"{'🚨' if urgent else '✅'} {t_plain}\n{b_plain}")}
            if use_html:
                p["parse_mode"] = "HTML"
            try:
                _post(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      json.dumps(p).encode(), {"Content-Type": "application/json"})
                sent.append("telegram")
                break
            except Exception as ex:                        # noqa: BLE001
                if not use_html:                           # ca 2 lan deu hong
                    sent.append(f"telegram:LOI {ex}")
    if e.get("DISCORD_WEBHOOK"):
        try:
            _post(e["DISCORD_WEBHOOK"],
                  json.dumps({"content": f"{'🚨' if urgent else '✅'} **{t_plain}**\n"
                                         f"{b_plain}"}).encode(),
                  {"Content-Type": "application/json"})
            sent.append("discord")
        except Exception as ex:                            # noqa: BLE001
            sent.append(f"discord:LOI {ex}")
    if _on("SLACK") and e.get("SLACK_BOT_TOKEN") and _slack_target():
        try:
            _slack_text(f"{'🚨' if urgent else '✅'} {t_plain}", b_plain)
            sent.append("slack")
        except Exception as ex:                            # noqa: BLE001
            sent.append(f"slack:LOI {ex}")
    _log(f"[{t_plain}] -> {', '.join(sent) or 'KHONG CO KENH'}")
    return sent


def send(title: str, body: str, urgent: bool = False) -> None:
    """Gui khong chan (thread nen) — goi tu MQTT handler an toan."""
    threading.Thread(target=_send_all, args=(title, body, urgent), daemon=True).start()


def send_sync(title: str, body: str, urgent: bool = False) -> list[str]:
    """Gui dong bo — cho /api/notify-test tra ket qua tung kenh."""
    return _send_all(title, body, urgent)


def alarm(title: str, body: str, times: int = 10, gap_s: float = 3.0) -> None:
    """BAO DONG DON DAP — gui lien tiep `times` tin cach nhau vai giay de danh thuc
    (user chot 2026-07-16: loi la spam 10 tin nhu bao dong). Chay thread nen."""
    def _run():
        for i in range(times):
            _send_all(f"{title} ({i + 1}/{times})", body, urgent=True)
            time.sleep(gap_s)
    threading.Thread(target=_run, daemon=True).start()


def call_twilio(text: str) -> bool:
    """GOI DIEN THAT qua Twilio Voice (doc canh bao bang giong noi vi-VN, lap 2 lan).
    Auth = API Key (SK.../secret) + Account SID trong URL. Can DU 5 gia tri:
    TWILIO_ACCOUNT_SID + TWILIO_API_KEY_SID + _SECRET + TWILIO_FROM (so Twilio) +
    ALERT_PHONE (so nhan +84...). Thieu 1 -> bo qua (return False). 1 request, KHONG
    retry (tranh goi trung)."""
    import base64
    import urllib.parse
    e = _env()
    acc, ks = e.get("TWILIO_ACCOUNT_SID"), e.get("TWILIO_API_KEY_SID")
    sec, frm = e.get("TWILIO_API_KEY_SECRET"), e.get("TWILIO_FROM")
    to = e.get("ALERT_PHONE")
    if not all([acc, ks, sec, frm, to]):
        return False
    # Trial CHAN `Twiml` noi tuyen -> dung `Url` tro toi TwiML Bin (TWILIO_TWIML_URL);
    # chua co thi dung URL demo cua Twilio (doc tieng Anh) de it nhat DIEN THOAI CO REO.
    url = e.get("TWILIO_TWIML_URL") or "http://demo.twilio.com/docs/voice.xml"
    data = urllib.parse.urlencode({"To": to, "From": frm, "Url": url}).encode()
    auth = base64.b64encode(f"{ks}:{sec}".encode()).decode()
    try:
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{acc}/Calls.json",
            data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Basic {auth}"})
        urllib.request.urlopen(req, timeout=20).read()
        _log(f"[twilio] GOI {to} OK")
        return True
    except Exception as ex:                                 # noqa: BLE001
        _log(f"[twilio] LOI goi {to}: {type(ex).__name__}: {str(ex)[:200]}")
        return False


def call_alert(text: str) -> None:
    """Goi dien canh bao (thread nen) — chi khi ENABLE_CALL!=0 + du cau hinh Twilio."""
    if not _on("CALL"):
        return
    threading.Thread(target=call_twilio, args=(text,), daemon=True).start()


def send_photos_telegram(jpgs: list, caption: str = "") -> bool:
    """FAN-OUT anh (milestone) sang MOI kenh dang bat: Telegram + Slack. TEN giu nguyen
    cho cac cho goi cu (bambu_web milestone line 134/1008/1027, telegram_bot analyze)."""
    ok = _tg_photos(jpgs, caption) if _on("TELEGRAM") else False
    _slack_photos(jpgs, caption)                              # Slack tu bo qua neu tat
    _ntfy_photos(jpgs, caption)                               # ntfy: anh + caption theo muc
    return ok


def send_photo_telegram(jpg: bytes, caption: str = "") -> bool:
    ok = _tg_photo(jpg, caption) if _on("TELEGRAM") else False
    _slack_photos([jpg], caption)
    _ntfy_photos([jpg], caption)
    return ok


def _tg_photos(jpgs: list, caption: str = "") -> bool:
    """Album Telegram (sendMediaGroup) — user thay DUNG cac frame AI da nhin, tu doi
    chieu (user hoi 2026-07-17). Caption vao anh dau. Loi -> fallback 1 anh net nhat."""
    e = _env()
    tok, chat = e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_CHAT_ID")
    jpgs = [j for j in (jpgs or []) if j]
    if not (tok and chat and jpgs):
        return False
    if len(jpgs) == 1:
        return _tg_photo(jpgs[0], caption)
    import uuid
    b = uuid.uuid4().hex
    media, parts = [], []
    for i, jpg in enumerate(jpgs[:10]):          # Telegram cho toi da 10 anh/album
        name = f"f{i}"
        m = {"type": "photo", "media": f"attach://{name}"}
        if i == 0 and caption:
            m["caption"] = caption[:1024]
        media.append(m)
        parts.append((name, jpg))
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"media\"\r\n\r\n"
            f"{json.dumps(media)}\r\n").encode("utf-8")
    for name, jpg in parts:
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                 f"filename=\"{name}.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
        body += jpg + b"\r\n"
    body += f"--{b}--\r\n".encode()
    try:
        _post(f"https://api.telegram.org/bot{tok}/sendMediaGroup", body,
              {"Content-Type": f"multipart/form-data; boundary={b}"})
        return True
    except Exception:                                   # noqa: BLE001
        return _tg_photo(max(jpgs, key=len), caption)


def _tg_photo(jpg: bytes, caption: str = "") -> bool:
    """1 anh Telegram (sendPhoto) — best-effort, loi thi thoi."""
    e = _env()
    tok, chat = e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_CHAT_ID")
    if not (tok and chat and jpg):
        return False
    import uuid
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"photo\"; "
            f"filename=\"cam.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode("utf-8")
    body += jpg + f"\r\n--{b}--\r\n".encode("utf-8")
    try:
        _post(f"https://api.telegram.org/bot{tok}/sendPhoto", body,
              {"Content-Type": f"multipart/form-data; boundary={b}"})
        return True
    except Exception:                                   # noqa: BLE001
        return False