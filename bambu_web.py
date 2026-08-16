#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bambu_web.py — Web dashboard + BANG DIEU KHIEN may in Bambu A1 qua LAN.
Chay tren PC, dien thoai/PC mo trinh duyet qua LAN. NGUOI DUNG bam nut dieu khien;
AI/Claude KHONG dinh vao (server chi gui lenh khi co POST tu trinh duyet).

Tinh nang:
  - Theo doi realtime (stage/%/lop/con-time/nozzle/bed/AMS/wifi) — tu refresh 2s.
  - Anh may in dong mo phong tien do in (scan-line dang len theo %).
  - Nut: Tam dung / Tiep tuc / DUNG (co xac nhan) — bam tu trinh duyet.
  - AMS Lite dung layout that (khe 1 4 / 2 3) + quan ly gam nhua con lai (sua tay,
    luu theo tag_uid RFID qua filament_store).
  - CANH BAO khi may loi / dung dot ngot (print_error, hms, FAILED, mat ket noi).

Dung:
  python bambu_web.py                 -> doc cau hinh tu .env / printer.local.json, cong 8787
  python bambu_web.py 8080
Yeu cau: pip install --user paho-mqtt ; may bat LAN Only. Access Code lay qua /bambu-check.
"""
import sys, os, re, ssl, json, time, threading, shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import paho.mqtt.client as mqtt

import printer_config
import filament_store
import filament_ftp
import slicer_cli
import analyzer
import optimize_e2e
import camera_stream
import notify
import ai_chat
import agent
import slack_bot
import telegram_bot
import ui_tg

# Theo doi moc tien do + ma loi cua BAN IN hien tai (bao 30/50/75%, loi hien ro —
# user chot 2026-07-16). vchecked: cac moc DA soi AI vision. Reset khi doi file.
MILE = {"file": None, "sent": set(), "err": 0, "vchecked": set()}

# Moc TU SOI CAMERA bang AI vision (user chot lan 2, 2026-07-17: chi 70 va 90 —
# vung loi vat cao lo mat ma khong soi qua day). ON -> gui anh + ket luan;
# NGHI NGO/HONG -> them tin khan + link.
VISION_CHECK_PCTS = (70, 90)


VISION_PROMPT = (
    "Đây là LOẠT ẢNH camera bàn in chụp cách nhau ~4 GIÂY. Máy A1 là bed-slinger — "
    "BÀN DI CHUYỂN liên tục nên trong 1 ảnh khối in có thể TRÔNG NGHIÊNG do góc "
    "chụp lúc bàn đang chạy xa tâm (false positive thật đã gặp 2026-07-17; không "
    "đọc được toạ độ bàn nên bù bằng chụp loạt — luôn có ảnh lúc bàn gần tâm). "
    "QUY TẮC: chỉ kết luận LỆCH TRỤC khi độ nghiêng giống nhau Ở MỌI ẢNH; nghiêng "
    "chỉ 1 ảnh = bàn đang chạy, KHÔNG phải lỗi. Ảnh NHÒE/MÉO kiểu bị kéo dài, mép "
    "cong như tan chảy = MOTION BLUR/rolling shutter do bàn phóng nhanh lúc chụp — "
    "là artifact máy ảnh, KHÔNG phải nhựa chảy; đánh giá dựa trên ảnh NÉT nhất. "
    "Soi các lỗi: spaghetti (nhựa rối), "
    "nhựa RỦ/chảy xệ, LỆCH TRỤC, XƠ/kéo sợi, cong vênh mép. DÒNG ĐẦU trả đúng 1 "
    "trong 3: 'KQ: ON' / 'KQ: NGHI NGO' / 'KQ: HONG'. Chỉ NGHI NGO/HONG khi THẤY "
    "RÕ lỗi nhất quán và nêu được nó; không thì PHẢI 'KQ: ON' — kết luận khớp lý "
    "do. Sau đó 1-3 dòng lý do ngắn.")


def _burst_frames(n: int = 6, gap_s: float = 2.5) -> list[bytes]:
    """Loat n frame cach nhau gap_s giay — ban bed-slinger dao dong qua lai nen
    trong loat luon co frame luc ban gan TAM camera; nghieng that = nhat quan moi
    frame. (Khong doc duoc toa do ban qua MQTT khi dang in -> bu bang thong ke.)

    CHONG ANH MEO (user bat tai moc 75% that: frame dinh luc ban phong nhanh ->
    motion blur nhu nhua chay): anh MO nen JPEG NHO hon han -> dung size byte lam
    thuoc do do net.

    VI SAO KHONG CHUP LUC BAN DUNG YEN (user hoi 2026-07-17): ban A1 KHONG BAO GIO
    dung yen khi in, va MQTT A1 KHONG phat toa do Y realtime -> hub khong the canh
    dung luc ban ve tam de bam may. Bu bang THONG KE: chup DU frame (6 x 2.5s), giu
    3 frame NET NHAT (JPEG lon nhat = luc ban cham/dao chieu = gan 'dung yen' nhat),
    bo cac frame mo do ban phong. Ca 3 frame nay deu duoc gui de user doi chieu.
    """
    # FIX GOC RE anh nhoe: BAT DEN truoc khi chup (in dem den tat -> camera phoi
    # sang dai -> nhoe du ban chay cham). Tra lai trang thai cu sau khi chup.
    was_on = _light_is_on()
    if not was_on:
        cmd_light(True)
        time.sleep(1.5)                      # cho camera can bang sang lai
    out: list[bytes] = []
    try:
        for i in range(n):
            f = camera_stream.get_frame(IP, CODE, wait_s=10 if not out else 6)
            if f and (not out or f != out[-1]):
                out.append(f)
            if i < n - 1:
                time.sleep(gap_s)
    finally:
        if not was_on:
            cmd_light(False)                 # user tat den thi tra lai nhu cu
    if len(out) > 3:                         # giu 3 frame NET NHAT (theo thu tu chup)
        keep = set(id(x) for x in sorted(out, key=len, reverse=True)[:3])
        out = [x for x in out if id(x) in keep]
    return out


def _vision_check(pct: int, fn: str) -> None:
    """Chup loat frame -> AI vision soi loi (spaghetti/ru/lech/xo/venh) -> bao neu xau."""
    frames = _burst_frames()
    if not frames:
        notify._log(f"[vision {pct}%] khong lay duoc frame")   # noqa: SLF001
        return
    jpg = max(frames, key=len)              # anh gui di = frame NET nhat
    # Hinh hoc A1 cho AI: camera CO DINH tren khung, ban chay truc Y (gay nghieng
    # phoi canh), gian nang truc Z theo lop — Z suy duoc tu layer_num (MQTT khong
    # phat toa do Y nen khong sync tam ban duoc; muon frame chuan tung lop thi bat
    # Timelapse truyen thong cua may — ban ve vi tri park moi lop).
    with LOCK:
        _d = dict(STATE["data"])
    geo = (f"Hình học: camera cố định trên khung máy, nhìn thấp lên bàn; bàn chạy "
           f"tới-lui trục Y liên tục. Khối in tới lớp {_d.get('layer_num', '?')}/"
           f"{_d.get('total_layer_num', '?')} — càng cao thì đỉnh khối càng gần mép "
           f"trên khung hình.")
    a = ai_chat.ask_vision(VISION_PROMPT, frames, context=_status_text() + "\n" + geo)
    verdict = (a or "").strip().upper()[:60]
    notify._log(f"[vision {pct}%] {verdict[:40] or 'AI KHONG PHAN HOI'}")  # noqa: SLF001
    if not a:
        return
    # LUON kem ANH ket qua soi (user chot 2026-07-17: 'phan tich vision phai kem
    # anh, on cung kem hinh') — anh nguyen do phan giai camera, caption = ket luan.
    # Gui CA LOAT anh AI da nhin (album) — user tu doi chieu, khong phai tin loi
    # suong (user hoi 2026-07-17: 'AI phan tich anh nao?').
    ic, lab = ui_tg.verdict_of(a)
    notify.send_photos_telegram(frames, caption=f"{ic} AI soi {pct}% — {lab}\n"
                                                f"{fn}\n{a[:800]}")
    if "NGHI NGO" in verdict or "HONG" in verdict:
        bad = "HỎNG" if "HONG" in verdict and "NGHI" not in verdict else "NGHI NGỜ"
        notify.send(f"Bambu A1: AI soi camera {pct}% — {bad} ⚠️",
                    f"{fn}\n{a[:500]}\nMỞ CAMERA: {notify.hub_url()}", urgent=True)

HERE = os.path.dirname(os.path.abspath(__file__))
PRINTER_NAME = "LongPham A1-3"

# Cache job dang in: gam + anh model + toan bo thong so (tai 1 lan qua FTP khi doi file)
JOB = {"file": None, "weight": None, "thumb": None, "info": None, "fetching": False, "subtracted": set()}
JOB_LOCK = threading.Lock()

# Tu dien lenh G-code (Marlin + rieng Bambu) -> giai thich tieng Viet
GCODE_DICT = {
    "G0": ["Di chuyển nhanh", "Đưa đầu phun tới vị trí (KHÔNG đùn nhựa) — di chuyển không in."],
    "G1": ["Di chuyển + in", "Di chuyển có kèm đùn nhựa (E) — đây là lệnh vẽ ra vật thể."],
    "G2": ["Cung tròn thuận", "Nội suy cung tròn theo chiều kim đồng hồ."],
    "G3": ["Cung tròn nghịch", "Nội suy cung tròn ngược chiều kim đồng hồ."],
    "G4": ["Dừng chờ", "Tạm dừng một khoảng thời gian (dwell)."],
    "G28": ["Về gốc (Home)", "Đưa các trục về vị trí gốc bằng công tắc/cảm biến."],
    "G29": ["Cân chỉnh bàn", "Auto bed leveling — quét lưới độ cao bàn để bù vênh."],
    "G90": ["Toạ độ tuyệt đối", "Mọi toạ độ tính từ gốc máy."],
    "G91": ["Toạ độ tương đối", "Toạ độ tính từ vị trí hiện tại."],
    "G92": ["Đặt lại toạ độ", "Gán giá trị vị trí hiện tại (thường reset E về 0)."],
    "M17": ["Bật động cơ", "Cấp điện giữ các động cơ bước."],
    "M18": ["Tắt động cơ", "Ngắt giữ động cơ (có thể xoay tay)."],
    "M82": ["Đùn tuyệt đối", "Trục E tính theo giá trị tuyệt đối."],
    "M83": ["Đùn tương đối", "Trục E tính theo lượng thêm mỗi đoạn (Bambu dùng cái này)."],
    "M84": ["Tắt giữ động cơ", "Nhả động cơ khi rảnh."],
    "M104": ["Đặt nhiệt nozzle", "Set nhiệt độ đầu phun, KHÔNG chờ đạt."],
    "M109": ["Nhiệt nozzle + chờ", "Set nhiệt đầu phun và CHỜ tới khi đạt."],
    "M106": ["Bật quạt", "Bật quạt làm mát vật in, chỉnh tốc độ S0-255."],
    "M107": ["Tắt quạt", "Tắt quạt làm mát."],
    "M140": ["Đặt nhiệt bàn", "Set nhiệt độ bàn nhiệt, không chờ."],
    "M190": ["Nhiệt bàn + chờ", "Set nhiệt bàn và CHỜ tới khi đạt."],
    "M204": ["Gia tốc", "Đặt gia tốc in/di chuyển (mm/s²)."],
    "M205": ["Jerk/độ giật", "Giới hạn thay đổi vận tốc đột ngột."],
    "M220": ["Tốc độ %", "Override tốc độ in tổng thể theo %."],
    "M221": ["Lưu lượng %", "Override lượng đùn (flow) theo %."],
    "M400": ["Chờ hết chuyển động", "Đợi buffer chuyển động chạy xong."],
    "M73": ["Tiến độ in", "Báo % hoàn thành + thời gian còn lại lên màn hình."],
    "M900": ["Pressure Advance", "Bù áp suất đùn để cạnh sắc nét, giảm phình góc."],
    "M620": ["AMS nạp nhựa", "Lệnh riêng Bambu — chọn/nạp cuộn từ AMS."],
    "M621": ["AMS nhả nhựa", "Lệnh riêng Bambu — rút nhựa khỏi đầu phun."],
    "M622": ["Điều kiện (Bambu)", "Rẽ nhánh có điều kiện trong macro Bambu."],
    "M623": ["Kết thúc điều kiện", "Đóng khối điều kiện macro Bambu."],
    "M991": ["Macro hệ thống Bambu", "Lệnh nội bộ điều phối in của firmware Bambu."],
    "M1002": ["Macro hệ thống Bambu", "Lệnh nội bộ (kiểm tra/hiệu chỉnh) của Bambu."],
    "T0": ["Chọn đầu/khe 0", "Chuyển sang tool/khe nhựa 0."],
    "T1": ["Chọn đầu/khe 1", "Chuyển sang tool/khe nhựa 1."],
}

# Layout vat ly AMS Lite: id MQTT 0..3 -> so khe 1..4, sap xep tren-duoi:
#   khe 1 (id0)  khe 4 (id3)
#   khe 2 (id1)  khe 3 (id2)
SLOT_LABEL = {0: 1, 1: 2, 2: 3, 3: 4}


def load_cfg(argv):
    port = 8787
    rest = argv[:]
    if rest and rest[0].isdigit():
        port = int(rest.pop(0))
    host, serial, code = printer_config.load(rest)
    return port, host, serial, code


PORT, IP, SERIAL, CODE = load_cfg(sys.argv[1:])
REPORT = f"device/{SERIAL}/report"
REQUEST = f"device/{SERIAL}/request"

STATE = {"data": {}, "ts": 0, "connected": False, "rc": None}
LOCK = threading.Lock()
MQTT = {"client": None, "seq": 0}

# Anh may in + AMS Lite (serve truc tiep, mo phong)
def _load_img(name):
    try:
        with open(os.path.join(HERE, name), "rb") as f:
            return f.read()
    except OSError:
        return b""


A1_IMG = _load_img("BAMBULAB A1.jpg")
AMS_IMG = _load_img("AMS.jpg")


# ---------- MQTT ----------
def _send(payload):
    c = MQTT["client"]
    if not c:
        return False, "chua ket noi MQTT"
    MQTT["seq"] += 1
    try:
        c.publish(REQUEST, json.dumps(payload))
        return True, "ok"
    except Exception as e:
        return False, str(e)


def cmd_print(command):
    return _send({"print": {"sequence_id": str(MQTT["seq"]), "command": command, "param": ""}})


def cmd_light(on: bool = True, node: str = "chamber_light"):
    """Bat/tat den may in (MQTT ledctrl — spec OpenBambuAPI, da doi chieu 2026-07-17).

    VI SAO CAN: camera A1 phoi sang theo anh sang phong. In DEM den tat -> exposure
    dai -> frame NHOE/meo khi ban chay (user bat: anh 65/75% toi va meo, con anh bam
    tay luc phong sang thi net cang du may VAN DANG IN). Bat den truoc khi chup la
    fix DUNG GOC RE, khong phai chup nhieu frame roi loc.
    """
    return _send({"system": {"sequence_id": str(MQTT["seq"]), "command": "ledctrl",
                             "led_node": node, "led_mode": "on" if on else "off",
                             "led_on_time": 500, "led_off_time": 500,
                             "loop_times": 1, "interval_time": 1000}})


def _light_is_on() -> bool:
    with LOCK:
        for l in (STATE["data"].get("lights_report") or []):
            if l.get("node") == "chamber_light":
                return str(l.get("mode")) == "on"
    return False


def cmd_project_file(name, path):
    """Ra lenh in 1 file .gcode.3mf da co san tren may (NGUOI DUNG bam)."""
    p = (path or ("/" + name)).lstrip("/")
    payload = {"print": {
        "sequence_id": str(MQTT["seq"]),
        "command": "project_file",
        "param": "Metadata/plate_1.gcode",
        "subtask_name": name.replace(".gcode.3mf", "").replace(".3mf", ""),
        "url": "file:///sdcard/" + p,
        "bed_type": "auto",
        "timelapse": False, "bed_leveling": True, "flow_cali": False,
        "vibration_cali": True, "layer_inspect": False, "use_ams": False,
        "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0",
    }}
    return _send(payload)


FILES_CACHE = {"ts": 0, "data": []}
THUMB_LOCK = threading.Lock()  # tai thumbnail tuan tu (Bambu FTP gioi han ket noi)

# Slice tren may tinh (Bambu Studio CLI) khi user upload file CHUA slice.
# Chi 1 job mot luc — CLI ngon RAM/CPU nhu mo ca app.
SLICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slice_jobs")
UPJOB = {"state": "idle", "name": None, "msg": "", "stats": None}
UPJOB_LOCK = threading.Lock()
LAST_SLICED = {"path": None, "name": None}   # file .gcode.3mf slice gan nhat -> cho tai ve
OPTJOB = {"state": "idle", "name": None, "msg": "", "report": None}
OPTJOB_LOCK = threading.Lock()
ANJOB = {"state": "idle", "name": None, "msg": "", "result": None}
ANJOB_LOCK = threading.Lock()


def _ams_filament_presets():
    """Sinh preset filament tu 4 khe AMS THAT (MQTT) — mau + loai + nhiet do that."""
    with LOCK:
        ams = (STATE["data"].get("ams") or {})
    out = []
    for u in ams.get("ams", []):
        for t in (u.get("tray") or []):
            sub = (t.get("tray_sub_brands") or t.get("tray_type") or "").strip()
            color = (t.get("tray_color") or "")[:6]
            if not sub or not color:
                continue
            slot = int(t.get("id", 0)) + 1
            preset = {
                "type": "filament",
                "from": "User",
                "inherits": f"Bambu {sub} @BBL A1",
                "name": f"{sub} #{color} (AMS khe {slot})",
                "filament_settings_id": [f"{sub} #{color} (AMS khe {slot})"],
                "filament_colour": [f"#{color}"],
                "version": "2.7.0.8",
            }
            if t.get("nozzle_temp_max"):
                preset["nozzle_temperature"] = [str(t["nozzle_temp_max"])]
            out.append({"slot": slot, "sub": sub, "color": f"#{color}", "preset": preset})
    return out


def _color_name(hexcol: str) -> str:
    """'#000000' -> 'Black' — ten mau ngan cho TEN PRESET (mau quyet dinh bo so:
    Matte den 230/12 khac Matte trang). Bang mau co ban, gan dung theo RGB."""
    h = (hexcol or "").lstrip("#")[:6]
    try:
        rv, gv, bv = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return ""
    mx, mn = max(rv, gv, bv), min(rv, gv, bv)
    if mx < 60:
        return "Black"
    if mn > 200:
        return "White"
    if mx - mn < 30:
        return "Gray"
    if rv >= gv and rv >= bv:
        if bv < 100 and gv > 170:
            return "Yellow"
        if bv < 100 and gv > 60:
            return "Orange"
        return "Pink" if bv > 140 else "Red"
    if gv >= rv and gv >= bv:
        return "Cyan" if bv > 160 else "Green"
    return "Purple" if rv > 120 else "Blue"


def _ams_first_color():
    """Mau hex cua khay AMS dau tien co nhua — de render preview dung mau that."""
    for f in _ams_filament_presets():
        return f["color"]
    return None


def _ams_tray_types():
    """Loai nhua THAT dang nam trong 4 khay AMS Lite (MQTT cache) — cung nguon voi
    panel AMS tren dashboard. Tra ['PLA LITE','PLA MATTE',...] theo khe 1-4;
    tra [] neu chua ket noi may (analyzer se fallback theo khai bao trong file)."""
    with LOCK:
        ams = (STATE["data"].get("ams") or {})
    out = []
    for u in ams.get("ams", []):
        for t in (u.get("tray") or []):
            typ = (t.get("tray_sub_brands") or t.get("tray_type") or "").strip()
            if typ:
                out.append(typ.upper())
    return out


def _ams_tray_colors():
    """Mau hex tung khay — KHOP DUNG thu tu voi _ams_tray_types (cung dieu kien loc)."""
    with LOCK:
        ams = (STATE["data"].get("ams") or {})
    out = []
    for u in ams.get("ams", []):
        for t in (u.get("tray") or []):
            typ = (t.get("tray_sub_brands") or t.get("tray_type") or "").strip()
            if typ:
                c = (t.get("tray_color") or "")[:6]
                out.append(f"#{c}" if c else "")
    return out


def _peek_file_fil(src_path):
    """Doc NHANH filament_type + filament_colour khai bao trong .3mf (khong phan tich
    full mesh) -> de mac dinh chon khe AMS khop nhat. Tra (types, colours)."""
    try:
        import zipfile
        with zipfile.ZipFile(src_path) as z:
            for n in z.namelist():
                if n.lower().endswith("project_settings.config"):
                    cfg = json.loads(z.read(n).decode("utf-8", "ignore"))
                    return (cfg.get("filament_type") or [], cfg.get("filament_colour") or [])
    except (OSError, ValueError, KeyError):
        pass
    return [], []


def _hex_dist(a, b):
    """Khoang cach mau RGB (Euclid) — de tim khe AMS gan mau file nhat. Sai -> vo cung."""
    def rgb(h):
        h = (h or "").lstrip("#")[:6]
        try:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except (ValueError, IndexError):
            return None
    ra, rb = rgb(a), rgb(b)
    if not ra or not rb:
        return 1e9
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5


def _resolve_fil(src_path, sel, plate=None):
    """Nhua NGUOI DUNG chon (Q1 2026-07-19) -> (fil_sel, color_sel, pick_value).

    Uu tien: khe AMS cu the (slot:N) > nhua generic (fil=KEY) > MAC DINH tu AMS that
    khop MAU THEO KHAY dang chon (khay 2 toan vat den -> khe den, KHONG phai filament
    #1 toan file — bug user 2026-07-19), khong khop thi khe 1. Khong sync AMS + khong
    chon -> (None, None, None): giu hanh vi cu (process theo khai bao trong file).
    """
    fils = _ams_filament_presets()
    sel = sel or {}
    if sel.get("slot"):
        t = next((x for x in fils if x["slot"] == sel["slot"]), None)
        if t:
            return t["sub"], t["color"], f"slot:{t['slot']}"
    if sel.get("fil"):
        return sel["fil"], (sel.get("color") or ""), sel["fil"]
    if fils:
        pcol, _ = analyzer.plate_fil(src_path, plate)   # mau CHINH cua KHAY dang chon
        if not pcol:                                     # fallback: filament dau file
            _, fcols = _peek_file_fil(src_path)
            pcol = (fcols or [None])[0]
        best = min(fils, key=lambda t: _hex_dist(pcol, t.get("color"))) if pcol else fils[0]
        return best["sub"], best["color"], f"slot:{best['slot']}"
    return None, None, None


def _apply_overrides(preset: dict, ov: dict) -> None:
    """Ap chinh sua NGUOI DUNG truoc khi in — panel kieu Bambu Prepare (2026-07-19):
    Layer / Bed adhesion / Support. Chi ghi field user THAT SU doi (khac rong) -> giu
    quyet dinh analyzer cho phan con lai. Cac key deu trong SAFE_KEYS nen apply_preset ghi."""
    if not ov:
        return
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    # ---- Layer ----
    lh = _num(ov.get("layer"))
    if lh:
        preset["layer_height"] = str(lh)
    il = _num(ov.get("init_layer"))
    if il:
        preset["initial_layer_print_height"] = str(il)
    # ---- Bed adhesion (brim) — KHONG dung brim_object_gap (ghi la CLI crash) ----
    if ov.get("brim"):
        preset["brim_type"] = ov["brim"]              # no_brim/outer_only/outer_and_inner/auto_brim
    bw = _num(ov.get("brim_w"))
    if bw is not None:
        preset["brim_width"] = str(bw)
    sk = _num(ov.get("skirt"))
    if sk is not None:
        preset["skirt_loops"] = str(int(sk))
    # ---- Speed ----
    v = _num(ov.get("outer"))
    if v:
        preset["outer_wall_speed"] = [str(int(v))]
    # ---- Support ----
    if ov.get("support") in ("0", "1"):
        preset["enable_support"] = ov["support"]      # config dung chuoi '0'/'1'
    if ov.get("sup_type"):
        preset["support_type"] = ov["sup_type"]       # tree(auto)/normal(auto)
    if ov.get("sup_style"):
        preset["support_style"] = ov["sup_style"]     # default/tree_hybrid/tree_strong/snug/grid
    a = _num(ov.get("sup_angle"))
    if a:
        preset["support_threshold_angle"] = str(int(a))
    if ov.get("sup_onplate") in ("0", "1"):
        preset["support_on_build_plate_only"] = ov["sup_onplate"]
    zt = _num(ov.get("sup_ztop"))
    if zt is not None:
        preset["support_top_z_distance"] = f"{zt:g}"
    zb = _num(ov.get("sup_zbot"))
    if zb is not None:
        preset["support_bottom_z_distance"] = f"{zb:g}"
    isp = _num(ov.get("sup_ispacing"))
    if isp is not None:
        preset["support_interface_spacing"] = f"{isp:g}"
    if ov.get("sup_ipattern"):
        preset["support_interface_pattern"] = ov["sup_ipattern"]
    itl = _num(ov.get("sup_itop"))
    if itl is not None:
        preset["support_interface_top_layers"] = str(int(itl))
    ibl = _num(ov.get("sup_ibot"))
    if ibl is not None:
        preset["support_interface_bottom_layers"] = str(int(ibl))
    if ov.get("sup_ifil"):                            # nhua interface (khac vat lieu)
        preset["support_interface_filament"] = str(ov["sup_ifil"])
    if ov.get("sup_basefil") not in (None, ""):       # nhua DE support (0 = theo model)
        preset["support_filament"] = str(ov["sup_basefil"])


def _run_analyze(name, src_path, plate=None, sel=None):
    """Phan tich chay NEN — file lon (300k+ tam giac) mat 30-60s, khong the
    giu request HTTP mo lau vay (Tailscale/trinh duyet cat -> tuong treo).

    plate: khay muon phan tich (file nhieu khay — tab khay goi lai voi so nay).
    sel: nhua NGUOI DUNG chon {slot|fil|color} -> DAN DAT process (#2/#3 2026-07-19)."""
    try:
        _fils = _ams_filament_presets()                  # preset filament tu AMS that
        fil_sel, color_sel, pick = _resolve_fil(src_path, sel, plate)
        res = analyzer.analyze(src_path, ams=_ams_tray_types(),
                               color=_ams_first_color(), ams_colors=_ams_tray_colors(),
                               plate=plate, fil_sel=fil_sel, color_sel=color_sel)
        res["ok"] = True
        res["name"] = name
        res["ams_filaments"] = _fils
        # Combo box nhua (T3): danh sach key xuat duoc preset an toan
        res["fil_options"] = list(analyzer.FIL_EXPORT.keys())
        res["fil_pick"] = pick                           # value de UI preselect dropdown nhua
        # Tab khay (T2): Bambu Studio da render san Metadata/plate_N.png trong .3mf
        # -> boc ra dia (file goc bi xoa sau phan tich), UI lay qua /api/plateimg.
        try:
            import zipfile
            from urllib.parse import quote
            img_dir = os.path.join(SLICE_DIR, "plateimg")
            os.makedirs(img_dir, exist_ok=True)
            # don anh cu >7 ngay — khong phinh dia vo han (review LOW-9)
            now = time.time()
            for old in os.listdir(img_dir):
                fp_old = os.path.join(img_dir, old)
                try:
                    if now - os.path.getmtime(fp_old) > 7 * 86400:
                        os.remove(fp_old)
                except OSError:
                    pass
            with zipfile.ZipFile(src_path) as z:
                znames = set(z.namelist())
                for p in res.get("plates") or []:
                    png = f"Metadata/plate_{p['id']}.png"
                    if png in znames:
                        with open(os.path.join(img_dir,
                                  f"{name}.plate_{p['id']}.png"), "wb") as f:
                            f.write(z.read(png))
                        p["img"] = f"/api/plateimg?name={quote(name)}&plate={p['id']}"
        except Exception:                                # noqa: BLE001
            pass    # anh khay chi la minh hoa — thieu anh van phan tich binh thuong
        with ANJOB_LOCK:
            ANJOB.update(state="done", msg="Xong", result=res)
    except Exception as e:                                # noqa: BLE001
        with ANJOB_LOCK:
            ANJOB.update(state="error", msg=f"Lỗi phân tích: {e}", result=None)
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def _run_optimize(name, src_path, plate=None, sel=None):
    """Slice BASELINE + 3 che do -> bao cao so sanh bang SO THAT. Khong dung may in.

    plate: khay dang chon tren tab (file nhieu khay) — bao cao tinh dung khay do.
    sel: nhua NGUOI DUNG chon -> so sanh dung cung nhua voi phan tich (#3 2026-07-19)."""
    try:
        with OPTJOB_LOCK:
            OPTJOB.update(state="running", name=name,
                          msg="Slice baseline + 3 chế độ (4 lần slice)…", report=None)
        fil_sel, color_sel, _ = _resolve_fil(src_path, sel, plate)
        rep = optimize_e2e.run_modes(src_path, os.path.join(SLICE_DIR, "e2e"),
                                     plate=plate or 1, fil_sel=fil_sel, color_sel=color_sel)
        with OPTJOB_LOCK:
            if rep.get("error"):
                OPTJOB.update(state="error", msg=rep["error"])
            else:
                OPTJOB.update(state="done", msg="Xong", report=rep)
    except Exception as e:                                # noqa: BLE001
        with OPTJOB_LOCK:
            OPTJOB.update(state="error", msg=f"Lỗi: {e}")
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def _slice_and_push(name, src_path, mode=None, push=True, plate=0, sel=None, overrides=None):
    """Chay nen: slice file du an (config A1 that + khay AMS) -> day .gcode.3mf
    xuong may in (push=True) HOAC giu lai cho user TAI VE (push=False).

    push=False: user mo file trong Bambu Studio/Handy de REVIEW roi tu bam in —
    khong tu day xuong may. Toan bo do 1 cu bam upload cua NGUOI DUNG khoi dong.
    plate>0: file nhieu khay — slice + bao so DUNG khay user dang chon tren tab.
    sel: nhua NGUOI DUNG chon -> preset che do theo cuon do + ghi MAU (#2/#3/#4).
    overrides: chinh sua truoc in (layer/toc do/support/brim — #4 2026-07-19).
    """
    base = re.sub(r"\.(3mf|stl)$", "", name, flags=re.I)
    out_name = base + ".gcode.3mf"
    try:
        mesh_info = None
        if name.lower().endswith(".stl"):
            with UPJOB_LOCK:
                UPJOB.update(state="slicing", name=name,
                             msg="Đang phân tích STL + bọc cấu hình A1…", stats=None)
            import stl_to_3mf
            wrapped = src_path + ".3mf"
            mesh_info = stl_to_3mf.wrap(src_path, wrapped)
            src_path = wrapped
        # Nhua + mau NGUOI DUNG chon (dan dat preset che do + ghi mau vao ban in).
        fil_sel, color_sel, _ = _resolve_fil(src_path, sel, plate or None)
        # MAU: chi ghi khi file 1 nhua (da mau -> khong dam len het thanh 1 mau).
        _, _fcols = _peek_file_fil(src_path)
        _single = len([c for c in (_fcols or []) if c]) <= 1
        extra_cfg = ({"filament_colour": [color_sel], "default_filament_colour": [color_sel]}
                     if color_sel and _single else None)
        if mode or overrides or extra_cfg:
            # Ap CHE DO (theo cuon chon) + chinh sua user + mau vao config nhung roi slice
            with UPJOB_LOCK:
                UPJOB.update(state="slicing", name=name,
                             msg="Đang áp cấu hình + slice…", stats=None)
            import optimize_e2e
            preset = {}
            if mode:
                an = analyzer.analyze(src_path, mode, ams=_ams_tray_types(),
                                      fil_sel=fil_sel, color_sel=color_sel)
                preset = dict(an["presets"][mode]["preset"])
            _apply_overrides(preset, overrides or {})
            # Cach lam SUPPORT user chon (2026-07-19) -> ghi de ke ca khi giu support file
            force_cfg = None
            _ss = (overrides or {}).get("sup_strat")
            if _ss:
                _mfam = fil_sel or ((_ams_tray_types() or [""]) + [""])[0]
                _strat = next((s for s in analyzer.support_strategy(_mfam, _ams_tray_types())
                               if s["id"] == _ss), None)
                if _strat:
                    force_cfg = _strat["keys"]
            tuned = src_path + f".{mode or 'edit'}.3mf"
            optimize_e2e.apply_preset(src_path, tuned, preset,
                                      extra_cfg=extra_cfg, force_cfg=force_cfg)
            src_path = tuned
        with UPJOB_LOCK:
            UPJOB.update(state="slicing", name=name, msg="Đang slice trên máy tính…", stats=None)
        ok, res, stats = slicer_cli.slice_3mf(src_path, SLICE_DIR)
        if mesh_info:
            stats = {**(stats or {}), **mesh_info}
        if not ok:
            with UPJOB_LOCK:
                UPJOB.update(state="error", msg=res)
            return
        if not push:
            # CHI SLICE DE TAI VE — giu file .gcode.3mf lai, KHONG day xuong may.
            keep = os.path.join(SLICE_DIR, out_name)
            if os.path.abspath(res) != os.path.abspath(keep):
                shutil.copyfile(res, keep)
            with UPJOB_LOCK:
                LAST_SLICED.update(path=keep, name=out_name)
                UPJOB.update(state="done", name=out_name, download=out_name, stats=stats,
                             msg=f"Đã slice xong: {out_name} — xem thời gian/nhựa rồi Đẩy xuống máy")
            return
        with UPJOB_LOCK:
            UPJOB.update(state="pushing", msg="Slice xong — đang chuyển xuống máy in…", stats=stats)
        with open(res, "rb") as f:
            data = f.read()
        with THUMB_LOCK:
            ok2, msg2 = filament_ftp.upload_file(IP, CODE, data, out_name)
        if ok2:
            FILES_CACHE["ts"] = 0
            keep = os.path.join(SLICE_DIR, out_name)      # giu ban sao de user cung tai duoc
            if os.path.abspath(res) != os.path.abspath(keep):
                try:
                    shutil.copyfile(res, keep)
                except OSError:
                    keep = res
            with UPJOB_LOCK:
                LAST_SLICED.update(path=keep, name=out_name)
                UPJOB.update(state="done", name=out_name, download=out_name,
                             msg=f"Đã slice + chuyển xuống máy: {out_name}")
        else:
            with UPJOB_LOCK:
                UPJOB.update(state="error", msg=f"Slice OK nhưng FTP lỗi: {msg2}")
    except Exception as e:                              # noqa: BLE001 - bao len UI
        with UPJOB_LOCK:
            UPJOB.update(state="error", msg=f"Lỗi slice: {e}")
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def get_files():
    """Danh sach file tren may, cache 25s de khong lam phien FTP."""
    now = time.time()
    with JOB_LOCK:
        if now - FILES_CACHE["ts"] < 25 and FILES_CACHE["data"]:
            return FILES_CACHE["data"]
    try:
        data = filament_ftp.list_files(IP, CODE)
    except Exception as e:
        print("[FTP] loi liet ke file:", e)
        return FILES_CACHE["data"]
    with JOB_LOCK:
        FILES_CACHE["ts"] = now
        FILES_CACHE["data"] = data
    return data


def ensure_file_meta(fpath):
    """Tai 1 file .3mf tren may (1 lan duy nhat) -> (anh PNG | None, da_slice | None).

    Cache ra job_cache/<key>.png + <key>.json nen lan sau tuc thi. Tra sliced=None
    khi khong tai duoc (de UI biet la "chua ro" chu khong phai "khong in duoc").
    """
    key = _cache_key(os.path.basename(fpath))
    png = os.path.join(CACHE_DIR, key + ".png")
    meta = os.path.join(CACHE_DIR, key + ".json")

    def _read_cache():
        thumb = None
        if os.path.isfile(png):
            try:
                with open(png, "rb") as f:
                    thumb = f.read()
            except OSError:
                pass
        if os.path.isfile(meta):
            try:
                with open(meta, encoding="utf-8") as f:
                    return thumb, json.load(f).get("sliced")
            except (OSError, ValueError):
                pass
        return thumb, None

    thumb, sliced = _read_cache()
    if sliced is not None:
        return thumb, sliced

    with THUMB_LOCK:                       # Bambu FTP chi chiu 1 ket noi -> tuan tu
        thumb, sliced = _read_cache()      # luong khac vua tai xong?
        if sliced is not None:
            return thumb, sliced
        try:
            m = filament_ftp.fetch_file_meta(IP, CODE, fpath)
        except Exception as e:             # noqa: BLE001 - chi log, UI van chay
            print("[filemeta] loi:", e)
            return None, None
        if not m:
            return None, None
        thumb, sliced = m.get("thumb"), bool(m.get("sliced"))
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            if thumb:
                with open(png, "wb") as f:
                    f.write(thumb)
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"sliced": sliced}, f)
        except OSError:
            pass
        return thumb, sliced


def is_busy():
    with LOCK:
        gc = STATE["data"].get("gcode_state")
    return gc in ("RUNNING", "PAUSE", "PREPARE")


def on_connect(c, u, f, rc, *a):
    with LOCK:
        STATE["rc"] = rc
        STATE["connected"] = (rc == 0)
    if rc == 0:
        c.subscribe(REPORT)
        c.publish(REQUEST, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))


def on_disconnect(c, u, rc, *a):
    with LOCK:
        STATE["connected"] = False


def _active_tag(data):
    ams = (data.get("ams") or {})
    try:
        active = int(ams.get("tray_now", 255))
    except (TypeError, ValueError):
        return None
    for u in ams.get("ams", []):
        for t in (u.get("tray") or []):
            try:
                if int(t.get("id")) == active:
                    return t.get("tray_uuid") or t.get("tag_uid")
            except (TypeError, ValueError):
                continue
    return None


CACHE_DIR = os.path.join(HERE, "job_cache")


def _cache_key(gcode_file):
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(gcode_file or ""))[:120]


def _load_cache(gcode_file):
    key = _cache_key(gcode_file)
    meta = os.path.join(CACHE_DIR, key + ".json")
    if not key or not os.path.isfile(meta):
        return None
    try:
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    thumb = None
    png = os.path.join(CACHE_DIR, key + ".png")
    if os.path.isfile(png):
        try:
            with open(png, "rb") as f:
                thumb = f.read()
        except OSError:
            pass
    return {"weight": m.get("weight"), "info": m.get("info"), "thumb": thumb}


def _save_cache(gcode_file, res):
    key = _cache_key(gcode_file)
    if not key or not res:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if res.get("thumb"):
            with open(os.path.join(CACHE_DIR, key + ".png"), "wb") as f:
                f.write(res["thumb"])
        with open(os.path.join(CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
            json.dump({"weight": res.get("weight"), "info": res.get("info")}, f, ensure_ascii=False)
    except OSError as e:
        print("[cache] loi ghi:", e)


def maybe_fetch_job(gcode_file):
    """Lay gam + anh + thong so. Uu tien cache dia (tuc thi) -> chi FTP khi chua co."""
    if not gcode_file:
        return
    with JOB_LOCK:
        if JOB["file"] == gcode_file and (JOB["weight"] is not None or JOB["thumb"] is not None):
            return
        if JOB["fetching"]:
            return
    cached = _load_cache(gcode_file)
    if cached and (cached["thumb"] or cached["weight"] is not None):
        with JOB_LOCK:
            JOB["file"] = gcode_file
            JOB["weight"] = cached["weight"]
            JOB["thumb"] = cached["thumb"]
            JOB["info"] = cached.get("info")
        return
    with JOB_LOCK:
        JOB["fetching"] = True
        JOB["file"] = gcode_file
        JOB["weight"] = None
        JOB["thumb"] = None
        JOB["info"] = None

    def worker():
        res = {}
        try:
            res = filament_ftp.fetch_job(IP, CODE, gcode_file)
        except Exception as e:
            print("[FTP] loi tai job:", e)
        with JOB_LOCK:
            JOB["weight"] = res.get("weight")
            JOB["thumb"] = res.get("thumb")
            JOB["info"] = res.get("info")
            JOB["fetching"] = False
        _save_cache(gcode_file, res)
        print(f"[FTP] job '{gcode_file}': {res.get('weight')} g, thumb={'co' if res.get('thumb') else 'khong'}")
    threading.Thread(target=worker, daemon=True).start()


def _on_finish(data):
    """Khi in XONG: tru gam that (job_weight) khoi cuon dang dung, 1 lan/ban in."""
    with JOB_LOCK:
        w = JOB["weight"]
        f = JOB["file"]
        done = f in JOB["subtracted"]
    if not w or not f or done:
        return
    tag = _active_tag(data)
    if tag and filament_store.get(tag):
        filament_store.subtract(tag, w)
        with JOB_LOCK:
            JOB["subtracted"].add(f)
        print(f"[GAM] tru {w} g khoi cuon {tag[:8]} (job xong)")


def on_message(c, u, msg):
    try:
        d = json.loads(msg.payload.decode("utf-8", "ignore"))
    except Exception:
        return
    if "print" in d:
        with LOCK:
            prev = STATE["data"].get("gcode_state")
            STATE["data"].update(d["print"])
            STATE["ts"] = time.time()
            snap = dict(STATE["data"])
        gc = snap.get("gcode_state")
        gf = snap.get("gcode_file")
        if gc in ("RUNNING", "PAUSE") and gf:
            maybe_fetch_job(gf)
        if prev == "RUNNING" and gc == "FINISH":
            _on_finish(snap)
        fn = os.path.basename(str(gf or "")) or "(khong ro file)"
        # MOC TIEN DO 30/50/75 (100% = FINISH ben duoi) — user chot 2026-07-16
        try:
            pct0 = int(snap.get("mc_percent") or 0)
        except (TypeError, ValueError):
            pct0 = 0
        # In lai CUNG file (FINISH/FAILED -> RUNNING): mo lai bo dem tu dau
        if prev in ("FINISH", "FAILED", "IDLE") and gc == "RUNNING" and prev != gc:
            MILE.update(file=gf, sent=set(), err=0, vchecked=set())
        if gf and gf != MILE["file"]:
            # File moi hoac hub vua RESTART giua chung: moc DA QUA danh dau IM LANG —
            # khong thi moi lan restart lai ban lai 'moc 50%' (notify.log ghi 3 tin
            # trung 00:23/00:30/00:35 dem 2026-07-17 do 3 lan restart lien tiep).
            MILE.update(file=gf, err=0,
                        sent={m for m in (30, 50, 75) if pct0 >= m},
                        vchecked={v for v in VISION_CHECK_PCTS if pct0 >= v})
        # BAT DAU IN 1 file -> bao Telegram (user chot 2026-07-19). Chi khi VUA CHUYEN vao
        # RUNNING tu trang thai KHONG-in (idle/xong/loi/chuan bi) o % thap: khong phai hub
        # restart giua chung (prev=None), khong phai resume tu PAUSE, khong spam moi update.
        if (prev in ("IDLE", "FINISH", "FAILED", "PREPARE", "SLICING", "PREPARING")
                and gc == "RUNNING" and gf and pct0 < 8):
            notify.send("🖨 <b>BẮT ĐẦU IN</b>",
                        ui_tg.status_card(snap, True, weight=_job_weight(),
                                          hub=notify.hub_url()))
        if gc == "RUNNING":
            try:
                pct = int(snap.get("mc_percent") or 0)
                rem = int(snap.get("mc_remaining_time") or 0)
            except (TypeError, ValueError):
                pct, rem = 0, 0
            # Nhieu moc thoa cung luc (restart giua chung / nhay %) -> GOM 1 tin moc
            # cao nhat, khoi ban 2-3 tin trung (thay that trong notify.log 00:15).
            # CHI 4 moc 30/50/75/100 (user chot) — tieu de mang DUNG so moc,
            # % thuc te nam trong noi dung. 100% = tin FINISH ben duoi.
            hit = [m for m in (30, 50, 75) if pct >= m and m not in MILE["sent"]]
            if hit:
                MILE["sent"].update(hit)
                w = _job_weight()
                # DUNG CHUNG the voi nut '📊 Tình hình in' (user chot: moc tien do
                # phai dep y het) — 1 ham, khong the lech nhau nua.
                notify.send(f"⏳ <b>MỐC {max(hit)}%</b>",
                            ui_tg.status_card(snap, True, weight=w,
                                              hub=notify.hub_url()))
                # ANH camera moc tien do (thread rieng — camera grab ~8s KHONG nghen MQTT)
                def _mphoto(p=max(hit), f=fn):
                    jpg = camera_stream.get_frame(IP, CODE, wait_s=8)
                    if jpg:
                        notify.send_photo_telegram(jpg, caption=f"⏳ Mốc {p}% — {f}")
                threading.Thread(target=_mphoto, daemon=True).start()
            # AI VISION tu soi camera o vung nguy hiem vat cao (65-90%) — thread
            # rieng (vision 6-30s, khong duoc nghen MQTT); chi 1 moc/lan.
            vhit = [m for m in VISION_CHECK_PCTS
                    if pct >= m and m not in MILE["vchecked"]]
            if vhit:
                MILE["vchecked"].update(vhit)
                threading.Thread(target=_vision_check, args=(max(vhit), fn),
                                 daemon=True).start()
        # LOI PHAI HIEN RO: bao ngay khi xuat hien MA LOI (ke ca chua doi trang thai)
        err = 0
        for k in ("print_error", "mc_print_error_code"):
            try:
                err = err or int(snap.get(k) or 0)
            except (TypeError, ValueError):
                pass
        if err and err != MILE["err"]:
            MILE["err"] = err
            # BAO DONG 10 TIN + ma HEX nhu man hinh may + NGHIA tieng Viet (neu co
            # trong bang da xac minh) + nhac RESUME duoc tu bot/web (user chot:
            # 'bao 302022663 tho thi ai hieu — phai noi ket nhua + cho resume')
            hexc = _hms_hex(err)
            meaning = HMS_VN.get(hexc)
            hmsl = snap.get("hms") or []
            hmstxt = " · ".join(f"{_hms_hex(int(h.get('attr', 0)))} "
                                f"{_hms_hex(int(h.get('code', 0)))}"
                                for h in hmsl if isinstance(h, dict))
            notify.alarm("Bambu A1: MÁY BÁO LỖI 🚨",
                         f"Mã [{hexc}]" + (f" · HMS: {hmstxt}" if hmstxt else "") +
                         f" — file {fn}.\n" +
                         (f"👉 {meaning}\n" if meaning else "") +
                         f"Xử lý xong → bấm ▶️ Tiếp tục ngay trong bot này (bàn phím "
                         f"dưới) hoặc trên web — y hệt nút Resume trên màn hình máy.\n"
                         f"MỞ CAMERA: {notify.hub_url()}\n"
                         f"Tra mã: wiki.bambulab.com", times=10)
            if not meaning:
                # ma la -> AI giai thich (tin RIENG, khong chan bao dong; AI phai
                # noi 'chua chac' neu khong biet, tranh bia)
                def _err_ai(hexc=hexc):
                    a = ai_chat.ask(
                        f"Máy Bambu A1 báo mã lỗi HMS [{hexc}] khi đang in. Nếu bạn "
                        f"BIẾT CHẮC mã này nghĩa gì: giải thích 1 câu + 3 bước xử lý "
                        f"ngắn. Nếu KHÔNG chắc: nói thẳng 'chưa chắc mã này' và chỉ "
                        f"cách tra wiki.bambulab.com. Tiếng Việt.")
                    if a:
                        notify.send(f"Giải thích mã [{hexc}]", a[:800])
                threading.Thread(target=_err_ai, daemon=True).start()
            # kem 1 anh camera hien truong (best-effort, khong chan luong bao)
            def _snap():
                notify.send_photo_telegram(
                    camera_stream.get_frame(IP, CODE, wait_s=10) or b"",
                    caption=f"Hiện trường lúc báo lỗi {err} — {fn}")
            threading.Thread(target=_snap, daemon=True).start()
        elif not err:
            MILE["err"] = 0
        # CHUONG ve dien thoai (ntfy/Telegram/Discord — cau hinh .env, xem notify.py).
        # Chi bao khi CHUYEN trang thai that — MQTT bao cao lien tuc, khong duoc spam.
        if prev and gc and prev != gc:
            if prev == "RUNNING" and gc == "FINISH":
                # 100%: AI soan loi nhan + ANH thanh pham tu camera + link hub
                def _fin(fn=fn):
                    tip = ai_chat.ask(
                        f"Bản in '{fn}' vừa hoàn thành 100% trên Bambu A1. Viết đúng 2 câu "
                        f"tiếng Việt: 1 câu báo xong thân thiện + 1 mẹo gỡ bản in an toàn.",
                        max_tokens=200) or ("Đợi bàn nguội hẳn rồi hãy gỡ — bản in tự "
                                            "bong, không cong đế, không trầy bàn PEI.")
                    notify.send("Bambu A1: In XONG ✅ 100%",
                                f"{fn}\n{tip}\n{notify.hub_url()}")
                    notify.send_photo_telegram(
                        camera_stream.get_frame(IP, CODE, wait_s=10) or b"",
                        caption=f"📸 Thành phẩm: {fn}")
                threading.Thread(target=_fin, daemon=True).start()
            elif gc == "FAILED":
                notify.call_alert(f"Cảnh báo. Máy in Bambu A1 in thất bại. "
                                  f"{fn}. Kiểm tra ngay.")   # GOI DIEN THAT (Twilio)
                # ma loi da bao dong 10 tin o tren roi thi khoi lap lai 10 tin nua
                if MILE["err"]:
                    notify.send("Bambu A1: In THẤT BẠI (đã báo động ở trên)",
                                f"{fn} — mã lỗi {MILE['err']} (hex {MILE['err']:X}).\n"
                                f"{notify.hub_url()}", urgent=True)
                else:
                    notify.alarm("Bambu A1: In THẤT BẠI 🚨",
                                 f"{fn} — kiểm tra máy ngay.\n"
                                 f"MỞ CAMERA: {notify.hub_url()}", times=10)
            elif prev == "RUNNING" and gc == "PAUSE":
                notify.alarm("Bambu A1: TẠM DỪNG giữa chừng ⚠️",
                             f"{fn} — có thể hết nhựa / lỗi.\n"
                             f"MỞ CAMERA: {notify.hub_url()}", times=3)


def mqtt_loop():
    while True:
        try:
            try:
                c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            except Exception:
                c = mqtt.Client()
            with LOCK:               # snapshot (ip, code) NGUYEN KHOI — tranh cap ip-moi/code-cu
                _ip, _code = IP, CODE
            c.username_pw_set("bblp", _code)
            c.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            c.tls_insecure_set(True)
            c.on_connect = on_connect
            c.on_disconnect = on_disconnect
            c.on_message = on_message
            c.connect(_ip, 8883, 30)
            MQTT["client"] = c

            def repush():
                while True:
                    time.sleep(30)
                    try:
                        c.publish(REQUEST, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
                    except Exception:
                        break
            threading.Thread(target=repush, daemon=True).start()
            c.loop_forever()
        except Exception as e:
            with LOCK:
                STATE["connected"] = False
            MQTT["client"] = None
            print("[MQTT] reconnect sau loi:", e)
            time.sleep(5)


def update_printer_config(host: str, serial: str, code: str) -> None:
    """Doi IP/serial/access-code NONG: ghi .env + printer.local.json (deu gitignore),
    cap nhat globals roi ngat MQTT — mqtt_loop tu tao client moi voi thong so moi.
    Khong can restart server, khong can sua code."""
    global IP, SERIAL, CODE, REPORT, REQUEST
    # Ghi FILE truoc — neu ghi loi thi global GIU NGUYEN (state va file khong lech nhau)
    printer_config.update_env(host, serial, code)
    printer_config.save(host, serial, code)
    with LOCK:                       # doc/ghi bo (IP,CODE) nguyen khoi — mqtt_loop snapshot cung LOCK
        IP, SERIAL, CODE = host, serial, code
        REPORT = f"device/{serial}/report"
        REQUEST = f"device/{serial}/request"
    for k, v in zip(printer_config.ENV_KEYS, (host, serial, code)):
        os.environ[k] = v            # nguon uu tien 2 (environ) cung phai khop
    c = MQTT.get("client")
    if c:
        try:
            c.disconnect()           # loop_forever thoat -> vong while tao client moi
        except Exception:
            pass


# ---------- Ghep du lieu nhua (AMS + store cuc bo) ----------
def build_filament():
    with LOCK:
        data = dict(STATE["data"])
    ams_root = (data.get("ams") or {})
    units = ams_root.get("ams") or []
    try:
        active = int(ams_root.get("tray_now", 255))
    except (TypeError, ValueError):
        active = 255
    try:
        pct = int(data.get("mc_percent"))
    except (TypeError, ValueError):
        pct = None
    with JOB_LOCK:
        jw = JOB["weight"]
    out = []
    for unit in units:
        for t in (unit.get("tray") or []):
            if not t.get("tray_type"):
                continue
            try:
                tid = int(t.get("id"))
            except (TypeError, ValueError):
                continue
            tag = t.get("tray_uuid") or t.get("tag_uid") or ""
            rec = filament_store.get(tag)
            color = str(t.get("tray_color") or "888888")[:6]
            is_active = (tid == active)
            # gam da dung o ban in hien tai (uoc theo % tien do) — chi cuon dang dung
            job_used = round(jw * pct / 100) if (is_active and jw and pct is not None) else None
            out.append({
                "id": tid,
                "slot": SLOT_LABEL.get(tid, tid + 1),
                "type": t.get("tray_sub_brands") or t.get("tray_type") or "?",
                "color": color,
                "tag_uid": tag,
                "machine_remain": t.get("remain"),
                "net": (rec or {}).get("net"),
                "remaining": (rec or {}).get("remaining"),
                "active": is_active,
                "job_used": job_used,
            })
    return out


# ---------- HTTP ----------
PAGE = r"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Bambu A1 — LongPham</title>
<style>
 :root{
   --bg0:#080b10;--bg1:#0e131b;--card1:#171d28;--card2:#1e2635;--line:#28324a;
   --txt:#eef3fb;--mut:#8ea0b8;--acc:#22c55e;--acc2:#16a34a;--amb:#f59e0b;--red:#ef4444;
   --cyan:#38bdf8;--pink:#f472b6;
   --sh:0 18px 34px -18px rgba(0,0,0,.85), 0 6px 12px -6px rgba(0,0,0,.6);
   --hl:inset 0 1px 0 rgba(255,255,255,.06), inset 0 0 0 1px rgba(255,255,255,.03);
 }
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 body{margin:0;background:
     radial-gradient(1200px 600px at 50% -10%, #16233a 0%, transparent 60%),
     linear-gradient(180deg,var(--bg1),var(--bg0));
   color:var(--txt);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
   padding:14px 14px 30px;max-width:480px;margin:auto;min-height:100vh}
 h1{font-size:17px;font-weight:700;margin:2px 2px 14px;display:flex;align-items:center;gap:9px}
 .dot{width:10px;height:10px;border-radius:50%;background:#556;flex:0 0 auto}
 .on{background:var(--acc);box-shadow:0 0 0 4px rgba(34,197,94,.18),0 0 10px var(--acc)}
 .off{background:var(--red);box-shadow:0 0 0 4px rgba(239,68,68,.18)}
 .card{position:relative;background:linear-gradient(160deg,var(--card2),var(--card1));
   border-radius:18px;padding:16px;margin:12px 0;box-shadow:var(--sh),var(--hl)}
 .lbl{color:var(--mut);font-size:12.5px;font-weight:600;letter-spacing:.3px;text-transform:uppercase}

 /* HERO */
 .hero{display:flex;gap:14px;align-items:stretch;overflow:hidden}
 .stagebox{flex:1;min-width:0}
 .stage{font-size:27px;font-weight:800;letter-spacing:-.3px;margin:2px 0 2px;color:#f4f8ff}
 .job{font-size:13px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .printer{position:relative;width:128px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;
   border-radius:14px;overflow:hidden}
 .printer img{width:100%;height:auto;border-radius:14px;display:block;
   filter:drop-shadow(0 8px 14px rgba(0,0,0,.55))}
 .glow{position:absolute;inset:-10px;border-radius:50%;pointer-events:none;opacity:0;transition:opacity .4s}
 .printer.run .glow{opacity:1;background:radial-gradient(circle at 50% 45%,rgba(34,197,94,.26),transparent 62%)}
 .plabel{position:absolute;left:6px;bottom:6px;font-size:10px;font-weight:700;color:#eafff0;
   background:rgba(0,0,0,.55);padding:2px 6px;border-radius:6px;opacity:0}
 .printer.run .plabel{opacity:1}

 /* PROGRESS */
 .bar{height:15px;background:#0a0e16;border-radius:10px;overflow:hidden;margin:12px 0 8px;
   box-shadow:inset 0 2px 5px rgba(0,0,0,.7)}
 .fill{height:100%;width:0%;border-radius:10px;transition:width .6s cubic-bezier(.2,.7,.2,1);
   background:linear-gradient(90deg,var(--acc2),var(--acc),#4ade80);background-size:200% 100%;
   box-shadow:0 0 12px rgba(34,197,94,.5);animation:flow 2.2s linear infinite}
 @keyframes flow{to{background-position:-200% 0}}
 .prow{display:flex;justify-content:space-between;font-size:14px}
 .prow .big{font-size:22px;font-weight:800} .prow .mut{color:var(--mut)}

 /* CONTROL */
 .ctrl{display:grid;grid-template-columns:1fr 1fr 1fr;gap:11px;margin:12px 0}
 .btn{min-height:56px;border:none;border-radius:15px;font-size:15px;font-weight:800;color:#fff;
   cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;
   box-shadow:var(--sh),inset 0 1px 0 rgba(255,255,255,.25);transition:transform .12s,filter .12s}
 .btn svg{width:20px;height:20px;fill:currentColor;flex:0 0 auto}
 .btn:active{transform:translateY(2px);filter:brightness(.92)}
 .btn:disabled{opacity:.38;box-shadow:var(--sh);cursor:not-allowed}
 .b-pause{background:linear-gradient(160deg,#fbbf24,#d97706)}
 .b-resume{background:linear-gradient(160deg,#34d399,#16a34a)}
 .b-stop{background:linear-gradient(160deg,#f87171,#dc2626)}
 .cb{position:sticky;top:0;z-index:60;margin:-14px -14px 12px;padding:10px 14px;font-weight:800;font-size:13.5px;
   text-align:center;letter-spacing:.3px}
 .cb.on{background:linear-gradient(90deg,#065f46,#16a34a);color:#eafff3}
 .cb.off{background:linear-gradient(90deg,#7f1d1d,#dc2626);color:#fff}
 .cb.wait{background:#1e2635;color:#8ea0b8}
 .chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px;align-items:center}
 .chip{display:flex;align-items:center;gap:6px;background:#0c111a;border:1px solid var(--line);
   border-radius:99px;padding:4px 10px 4px 5px;font-size:11.5px;font-weight:700}
 .chip i{width:15px;height:15px;border-radius:50%;border:1px solid rgba(255,255,255,.25);display:block}
 .up{padding:13px;border-radius:14px;background:linear-gradient(160deg,var(--card2),var(--card1));
   box-shadow:var(--sh),var(--hl);margin:2px 0 12px}
 .ubtn{width:100%;background:linear-gradient(160deg,#38bdf8,#0284c7);color:#fff;border:none;border-radius:12px;
   padding:13px;font-weight:800;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px}
 .ubtn:disabled{opacity:.45;cursor:not-allowed;background:#334155}
 .ubtn svg{width:17px;height:17px;fill:currentColor}
 .ubar{height:7px;border-radius:99px;background:#0c111a;border:1px solid var(--line);margin-top:10px;overflow:hidden;display:none}
 .ubar > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#38bdf8,#22c55e);transition:width .2s}
 .uhint{font-size:11.5px;color:var(--mut);margin-top:8px}
 .linkrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px;margin:2px 0 12px}
 .infolink{display:flex;align-items:center;justify-content:center;gap:7px;text-align:center;
   min-height:52px;padding:10px;border-radius:14px;background:linear-gradient(160deg,var(--card2),var(--card1));
   color:var(--cyan);font-weight:700;font-size:13px;text-decoration:none;box-shadow:var(--sh),var(--hl)}
 .infolink svg{width:18px;height:18px;fill:currentColor;flex:0 0 auto}
 .infolink:active{transform:translateY(1px)}

 /* STAT TILES (number card 3D) */
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .tile{position:relative;background:linear-gradient(160deg,var(--card2),var(--card1));
   border-radius:18px;padding:15px 16px;box-shadow:var(--sh),var(--hl);overflow:hidden}
 .tile .ic{position:absolute;top:13px;right:13px;width:22px;height:22px;opacity:.5}
 .tile .ic svg{width:100%;height:100%;fill:none;stroke:var(--mut);stroke-width:1.8}
 .num{font-size:34px;font-weight:800;letter-spacing:-1px;line-height:1.05;margin-top:6px;
   text-shadow:0 2px 0 rgba(0,0,0,.35),0 6px 14px rgba(0,0,0,.45)}
 .num .u{font-size:14px;font-weight:700;color:var(--mut);margin-left:3px}
 .sub{font-size:12.5px;color:var(--mut);margin-top:3px}
 .num.nz{color:#ffcaa8} .num.bed{color:#ff9db0}

 /* AMS */
 .amsnote{font-size:11px;color:var(--mut);font-weight:500;text-transform:none;letter-spacing:0}
 .amshead{display:flex;justify-content:space-between;align-items:center;gap:12px}
 .amsimg{width:104px;height:auto;flex:0 0 auto;border-radius:12px;background:#f4f6fb;padding:6px;
   box-shadow:var(--sh);opacity:.96}
 .ams{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:12px}
 .slot{position:relative;background:linear-gradient(160deg,var(--card2),#141a24);
   border-radius:16px;padding:12px;box-shadow:var(--sh),var(--hl);border:1px solid transparent}
 .slot.act{border-color:var(--acc);box-shadow:var(--sh),0 0 0 1px var(--acc),0 0 16px rgba(34,197,94,.35)}
 .slot .top{display:flex;align-items:center;gap:10px}
 .snum{width:30px;height:30px;flex:0 0 auto;border-radius:9px;display:flex;align-items:center;justify-content:center;
   font-weight:800;font-size:15px;color:#fff;background:linear-gradient(160deg,#2a3446,#1a2130);
   box-shadow:inset 0 1px 0 rgba(255,255,255,.15),0 3px 6px rgba(0,0,0,.5)}
 .sw{width:26px;height:26px;flex:0 0 auto;border-radius:50%;border:2px solid rgba(255,255,255,.15);
   box-shadow:0 2px 5px rgba(0,0,0,.5),inset 0 2px 4px rgba(255,255,255,.25)}
 .stype{font-size:12.5px;font-weight:700;line-height:1.15;flex:1;min-width:0;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .gram{margin-top:9px}
 .gram .n{font-size:20px;font-weight:800} .gram .n .u{font-size:12px;color:var(--mut);font-weight:700}
 .gbar{height:7px;border-radius:5px;background:#0a0e16;overflow:hidden;margin-top:6px;box-shadow:inset 0 1px 3px rgba(0,0,0,.7)}
 .gbar > i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,#f59e0b,#22c55e)}
 .gramrow{display:flex;align-items:center;justify-content:space-between;margin-top:8px}
 .edit{background:#222c3d;color:var(--cyan);border:1px solid var(--line);border-radius:9px;
   padding:7px 11px;font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:5px;min-height:34px}
 .edit svg{width:14px;height:14px;fill:currentColor}
 .undecl{font-size:11.5px;color:var(--amb)}
 /* AMS Lite — 4 cuon nhua + dong chay (giong man hinh may) */
 .amsviz{display:grid;grid-template-columns:1fr 40px 1fr;grid-template-rows:1fr 1fr;gap:9px;margin-top:12px}
 .spool{position:relative;border-radius:13px;padding:10px;min-height:98px;display:flex;flex-direction:column;
   align-items:center;justify-content:center;cursor:pointer;box-shadow:var(--sh),inset 0 0 0 1px rgba(255,255,255,.10)}
 .spool .num{position:absolute;top:7px;left:8px;width:22px;height:22px;border-radius:50%;background:rgba(0,0,0,.42);
   display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px;color:#fff}
 .spool .flag{position:absolute;top:7px;right:8px;font-size:9px;font-weight:800;background:rgba(0,0,0,.5);color:#fff;padding:2px 6px;border-radius:6px}
 .spool .pla{font-size:24px;font-weight:900;letter-spacing:1.5px;line-height:1;text-shadow:0 1px 3px rgba(0,0,0,.35)}
 .spool .sub{font-size:10.5px;font-weight:700;opacity:.92;margin-top:2px}
 .spool .g{font-size:12.5px;font-weight:800;margin-top:6px;opacity:.95}
 .spool.act{box-shadow:var(--sh),0 0 0 2px #eafff0,0 0 18px rgba(234,255,240,.45)}
 .s1{grid-column:1;grid-row:1}.s4{grid-column:3;grid-row:1}.s2{grid-column:1;grid-row:2}.s3{grid-column:3;grid-row:2}
 .buffer{grid-column:2;grid-row:1/3;display:flex;align-items:stretch;justify-content:center}
 .tube{width:16px;border-radius:9px;background:linear-gradient(90deg,#1c2433,#33405a,#1c2433);position:relative;
   overflow:hidden;box-shadow:inset 0 0 5px rgba(0,0,0,.7)}
 .tube .flow{position:absolute;left:2px;right:2px;top:-100%;height:200%;
   background:repeating-linear-gradient(180deg,var(--fc,#22c55e) 0 6px,transparent 6px 15px);opacity:0}
 .buffer.run .tube .flow{opacity:.95;animation:flowdown .85s linear infinite}
 @keyframes flowdown{to{transform:translateY(25%)}}

 /* ALERT / TOAST */
 #alert{display:none;color:#fff;padding:15px;border-radius:15px;margin:12px 0;font-weight:800;
   font-size:16px;box-shadow:var(--sh);align-items:center;gap:10px;cursor:pointer;
   background:linear-gradient(160deg,#ef4444,#b91c1c);animation:blink 1.1s steps(2) infinite}
 #alert.al-error{background:linear-gradient(160deg,#ef4444,#b91c1c)}
 #alert.al-warn{background:linear-gradient(160deg,#f59e0b,#b45309)}
 #alert.al-done{background:linear-gradient(160deg,#22c55e,#15803d);animation:none}
 #alert svg{width:24px;height:24px;fill:#fff;flex:0 0 auto}
 @keyframes blink{50%{opacity:.6}}
 .sndbtn{margin-left:auto;background:#1e2635;border:1px solid var(--line);color:var(--cyan);
   border-radius:10px;padding:7px 11px;font-size:12px;font-weight:700;cursor:pointer;
   display:flex;align-items:center;gap:6px;min-height:36px}
 .sndbtn svg{width:15px;height:15px;fill:currentColor} .sndbtn.on{color:var(--acc);border-color:var(--acc)}
 .foot{color:var(--mut);font-size:12px;text-align:center;margin-top:14px;display:flex;align-items:center;justify-content:center;gap:6px}
 #toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%) translateY(10px);background:#0b1220;
   border:1px solid var(--line);color:#fff;padding:11px 18px;border-radius:12px;opacity:0;pointer-events:none;
   transition:opacity .25s,transform .25s;font-size:14px;box-shadow:var(--sh);z-index:50}
 #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
 @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div id="connbar" class="cb wait">Đang kiểm tra kết nối máy in…</div>
<h1><span id="dot" class="dot"></span> Bambu A1 · <span id="name">—</span>
  <button id="sndBtn" class="sndbtn" onclick="enableSound()"><svg viewBox="0 0 24 24"><path d="M12 3a1 1 0 0 0-1 1v.28C8.5 4.9 7 7.1 7 9.7V13l-1.7 2.5A1 1 0 0 0 6.1 17h11.8a1 1 0 0 0 .8-1.5L17 13V9.7c0-2.6-1.5-4.8-4-5.42V4a1 1 0 0 0-1-1zm0 18a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 21z"/></svg><span>Bật âm</span></button>
  <button class="sndbtn" onclick="toggleCfg()" title="Kết nối máy in"><svg viewBox="0 0 24 24"><path d="M19.4 13a7.6 7.6 0 0 0 .1-1l2-1.6a.5.5 0 0 0 .1-.6l-1.9-3.3a.5.5 0 0 0-.6-.2l-2.4 1a7.5 7.5 0 0 0-1.7-1l-.4-2.6a.5.5 0 0 0-.5-.4h-3.8a.5.5 0 0 0-.5.4l-.4 2.6a7.5 7.5 0 0 0-1.7 1l-2.4-1a.5.5 0 0 0-.6.2L2.4 9.8a.5.5 0 0 0 .1.6l2 1.6a7.6 7.6 0 0 0 0 2l-2 1.6a.5.5 0 0 0-.1.6l1.9 3.3c.1.2.4.3.6.2l2.4-1c.5.4 1.1.8 1.7 1l.4 2.6c0 .3.3.4.5.4h3.8c.2 0 .5-.1.5-.4l.4-2.6c.6-.2 1.2-.6 1.7-1l2.4 1c.2.1.5 0 .6-.2l1.9-3.3a.5.5 0 0 0-.1-.6l-2-1.6zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"/></svg><span>Kết nối</span></button></h1>

<div id="alert" onclick="dismissAlert()"><svg viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg><span id="alertmsg"></span></div>

<div class="card" id="cfgcard" style="display:none">
  <h3 style="margin-top:0">⚙ Kết nối máy in (LAN) — như Bambu Studio</h3>
  <div class="mut" style="font-size:12px;margin-bottom:8px">Xem trên màn hình máy in: <b>Cài đặt → WLAN</b> có IP + Access Code (đổi mỗi lần máy reset WLAN). Lưu vào <code>.env</code> trên server — không nằm trong git, không cần build lại code.</div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
    <label style="font-size:12px">IP máy in<br><input id="cfgHost" placeholder="192.168.x.x" style="width:140px;padding:7px;border-radius:8px;border:1px solid #334;background:#0f1523;color:#e8ecf4"></label>
    <label style="font-size:12px">Serial (giữ trống = giữ nguyên)<br><input id="cfgSerial" placeholder="" style="width:170px;padding:7px;border-radius:8px;border:1px solid #334;background:#0f1523;color:#e8ecf4"></label>
    <label style="font-size:12px">Access Code (8 ký tự)<br><input id="cfgCode" placeholder="" maxlength="8" style="width:120px;padding:7px;border-radius:8px;border:1px solid #334;background:#0f1523;color:#e8ecf4"></label>
    <button class="btn" onclick="saveCfg()" id="cfgSave" style="padding:9px 16px">Lưu &amp; kết nối lại</button>
  </div>
  <div class="mut" id="cfgHint" style="font-size:12px;margin-top:8px"></div>
</div>
<script>
async function loadCfg(){
  try{ const r=await fetch("/api/printer-config"); const j=await r.json();
    document.getElementById("cfgHost").value=j.host||"";
    document.getElementById("cfgSerial").placeholder=j.serial_set?"đã cấu hình (trống = giữ nguyên)":"chưa có";
    document.getElementById("cfgCode").placeholder=j.code_set?"••••••••  (trống = giữ nguyên)":"chưa có";
    document.getElementById("cfgHint").textContent=j.connected?"Đang kết nối OK với cấu hình hiện tại.":"⚠ Chưa kết nối được — kiểm tra IP/Access Code (mã đổi khi máy reset WLAN).";
  }catch(e){}
}
function toggleCfg(){ const c=document.getElementById("cfgcard");
  const show=c.style.display==="none"; c.style.display=show?"block":"none"; if(show) loadCfg(); }
async function saveCfg(){
  const b=document.getElementById("cfgSave"); b.disabled=true;
  try{
    const r=await fetch("/api/printer-config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({host:document.getElementById("cfgHost").value,
                           serial:document.getElementById("cfgSerial").value,
                           code:document.getElementById("cfgCode").value})});
    const j=await r.json(); toast(j.msg||(j.ok?"Đã lưu":"Lỗi"));
    if(j.ok){ document.getElementById("cfgCode").value=""; setTimeout(loadCfg,4000); }
  }catch(e){ toast("Mất kết nối server"); }
  b.disabled=false;
}
</script>

<div class="card hero">
  <div class="stagebox">
    <div class="lbl">Trạng thái</div>
    <div class="stage" id="stage">…</div>
    <div class="job" id="job">—</div>
    <div class="bar"><div class="fill" id="fill"></div></div>
    <div class="prow"><span class="big" id="pct">—%</span><span class="mut" id="rem">còn —</span></div>
    <div class="prow" style="margin-top:6px"><span class="mut">Lớp</span><span id="layer">—</span></div>
  </div>
  <div class="printer" id="printer">
    <div class="glow"></div>
    <img id="heroImg" src="/a1.jpg" alt="Model đang in" onerror="this.onerror=null;this.src='/a1.jpg'">
    <div class="plabel">đang in</div>
  </div>
</div>

<div class="ctrl">
  <button class="btn b-pause"  id="bPause"  onclick="cmd('pause')" aria-label="Tạm dừng">
    <svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>Tạm dừng</button>
  <button class="btn b-resume" id="bResume" onclick="cmd('resume')" aria-label="Tiếp tục">
    <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>Tiếp tục</button>
  <button class="btn b-stop"   id="bStop"   onclick="stopPrint()" aria-label="Dừng hẳn">
    <svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>DỪNG</button>
</div>

<div class="card">
  <details id="cambox" ontoggle="camTog()">
    <summary style="cursor:pointer;font-weight:800;font-size:14px">📹 Camera bàn in — live từ camera A1 tích hợp (bấm để mở)</summary>
    <div style="margin-top:10px;text-align:center">
      <img id="camimg" alt="camera bàn in" style="max-width:100%;border-radius:12px;background:#0a0e14;min-height:120px">
      <div class="mut" style="margin-top:6px;font-size:12px">Lấy thẳng từ camera A1 qua LAN (cổng 6000, cùng Access Code) —
      đóng khung này là tự ngắt kết nối. Xem được cả qua Tailscale trên điện thoại.</div>
    </div>
  </details>
  <div style="display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap">
    <label style="display:flex;gap:6px;align-items:center;font-size:13px;cursor:pointer">
      <input type="checkbox" id="bellchk" onchange="bellTog()"> 🔔 Chuông trên trang (In XONG / LỖI / TẠM DỪNG)
    </label>
    <button class="ubtn" style="width:auto;padding:8px 14px;font-size:12.5px" onclick="notifyTest()">📱 Gửi thử chuông điện thoại</button>
    <button class="ubtn" style="width:auto;padding:8px 14px;font-size:12.5px" onclick="aiVision()">🔍 AI soi bản in</button>
    <span class="mut" id="ntfyst" style="font-size:12px"></span>
  </div>
  <div id="visout" style="display:none;margin-top:8px;font-size:13px;line-height:1.55;background:rgba(56,189,248,.08);border-left:3px solid var(--cyan,#38bdf8);border-radius:8px;padding:8px 11px;white-space:pre-wrap"></div>
</div>

<div class="card">
  <h3 style="margin-top:0">🤖 Hỏi đáp AI <span class="mut" style="font-size:12px">· Nemotron free · biết trạng thái máy + kho số đã kiểm chứng của hub</span></h3>
  <div id="ailog" style="max-height:280px;overflow-y:auto;font-size:13px;line-height:1.6"></div>
  <div style="display:flex;gap:8px;margin-top:8px">
    <input id="aiq" placeholder="Vd: in tới đâu rồi? / PLA Matte để nhiệt bao nhiêu?"
      style="flex:1;min-width:0;background:#0c111a;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:11px;font-size:13px"
      onkeydown="if(event.key==='Enter')aiAsk()">
    <button class="ubtn" style="width:auto;padding:10px 18px" onclick="aiAsk()">Hỏi</button>
  </div>
</div>

<div class="linkrow">
  <a class="infolink" href="/info"><svg viewBox="0 0 24 24"><path d="M11 7h2v2h-2zM11 11h2v6h-2zM12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16z"/></svg> Thông tin G-code</a>
  <a class="infolink" href="/files"><svg viewBox="0 0 24 24"><path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg> File trên máy · chọn in</a>
  <a class="infolink" href="/analyze"><svg viewBox="0 0 24 24"><path d="M3 3v18h18v-2H5V3H3zm4 12h2v-5H7v5zm4 0h2V7h-2v8zm4 0h2v-3h-2v3z"/></svg> Phân tích .3mf / .stl</a>
</div>

<div class="chips" id="chips"></div>

<div class="up" id="up">
  <input type="file" id="fpick" accept=".3mf,.stl" style="display:none" onchange="pick()">
  <button class="ubtn" id="ubtn" onclick="document.getElementById('fpick').click()">
    <svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zM12 2L6.5 9.5h4V16h3V9.5h4L12 2z"/></svg>
    <span id="ulabel">Đẩy file .3mf / .stl lên máy in — chưa slice cũng được</span>
  </button>
  <div class="ubar" id="ubar"><i id="ufill"></i></div>
  <div class="uhint">File <b>đã slice</b> → chuyển thẳng xuống máy. File <b>dự án thô</b> → máy tính tự slice
  (Bambu Studio, ~1-2 phút) rồi chuyển, kèm thời gian in + gam nhựa. <span id="uhintx" style="color:var(--acc)"></span></div>
</div>

<div class="grid">
  <div class="tile">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M14 14.76V5a2 2 0 1 0-4 0v9.76a4 4 0 1 0 4 0z"/></svg></div>
    <div class="lbl">Nozzle</div>
    <div class="num nz"><span id="nz">—</span><span class="u">°C</span></div>
    <div class="sub" id="nzt">→ — °C</div>
  </div>
  <div class="tile">
    <div class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="14" width="18" height="4" rx="1"/><path d="M6 14V9M12 14V7M18 14V9"/></svg></div>
    <div class="lbl">Bed</div>
    <div class="num bed"><span id="bed">—</span><span class="u">°C</span></div>
    <div class="sub" id="bedt">→ — °C</div>
  </div>
</div>

<div class="card">
  <div class="amshead">
    <div><div class="lbl">AMS Lite</div><div class="amsnote">khe 1-4 theo máy thật · gam bạn khai báo (RFID)</div></div>
    <img class="amsimg" src="/ams.jpg" alt="Sơ đồ AMS Lite 4 khe">
  </div>
  <div class="ams" id="ams">—</div>
</div>

<div class="grid">
  <div class="tile">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M12 12a4 4 0 0 1 4-4c3 0 4 2 4 4M12 12a4 4 0 0 1-4 4c-3 0-4-2-4-4M12 12a4 4 0 0 1 4 4c0 3-2 4-4 4M12 12a4 4 0 0 1-4-4c0-3 2-4 4-4"/></svg></div>
    <div class="lbl">Quạt (part)</div>
    <div class="num" id="fan" style="color:#a7f3d0">—</div>
  </div>
  <div class="tile">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M5 12.5a10 10 0 0 1 14 0M8 16a5 5 0 0 1 8 0"/><circle cx="12" cy="19" r="1" fill="currentColor" stroke="none"/></svg></div>
    <div class="lbl">Wi‑Fi</div>
    <div class="num" id="wifi" style="color:#bae6fd;font-size:26px">—</div>
  </div>
</div>

<div class="foot" id="foot">Đang tải…</div>
<div id="toast"></div>

<script>
const STAGE={IDLE:"Đang rảnh",PREPARE:"Đang chuẩn bị",RUNNING:"ĐANG IN",PAUSE:"Tạm dừng",FINISH:"In XONG",FAILED:"In LỖI",SLICING:"Đang slice"};
/* ===== CAMERA A1 (lazy — chi ket noi khi mo khung) =====
   DOUBLE-BUFFER thay vi MJPEG: camera A1 chi phat ~1 frame/2s; MJPEG tren mang cham
   lam browser ve NUA frame (xe hinh) + khung khong deu. Tai ngam anh moi qua
   /api/camera.jpg, tai XONG moi trao src -> khong xe, nhip deu, chiu mang yeu. */
let CAMT=null;
function camTog(){
  const d=document.getElementById("cambox"),im=document.getElementById("camimg");
  if(d&&d.open){ camPoll(im); }
  else { if(CAMT){clearTimeout(CAMT);CAMT=null;} if(im)im.removeAttribute("src"); }
}
function camPoll(im){
  const pre=new Image();
  const next=()=>{ CAMT=setTimeout(()=>camPoll(im),1200); };  /* ~nhip nguon 1-2s */
  pre.onload=()=>{ im.src=pre.src; next(); };
  pre.onerror=next;                       /* mat mang tam thoi -> thu lai nhip sau */
  pre.src="/api/camera.jpg?t="+Date.now();
}
/* ===== CHUONG TREN TRANG (WebAudio — can 1 cu bam de mo khoa am thanh) ===== */
let BELL=localStorage.getItem("lp_bell")==="1", AC=null, prevGc=null;
function beep(freq,dur,times){
  try{
    AC=AC||new (window.AudioContext||window.webkitAudioContext)();
    let t=AC.currentTime; times=times||1;
    for(let i=0;i<times;i++){
      const o=AC.createOscillator(),g=AC.createGain();
      o.frequency.value=freq;o.connect(g);g.connect(AC.destination);
      g.gain.setValueAtTime(0.25,t+i*(dur+0.12));
      g.gain.exponentialRampToValueAtTime(0.001,t+i*(dur+0.12)+dur);
      o.start(t+i*(dur+0.12));o.stop(t+i*(dur+0.12)+dur);
    }
  }catch(e){}
}
function bellTog(){
  BELL=document.getElementById("bellchk").checked;
  localStorage.setItem("lp_bell",BELL?"1":"0");
  if(BELL){try{AC=AC||new (window.AudioContext||window.webkitAudioContext)();AC.resume();beep(880,0.12);}catch(e){}}
}
function bellCheck(gc){
  if(prevGc&&gc&&gc!==prevGc&&BELL){
    if(gc==="FINISH")beep(880,0.25,3);                     /* teng teng teng — xong */
    else if(gc==="FAILED")beep(220,0.5,5);                 /* tram, keo dai — loi */
    else if(gc==="PAUSE"&&prevGc==="RUNNING")beep(440,0.35,4); /* het nhua/tam dung */
  }
  prevGc=gc;
}
async function notifyTest(){
  const el=document.getElementById("ntfyst"); el.textContent="đang gửi…";
  try{
    const j=await (await fetch("/api/notify-test")).json();
    el.textContent=j.ok?("✓ đã gửi: "+j.sent.join(", ")):j.msg;
  }catch(e){el.textContent="lỗi: "+e;}
}
/* ===== AI SOI CAMERA (vision) ===== */
let VBUSY=false;
async function aiVision(){
  if(VBUSY) return; VBUSY=true;
  const o=document.getElementById("visout");
  o.style.display="block"; o.textContent="🔍 Đang chụp camera + AI soi lỗi (5-30s)…";
  try{
    const j=await (await fetch("/api/vision-check")).json();
    o.textContent=j.answer||"?";
  }catch(e){o.textContent="Lỗi mạng: "+e;}
  VBUSY=false;
}
/* ===== HOI DAP AI ===== */
let AIBUSY=false;
/* Tô ĐỎ dòng cảnh báo (bắt đầu ⚠️ hoặc chứa CẢNH BÁO/DÍNH CHẾT) trong câu trả lời AI */
function aiFmt(s){
  return esc2(s||"?").split("\n").map(function(ln){
    return /⚠️|⚠|CẢNH BÁO|DÍNH CHẾT/i.test(ln)
      ? '<span style="color:#f87171;font-weight:600">'+ln+'</span>' : ln;
  }).join("\n");
}
async function aiAsk(){
  const inp=document.getElementById("aiq"), log=document.getElementById("ailog");
  const q=(inp.value||"").trim(); if(!q||AIBUSY) return;
  AIBUSY=true; inp.value="";
  log.innerHTML+='<div style="margin:6px 0;text-align:right"><span style="background:rgba(56,189,248,.15);border-radius:10px;padding:6px 10px;display:inline-block">'+esc2(q)+'</span></div>';
  log.innerHTML+='<div id="aiwait" class="mut" style="margin:6px 0">🤖 đang nghĩ…</div>';
  log.scrollTop=log.scrollHeight;
  try{
    const j=await (await fetch("/api/ai-chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({q:q})})).json();
    document.getElementById("aiwait").outerHTML=
      '<div style="margin:6px 0"><span style="background:rgba(34,197,94,.12);border-left:3px solid var(--acc);border-radius:8px;padding:6px 10px;display:inline-block;white-space:pre-wrap">'+aiFmt(j.answer)+'</span></div>';
  }catch(e){
    document.getElementById("aiwait").outerHTML='<div class="mut" style="margin:6px 0">lỗi mạng: '+esc2(String(e))+'</div>';
  }
  log.scrollTop=log.scrollHeight; AIBUSY=false;
}
function esc2(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");}
let prevState=null, wasConnected=false, connLost=false, curAlert=null, lastBeepTs=0, doneShown=false, dismissed=null, ac=null;
const PENCIL='<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.8 9.94l-3.75-3.75L3 17.25zM20.7 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';

function toast(m){const t=document.getElementById("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2500);}
function ensureAudio(){ if(!ac){ try{ ac=new (window.AudioContext||window.webkitAudioContext)(); }catch(e){} } if(ac&&ac.state==="suspended"){ try{ac.resume();}catch(e){} } }
function tone(f,d,type,delay){ if(!ac)return; setTimeout(()=>{ try{ const o=ac.createOscillator(),g=ac.createGain(); o.connect(g);g.connect(ac.destination); o.type=type||"sine"; o.frequency.value=f; const s=ac.currentTime; g.gain.setValueAtTime(0.25,s); g.gain.exponentialRampToValueAtTime(0.001,s+d); o.start(s); o.stop(s+d);}catch(e){} }, delay||0); }
function soundError(){ ensureAudio(); tone(880,.18,"square",0); tone(660,.2,"square",210); tone(880,.18,"square",440); }
function soundDone(){ ensureAudio(); tone(659,.16,"sine",0); tone(880,.16,"sine",170); tone(1046,.34,"sine",340); }
function soundWarn(){ ensureAudio(); tone(520,.2,"triangle",0); tone(400,.26,"triangle",230); }
function soundReconnect(){ ensureAudio(); tone(523,.14,"sine",0); tone(784,.2,"sine",150); }
function vibrate(p){ try{ if(navigator.vibrate) navigator.vibrate(p); }catch(e){} }
function notify(title,body){ try{ if("Notification" in window && Notification.permission==="granted") new Notification(title,{body:body||""}); }catch(e){} }
function enableSound(){ ensureAudio(); tone(880,.12,"sine",0); tone(1174,.14,"sine",130); try{ if("Notification" in window && Notification.permission==="default") Notification.requestPermission(); }catch(e){} const b=document.getElementById("sndBtn"); if(b){ b.classList.add("on"); b.querySelector("span").textContent="Âm bật"; } toast("Đã bật âm thanh + thông báo"); }
function dismissAlert(){ dismissed=curAlert; document.getElementById("alert").style.display="none"; }
function setAlert(type,msg,showResume){
  const el=document.getElementById("alert"); const key=type?(type+":"+msg):null;
  if(!type){ el.style.display="none"; curAlert=null; const ob=document.getElementById("alertresume"); if(ob)ob.remove(); return; }
  if(key===dismissed) return;
  el.className="al-"+type; el.style.display="flex";
  document.getElementById("alertmsg").textContent=msg;
  /* Nut 'Tiep tuc (da xu ly xong)' — nhu nut Resume tren man hinh may */
  const ob=document.getElementById("alertresume");
  if(showResume){
    if(!ob){
      const b=document.createElement("button"); b.id="alertresume";
      b.textContent="▶️ Tiếp tục (đã xử lý xong)";
      b.style.cssText="margin-left:10px;background:linear-gradient(160deg,#34d399,#16a34a);color:#fff;border:none;border-radius:10px;padding:9px 14px;font-weight:800;cursor:pointer;flex-shrink:0";
      b.onclick=function(){ cmd("resume"); b.textContent="Đã gửi lệnh…"; };
      el.appendChild(b);
    }
  } else if(ob){ ob.remove(); }
  if(curAlert!==key){ curAlert=key; lastBeepTs=Date.now();
    if(type==="done"){ soundDone(); notify("Bambu A1 — IN XONG",msg); vibrate([120,60,120]); }
    else if(type==="error"){ soundError(); notify("Bambu A1 — LỖI",msg); vibrate([220,90,220,90,220]); }
    else { soundWarn(); notify("Bambu A1 — Cảnh báo",msg); vibrate([160,80,160]); }
  } else if(type!=="done" && Date.now()-lastBeepTs>5000){ lastBeepTs=Date.now(); (type==="error"?soundError:soundWarn)(); vibrate([150]); }
}
function fmtMin(m){m=parseInt(m);if(isNaN(m))return"—";return m>=60?(Math.floor(m/60)+"h"+String(m%60).padStart(2,"0")+"m"):(m+"m");}

async function cmd(action){
  try{const r=await fetch("/api/cmd/"+action,{method:"POST"});const j=await r.json();
    toast(j.ok?("Đã gửi lệnh: "+action):("Lỗi: "+j.msg));}catch(e){toast("Lỗi gửi lệnh: "+e);}
}
function stopPrint(){ if(confirm("DỪNG hẳn bản in? Không thể hoàn tác.")) cmd("stop"); }

async function editGram(tag, slot, cur, net){
  if(!tag){ toast("Khe này chưa có cuộn RFID"); return; }
  const v=prompt("Khe "+slot+" — nhập số GAM nhựa còn lại:", (cur!=null?cur:net||1000));
  if(v===null) return;
  const g=parseInt(v); if(isNaN(g)||g<0){ toast("Số không hợp lệ"); return; }
  try{
    const r=await fetch("/api/filament",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tag_uid:tag, remaining:g, net:(net||Math.max(1000,g))})});
    const j=await r.json();
    toast(j.ok?("Khe "+slot+": "+g+"g"):("Lỗi: "+(j.msg||"")));
  }catch(e){toast("Lỗi lưu: "+e);}
}

function lum(h){const r=parseInt(h.substr(0,2),16),g=parseInt(h.substr(2,2),16),b=parseInt(h.substr(4,2),16);return(0.299*r+0.587*g+0.114*b)/255;}
function renderAms(fil, printing){
  const by={}; (fil||[]).forEach(f=>by[f.id]=f);
  const SLOT={0:1,1:2,2:3,3:4};
  let anyActive=false, activeColor=null;
  function spool(id,cls){
    const f=by[id]; const slot=SLOT[id];
    if(!f) return '<div class="spool '+cls+'" style="background:#141a24;color:var(--mut)"><div class="num">'+slot+'</div><div class="sub">trống</div></div>';
    const col=(f.color||"888888").substr(0,6);
    const txt=lum(col)>0.62?"#141a24":"#ffffff";
    const net=f.net||1000;
    const used=(f.active&&f.job_used!=null)?f.job_used:0;
    const live=(f.remaining!=null)?Math.max(0,f.remaining-used):null;
    if(f.active){ anyActive=true; activeColor="#"+col; }
    return '<div class="spool '+cls+(f.active?' act':'')+'" style="background:#'+col+';color:'+txt+'"'
      +' onclick="editGram(\''+(f.tag_uid||'')+'\','+slot+','+(f.remaining!=null?f.remaining:'null')+','+net+')">'
      +'<div class="num">'+slot+'</div>'
      +(f.active?'<div class="flag">đang in</div>':'')
      +'<div class="pla">PLA</div><div class="sub">'+(f.type||'')+'</div>'
      +'<div class="g">'+(live!=null?(live+' g'+(used>0?(' (−'+used+')'):'')):'chạm để khai báo')+'</div></div>';
  }
  const a=spool(0,'s1'), b=spool(3,'s4'), c=spool(1,'s2'), d=spool(2,'s3');
  const run=(printing&&anyActive)?' run':'';
  const buf='<div class="buffer'+run+'"><div class="tube"><div class="flow" style="--fc:'+(activeColor||'#22c55e')+'"></div></div></div>';
  document.getElementById("ams").innerHTML='<div class="amsviz">'+a+b+buf+c+d+'</div>';
}
let heroFile=null;
function updateHero(hasThumb, gcodeFile){
  const img=document.getElementById("heroImg");
  if(hasThumb){
    if(gcodeFile!==heroFile){ img.src="/thumb.png?t="+Date.now(); heroFile=gcodeFile; }
  } else if(heroFile!==null){ img.src="/a1.jpg"; heroFile=null; }
}

// ===== Upload + tu slice (tich hop hub) =====
function pick(){
  const inp=document.getElementById("fpick"), f=inp.files&&inp.files[0];
  inp.value="";
  if(!f) return;
  if(!/\.(3mf|stl)$/i.test(f.name)){ toast("Chỉ nhận file .3mf hoặc .stl"); return; }
  upload(f);
}
function upload(f){
  const btn=document.getElementById("ubtn"), lab=document.getElementById("ulabel");
  const bar=document.getElementById("ubar"), fill=document.getElementById("ufill");
  btn.disabled=true; bar.style.display="block"; fill.style.width="0";
  const mb=(f.size/1048576).toFixed(1);
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/upload?name="+encodeURIComponent(f.name));
  xhr.upload.onprogress=e=>{
    if(!e.lengthComputable) return;
    const p=Math.round(e.loaded/e.total*100);
    fill.style.width=p+"%";
    lab.textContent = p<100 ? ("Đang đẩy… "+p+"% ("+mb+" MB)") : "Đang ghi vào máy in… (chờ máy xác nhận)";
  };
  xhr.onload=()=>{
    let j={}; try{ j=JSON.parse(xhr.responseText); }catch(e){}
    if(xhr.status===200 && j.ok && j.queued){ pollSlice(); return; }
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl lên máy in — chưa slice cũng được";
    if(xhr.status===200 && j.ok){ toast("Đã đẩy lên máy: "+j.name); }
    else{ toast("Lỗi: "+(j.msg||("HTTP "+xhr.status))); }
  };
  xhr.onerror=()=>{
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl lên máy in — chưa slice cũng được";
    toast("Mất kết nối khi đẩy file");
  };
  lab.textContent="Đang đẩy… 0% ("+mb+" MB)";
  xhr.send(f);
}
function fmtStats(s){
  if(!s) return "";
  const p=[];
  if(s.time) p.push("in "+s.time);
  if(s.weight_g) p.push(s.weight_g.toFixed(0)+" g");
  if(s.layers) p.push(s.layers+" lớp");
  if(s.dims) p.push(s.dims.join("×")+" mm");
  if(s.overhang_pct>0) p.push("overhang "+s.overhang_pct+"%");
  return p.join(" · ");
}
async function pollSlice(){
  const btn=document.getElementById("ubtn"), lab=document.getElementById("ulabel");
  const bar=document.getElementById("ubar"), fill=document.getElementById("ufill");
  btn.disabled=true; bar.style.display="block"; fill.style.width="100%";
  try{
    const j=await (await fetch("/api/upstatus",{cache:"no-store"})).json();
    if(j.state==="slicing"||j.state==="pushing"){
      lab.textContent=j.msg||"Đang xử lý…";
      setTimeout(pollSlice, 3000); return;
    }
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl lên máy in — chưa slice cũng được";
    if(j.state==="done"){
      toast("✔ "+j.msg+(j.stats?(" — "+fmtStats(j.stats)):""));
      const hint=document.getElementById("uhintx");
      if(hint&&j.stats) hint.textContent="Kết quả slice: "+fmtStats(j.stats);
    }
    else if(j.state==="error"){ toast("Lỗi: "+j.msg); }
  }catch(e){ setTimeout(pollSlice, 4000); }
}

async function tick(){
 try{
  const r=await fetch("/api/status",{cache:"no-store"});const s=await r.json();const d=s.data||{};
  document.getElementById("dot").className="dot "+(s.connected?"on":"off");
  document.getElementById("name").textContent=s.name||"—";
  const gc=d.gcode_state||"?";
  bellCheck(gc);                       /* chuong tren trang khi doi trang thai */
  document.getElementById("stage").textContent=STAGE[gc]||gc;
  document.getElementById("job").textContent=d.subtask_name||d.gcode_file||"—";
  let pct=parseInt(d.mc_percent);if(isNaN(pct))pct=0;
  document.getElementById("fill").style.width=pct+"%";
  document.getElementById("pct").textContent=pct+"%";
  document.getElementById("rem").textContent="còn ~"+fmtMin(d.mc_remaining_time);
  document.getElementById("layer").textContent=(d.layer_num??"—")+" / "+(d.total_layer_num??"—");
  const rnd=v=>{v=parseFloat(v);return isNaN(v)?"—":Math.round(v);};
  document.getElementById("nz").textContent=rnd(d.nozzle_temper);
  document.getElementById("nzt").textContent="→ "+rnd(d.nozzle_target_temper)+" °C";
  document.getElementById("bed").textContent=rnd(d.bed_temper);
  document.getElementById("bedt").textContent="→ "+rnd(d.bed_target_temper)+" °C";
  document.getElementById("fan").textContent=(d.cooling_fan_speed??"—");
  document.getElementById("wifi").textContent=(d.wifi_signal||"—");
  // printer/model animation + anh model that
  const printing=(gc==="RUNNING");
  const pr=document.getElementById("printer");
  pr.classList.toggle("run",printing);
  updateHero(s.has_thumb, d.gcode_file);
  // Mau nhua THUC SU dung trong ban in (tu slice_info cua file dang in)
  const fils=s.job_filaments||[];
  document.getElementById("chips").innerHTML = fils.length
    ? fils.map(f=>'<span class="chip"><i style="background:'+(f.color||"#888")+'"></i>'
        +(f.type||"?")+' · '+(f.used_g||0)+' g</span>').join("")
    : "";
  // buttons
  const paused=(gc==="PAUSE");
  document.getElementById("bPause").disabled=!printing;
  document.getElementById("bResume").disabled=!paused;
  document.getElementById("bStop").disabled=!(printing||paused);
  // AMS + gam nhua + dong chay
  renderAms(s.filament, printing);
  // ===== su kien: mat ket noi / in xong / loi =====
  const err=parseInt(d.print_error)||parseInt(d.mc_print_error_code)||0;
  const hms=(d.hms&&d.hms.length)?d.hms.length:0;
  if(!s.connected){
    if(wasConnected){ connLost=true; setAlert("warn","Mất kết nối máy in!"); }
  } else {
    if(connLost){ connLost=false; soundReconnect(); notify("Bambu A1 — Đã kết nối lại",""); vibrate([80,40,80]); toast("Đã kết nối lại máy in"); setAlert(null); }
    if(gc==="FINISH" && (prevState==="RUNNING"||doneShown)){
      doneShown=true; setAlert("done","Đã in XONG: "+(d.subtask_name||d.gcode_file||""));
    } else {
      doneShown=false;
      let type=null,msg=null;
      /* Ma loi hien HEX giong man hinh may: 302051349 -> [1200-8007]; kem HMS chi tiet */
      const hx=n=>{const s=("00000000"+((n>>>0).toString(16).toUpperCase())).slice(-8);return s.slice(0,4)+"-"+s.slice(4);};
      const hmsCodes=(d.hms&&d.hms.length)?d.hms.map(h=>hx(h.attr)+" "+hx(h.code)).join(" · "):"";
      if(err&&err!==0){ type="error"; msg="Máy báo LỖI ["+hx(err)+(hmsCodes?(" · "+hmsCodes):"")+"] — xem camera, xử lý xong bấm Tiếp tục"; }
      else if(gc==="FAILED"){ type="error"; msg="Bản in THẤT BẠI"+(hmsCodes?(" ["+hmsCodes+"]"):""); }
      else if(hms>0){ type="error"; msg="Cảnh báo HMS: "+hmsCodes; }
      else if(prevState==="RUNNING"&&gc==="IDLE"){ type="error"; msg="Máy đang in bỗng DỪNG đột ngột!"; }
      /* Loi kieu tam-dung (ket dun, het nhua...) -> nut Tiep tuc NGAY TRONG banner,
         y het nut 'Resume (problem solved)' tren man hinh may (cung lenh MQTT) */
      if(type) setAlert(type,msg,gc==="PAUSE"); else setAlert(null);
    }
    wasConnected=true;
  }
  prevState=gc;
  const age=s.ts?Math.round((Date.now()/1000)-s.ts):null;
  // May in TAT: mat MQTT, HOAC "connected" nhung >90s khong co tin hieu (dung hinh)
  const offline=!s.connected||(age!==null&&age>90);
  const cb=document.getElementById("connbar");
  if(offline){ cb.className="cb off"; cb.textContent="⏻ MÁY IN ĐANG TẮT hoặc mất kết nối — sẽ tự báo khi máy bật lại"; }
  else { cb.className="cb on"; cb.textContent="● ĐÃ KẾT NỐI — "+(s.name||"máy in")+(age!=null?(" · tín hiệu "+age+"s trước"):""); }
  document.getElementById("foot").innerHTML=(s.connected?'<span class="dot on"></span> Đã kết nối':'<span class="dot off"></span> Mất kết nối')+(age!=null?(" · cập nhật "+age+"s trước"):"");
 }catch(e){
   if(wasConnected) setAlert("warn","Mất kết nối (không tải được dữ liệu)!");
   const cb=document.getElementById("connbar");
   cb.className="cb off"; cb.textContent="⏻ MẤT KẾT NỐI TỚI SERVER (máy tính tắt hoặc rớt mạng)";
   document.getElementById("foot").textContent="Lỗi tải: "+e;
 }
}
document.getElementById("bellchk").checked=BELL;   /* nho lua chon chuong */
tick();setInterval(tick,2000);
</script></body></html>"""


INFO_PAGE = r"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thông tin file in — Bambu A1</title>
<style>
 :root{--bg0:#080b10;--bg1:#0e131b;--card1:#171d28;--card2:#1e2635;--line:#28324a;
   --txt:#eef3fb;--mut:#8ea0b8;--acc:#22c55e;--cyan:#38bdf8;--amb:#f59e0b;
   --sh:0 16px 30px -16px rgba(0,0,0,.85);--hl:inset 0 1px 0 rgba(255,255,255,.06)}
 *{box-sizing:border-box}
 body{margin:0;background:linear-gradient(180deg,var(--bg1),var(--bg0));color:var(--txt);
   font-family:-apple-system,"Segoe UI",Roboto,sans-serif;padding:14px 14px 40px;max-width:620px;margin:auto}
 a.back{color:var(--cyan);text-decoration:none;font-weight:700;font-size:14px;display:inline-flex;align-items:center;gap:6px;margin-bottom:10px}
 h2{font-size:18px;margin:16px 2px 8px}
 .lbl{color:var(--mut);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
 .card{background:linear-gradient(160deg,var(--card2),var(--card1));border-radius:16px;padding:15px;margin:10px 0;box-shadow:var(--sh),var(--hl)}
 .stat{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;text-align:center}
 .stat .n{font-size:22px;font-weight:800} .stat .u{font-size:12px;color:var(--mut)}
 table{width:100%;border-collapse:collapse;font-size:13.5px}
 td{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
 td.k{color:var(--mut);width:55%} td.v{font-weight:700;text-align:right}
 .cmd{display:flex;gap:11px;padding:11px 6px;border-bottom:1px solid var(--line);align-items:flex-start}
 .badge{flex:0 0 auto;background:linear-gradient(160deg,#2a3446,#1a2130);color:var(--cyan);font-weight:800;
   font-size:13px;padding:6px 9px;border-radius:9px;box-shadow:inset 0 1px 0 rgba(255,255,255,.12);min-width:52px;text-align:center}
 .cname{font-weight:800;font-size:14px} .cdesc{font-size:12.5px;color:var(--mut);margin-top:2px}
 .cnt{flex:0 0 auto;color:var(--mut);font-size:12px;font-weight:700;align-self:center}
 .unk{color:var(--amb)}
 details{margin-top:8px} summary{cursor:pointer;color:var(--cyan);font-weight:700;font-size:13px;padding:6px 0}
 .foot{color:var(--mut);font-size:11.5px;text-align:center;margin-top:16px}
 .loading{color:var(--mut);text-align:center;padding:30px}
</style></head><body>
<a class="back" href="/"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M15 6l-6 6 6 6z"/></svg> Về dashboard</a>
<h2 id="title">Thông tin file in</h2>
<div id="root"><div class="loading">Đang tải thông số…</div></div>
<div class="foot">Chú giải G-code tham khảo: github.com/rjduran/bambu-gcode-reference · x1plus Gcode.md</div>
<script>
const LABELS={
 layer_height:["Chiều cao lớp","mm"],initial_layer_print_height:["Lớp đầu tiên","mm"],
 line_width:["Bề rộng đường","mm"],wall_loops:["Số vòng tường",""],
 top_shell_layers:["Lớp mặt trên",""],bottom_shell_layers:["Lớp đáy",""],
 sparse_infill_density:["Mật độ infill",""],sparse_infill_pattern:["Kiểu infill",""],
 outer_wall_speed:["Tốc độ tường ngoài","mm/s"],inner_wall_speed:["Tốc độ tường trong","mm/s"],
 sparse_infill_speed:["Tốc độ infill","mm/s"],internal_solid_infill_speed:["Tốc độ infill đặc","mm/s"],
 top_surface_speed:["Tốc độ mặt trên","mm/s"],travel_speed:["Tốc độ di chuyển","mm/s"],
 outer_wall_acceleration:["Gia tốc tường ngoài","mm/s²"],default_acceleration:["Gia tốc mặc định","mm/s²"],
 enable_support:["Bật support",""],support_type:["Kiểu support",""],support_style:["Style support",""],
 brim_type:["Kiểu brim",""],ironing_type:["Ironing",""],seam_position:["Vị trí đường nối",""],
 wall_generator:["Bộ tạo tường",""],nozzle_temperature:["Nhiệt nozzle","°C"],
 hot_plate_temp:["Nhiệt bàn","°C"],filament_type:["Loại nhựa",""],
 filament_max_volumetric_speed:["Trần lưu lượng","mm³/s"]
};
const ORDER=Object.keys(LABELS);
function fmtVal(v){ if(Array.isArray(v)) return v.join(", "); return String(v); }
function fmtTime(s){ s=parseInt(s); if(isNaN(s))return"—"; const h=Math.floor(s/3600),m=Math.round((s%3600)/60); return (h?h+"h":"")+m+"m"; }
function enableTxt(v){ const s=fmtVal(v); return s==="1"?"Bật":(s==="0"?"Tắt":s); }

async function load(){
  let jr={}, dict={};
  try{ jr=await (await fetch("/api/jobinfo",{cache:"no-store"})).json(); }catch(e){}
  try{ dict=await (await fetch("/api/gcodedict")).json(); }catch(e){}
  const root=document.getElementById("root");
  const info=jr.info;
  document.getElementById("title").textContent="File: "+(jr.file||"—");
  if(!info){ root.innerHTML='<div class="card loading">'+(jr.fetching?"Đang tải file từ máy in… (thử lại sau vài giây)":"Chưa có dữ liệu file in. Cần máy đang in / vừa in một file.")+'</div>'; setTimeout(load,4000); return; }
  const sl=info.slice||{};
  let html='<div class="card stat">'
    +'<div><div class="n">'+(jr.weight!=null?jr.weight:(sl.weight_g??"—"))+'</div><div class="u">gam</div></div>'
    +'<div><div class="n">'+fmtTime(sl.time_s)+'</div><div class="u">thời gian</div></div>'
    +'<div><div class="n">'+(sl.length_m??"—")+'</div><div class="u">mét nhựa</div></div></div>';
  // thong so chinh
  const cfg=info.config||{};
  let rows="";
  for(const k of ORDER){ if(cfg[k]===undefined) continue;
    let v=(k==="enable_support")?enableTxt(cfg[k]):fmtVal(cfg[k]);
    rows+='<tr><td class="k">'+LABELS[k][0]+(LABELS[k][1]?(' ('+LABELS[k][1]+')'):'')+'</td><td class="v">'+v+'</td></tr>';
  }
  if(rows) html+='<h2>Thông số chính</h2><div class="card"><table>'+rows+'</table></div>';
  // lenh gcode
  const cmds=info.commands||{};
  const keys=Object.keys(cmds).sort((a,b)=>cmds[b]-cmds[a]);
  if(keys.length){
    let clist="";
    for(const c of keys){
      const d=dict[c];
      const nm=d?d[0]:"(chưa có chú giải)";
      const ds=d?d[1]:"Lệnh G/M-code — tra thêm ở tài liệu tham khảo bên dưới.";
      clist+='<div class="cmd"><span class="badge">'+c+'</span><div style="flex:1"><div class="cname'+(d?'':' unk')+'">'+nm+'</div><div class="cdesc">'+ds+'</div></div><span class="cnt">'+cmds[c]+'×</span></div>';
    }
    html+='<h2>Lệnh G-code trong file <span class="lbl">('+keys.length+' loại)</span></h2><div class="card" style="padding:6px 12px">'+clist+'</div>';
  }
  // raw
  const allKeys=Object.keys(cfg).sort();
  if(allKeys.length){
    let raw="";
    for(const k of allKeys){ raw+='<tr><td class="k">'+k+'</td><td class="v">'+fmtVal(cfg[k])+'</td></tr>'; }
    html+='<details><summary>Toàn bộ thông số nâng cao ('+allKeys.length+' khoá)</summary><div class="card"><table>'+raw+'</table></div></details>';
  }
  root.innerHTML=html;
}
load();
</script></body></html>"""


FILES_PAGE = r"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>File trên máy — Bambu A1</title>
<style>
 :root{--bg0:#080b10;--bg1:#0e131b;--card1:#171d28;--card2:#1e2635;--line:#28324a;
   --txt:#eef3fb;--mut:#8ea0b8;--acc:#22c55e;--cyan:#38bdf8;--amb:#f59e0b;--red:#ef4444;
   --sh:0 16px 30px -16px rgba(0,0,0,.85);--hl:inset 0 1px 0 rgba(255,255,255,.06)}
 *{box-sizing:border-box}
 body{margin:0;background:linear-gradient(180deg,var(--bg1),var(--bg0));color:var(--txt);
   font-family:-apple-system,"Segoe UI",Roboto,sans-serif;padding:14px 14px 40px;max-width:620px;margin:auto}
 a.back{color:var(--cyan);text-decoration:none;font-weight:700;font-size:14px;display:inline-flex;align-items:center;gap:6px;margin-bottom:10px}
 h2{font-size:18px;margin:12px 2px 6px}
 .busy{background:linear-gradient(160deg,#f59e0b,#b45309);color:#fff;padding:12px;border-radius:12px;font-weight:800;font-size:14px;margin:8px 0}
 .search{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:#0c111a;color:var(--txt);font-size:15px;margin:6px 0 10px}
 .file{display:flex;gap:11px;align-items:center;padding:12px;border-radius:14px;background:linear-gradient(160deg,var(--card2),var(--card1));box-shadow:var(--sh),var(--hl);margin:9px 0}
 .fthumb{flex:0 0 auto;width:58px;height:58px;border-radius:10px;object-fit:contain;background:#0c111a;border:1px solid var(--line)}
 .fmeta{flex:1;min-width:0}
 .fname{font-weight:700;font-size:14px;word-break:break-word}
 .fsub{font-size:11.5px;color:var(--mut);margin-top:3px}
 .tag{display:inline-block;background:#0c111a;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:10.5px;margin-right:5px}
 .pbtn{flex:0 0 auto;background:linear-gradient(160deg,#34d399,#16a34a);color:#fff;border:none;border-radius:12px;
   padding:12px 15px;font-weight:800;font-size:14px;cursor:pointer;display:flex;align-items:center;gap:6px;min-height:46px}
 .pbtn:disabled{opacity:.4;cursor:not-allowed;background:#334155}
 .pbtn svg{width:16px;height:16px;fill:currentColor}
 .loading{color:var(--mut);text-align:center;padding:30px}
 .up{padding:13px;border-radius:14px;background:linear-gradient(160deg,var(--card2),var(--card1));
   box-shadow:var(--sh),var(--hl);margin:10px 0}
 .uprow{display:flex;gap:10px;align-items:center}
 .ubtn{flex:1;background:linear-gradient(160deg,#38bdf8,#0284c7);color:#fff;border:none;border-radius:12px;
   padding:13px;font-weight:800;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px}
 .ubtn:disabled{opacity:.45;cursor:not-allowed;background:#334155}
 .ubtn svg{width:17px;height:17px;fill:currentColor}
 .ubar{height:7px;border-radius:99px;background:#0c111a;border:1px solid var(--line);margin-top:10px;overflow:hidden;display:none}
 .ubar > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#38bdf8,#22c55e);transition:width .2s}
 .uhint{font-size:11.5px;color:var(--mut);margin-top:8px}
 #toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#0b1220;border:1px solid var(--line);
   color:#fff;padding:11px 18px;border-radius:12px;opacity:0;transition:opacity .25s;font-size:14px;box-shadow:var(--sh);z-index:50;max-width:90%}
 #toast.show{opacity:1}
 .foot{color:var(--mut);font-size:11.5px;text-align:center;margin-top:16px}
</style></head><body>
<a class="back" href="/"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M15 6l-6 6 6 6z"/></svg> Về dashboard</a>
<h2>File in trên máy <span id="count" style="color:var(--mut);font-size:13px"></span></h2>
<div id="busy"></div>

<div class="up" id="up">
  <div class="uprow">
    <input type="file" id="fpick" accept=".3mf,.stl" style="display:none" onchange="pick()">
    <button class="ubtn" id="ubtn" onclick="document.getElementById('fpick').click()">
      <svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zM12 2L6.5 9.5h4V16h3V9.5h4L12 2z"/></svg>
      <span id="ulabel">Đẩy file .3mf / .stl từ máy tính lên máy in</span>
    </button>
  </div>
  <div class="ubar" id="ubar"><i id="ufill"></i></div>
  <div class="uhint">Nhận cả 2 loại: file <b>đã slice</b> (.gcode.3mf) → chuyển thẳng xuống máy in;
  file <b>dự án thô</b> (.3mf) → máy tính tự slice bằng Bambu Studio (vài phút) rồi mới chuyển.
  Tất cả qua LAN, không cần cloud. <span id="uhintx" style="color:var(--acc)"></span></div>
</div>

<input class="search" id="q" placeholder="Tìm file…" oninput="render()">
<div id="root"><div class="loading">Đang tải danh sách từ máy…</div></div>
<div class="foot">Nút "In" chỉ hoạt động khi máy RẢNH. Đây là lệnh điều khiển do BẠN bấm.</div>
<div id="toast"></div>
<script>
let FILES=[], BUSY=true, META={}, OBS=null;   // META: path -> true(da slice) / false / null(loi)
function toast(m){const t=document.getElementById("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),3000);}
function fsize(b){ if(!b) return "?"; const m=b/1048576; return m>=1?(m.toFixed(1)+" MB"):((b/1024).toFixed(0)+" KB"); }
function folder(p){ if(p.startsWith("/cache")) return "cache"; if(p.startsWith("/model")) return "model"; return "máy"; }
async function printFile(name,path){
  if(BUSY){ toast("Máy đang bận — chờ in xong mới in file mới"); return; }
  if(!confirm('IN file này?\n\n'+name+'\n\nMáy sẽ bắt đầu in ngay.')) return;
  try{
    const r=await fetch("/api/print",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name,path:path})});
    const j=await r.json();
    toast(j.ok?("Đã gửi lệnh in: "+name):("Lỗi: "+(j.msg||"")));
    setTimeout(()=>location.href="/",1500);
  }catch(e){ toast("Lỗi gửi lệnh: "+e); }
}
function pick(){
  const inp=document.getElementById("fpick"), f=inp.files&&inp.files[0];
  inp.value="";                                  // cho phep chon lai cung file
  if(!f) return;
  if(!/\.(3mf|stl)$/i.test(f.name)){ toast("Chỉ nhận file .3mf hoặc .stl"); return; }
  upload(f);
}
function upload(f){
  const btn=document.getElementById("ubtn"), lab=document.getElementById("ulabel");
  const bar=document.getElementById("ubar"), fill=document.getElementById("ufill");
  btn.disabled=true; bar.style.display="block"; fill.style.width="0";
  const mb=(f.size/1048576).toFixed(1);
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/upload?name="+encodeURIComponent(f.name));
  xhr.upload.onprogress=e=>{
    if(!e.lengthComputable) return;
    const p=Math.round(e.loaded/e.total*100);
    fill.style.width=p+"%";
    // 100% = trinh duyet gui xong cho SERVER. Server con phai ghi tiep vao may in
    // qua FTPS -> phai noi ro, khong de im lang nhu treo.
    lab.textContent = p<100 ? ("Đang đẩy… "+p+"% ("+mb+" MB)")
                            : "Đang ghi vào máy in… (chờ máy xác nhận)";
  };
  xhr.onload=()=>{
    let j={}; try{ j=JSON.parse(xhr.responseText); }catch(e){}
    if(xhr.status===200 && j.ok && j.queued){ pollSlice(); return; }   // chua slice -> server dang slice
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl từ máy tính lên máy in";
    if(xhr.status===200 && j.ok){ toast("Đã đẩy lên máy: "+j.name); load(); }
    else{ toast("Lỗi: "+(j.msg||("HTTP "+xhr.status))); }
  };
  xhr.onerror=()=>{
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl từ máy tính lên máy in";
    toast("Mất kết nối khi đẩy file");
  };
  lab.textContent="Đang đẩy… 0% ("+mb+" MB)";
  xhr.send(f);
}
function fmtStats(s){
  if(!s) return "";
  const p=[];
  if(s.time) p.push("in "+s.time);
  if(s.weight_g) p.push(s.weight_g.toFixed(0)+" g");
  if(s.layers) p.push(s.layers+" lớp");
  if(s.dims) p.push(s.dims.join("×")+" mm");
  if(s.overhang_pct>0) p.push("overhang "+s.overhang_pct+"%");
  return p.join(" · ");
}
async function pollSlice(){
  const btn=document.getElementById("ubtn"), lab=document.getElementById("ulabel");
  const bar=document.getElementById("ubar"), fill=document.getElementById("ufill");
  btn.disabled=true; bar.style.display="block"; fill.style.width="100%";
  try{
    const j=await (await fetch("/api/upstatus",{cache:"no-store"})).json();
    if(j.state==="slicing"||j.state==="pushing"){
      lab.textContent=j.msg||"Đang xử lý…";
      setTimeout(pollSlice, 3000); return;
    }
    btn.disabled=false; bar.style.display="none";
    lab.textContent="Đẩy file .3mf / .stl từ máy tính lên máy in";
    if(j.state==="done"){
      toast("✔ "+j.msg+(j.stats?(" — "+fmtStats(j.stats)):"")); load();
      const hint=document.getElementById("uhintx");
      if(hint&&j.stats) hint.textContent="Kết quả slice: "+fmtStats(j.stats);
    }
    else if(j.state==="error"){ toast("Lỗi: "+j.msg); }
  }catch(e){ setTimeout(pollSlice, 4000); }
}
function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;"); }
// Khong the doan "da slice" bang duoi ten file: Bambu luu file DA slice thanh
// "<ten>.3mf" trong /cache chu khong phai ".gcode.3mf". Phai hoi server (server mo
// zip xem co Metadata/plate_N.gcode khong). Hoi luoi khi cuon toi, roi cache.
function statusHtml(p){
  const s=META[p];
  if(s===undefined) return ' · <span style="color:var(--mut)">đang kiểm tra…</span>';
  if(s===null)      return ' · <span style="color:var(--red)">không đọc được file</span>';
  return s ? ' · <span style="color:var(--acc)">đã slice — in được</span>'
           : ' · <span style="color:var(--amb)">chưa slice (file dự án)</span>';
}
function paint(row){
  const p=row.dataset.path;
  row.querySelector(".fstat").innerHTML=statusHtml(p);
  row.querySelector(".pbtn").disabled = BUSY || META[p]!==true;
}
async function checkMeta(row){
  const p=row.dataset.path;
  if(p in META){ paint(row); return; }
  try{
    const j=await (await fetch("/api/filemeta?path="+encodeURIComponent(p))).json();
    META[p]= j.ok ? !!j.sliced : null;
  }catch(e){ META[p]=null; }
  paint(row);
}
function render(){
  const q=(document.getElementById("q").value||"").toLowerCase();
  const list=FILES.filter(f=>f.name.toLowerCase().includes(q));
  document.getElementById("count").textContent="("+FILES.length+")";
  const root=document.getElementById("root");
  if(!FILES.length){
    root.innerHTML='<div class="loading">Máy chưa có file in nào (hoặc chưa đọc được thẻ SD).<br><br>'
      +'👆 Dùng nút <b>"Đẩy file .3mf"</b> phía trên để đưa file đầu tiên lên — '
      +'file chưa slice máy tính sẽ tự slice giúp bạn.</div>';
    return;
  }
  if(!list.length){ root.innerHTML='<div class="loading">Không có file khớp từ khoá tìm.</div>'; return; }
  let html="";
  for(const f of list){
    html+='<div class="file" data-path="'+esc(f.path)+'">'
      +'<img class="fthumb" loading="lazy" src="/api/filethumb?path='+encodeURIComponent(f.path)+'" onerror="this.style.visibility=\'hidden\'">'
      +'<div class="fmeta"><div class="fname">'+esc(f.name)+'</div>'
      +'<div class="fsub"><span class="tag">'+folder(f.path)+'</span>'+fsize(f.size)
      +'<span class="fstat"></span></div></div>'
      +'<button class="pbtn" disabled onclick="printFile('+JSON.stringify(f.name)+','+JSON.stringify(f.path)+')">'
      +'<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>In</button></div>';
  }
  root.innerHTML=html;
  if(OBS) OBS.disconnect();
  OBS=new IntersectionObserver(es=>{
    for(const e of es) if(e.isIntersecting){ OBS.unobserve(e.target); checkMeta(e.target); }
  },{rootMargin:"200px"});
  root.querySelectorAll(".file").forEach(r=>{ paint(r); OBS.observe(r); });
}
async function load(){
  try{
    const j=await (await fetch("/api/files",{cache:"no-store"})).json();
    FILES=j.files||[]; BUSY=!!j.busy;
    document.getElementById("busy").innerHTML=BUSY?'<div class="busy">Máy đang IN — nút "In" tạm khoá. Xong bản in mới chọn được file mới.</div>':'';
    render();
  }catch(e){ document.getElementById("root").innerHTML='<div class="loading">Lỗi tải danh sách: '+e+'</div>'; }
}
load();
</script></body></html>"""


ANALYZE_PAGE = r"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phân tích file — Bambu A1</title>
<style>
 :root{--bg0:#080b10;--bg1:#0e131b;--card1:#171d28;--card2:#1e2635;--line:#28324a;
   --txt:#eef3fb;--mut:#8ea0b8;--acc:#22c55e;--cyan:#38bdf8;--amb:#f59e0b;--red:#ef4444;
   --sh:0 16px 30px -16px rgba(0,0,0,.85);--hl:inset 0 1px 0 rgba(255,255,255,.06)}
 *{box-sizing:border-box}
 body{margin:0;background:linear-gradient(180deg,var(--bg1),var(--bg0));color:var(--txt);
   font-family:-apple-system,"Segoe UI",Roboto,sans-serif;padding:14px 14px 40px;max-width:680px;margin:auto}
 a.back{color:var(--cyan);text-decoration:none;font-weight:700;font-size:14px;display:inline-flex;align-items:center;gap:6px;margin-bottom:10px}
 h2{font-size:18px;margin:12px 2px 6px} h3{font-size:14px;margin:18px 2px 8px;color:var(--cyan)}
 .card{padding:14px;border-radius:14px;background:linear-gradient(160deg,var(--card2),var(--card1));
   box-shadow:var(--sh),var(--hl);margin:10px 0}
 .btn{width:100%;background:linear-gradient(160deg,#38bdf8,#0284c7);color:#fff;border:none;border-radius:12px;
   padding:14px;font-weight:800;font-size:14px;cursor:pointer}
 .btn:disabled{opacity:.45;background:#334155;cursor:not-allowed}
 .btn.go{background:linear-gradient(160deg,#34d399,#16a34a);margin-top:10px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:4px}
 .kv{background:#0c111a;border:1px solid var(--line);border-radius:10px;padding:9px 11px}
 .kv b{display:block;font-size:16px;margin-top:2px}
 .kv span{font-size:11px;color:var(--mut)}
 .iss,.tip{border-radius:10px;padding:10px 12px;margin:7px 0;font-size:13px;line-height:1.5}
 .iss{background:rgba(239,68,68,.12);border-left:3px solid var(--red)}
 .tip{background:rgba(34,197,94,.12);border-left:3px solid var(--acc)}
 table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
 td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
 th{color:var(--mut);font-weight:600}
 .bad{color:var(--red);font-weight:700} .good{color:var(--acc);font-weight:700}
 .mut{color:var(--mut);font-size:12px}
 #toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#0b1220;border:1px solid var(--line);
   color:#fff;padding:11px 18px;border-radius:12px;opacity:0;transition:opacity .25s;font-size:14px;z-index:50;max-width:90%}
 #toast.show{opacity:1}
</style></head><body>
<a class="back" href="/"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M15 6l-6 6 6 6z"/></svg> Về dashboard</a>
<h2>Phân tích file in <span class="mut">· .3mf và .stl</span></h2>

<div class="card">
  <input type="file" id="fp" accept=".3mf,.stl" style="display:none" onchange="go()">
  <button class="btn" id="bt" onclick="document.getElementById('fp').click()">
    <span id="lb">Chọn file .3mf / .stl để phân tích</span></button>
  <div class="mut" style="margin-top:8px">Máy tính phân tích: kích thước · overhang · support · thử xoay ·
  Variable Layer Height · trần lưu lượng. <b>Chỉ tính toán — không đụng tới máy in.</b></div>
</div>
<div id="out"></div>

<details class="card" style="margin-top:14px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">📚 Mẹo gỡ support đẹp như mặt kính — PETG interface cho PLA</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>Nguyên lý:</b> PLA và PETG <b>không dính nhau về hóa học</b>. Bình thường support cùng
  vật liệu phải chừa khe 0.2mm (Top Z distance) để bóc ra — chính khe đó làm mặt dưới rỗ.
  Đổi lớp tiếp xúc (interface) sang nhựa đối ứng thì ép khít <b>0mm</b> vẫn bóc rời →
  mặt dưới bóng như mặt trên.<br><br>
  <b>5 bước trong Bambu Studio (tab Support, bật Advanced):</b><br>
  1️⃣ Filament for Supports → <b>Support/raft interface = PETG</b> (đúng slot AMS đang nạp)<br>
  2️⃣ <b>Top Z distance = 0</b> · Bottom Z distance = 0<br>
  3️⃣ <b>Top interface spacing = 0</b> (interface đặc 100%)<br>
  4️⃣ Interface pattern = <b>Rectilinear Interlaced</b><br>
  5️⃣ TẮT <b>Independent support layer height</b> (khỏi bị làm tròn lệch lớp)<br><br>
  ✅ Hub <b>TỰ ÁP</b> bộ này khi bạn upload .3mf có khai báo PETG trong Project Filaments
  (thân PLA) — hoặc ngược lại PLA làm interface cho thân PETG.<br>
  🔁 Máy chỉ có 1 loại nhựa → hub fallback interface cùng vật liệu đúng slot thân in,
  khe an toàn 0.2mm.<br>
  ⚠️ <b>Cấm</b> để Z distance = 0 khi interface CÙNG vật liệu — support dính chết vào model.<br><br>
  <b>Cả 4 khay đều PLA (không có nhựa đối ứng) thì set thế này:</b><br>
  • Support/raft interface = <b>Default</b> (hoặc đúng khay thân in)<br>
  • <b>Top Z distance = 0.2mm</b> (vẫn khó bóc → tăng 0.25; đây là khe hở sống còn)<br>
  • Bottom Z distance = 0.2 · Top interface spacing = <b>0.5</b> (KHÔNG để 0)<br>
  • Interface pattern = Rectilinear Interlaced · Top interface layers = 2-3<br>
  → Bóc được nhưng mặt dưới hơi rỗ — đó là giới hạn vật lý của cùng nhựa; muốn bóng
  như mặt trên bắt buộc phải có nhựa đối ứng. Hub tự set đúng bộ này khi phân tích file.<br><br>
  ⏱️ Giá phải trả (trick PETG): single-nozzle đổi nhựa mỗi lớp interface → tốn thời gian + nhựa purge.<br><br>
  <b>An toàn trước khi in model lớn (case thất bại cộng đồng đã quét):</b><br>
  🧪 Lần đầu dùng trick: in <b>thử 1 miếng nhỏ có overhang</b> (~20 phút) trước, đừng đặt cược model 8 tiếng.<br>
  🔍 Import preset xong phải <b>chọn nó ở dropdown Process</b> — import KHÔNG tự áp; bóc không ra đa số do preset chưa được chọn, Z distance vẫn của preset cũ.<br>
  💧 Giữ nguyên flush volume Bambu tự tính khi đổi PLA↔PETG — giảm flush quá tay thì vùng nhựa trộn có thể bám nhẹ.<br>
  🔗 Kiểm chứng: forum.bambulab.com/t/5942 · 3djake.ie (PLA trick) · wiki.bambulab.com/en/software/bambu-studio/Seam
  </div>
</details>

<details style="max-width:900px;margin:14px auto 0;padding:0 16px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">📐 Lắp 2 part KHÍT / LỎNG (nắp-lọ, hoa-khuôn) — chỉnh khe hở ±mm</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>Triệu chứng:</b> part cắm vào khuôn / lỗ <b>khít quá không lọt</b>, hoặc lỏng rơi ra.<br>
  <b>Cần chỉnh:</b> <b>X-Y size compensation</b> — KHÔNG phải Scale (Scale đổi cả kích thước + méo hoa văn).<br><br>
  <b>Vị trí Bambu Studio → tab Quality ▸ mục Precision:</b><br>
  • <b>X-Y contour compensation</b> (biên ngoài): <b>ÂM</b> = co model nhỏ lại → lọt dễ · <b>DƯƠNG</b> = nở ra, chặt hơn.
  Ví dụ bông hoa khít quá → <b>-0.2</b> (co mỗi cạnh 0.2mm ≈ giảm 0.4mm đường kính; chỉ hơi khít thì -0.1 đủ).<br>
  • <b>X-Y hole compensation</b> (lỗ): <b>DƯƠNG</b> = lỗ to ra → trục / part cắm vào dễ hơn.<br><br>
  <b>Khe hở lắp ghép FDM chuẩn (Bambu A1, nozzle 0.4):</b> khít bấm nhẹ ~0.1 · trượt êm ~0.2 · lỏng thoải mái ~0.3mm.<br>
  <b>Chỉnh RIÊNG 1 part</b> (chỉ hoa, không đụng khuôn): phải chuột lên object → <b>Add settings</b> → thêm
  <i>X-Y contour compensation</i> → đặt -0.2 riêng cho part đó.<br>
  ⚠️ Compensation giữ nguyên chi tiết/hoa văn; Scale −% thì méo cả bông hoa. Khít nhiều thì hạ tiếp -0.05 mỗi lần.<br>
  🔗 Kiểm chứng: wiki.bambulab.com (Precision) — in thử <b>1 cánh hoa</b> trước khi in cả bộ.
  </div>
</details>

<details style="max-width:900px;margin:14px auto 0;padding:0 16px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">📚 Fix mặt trên lấm tấm / lỗ li ti / vân thưa — vị trí chỉnh chính xác</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>Triệu chứng:</b> mặt trên cùng có lỗ li ti, lấm tấm, vân thưa (đường in không khít nhau),
  rõ nhất ở góc nhọn và vùng hẹp.<br>
  <b>Thủ phạm:</b> đường in tròn đầu — chỗ queo và đầu mút để lại khe; nozzle to thì khe to
  (đồng thuận forum Bambu, thread 14.7k view).<br><br>
  <b>Cách chỉnh — theo tab Bambu Studio (bật Advanced):</b><br>
  1️⃣ <b>Quality › Line width › Top surface = 0.25</b> mm (nozzle 0.4) — đường mảnh nhét kín khe. Đây là fix số 1.<br>
  2️⃣ <b>Strength › Top/bottom shells › Top surface pattern = Monotonic line</b> — đi đường một chiều, khít nhất.<br>
  3️⃣ <b>Quality › Wall generator = Arachne</b> — độ rộng biến thiên, nhét được góc nhọn.<br>
  4️⃣ <b>Speed › Other layers speed › Top surface ≤ 150</b> mm/s — chậm để nhựa dàn đều.<br>
  5️⃣ <b>Strength › Top/bottom shells › Top shell layers ≥ 5</b> (đủ dày để lấp) — hub tính theo độ dày 1.0mm.<br>
  6️⃣ Còn rỗ nữa → <b>Quality › Ironing › Ironing Type = Top surfaces</b> (ủi phẳng, đánh đổi thời gian).<br><br>
  🔧 <b>Nguyên nhân GỐC = dòng chảy chưa chuẩn.</b> Preset chỉ giảm; muốn HẾT hẳn phải hiệu chỉnh cho ĐÚNG cuộn nhựa:<br>
  • Máy/ app: <b>Calibration › Flow Dynamics (PA)</b> + <b>Flow Rate</b> — mỗi cuộn/màu chạy 1 lần, lưu lại.<br>
  • Nhiệt độ cao hơn ~5-10°C cũng giúp nhựa dàn (PLA 220→230).<br><br>
  ✅ Hub <b>TỰ ÁP</b> mục 1-5 khi phân tích (mọi chế độ); mục 6 bật ở chế độ Đẹp khi có mặt phẳng lớn.<br>
  🔗 Kiểm chứng: forum.bambulab.com/t/top-surface-has-tiny-holes-and-gaps/5489 (14.7k view, đồng thuận frank.d/albin/Flashy_DE)
  </div>
</details>

<details style="margin-top:12px;background:#0f1523;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:12px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">📖 CÓ PHẢI CỨ TĂNG LÀ TỐT? — nguyên tắc chỉnh thông số (đọc kỹ trước khi tự vặn)</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>KHÔNG.</b> Đa số thông số có VÙNG TỐI ƯU — tăng quá tay còn hại hơn. Chia 4 nhóm:<br><br>
  <b>① Có VÙNG TỐI ƯU — tăng quá = XẤU (nguy hiểm nhất):</b><br>
  • <b>Bridge flow</b>: cao quá → vón cục, chảy xệ (PLA ~1.5, PETG chỉ ~1.05 — KHÁC nhau, không phải cao hơn là tốt).<br>
  • <b>Nhiệt độ</b>: cao quá → chảy nhão, rủ overhang, kéo sợi; thấp quá → yếu lớp. Có điểm ngọt.<br>
  • <b>Quạt làm mát</b>: PLA thích tối đa, nhưng ABS/ASA bật nhiều → nứt/tách lớp. Ngược nhau theo nhựa.<br>
  • <b>Gia tốc</b>: cao → nhanh nhưng RUNG/lệch trục (vật cao); thấp → sạch nhưng chậm. Đánh đổi, không "cao là ngon".<br>
  • <b>Retraction (rút)</b>: nhiều quá → kẹt/mài nhựa, thiếu → kéo sợi.<br><br>
  <b>② LỢI ÍCH GIẢM DẦN — tăng thêm chỉ tốn công/nhựa:</b><br>
  • <b>Infill</b>: 15-25% đã chắc; >40-50% gần như không bền thêm mà lâu + nặng (Markforged/Ultimaker). Độ bền do THÀNH + hướng in quyết định, không phải ruột.<br>
  • <b>Số thành (walls)</b>: 3-5 là điểm ngọt; >6 bền không đáng kể mà chậm (Sandwich Panel Theory).<br>
  • <b>Lớp mặt trên</b>: đủ lấp kín (~5-6 lớp ở 0.2mm) là dừng; dư chỉ phí giờ.<br><br>
  <b>③ TĂNG = TỐT nhưng ĐÁNH ĐỔI (cân theo nhu cầu):</b><br>
  • <b>Brim</b> rộng: bám chắc hơn nhưng khó gỡ + phải gọt via.<br>
  • <b>Support</b> dày: đỡ tốt hơn nhưng tốn nhựa + để lại sẹo mặt.<br><br>
  <b>④ TĂNG = ẢO (máy chặn):</b><br>
  • <b>Tốc độ</b> đặt cao hơn TRẦN LƯU LƯỢNG (nhựa mm³/s ÷ (layer × line width)) = số ảo, máy tự hãm. Hub đã tính trần này.<br><br>
  🎯 <b>Thứ tự ưu tiên khi muốn CHẮC/ĐẸP hơn</b> (cộng đồng đồng thuận): <b>Hướng in > Số thành > Nhựa+calib > Ruột.</b> Đổi hướng + tăng thành hiệu quả hơn tăng infill nhiều.<br>
  🔗 Nguồn: Ultimaker/Markforged (infill diminishing returns), wiki Bambu (bridge/flow/tốc độ), r/3Dprinting (orientation > walls > infill).
  </div>
</details>

<details style="margin-top:12px;background:#0f1523;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:12px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">🌡️ Nhão / rủ overhang / kéo sợi — do NHIỆT hay ẨM hay QUẠT?</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>3 thủ phạm dễ nhầm nhau — phân biệt trước khi chỉnh:</b><br>
  • <b>NHIỆT cao</b> → mặt bóng nhẫy, overhang rủ/xệ, blob ở góc, kéo sợi DÀY. Nhựa ra chảy loãng.<br>
  • <b>ẨM (nhựa hút nước)</b> → kéo sợi MẢNH như tơ + xù lông + nghe <b>lách tách/xì</b> ở đầu phun, mặt rỗ li ti. PLA Lite rẻ, hút ẩm rất nhanh, khay AMS Lite KHÔNG sấy.<br>
  • <b>THIẾU QUẠT</b> → overhang rủ dù nhiệt đúng, chi tiết nhỏ dính chảy (lớp chưa kịp nguội đã in lớp kế).<br><br>
  <b>Cách thử nhanh:</b> đùn 100mm nhựa giữa không khí → sợi có bọt li ti/lởm chởm = ẨM; sợi bóng mượt chảy nhanh = NHIỆT cao.<br><br>
  <b>Fix cho PLA Lite (số official Bambu GitHub):</b><br>
  1️⃣ <b>Nhiệt đầu phun chuẩn A1 = 220°C</b> (official: 'Bambu PLA Lite @BBL A1' OVERRIDE bản @base 210 lên 220 — đã đối chiếu cả 2 tầng GitHub). Để 220 là ĐÚNG chuẩn máy bạn. Filament ▸ Nozzle temperature.<br>
  2️⃣ Overhang rủ / kéo sợi → hạ DẦN <b>210-215°C</b> (hướng tuning, không phải chuẩn) + <b>quạt 100%</b> (PLA thích tối đa) + hạ tốc overhang (hub đã set 0/50/30/10).<br>
  3️⃣ Kéo sợi + xù lông → <b>SẤY nhựa 50-55°C trong 8h</b> (máy sấy / nồi chiên không dầu hé) rồi cất kèm hút ẩm. Đây là fix số 1 nếu là ẩm — chỉnh nhiệt không cứu được ẩm.<br>
  4️⃣ Bàn 65°C (textured PEI) cho PLA là ĐÚNG — không liên quan nhão/rủ.<br>
  5️⃣ Muốn hết hẳn: <b>Calibration ▸ Temp tower</b> (in tháp nhiệt 190-220) → chọn tầng đẹp nhất cho đúng cuộn.<br><br>
  ⚠️ <b>Với cuộn PLA Lite vàng của bạn:</b> kéo sợi + xù (ảnh bạn gửi trước) là chữ ký ĐIỂN HÌNH của nhựa ẨM, không phải chỉ nhiệt. Sấy trước, rồi mới giảm nhiệt dần 220→210. Đừng chỉ vặn nhiệt.<br><br>
  <b>❓ Tại sao ĐẦU in đẹp, tới ~2/3 (70%) mới hỏng?</b> — "2/3 mới hỏng" LOẠI TRỪ ẩm/nhiệt toàn cục (mấy cái đó hỏng từ lớp 1). Là lỗi PHỤ THUỘC CHIỀU CAO, 3 khả năng:<br>
  • <b>Tích nhiệt (heat soak):</b> phần dưới in xong vẫn ấm, càng cao càng bí nhiệt → lớp trên chưa nguội đã in lớp kế → overhang bắt đầu xệ. Cùng 1 overhang in đẹp dưới nhưng rủ trên. Fix: min layer time 10s + quạt 100% + hạ nhiệt.<br>
  • <b>Hình học đổi ở 2/3:</b> 2/3 dưới là thành thẳng (dễ), 1/3 trên mới có mái nghiêng/khe (overhang tập trung). Fix: support chỗ đó / XOAY nằm. <i>Hub tự dò: nếu >40% overhang dồn ở 1/3 trên sẽ cảnh báo khi phân tích.</i><br>
  • <b>Rung/lệch trục ở cao:</b> nếu là NGHIÊNG/DỊCH cả khối (không phải chảy xệ) → xem card vật cao.<br>
  🔗 Nguồn: filament preset official bambulab/BambuStudio — 2 TẦNG: 'PLA Lite @base' 210°C nhưng bản máy 'PLA Lite @BBL A1' override 220°C/bàn 65 (tra cả 2 tầng mới đúng); wiki Bambu filament drying + heat-creep.
  </div>
</details>

<details style="margin-top:12px;background:#0f1523;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:12px">
  <summary style="cursor:pointer;font-weight:700;font-size:15px">🧱 PLA Matte / PLA Lite ĐEN hay KẸT NHỰA — thông số nào sai + cách fix</summary>
  <div class="mut" style="margin-top:10px;line-height:1.7">
  <b>Triệu chứng kẹt:</b> thiếu đùn (gap trên model) hoặc KHÔNG ra nhựa dù đầu phun vẫn chạy.<br><br>
  <b>Vì sao Matte & đen dễ kẹt hơn PLA thường (wiki Bambu + cộng đồng A1):</b><br>
  • <b>Matte có HẠT ĐỘN</b> tạo bề mặt lì → mài mòn + tích cặn trong nozzle như nhựa sợi (CF). Đen thì <b>bột màu carbon</b> cũng gây tích cặn + hút nhiệt → cộng đồng A1 báo đen kẹt nhiều hơn.<br>
  • <b>Heat creep (bò nhiệt):</b> PLA mềm, phình trong ống dẫn phía trên vùng nóng — bàn nóng + in chậm → nhựa buckling/nghẽn ở extruder.<br>
  • <b>ẨM:</b> hơi nước sôi trong nozzle → bọt, tắc từng phần.<br>
  • <b>Nhiệt THẤP:</b> chưa chảy đủ → under-melt → tắc (ngược với nhão).<br>
  • <b>Tốc quá nhanh</b> (Ludicrous): không kịp chảy → tắc từng phần.<br><br>
  <div style="background:rgba(34,197,94,.12);border-left:3px solid #22c55e;border-radius:8px;padding:9px 11px;margin:6px 0 10px">
  <b>✅ SỐ AN TOÀN CỘNG ĐỒNG dùng cho Matte / đen (A1) — chốt từ forum + Reddit:</b><br>
  • <b>Nhiệt đầu phun 230°C</b> (KHÔNG để 220 stock, tuyệt đối không mượn profile Lite). Ca cứng đầu tăng dần +5°C tới <b>≤255°C</b>. <span class="mut">— Olias, DRCGRAPIX, Kevin1973</span><br>
  • <b>Max volumetric speed HẠ còn ~12 mm³/s</b> (stock PLA ~21-22 — Matte chảy chậm hơn, giảm ~½ là chống kẹt hiệu quả nhất). <span class="mut">— oksanka 22→12; Olias: Generic PLA để ½ flow của Bambu PLA</span><br>
  • <b>Flow ratio ≈ 0.98-0.99</b> (Matte đùn hơi thiếu so với PLA thường). <span class="mut">— Kevin1973: Elegoo Matte Gray 230°C + flow 0.99 hết lỗi</span><br>
  • <b>Bàn 55°C</b> (đầu bảng PLA 45-60°C của wiki — đen hút nhiệt, để thấp giảm heat creep). Khung A1 HỞ sẵn nên PLA gần như không heat-creep, đây là lợi thế.<br>
  • <b>Sấy trước khi in:</b> cộng đồng đo cuộn Bambu Matte ra lò đã ẩm ~15g/cuộn, đẩy ẩm AMS 10%→30%+. Trắng in đẹp, ĐEN/xám hay lỗi nhất.
  </div>
  <b>Thông số HAY SAI (kiểm lại):</b><br>
  1️⃣ <b>Dùng NHẦM profile nhựa</b> → sai nhiệt. Matte cần <b>230°C</b> (hạt độn cản chảy, cần NÓNG hơn Lite/Basic 220); dùng profile Lite/generic cho Matte = quá nguội → under-melt kẹt. Chọn ĐÚNG "Bambu PLA Matte" / "PLA Lite" trong Project Filaments.<br>
  2️⃣ <b>Max volumetric speed</b>: Matte chảy chậm hơn — đặt tốc cao vượt trần → kẹt. Hạ trần còn ~12 (Filament ▸ Setting Overrides ▸ Max volumetric speed). Hub đã tính trần theo cuộn.<br>
  3️⃣ <b>Chưa COLD PULL:</b> Matte/đen tích cặn — wiki khuyên cold pull ≥1 lần/tháng (làm nóng 260°C, để nguội một phần, rút ra kéo theo cặn). Đây là fix số 1 cho kẹt lặp lại.<br>
  4️⃣ Bàn nóng + phòng nóng → heat creep: PLA nên MỞ thoáng, không che kín.<br><br>
  <b>Fix nhanh khi ĐANG kẹt cứng:</b> nâng nozzle <b>280-300°C</b> để hoá lỏng cục Matte kẹt trong nozzle rồi đùn/rút mạnh (Reddit r/BambuLab) → Cold pull (A1: wiki.bambulab.com/en/a1-mini/troubleshooting/nozzle-clog) → sấy cuộn → chọn đúng profile → chậm lại (đừng Ludicrous) → kiểm silicone sock còn ôm nozzle.<br>
  🔗 Nguồn: forum.bambulab.com/t/matte-filament-poor-print-quality/30567 + wiki heat-creep (A1 khung hở) + r/BambuLab (đen kẹt, 300°C hoá lỏng) + how_to_avoid_nozzle_clogs.
  </div>
</details>

<div id="toast"></div>
<script>
let FILE=null;
function toast(m){const t=document.getElementById("toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),3500);}
/* T3: tai preset FILAMENT .json (file rieng — user chot khong nhet vao 3mf) */
async function dlFil(){
  const sel=document.getElementById("filsel"); if(!sel||!sel.value) return;
  try{
    const v=sel.value;
    const qs=v.indexOf("slot:")===0?("slot="+v.slice(5)):("fil="+encodeURIComponent(v));
    const j=await (await fetch("/api/filpreset?"+qs)).json();
    if(!j.ok){toast(j.msg||"Không sinh được preset");return;}
    const info=document.getElementById("filinfo");
    if(info) info.innerHTML=(j.verified
        ?'✓ inherits <b>'+esc(j.inherits)+'</b> — tên preset gốc ĐÃ XÁC MINH'
        :'⚠ inherits <b>'+esc(j.inherits)+'</b> — tên suy luận, nếu Studio báo lỗi import hãy kiểm tra tên preset gốc')
      +'<br>'+esc(j.why||'');
    _saveJson(j.preset, ((j.preset&&j.preset.name)||"LP-filament-safe")+".json");
    toast("Đã tải preset "+(j.key||sel.value));
  }catch(e){toast("Lỗi tải preset: "+e);}
}
/* Tai preset nhua DA SUA cho khay dang chon (so an toan cua cuon that + mau khay) */
async function dlFilFix(){
  const fc=window.__filCheck; if(!fc||!fc.key){toast("Chưa xác định được nhựa khay");return;}
  try{
    const j=await (await fetch("/api/filpreset?fil="+encodeURIComponent(fc.key))).json();
    if(!j.ok){toast(j.msg||"Không sinh được preset");return;}
    if(fc.color){ j.preset.filament_colour=[fc.color]; j.preset.default_filament_colour=[fc.color]; }
    _saveJson(j.preset, ((j.preset&&j.preset.name)||("LP-"+fc.key))+".json");
    toast("Đã tải preset nhựa ĐÃ SỬA: "+(j.key||fc.key));
  }catch(e){toast("Lỗi tải preset: "+e);}
}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");}
/* PANEL kieu Bambu Prepare (2026-07-19) — muc + hang nhan-trai/o-phai + o so */
const IST='style="background:#0f172a;color:#e2e8f0;border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:6px 8px;font-size:13px"';
function bSec(t){ return '<div style="font-weight:700;font-size:13.5px;margin:15px 0 4px;padding-bottom:5px;border-bottom:1px solid rgba(255,255,255,.12)">'+t+'</div>'; }
function bRow(k,c){ return '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)">'
  +'<span class="mut" style="font-size:13px">'+k+'</span><span style="text-align:right;white-space:nowrap">'+c+'</span></div>'; }
function nIn(id,mn,mx,st){ return '<input id="'+id+'" type="number" min="'+mn+'" max="'+mx+'" step="'+st+'" onchange="this.dataset.touched=1" '+IST+' style="width:78px;text-align:right">'; }
function revRow(k,c){ return bRow(k,c); }   /* tuong thich cu */
function _v1(x){ return Array.isArray(x)?x[0]:x; }
function fillReview(){
  const p=window.__preset||{}, g=id=>document.getElementById(id), sv=(id,v)=>{const e=g(id);if(e&&v!=null&&v!=="")e.value=v;};
  sv("ov_outer",_v1(p.outer_wall_speed)); sv("ov_init_layer",_v1(p.initial_layer_print_height));
  sv("ov_brim_w",_v1(p.brim_width)); sv("ov_skirt",_v1(p.skirt_loops));
  sv("ov_sup_angle",_v1(p.support_threshold_angle)||30);
  sv("ov_sup_ztop",_v1(p.support_top_z_distance)); sv("ov_sup_zbot",_v1(p.support_bottom_z_distance));
  sv("ov_sup_ispacing",_v1(p.support_interface_spacing)); sv("ov_sup_itop",_v1(p.support_interface_top_layers));
  const setSel=(id,v)=>{const e=g(id);if(e&&v)e.value=v;};
  setSel("ov_brim",_v1(p.brim_type)); setSel("ov_sup_type",_v1(p.support_type));
  setSel("ov_sup_style",_v1(p.support_style)); setSel("ov_sup_ipattern",_v1(p.support_interface_pattern));
  setSel("ov_sup_ifil",_v1(p.support_interface_filament)); setSel("ov_sup_basefil",_v1(p.support_filament));
  const es=_v1(p.enable_support); if(g("ov_support")) g("ov_support").checked=(es===true||es==="1"||es===1);
  const op=_v1(p.support_on_build_plate_only); if(g("ov_sup_onplate")) g("ov_sup_onplate").checked=(op==="1"||op===1||op===true);
  const fs=window.__filSelInfo||{}, s=g("revsum");
  if(s){ s.innerHTML='Đang đặt: layer <b>'+esc(_v1(p.layer_height)||"?")+'mm</b> · tường <b>'+esc(_v1(p.wall_loops)||"?")+'</b>'
    +(fs.mvs?(' · trần chảy <b>'+esc(fs.mvs)+' mm³/s</b>'):'')+(fs.temp?(' · nhiệt <b>'+esc(fs.temp)+'°C</b>'):'')
    +' — sửa ô nào thì áp ô đó.'; }
}
function onSmode(){ const s=document.getElementById("revsum"); if(s) s.innerHTML='Đổi chế độ → bấm <b>Slice lại</b> để cập nhật số. Các ô bạn đã sửa vẫn giữ.'; }
/* Chon cach lam support -> NAP thang vao cac o Support cua panel (1 nguon), danh dau touched */
function onSupStrat(){
  const v=(document.getElementById("supstrat")||{}).value||"", g=id=>document.getElementById(id);
  window.__supStrat=v;
  const s=(window.__supStrats||[]).find(x=>x.id===v);
  const el=document.getElementById("supstratwhy");
  if(el) el.textContent = s ? s.why : "Giữ nguyên cấu hình support trong file.";
  if(!s) return;
  const k=s.keys||{}, put=(id,val)=>{const e=g(id);if(e&&val!=null){e.value=val;e.dataset.touched=1;}};
  if(g("ov_support")){g("ov_support").checked=true;g("ov_support").dataset.touched=1;}
  put("ov_sup_ztop",k.support_top_z_distance); put("ov_sup_zbot",k.support_bottom_z_distance);
  put("ov_sup_ispacing",k.support_interface_spacing); put("ov_sup_itop",k.support_interface_top_layers);
  put("ov_sup_ipattern",k.support_interface_pattern);
  put("ov_sup_ifil",k.support_interface_filament); put("ov_sup_basefil",k.support_filament);  /* nap dropdown nhua support */
}
function ovQS(){
  const g=id=>document.getElementById(id), t=id=>{const e=g(id);return e&&e.dataset&&e.dataset.touched;};
  const S=(id,key)=>{ if(t(id)&&g(id).value!=="") return "&"+key+"="+encodeURIComponent(g(id).value); return ""; };
  const C=(id,key)=>{ return t(id) ? "&"+key+"="+(g(id).checked?"1":"0") : ""; };
  let q="";
  q+=S("ov_layer","ov_layer")+S("ov_init_layer","ov_init_layer")+S("ov_outer","ov_outer");
  q+=S("ov_brim","ov_brim")+S("ov_brim_w","ov_brim_w")+S("ov_skirt","ov_skirt");
  q+=C("ov_support","ov_support")+S("ov_sup_type","ov_sup_type")+S("ov_sup_style","ov_sup_style");
  q+=S("ov_sup_angle","ov_sup_angle")+C("ov_sup_onplate","ov_sup_onplate");
  q+=S("ov_sup_ztop","ov_sup_ztop")+S("ov_sup_zbot","ov_sup_zbot")+S("ov_sup_itop","ov_sup_itop");
  q+=S("ov_sup_ispacing","ov_sup_ispacing")+S("ov_sup_ipattern","ov_sup_ipattern");
  q+=S("ov_sup_ifil","ov_sup_ifil")+S("ov_sup_basefil","ov_sup_basefil");   // nhua support chon tu AMS
  return q;
}
function reset(msg){
  const bt=document.getElementById("bt"), lb=document.getElementById("lb");
  bt.disabled=false; lb.textContent="Chọn file .3mf / .stl để phân tích";
  if(msg) toast(msg);
}
function go(){
  const inp=document.getElementById("fp"); FILE=inp.files&&inp.files[0]; inp.value="";
  if(!FILE) return;
  send(null);
}
/* Tab khay: phan tich lai theo khay N — FILE van con trong bien, gui lai kem plate=.
   Doi KHAY -> RESET nhua ve mac dinh THEO KHAY do (khay 2 vat den -> tu nhay ve khe
   den), roi user chon khac duoc (bug user 2026-07-19). */
function rePlate(n){
  if(!FILE){toast("Chọn lại file — trình duyệt không còn giữ nội dung");return;}
  window.__filReq={};        // xoa override -> server chon nhua mac dinh theo khay moi
  send(n);
}
/* Nhua dan dat process (#2/#3): query string cua nhua dang chon — dung chung cho
   analyze/optimize/slice de MOI noi cung 1 cuon. */
function filQS(){
  const r=window.__filReq||{};
  if(r.slot) return "&slot="+encodeURIComponent(r.slot);
  if(r.fil) return "&fil="+encodeURIComponent(r.fil)+(r.color?("&color="+encodeURIComponent(r.color)):"");
  return "";
}
/* Doi nhua o dropdown -> phan tich lai process theo cuon do */
function reFil(){
  if(!FILE){toast("Chọn lại file để đổi nhựa");return;}
  const v=(document.getElementById("filsel")||{}).value||"";
  const col=(document.getElementById("filcolor")||{}).value||"";
  window.__filReq = v.indexOf("slot:")===0 ? {slot:+v.slice(5)} : {fil:v, color:col};
  toast("Phân tích lại theo nhựa: "+v);
  send(window.__plate);
}
/* Doi MAU (#4) -> giu nhua hien tai, phan tich lai voi mau moi (che do generic) */
function reColor(){
  if(!FILE){toast("Chọn lại file để đổi màu");return;}
  const col=(document.getElementById("filcolor")||{}).value||"";
  const key=window.__filKey||"";
  if(!key){ toast("Chưa xác định được loại nhựa để gắn màu"); return; }
  window.__filReq={fil:key, color:col};
  toast("Đổi màu → "+col);
  send(window.__plate);
}
function send(plate){
  const bt=document.getElementById("bt"), lb=document.getElementById("lb");
  bt.disabled=true;
  document.getElementById("out").innerHTML="";
  const mb=(FILE.size/1048576).toFixed(1);
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/analyze?name="+encodeURIComponent(FILE.name)+(plate?("&plate="+plate):"")+filQS());
  xhr.upload.onprogress=e=>{
    if(!e.lengthComputable) return;
    const p=Math.round(e.loaded/e.total*100);
    lb.textContent = p<100 ? ("Đang gửi… "+p+"% ("+mb+" MB)") : "Đã gửi — server bắt đầu phân tích…";
  };
  xhr.onload=()=>{
    let j={}; try{ j=JSON.parse(xhr.responseText); }catch(e){}
    if(xhr.status===200 && j.ok && j.queued){ pollAn(); }
    else reset("Lỗi: "+(j.msg||("HTTP "+xhr.status)));
  };
  xhr.onerror=()=>reset("Mất kết nối khi gửi file");
  lb.textContent="Đang gửi… 0% ("+mb+" MB)";
  xhr.send(FILE);
}
async function pollAn(){
  const lb=document.getElementById("lb");
  try{
    const j=await (await fetch("/api/anstatus",{cache:"no-store"})).json();
    if(j.state==="running"){
      lb.textContent=(j.msg||"Đang phân tích…")+" ("+(j.name||"")+")";
      setTimeout(pollAn, 2000); return;
    }
    if(j.state==="done" && j.result){ reset(); render(j.result); }
    else reset("Lỗi: "+(j.msg||"không rõ"));
  }catch(e){ setTimeout(pollAn, 3000); }
}
function render(j){
  const m=j.mesh||{}; let h="";
  /* Khay dang chon — optimize/slice/push deu di theo khay nay (review HIGH-3) */
  window.__plate=+j.plate||1; window.__platesN=(j.plates||[]).length;
  /* ===== TAB KHAY (T2): file nhieu khay -> chon khay, moi khay so do rieng ===== */
  if(j.plates&&j.plates.length>1){
    h+='<div class="card"><h3 style="margin-top:0">Khay trong file <span class="mut" style="font-size:12px">· '+j.plates.length+' khay — bấm để phân tích khay đó</span></h3>';
    h+='<div style="display:flex;gap:10px;flex-wrap:wrap">';
    for(const p of j.plates){
      const pid=+p.id||0, act=pid===(+j.plate||1);   /* ep so — phong ngua XSS neu upstream doi kieu */
      h+='<div onclick="rePlate('+pid+')" style="cursor:pointer;text-align:center;border:2px solid '+(act?'#22c55e':'var(--line)')+';border-radius:12px;padding:8px;background:'+(act?'rgba(34,197,94,.10)':'#0c111a')+'">'
       +(p.img?'<img src="'+esc(p.img)+'" alt="khay '+pid+'" style="width:108px;height:108px;object-fit:contain;border-radius:8px;background:#0a0e14">'
              :'<div class="mut" style="width:108px;height:108px;display:flex;align-items:center;justify-content:center;font-size:11px">không có ảnh</div>')
       +'<div style="font-weight:700;font-size:12.5px;margin-top:5px">'+(act?'▶ ':'')+'Khay '+pid+'</div>'
       +'<div class="mut" style="font-size:11px;max-width:116px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(p.name)+'">'+esc(p.name)+'</div>'
       +'<div class="mut" style="font-size:11px">'+(+p.n_obj||0)+' vật thể</div></div>';
    }
    h+='</div><div class="mut" style="margin-top:8px">Mỗi khay in riêng 1 lần — kích thước/overhang/bám bàn/preset đều tính THEO KHAY đang chọn.</div></div>';
  }
  h+='<div class="card"><h3 style="margin-top:0">'+esc(j.name)
   +(j.plate_name?' <span class="mut" style="font-size:12px">· khay '+j.plate+": "+esc(j.plate_name)+'</span>':'')+'</h3>';
  h+='<div class="grid">'
    +kv("Kích thước",(m.dims||[]).join(" × ")+" mm")
    +kv("Số tam giác",(m.triangles||0).toLocaleString())
    +kv("Mặt hẫng >45°",(m.overhang_pct||0)+"% · "+(m.overhang_cm2||0)+" cm²")
    +kv("Bám bàn",(m.bed_cm2||0)+" cm²")
    +'</div>';
  h+='<div class="mut" style="margin-top:9px">'+(j.sliced?"Đã slice (có G-code)":"File thô — chưa slice")+'</div></div>';

  // Khay AMS THAT (MQTT) — quyet dinh cau hinh support interface. Khong sync duoc
  // thi phai canh bao TO: cau hinh suy theo file co the sai (Z=0 voi nhua khong co).
  {
    const af=j.ams_filaments||[], amsl=j.ams||[];
    h+='<div class="card"><h3 style="margin-top:0">Khay AMS thật <span class="mut" style="font-size:12px">· sync qua MQTT lúc phân tích</span></h3>';
    if(amsl.length){
      h+='<div class="grid">';
      if(af.length){ for(const t of af) h+=kv("Khe "+t.slot,'⬤ '+t.sub).replace('⬤','<span style="color:'+esc(t.color)+'">⬤</span>'); }
      else { for(let i=0;i<amsl.length;i++) h+=kv("Khe "+(i+1), amsl[i]); }
      h+='</div>';
      // CANH BAO DO NGAY TUNG KHAY: nhiet chuan + rui ro (am/ket/warp/den)
      const adv=j.ams_advice||[];
      if(adv.length){
        const anyOnPlate=adv.some(a=>a.on_plate);
        h+='<div style="margin-top:10px"><div class="mut" style="font-weight:700;margin-bottom:4px">🌡️ Nhiệt chuẩn + cảnh báo từng khay (đối chiếu nguồn Bambu chính thức)'
          +(anyOnPlate?' — <span style="color:#22c55e">◀ khe khay đang chọn IN</span>':'')+':</div>';
        for(const a of adv){
          const warn=a.level==="warn", onp=!!a.on_plate;   /* khe khay dang chon dung */
          const bg=warn?"rgba(239,68,68,.14)":(onp?"rgba(34,197,94,.12)":"rgba(56,189,248,.10)");
          const bd=warn?"#ef4444":(onp?"#22c55e":"rgba(56,189,248,.4)");
          h+='<div style="background:'+bg+';border-left:3px solid '+bd+';border-radius:8px;padding:8px 10px;margin-bottom:6px'+(onp?';box-shadow:0 0 0 1px rgba(34,197,94,.35)':'')+'">'
           +'<b>'+(warn?"🔴 ":"")+'Khe '+a.slot+': '+esc(a.name)+'</b>'
           +(onp?' <span style="color:#22c55e;font-weight:700">◀ khay này dùng</span>':'')
           +' <span style="color:#fca5a5;font-weight:700">'+esc(a.temp||"?")+'</span>'
           +(a.flow?' <span class="mut">· flow '+a.flow+' mm³/s</span>':'')
           +'<div class="mut" style="font-size:12px;margin-top:3px;line-height:1.5">'+esc(a.note||"")+'</div></div>';
        }
        h+='</div>';
      }
      const hasPETG=amsl.some(t=>t.indexOf("PETG")===0), hasPLA=amsl.some(t=>t.indexOf("PLA")===0);
      h+= (hasPETG&&hasPLA)
        ? '<div class="tip" style="margin-top:9px">✓ Có cặp PLA + PETG thật trong khay — cấu hình support interface Z=0 (gỡ đẹp) dùng được. Khai báo cả 2 nhựa trong Project Filaments để hub tự áp.</div>'
        : '<div class="iss" style="margin-top:9px">Khay chỉ có 1 họ nhựa ('+esc(amsl.join(", "))+') — hub dùng cấu hình interface CÙNG vật liệu (khe hở 0.2mm, an toàn). KHÔNG tự ý chỉnh Z distance = 0.</div>';
    } else {
      h+='<div class="iss">⚠️ CHƯA SYNC ĐƯỢC KHAY AMS (máy in tắt / mất kết nối) — cấu hình support bên dưới suy theo KHAI BÁO TRONG FILE, có thể lệch thực tế. Bật máy in rồi phân tích lại để chắc chắn.</div>';
    }
    h+='</div>';
  }

  /* ===== NHUA DAN DAT PROCESS (#2/#3 2026-07-19): chon nhua+mau -> PHAN TICH LAI
     process theo CUON DANG DUNG (tran mvs chong ket / nhiet / ten preset / mau).
     Mac dinh tu khay AMS that (khop mau file), doi duoc. Van tai preset FILAMENT
     .json rieng nhu cu. Selection nay dong bo ca 'So sanh 3 che do' + 'Slice+day'. */
  {
    const opts=j.fil_options||[], af=j.ams_filaments||[], seen=new Set();
    const pick=j.fil_pick||"", fs=j.fil_sel||{};
    if(opts.length){
      /* luu selection hien tai -> optimize()/slice() gui kem (dong bo moi noi) */
      window.__filKey=fs.key||""; window.__filColor=(fs.color||""); window.__filSelInfo=fs;
      window.__filReq = (pick.indexOf("slot:")===0) ? {slot:+pick.slice(5)}
                        : (pick ? {fil:pick, color:(fs.color||"")} : {});
      h+='<div class="card" id="filcard"><h3 style="margin-top:0">Nhựa đang dùng cho bản in <span class="mut" style="font-size:12px">· dẫn dắt cấu hình process — đổi là phân tích lại</span></h3>';
      h+='<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">';
      h+='<select id="filsel" onchange="reFil()" style="flex:1;min-width:200px;background:#0c111a;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:11px;font-size:13px">';
      const selv=(v)=> v===pick?' selected':'';
      for(const t of af){const k=(t.sub||"").toUpperCase(); if(!k)continue; seen.add(k);
        const v='slot:'+(+t.slot||0);
        h+='<option value="'+v+'"'+selv(v)+'>Khe '+(+t.slot||0)+' — '+esc(t.sub)+' '+esc(t.color||'')+' (AMS thật)</option>';}
      for(const o of opts){if(!seen.has(o)){seen.add(o);h+='<option value="'+esc(o)+'"'+selv(o)+'>'+esc(o)+'</option>';}}
      h+='</select>';
      /* #4: doi MAU truoc khi in — prefill mau cuon dang chon */
      const cv=((fs.color||'#000000').slice(0,7))||'#000000';
      h+='<input type="color" id="filcolor" value="'+esc(cv)+'" onchange="reColor()" title="Đổi màu nhựa (bấm để chọn)" style="width:46px;height:46px;padding:2px;border:1px solid var(--line);border-radius:10px;background:#0c111a;cursor:pointer">';
      h+='<button class="btn" style="width:auto;padding:11px 16px" onclick="dlFil()">⬇ Tải preset nhựa</button></div>';
      /* dong hien nhua dang DAN DAT process: ten + mau + nhiet + tran chay chong ket */
      let drv='';
      if(fs.key){ drv='✓ Đang dùng <b>'+esc(fs.key)+'</b>'
        +(fs.color?(' <span style="color:'+esc(fs.color)+'">⬤</span> <code>'+esc(fs.color)+'</code>'):'')
        +' — nhiệt <b>'+esc(fs.temp||'?')+'°C</b> · trần chảy <b>'+esc(fs.mvs||'?')+' mm³/s</b> (số chống kẹt của cuộn này). '
        +'Tốc độ, tên preset và màu bên dưới đã tính THEO cuộn này.'; }
      else { drv='⚠ Chưa chọn được cuộn — process đang theo KHAI BÁO trong file (có thể khác cuộn đang gắn). '
        +'Bật máy in để sync khay AMS, hoặc chọn nhựa ở ô trên.'; }
      h+='<div id="filinfo" class="'+(fs.key?'tip':'iss')+'" style="margin-top:9px;font-size:12.5px;line-height:1.55">'+drv+'</div></div>';
    }
  }
  /* ===== KIEM NHUA (user hoi 2026-07-19): so FILE khai bao vs SO AN TOAN cuon that
     cho KHAY dang chon -> SAI thi canh bao + nut tai preset nhua DA SUA. ===== */
  { const fc=j.filament_check;
    if(fc&&fc.key){
      const warn=fc.level==="warn";
      window.__filCheck=fc;
      h+='<div class="card" style="border:1px solid '+(warn?'rgba(239,68,68,.5)':'rgba(34,197,94,.4)')+'">'
       +'<h3 style="margin-top:0">'+(warn?'⚠️ Nhựa khai báo trong file KHÁC số an toàn':'✅ Nhựa khai báo trong file khớp số an toàn')
       +' <span class="mut" style="font-size:12px">· khay đang chọn · '+esc(fc.key)+'</span></h3>';
      h+='<table style="width:100%;font-size:13px"><tr><th style="text-align:left">Thông số</th><th>File khai báo</th><th>An toàn (cuộn '+esc(fc.key)+')</th></tr>';
      const row=(k,fv,sv,bad)=>'<tr><td class="mut">'+k+'</td><td style="text-align:center'+(bad?';color:#fca5a5;font-weight:700':'')+'">'+esc(fv==null?'—':fv)+'</td><td style="text-align:center;color:#22c55e">'+esc(sv==null?'—':sv)+'</td></tr>';
      const f=fc.file||{},s=fc.safe||{};
      const bmvs=(+f.mvs)>(+s.mvs)+0.5, btemp=Math.abs((+f.temp)-(+s.temp))>=5;
      h+=row('Nhiệt (°C)',f.temp,s.temp,btemp)+row('Trần chảy mvs',f.mvs,s.mvs,bmvs)+row('Flow',f.flow,s.flow,false);
      h+='</table>';
      for(const it of (fc.issues||[])) h+='<div class="iss" style="margin-top:7px">⚠ '+esc(it)+'</div>';
      if(!warn) h+='<div class="tip" style="margin-top:7px">File đã đặt đúng số an toàn cho cuộn này — cứ in.</div>';
      h+='<button class="btn" style="margin-top:9px" onclick="dlFilFix()">⬇ Tải preset nhựa ĐÃ SỬA (số an toàn) — import tab Filament</button>';
      h+='</div>';
    }
  }
  /* ===== CACH LAM SUPPORT theo vat lieu (user hoi 2026-07-19) — CHON option. Nguon:
     Bambu wiki PLA/PETG mutual support + forum.bambulab. Mac dinh GIU support cua file
     (an toan cho file nhieu mau); chon 1 cach thi ap khi slice. ===== */
  { const ss=j.support_strategies||[];
    if(ss.length){
      window.__supStrats=ss; window.__supStrat="";
      const rec=ss.find(s=>s.recommend)||ss[0];
      h+='<div class="card"><h3 style="margin-top:0">🩹 Cách làm support <span class="mut" style="font-size:12px">· theo vật liệu — chọn cách hợp nhất, dễ gỡ + mặt đẹp</span></h3>';
      h+='<select id="supstrat" onchange="onSupStrat()" '+IST+' style="width:100%;padding:11px">';
      h+='<option value="" selected>Giữ support của file (mặc định)</option>';
      for(const s of ss) h+='<option value="'+esc(s.id)+'">'+esc(s.label)+(s.recommend?' ★ đề xuất':'')+'</option>';
      h+='</select>';
      /* HIEN het cac phuong an (cung loai + khac loai) kem GIA TRI + meo — user 2026-08-03 */
      h+='<div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">';
      for(const s of ss){
        h+='<div style="border:1px solid #2a3550;border-radius:8px;padding:9px 11px">'
          +'<div style="font-weight:600;font-size:12.5px">'+esc(s.label)+(s.recommend?' <span style="color:#4ade80">★ đề xuất</span>':'')+'</div>'
          +(s.summary?'<div class="mut" style="font-size:11.5px;margin:4px 0;font-family:ui-monospace,monospace;color:#9fd0ff">'+esc(s.summary)+'</div>':'')
          +'<div class="mut" style="font-size:11.5px;line-height:1.5">'+esc(s.why||'')+'</div>'
          +'</div>';
      }
      h+='</div>';
      h+='<div id="supstratwhy" class="mut" style="margin-top:9px;font-size:12.5px;line-height:1.55">Chọn 1 cách ở trên (dropdown) để ÁP vào ô Support khi Slice. Đề xuất theo khay hiện tại: <b>'+esc(rec.label)+'</b>.</div>';
      h+='<div class="mut" style="font-size:11.5px;margin-top:6px">Áp khi bấm <b>Slice + đẩy xuống</b>/<b>Slice tải về</b>. Nguồn: Bambu wiki (PLA↔PETG không dính → Z=0 bóc sạch) + forum.bambulab (cùng nhựa: đánh đổi mặt-vs-gỡ).</div>';
      h+='</div>';
    }
  }
  if(j.issues&&j.issues.length){ h+='<div class="card"><h3 style="margin-top:0">Vấn đề phát hiện</h3>';
    for(const i of j.issues) h+='<div class="iss">'+esc(i)+'</div>'; h+='</div>'; }
  if(j.tips&&j.tips.length){ h+='<div class="card"><h3 style="margin-top:0">Khuyến nghị</h3>';
    for(const t of j.tips) h+='<div class="tip">'+esc(t)+'</div>'; h+='</div>'; }

  if(j.rotations&&j.rotations.length){
    h+='<div class="card"><h3 style="margin-top:0">Thử xoay 2 trục X + Y — tìm mặt úp tốt nhất</h3>'
     +'<div class="mut" style="font-size:12px;margin-bottom:8px">Tiêu chí xếp hạng: ① <b>SUPPORT ít nhất</b> — số cm³ là ƯỚC LƯỢNG TƯƠNG ĐỐI (diện tích hẫng × chiều cao cột chống) để SO SÁNH giữa các hướng, KHÔNG phải gam thật (support in ở ~15% mật độ nên nhẹ hơn nhiều). Điểm mấu chốt: hướng bám bàn to mà support nhiều thì vẫn IN LÂU → xếp support trước. → ② tiếp xúc bàn lớn (bám chắc, chống warp) → ③ thấp nhất. Nguyên lý Tweaker (Schranz 2016 — Auto-Orientation của Cura) + bổ sung ước lượng support theo chiều cao.</div>'
     +'<table><tr><th>Hướng</th><th>Support</th><th>Overhang</th><th>Bám bàn</th><th>Cao</th><th>Dùng được?</th></tr>';
    for(const r of j.rotations){
      const isCur=(r.axis==="X"||r.axis==null)&&(r.angle===0||r.angle_x===0);
      const style=r.recommend?' style="background:rgba(34,197,94,.16)"':(isCur?' style="background:rgba(56,189,248,.1)"':'');
      const ax=r.axis||"X", ang=(r.angle!=null?r.angle:r.angle_x);
      h+='<tr'+style+'><td>'+ax+' '+ang+'°'
       +(isCur?' <span class="mut">(hiện tại)</span>':'')
       +(r.recommend?' <b style="color:#22c55e">★ ĐỀ XUẤT</b>':'')+'</td>'
       +'<td><b>~'+(r.support_cm3!=null?r.support_cm3:'?')+' cm³</b></td>'
       +'<td>'+r.overhang_pct+'%</td><td>'+r.bed_cm2+' cm²</td><td>'+r.height+' mm</td>'
       +'<td class="'+(r.usable?'good':'bad')+'">'+(r.usable?'OK':'bám bàn quá ít')+'</td></tr>';
    }
    h+='</table><div class="mut" style="margin-top:8px">Xếp hạng: ít support nhất → bám bàn nhiều nhất → thấp nhất. '
     +'Overhang thấp mà bám bàn ~0 là BẪY: model đứng trên cạnh dao, lớp đầu không bám.</div>';
    // Anh render de user NHIN thay xoay the nao — khong phai doan tu con so
    if(j.rot_preview&&j.rot_preview.current){
      const pv=j.rot_preview;
      const cm=pv.current_meta;
      const cmeta=cm?('support ~'+(cm.support_cm3||0)+'cm³ · overhang '+cm.overhang_pct+'% · bám '+cm.bed_cm2+'cm² · cao '+cm.height+'mm'):'';
      h+='<div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;align-items:flex-start">'
       +'<div style="text-align:center"><div class="mut" style="margin-bottom:4px">Hướng hiện tại'
       +(pv.current_is_best?' <span style="color:#22c55e">✓ tốt nhất</span>':'')+'</div>'+pv.current
       +(cmeta?'<div class="mut" style="font-size:11px;margin-top:2px">'+cmeta+'</div>':'')+'</div>';
      // 1-2 GOI Y MEM — user tu chon, khong ep. Vien xanh cho phuong an tot hon hien tai.
      const opts=pv.options||[];
      for(let i=0;i<opts.length;i++){ const o=opts[i];
        const better=cm&&((o.support_cm3||0)<(cm.support_cm3||0)-1);
        const saved=cm?Math.max(0,Math.round(((cm.support_cm3||0)-(o.support_cm3||0))*10)/10):0;
        h+='<div style="text-align:center"><div style="font-weight:700;margin-bottom:4px;color:'
         +(better?'#22c55e':'#93c5fd')+'">'+(better?'★ ':'')+'Gợi ý '+(i+1)+': xoay '+o.angle+'° trục '+o.axis
         +(saved>0?' — bớt ~'+saved+'cm³ support':'')+'</div>'
         +'<div style="border:2px solid '+(better?'rgba(34,197,94,.5)':'rgba(147,197,253,.35)')+';border-radius:10px;display:inline-block">'+o.svg+'</div>'
         +'<div class="mut" style="font-size:11px;margin-top:2px">support ~'+(o.support_cm3||0)+'cm³ · overhang '+o.overhang_pct+'% · bám '+o.bed_cm2+'cm² · cao '+o.height+'mm</div></div>';
      }
      h+='</div>';
      h+='<div class="mut" style="font-size:12px;margin-top:8px">'
       +(pv.current_is_best?'✓ Hướng hiện tại đang tốt nhất theo số liệu — 1-2 gợi ý trên chỉ để bạn CÂN NHẮC (vd cần mặt đẹp/chịu lực khác), không bắt buộc xoay. ':'Cân nhắc 1-2 gợi ý trên — chọn cái hợp mục đích (ít support / mặt đẹp / chịu lực). ')
       +'Trong Bambu Studio: chọn model → phím <b>R</b> → nhập góc quanh trục tương ứng.</div>';
    }
    h+='</div>';
  }

  if(j.flow){ const f=j.flow; const ov=Object.entries(f.over_ceiling||{});
    h+='<div class="card"><h3 style="margin-top:0">Trần lưu lượng</h3><table>'
     +'<tr><td>Nhựa chảy tối đa</td><td><b>'+f.mvs+' mm³/s</b></td></tr>'
     +'<tr><td>Layer height</td><td>'+f.layer_height+' mm</td></tr>'
     +'<tr><td>→ Tốc độ tối đa THẬT</td><td class="good"><b>'+f.v_max+' mm/s</b></td></tr></table>';
    if(ov.length){ h+='<table style="margin-top:8px"><tr><th>Đang đặt</th><th>Thực tế</th></tr>';
      for(const [k,v] of ov) h+='<tr><td>'+esc(k)+'</td><td class="bad">'+v+' mm/s → máy hãm còn '+f.v_max+'</td></tr>';
      h+='</table>'; }
    h+='</div>';
  }
  if(j.variable_layer){ const v=j.variable_layer;
    h+='<div class="card"><h3 style="margin-top:0">Variable Layer Height</h3><table>'
     +'<tr><td>Mỏng nhất / dày nhất</td><td>'+v.min+' / '+v.max+' mm</td></tr>'
     +'<tr><td>Trung bình</td><td>'+v.avg+' mm</td></tr>'
     +'<tr><td>Số lớp thực tế</td><td class="bad"><b>'+v.layers_actual+'</b></td></tr>'
     +'<tr><td>Nếu để phẳng</td><td class="good"><b>'+v.layers_flat+'</b></td></tr>'
     +'<tr><td>Cộng thêm</td><td class="bad"><b>+'+v.extra_layers+' lớp (+'+v.extra_pct+'%)</b></td></tr>'
     +'</table></div>';
  }
  if(j.config){ h+='<div class="card"><h3 style="margin-top:0">Cấu hình trong file</h3><table>';
    for(const [k,v] of Object.entries(j.config)) if(v!==null&&v!==undefined)
      h+='<tr><td class="mut">'+esc(k)+'</td><td>'+esc(Array.isArray(v)?v.join(", "):v)+'</td></tr>';
    h+='</table></div>';
  }
  if(j.export){ const e=j.export;
    h+='<div class="card"><h3 style="margin-top:0">Cấu hình tối ưu — sinh từ chính các vấn đề trên</h3>';
    for(const w of e.why) h+='<div class="tip">'+esc(w)+'</div>';
    if(e.guide&&e.guide.length){
      h+='<div style="margin-top:14px;border-top:1px solid rgba(255,255,255,.12);padding-top:12px">'
       +'<div style="font-weight:800;font-size:14px;margin-bottom:4px">📋 Chỉnh ở đâu trong Bambu Studio — đọc trước khi xuất</div>'
       +'<div class="mut" style="font-size:12px;margin-bottom:10px">Bật <b>Advanced</b> (góc trên phần Process) mới thấy đủ ô. Mỗi dòng = 1 ô trong Studio: <b>Tab › mục › tên tiếng Anh = giá trị</b>.</div>';
      const tabvi={Quality:"Quality (Chất lượng)",Strength:"Strength (Độ bền)",Speed:"Speed (Tốc độ)",Support:"Support (Đỡ)",Others:"Others (Khác)"};
      for(const g of e.guide){
        h+='<div style="margin:8px 0 4px;font-weight:700;color:#38bdf8">'+esc(tabvi[g.tab]||g.tab)+'</div>'
         +'<table style="width:100%;font-size:13px"><tr><th style="text-align:left">Mục</th><th style="text-align:left">Thông số (EN)</th><th style="text-align:right">Giá trị</th><th style="text-align:left">Vì sao (theo số liệu model)</th></tr>';
        for(const it of g.items)
          h+='<tr><td class="mut" style="vertical-align:top">'+esc(it.section)+'</td><td style="vertical-align:top">'+esc(it.en)+'</td><td style="text-align:right;vertical-align:top"><b>'+esc(it.value)+'</b></td><td class="mut" style="font-size:12px;line-height:1.5">'+esc(it.why||'')+'</td></tr>';
        h+='</table>';
      }
      h+='</div>';
    }
    h+='<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
     +'<label class="mut" style="font-size:13px">Tên preset:</label>'
     +'<code style="font-size:12px;color:#93c5fd" id="pnamePrefix">'+esc(e.preset&&e.preset.name||"LP-PLA")+'</code>'
     +'<input id="pnameExtra" placeholder="thêm tên model/ghi chú (tùy chọn)" oninput="pnamePreview()" '
     +'style="flex:1;min-width:160px;padding:7px;border-radius:8px;border:1px solid #334;background:#0f1523;color:#e8ecf4;font-size:13px">'
     +'</div>'
     +'<div class="mut" id="pnameFull" style="font-size:12px;margin:5px 0 8px"></div>'
     +'<button class="btn" style="margin-top:4px" onclick="dl()">Tải preset .json (import vào Bambu Studio)</button>'
     +'<div class="mut" style="margin-top:9px;line-height:1.6">✅ <b>Checklist sau khi import</b> (File ▸ Import ▸ Import Configs):<br>'
     +'1️⃣ <b>CHỌN preset ở dropdown Process</b> — import xong Studio KHÔNG tự áp, đây là lỗi số 1.<br>'
     +'2️⃣ Có dùng support: tab Support bật <b>Advanced</b> → kiểm Support/raft interface = đúng khay, Top Z distance đúng như dòng giải thích ở trên.<br>'
     +'3️⃣ Bấm in: map khay AMS đúng nhựa/màu như file khai báo.<br>'
     +'4️⃣ Slice → Preview: kéo thanh lớp, nhìn lớp interface đổi màu ngay dưới mặt hẫng là chuẩn.</div></div>';
    window.__preset=e.preset; window.__pname=(j.name||"file").replace(/\.[^.]+$/,"");
    // AUTO dien TEN MODEL (file 3D) vao ten preset (user 2026-08-15: "ten model khi
    // export luon auto") — khoi phai go tay; user van sua/xoa duoc.
    const _ext=document.getElementById("pnameExtra");
    if(_ext) _ext.value=window.__pname.replace(/[^A-Za-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,24).replace(/-+$/,"");
    pnamePreview();
  }
// Chen text user go vao ten preset NGAY TRUOC che do (Fast/Balanced/HighQuality):
// LP-PLA-Lite-Balanced-0.2mm + "vase" -> LP-PLA-Lite-vase-Balanced-0.2mm
function pnameWith(base,extra){
  extra=(extra||"").trim().replace(/[^A-Za-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,24);
  if(!extra) return base;
  const m=base.match(/^(LP-.+?)-(Fast|Balanced|HighQuality)-(.+)$/);
  return m ? m[1]+"-"+extra+"-"+m[2]+"-"+m[3] : base+"-"+extra;
}
function pnamePreview(){
  const p=window.__preset; if(!p) return;
  const ex=(document.getElementById("pnameExtra")||{}).value||"";
  const full=pnameWith(p.name||"LP-PLA-preset",ex);
  const el=document.getElementById("pnameFull"); if(el) el.innerHTML="→ Tên khi xuất: <b style=\"color:#22c55e\">"+esc(full)+"</b>";
}
  h+='<button class="btn go" id="e2e" onclick="optimize()">So sánh 3 chế độ — slice thật 4 lần (~15s)</button>'
   +'<div id="e2eout"></div>'
   +'<div class="card" style="margin-top:10px"><h3 style="margin-top:0">🧩 Chuẩn bị in (Prepare) '
   +'<span class="mut" style="font-size:12px">· chỉnh thông số kiểu Bambu → Slice lại → Đẩy xuống máy</span></h3>'
   +bRow("Chế độ (Process)", '<select id="smode" onchange="onSmode()" '+IST+' style="min-width:210px">'
       +'<option value="balanced" selected>Cân bằng — 0.20mm (khuyên dùng)</option>'
       +'<option value="fast">Nhanh — 0.28mm</option>'
       +'<option value="quality">Đẹp — 0.16mm</option>'
       +'<option value="">Giữ nguyên config trong file</option></select>')
   /* PANEL kieu Bambu Prepare (2026-07-19): 3 muc Quality/Bed adhesion/Support, moi dong
      nhan trai — o phai + don vi (nhu Studio). Mau doi o "Nhua dang dung" phia tren. */
   +bSec("📐 Quality — Layer height")
   +bRow("Layer height", '<select id="ov_layer" onchange="this.dataset.touched=1" '+IST+'>'
       +'<option value="">Theo chế độ</option>'
       +'<option>0.28</option><option>0.24</option><option>0.20</option><option>0.16</option><option>0.12</option></select> mm')
   +bRow("Initial layer height", nIn("ov_init_layer",0.06,0.35,0.02)+' mm')
   +bRow("Tốc độ mặt ngoài (outer wall)", nIn("ov_outer",20,500,1)+' mm/s <span class="mut" style="font-size:11px">— cao quá máy tự hãm</span>')
   +bSec("🛬 Others — Bed adhesion (Brim)")
   +bRow("Brim type", '<select id="ov_brim" onchange="this.dataset.touched=1" '+IST+'>'
       +'<option value="">Theo phân tích</option><option value="no_brim">no_brim (không)</option>'
       +'<option value="outer_only">outer_only (ngoài)</option><option value="outer_and_inner">outer_and_inner (quanh)</option>'
       +'<option value="auto_brim">Auto</option></select>')
   +bRow("Brim width", nIn("ov_brim_w",0,20,0.5)+' mm')
   +bRow("Skirt loops", nIn("ov_skirt",0,5,1))
   +bSec("⛰ Support")
   +bRow("Enable support", '<label style="cursor:pointer"><input id="ov_support" type="checkbox" onchange="this.dataset.touched=1"> bật</label>')
   +bRow("Type", '<select id="ov_sup_type" onchange="this.dataset.touched=1" '+IST+'>'
       +'<option value="">—</option><option value="normal(auto)">normal(auto)</option><option value="tree(auto)">tree(auto)</option></select>')
   +bRow("Style", '<select id="ov_sup_style" onchange="this.dataset.touched=1" '+IST+'>'
       +'<option value="">—</option><option value="default">Default</option><option value="snug">Snug</option>'
       +'<option value="tree_hybrid">Tree Hybrid</option><option value="tree_strong">Tree Strong</option><option value="tree_slim">Tree Slim</option></select>')
   /* Nhua lam support — CHON tu khay AMS THAT (user 2026-07-19): de (base) + mat tiep
      xuc (interface). PLA in -> interface = PETG (khac vat lieu) go sach; de van PLA. */
   +(function(){ const af=j.ams_filaments||[];
       let b='<option value="0">Default (theo model)</option>', f='<option value="">— (giữ theo phân tích)</option>';
       for(const t of af){ const o='<option value="'+(+t.slot||0)+'">Khe '+(+t.slot||0)+' — '+esc(t.sub)+' '+esc(t.color||'')+'</option>'; b+=o; f+=o; }
       return bRow('Support/raft base (đế)', '<select id="ov_sup_basefil" onchange="this.dataset.touched=1" '+IST+'>'+b+'</select>')
            + bRow('Support/raft interface (mặt tiếp xúc)', '<select id="ov_sup_ifil" onchange="this.dataset.touched=1" '+IST+'>'+f+'</select>'
                +' <span class="mut" style="font-size:11px">★ khác vật liệu = gỡ sạch</span>');
     })()
   +bRow("Threshold angle", nIn("ov_sup_angle",0,90,1)+' °')
   +bRow("On build plate only", '<label style="cursor:pointer"><input id="ov_sup_onplate" type="checkbox" onchange="this.dataset.touched=1"> chỉ từ mặt bàn</label>')
   +bRow("Top Z distance", nIn("ov_sup_ztop",0,1,0.01)+' mm <span class="mut" style="font-size:11px">— 0 = khác vật liệu; ≥0.15 cùng loại</span>')
   +bRow("Bottom Z distance", nIn("ov_sup_zbot",0,1,0.01)+' mm')
   +bRow("Top interface layers", nIn("ov_sup_itop",0,5,1)+' <span class="mut" style="font-size:11px">layers</span>')
   +bRow("Top interface spacing", nIn("ov_sup_ispacing",0,2,0.05)+' mm')
   +bRow("Interface pattern", '<select id="ov_sup_ipattern" onchange="this.dataset.touched=1" '+IST+'>'
       +'<option value="">—</option><option value="concentric">Concentric</option>'
       +'<option value="rectilinear">Rectilinear</option><option value="rectilinear_interlaced">Rectilinear Interlaced</option></select>')
   +'<div id="revsum" class="mut" style="font-size:12px;margin:10px 0 4px"></div>'
   +'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">'
   +'<button class="btn go" style="margin-top:0;flex:1;min-width:200px" onclick="sliceReview()">🔪 Slice lại — cập nhật thời gian & nhựa</button>'
   +'</div>'
   +'<div id="sliceresult" style="margin-top:9px"></div>'
   +'<div class="mut" style="margin-top:7px;line-height:1.6">Chỉnh xong bấm <b>Slice lại</b> để xem thời gian/nhựa MỚI (dùng cấu hình máy A1 thật + khay AMS), rồi mới <b>Đẩy xuống máy in</b>. Số này khớp với Bambu Studio.</div></div>';
  document.getElementById("out").innerHTML=h;
  fillReview();                     // nap gia tri mac dinh vao panel chinh sua (kieu Bambu)
}
function optimize(){
  if(!FILE){ toast("Chọn lại file"); return; }
  const b=document.getElementById("e2e"); b.disabled=true; b.textContent="Đang slice baseline + 3 chế độ…";
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/optimize?name="+encodeURIComponent(FILE.name)
    +(window.__platesN>1?("&plate="+(window.__plate||1)):"")+filQS());
  xhr.onload=()=>{ let j={}; try{j=JSON.parse(xhr.responseText);}catch(e){}
    if(j.ok&&j.queued) pollOpt(); else { b.disabled=false; toast("Lỗi: "+(j.msg||xhr.status)); } };
  xhr.onerror=()=>{ b.disabled=false; toast("Mất kết nối"); };
  xhr.send(FILE);
}
async function pollOpt(){
  const b=document.getElementById("e2e");
  try{
    const j=await (await fetch("/api/optstatus",{cache:"no-store"})).json();
    if(j.state==="running"){ b.textContent=j.msg||"Đang xử lý…"; setTimeout(pollOpt,2500); return; }
    b.disabled=false; b.textContent="So sánh lại 3 chế độ";
    if(j.state==="error"){ toast("Lỗi: "+j.msg); return; }
    if(j.state==="done"&&j.report) renderE2E(j.report);
  }catch(e){ setTimeout(pollOpt,4000); }
}
function renderE2E(r){
  const b=r.baseline||{}; const MS=["fast","balanced","quality"];
  let h='<div class="card"><h3 style="margin-top:0">So sánh — mỗi dòng là 1 lần slice THẬT</h3>'
    +'<table><tr><th>Chế độ</th><th>Thời gian</th><th>Nhựa</th><th>Lớp</th></tr>'
    +'<tr style="background:rgba(255,255,255,.04)"><td><b>Mặc định</b><br><span class="mut">'+esc(b.preset_name||"0.20mm Standard @BBL A1")+'</span></td>'
    +'<td>'+esc(b.time||"?")+'</td><td>'+(b.weight_g||"?")+' g</td><td>'+(b.layers||"?")+'</td></tr>';
  const LBL={fast:"Nhanh",balanced:"Cân bằng",quality:"Đẹp"};
  let anyErr=false;
  for(const k of MS){
    const d=(r.modes||{})[k]; if(!d) continue;
    if(d.error){ anyErr=true;   /* KHONG giau loi nua: hien ro chinh dong do (bug #1) */
      h+='<tr><td><b>'+esc(LBL[k]||k)+'</b></td>'
        +'<td class="bad" colspan="3">⚠ Lỗi slice: '+esc(d.error)+'</td></tr>';
      continue;
    }
    const tp=d.time_pct, cls=tp>2?"good":(tp<-2?"bad":"mut");
    h+='<tr><td><b>'+esc(d.label)+'</b></td><td class="'+cls+'">'+esc(d.time)
      +'<br><span class="mut">'+(tp>0?"−"+tp:"+"+(-tp))+'%</span></td>'
      +'<td>'+d.weight_g+' g</td><td>'+d.layers+'</td></tr>';
  }
  h+='</table>';
  if(anyErr){
    const guiErr=MS.some(k=>{const d=(r.modes||{})[k]||{}; return d.error&&/Bambu Studio.*MỞ|đang MỞ/i.test(d.error);});
    h+='<div class="iss" style="margin-top:8px">⚠ Có chế độ slice lỗi ở trên. '
      +(guiErr
        ?'Lỗi báo <b>Bambu Studio đang mở</b> → ĐÓNG hẳn Bambu Studio (giao diện) rồi bấm <b>So sánh lại 3 chế độ</b>. Hub và Studio dùng chung 1 file chương trình.'
        :'Đọc dòng đỏ để biết lý do (thường do cấu hình file). Nếu file slice bình thường trong Bambu Studio mà đây vẫn lỗi, gửi tên file để kiểm tra.')
      +'</div>';
  }
  h+='</div>';
  for(const k of MS){
    const d=(r.modes||{})[k]; if(!d||d.error) continue;
    h+='<div class="card"><h3 style="margin-top:0">'+esc(d.label)+' — vì sao</h3>';
    /* Ngân sách thời gian (chuyên gia tư vấn, không cứng nhắc): note + các bước đã cắt
       kèm GIÁ ĐO THẬT của từng bước — user thấy rõ đã đánh đổi gì và vì sao. */
    const bud=d.budget||{};
    if(bud.note) h+='<div class="iss" style="border-left-color:#f59e0b;background:rgba(245,158,11,.10)">⏱ '+esc(bud.note)+'</div>';
    if(bud.trims&&bud.trims.length){
      h+='<div class="tip">✂️ Đã cắt '+bud.trims.length+' bước để giữ ngân sách (+1h30 mục tiêu / +2h sai số) — lever kỹ thuật (vật cao/chống kẹt/brim/support) GIỮ NGUYÊN:';
      for(const t of bud.trims){
        const sv=t.saved_secs?(' → tiết kiệm '+Math.round(t.saved_secs/60)+' phút (đo thật)'):'';
        h+='<br>• '+esc(t.step||'')+sv;
      }
      h+='</div>';
    }
    for(const w of d.why) h+='<div class="tip">'+esc(w)+'</div>';
    h+='<button class="btn" style="margin-top:9px" onclick="dlp(\''+k+'\')">Tải preset '+esc(d.label)+' (.json)</button></div>';
  }
  window.__rep=r;
  document.getElementById("e2eout").innerHTML=h;
}
function dlp(k){
  _clog("dlp click k="+k+" hasRep="+(!!window.__rep)+" hasModes="+(!!(window.__rep&&window.__rep.modes))+" hasMode="+(!!(window.__rep&&window.__rep.modes&&window.__rep.modes[k])));
  if(!window.__rep||!window.__rep.modes||!window.__rep.modes[k]){ toast("Chưa có kết quả — So sánh lại"); return; }
  const p=Object.assign({},window.__rep.modes[k].preset);  // copy, khong sua ban goc
  // AUTO kem TEN MODEL vao ten preset 3 che do (user 2026-08-15) — dong bo voi export chinh
  const mdl=_mdlName() || (window.__rep.name||"").replace(/\.[^.]+$/,"");
  const full=pnameWith(p.name||("LP-"+k),mdl);          // chen ten model TRUOC che do
  p.name=full; p.print_settings_id=full;
  _saveJson(p, full+".json");
  toast("Đã tải: "+full+" — Import xong nhớ CHỌN preset ở dropdown Process (không tự áp)");
}
function kv(k,v){ return '<div class="kv"><span>'+k+'</span><b>'+esc(v)+'</b></div>'; }
function pnameWith(name, extra){                        // chen TEN MODEL vao TRUOC che do
  name = String(name || "LP-preset");
  extra = String(extra || "").replace(/[^A-Za-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"");
  if(!extra || name.indexOf(extra) >= 0) return name;   // khong co model / da co -> giu nguyen
  var m = name.match(/^(LP-.+?)-(Fast|Balanced|HighQuality|Draft|Quality)-(.+)$/);
  return m ? (m[1]+"-"+extra+"-"+m[2]+"-"+m[3]) : (name+"-"+extra);
}
function _mdlName(){                                    // ten model (file 3D) da lam sach
  return (window.__pname||"").replace(/\.[^.]+$/,"")
    .replace(/[^A-Za-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,24).replace(/-+$/,"");
}
function _clog(m){ try{ fetch("/api/clientlog",{method:"POST",body:String(m)}); }catch(_){ } }
window.onerror=function(m,u,l,c){ _clog("JSERR "+m+" @"+(u||"")+":"+l+":"+c); };
function _saveJson(obj, fname){                         // tai kieu SERVER (form POST -> attachment)
  _clog("saveJson start "+fname);
  try{
    let ifr=document.getElementById("_dlfr");           // iframe an de trang KHONG dieu huong
    if(!ifr){ ifr=document.createElement("iframe"); ifr.id="_dlfr"; ifr.name="_dlfr";
              ifr.style.display="none"; document.body.appendChild(ifr); }
    const f=document.createElement("form"); f.method="POST"; f.action="/api/preset-dl";
    f.target="_dlfr"; f.style.display="none";
    const mk=function(n,v){ const t=document.createElement("textarea"); t.name=n; t.value=v; f.appendChild(t); };
    mk("name",fname); mk("data",JSON.stringify(obj,null,4));
    document.body.appendChild(f); f.submit();
    _clog("saveJson submitted "+fname);
    setTimeout(function(){ f.remove(); }, 4000);
  }catch(e){ _clog("saveJson ERR "+e); alert("Lỗi tải preset: "+e); }
}
function dl(){
  _clog("dl click hasPreset="+(!!(window.__preset&&window.__preset.name)));
  const p=Object.assign({},window.__preset||{});       // copy, khong sua ban goc
  if(!p.name){ _clog("dl no preset"); toast("Chưa có preset — bấm Phân tích lại file"); return; }
  const ex=((document.getElementById("pnameExtra")||{}).value||"").trim() || _mdlName();  // o trong -> TEN MODEL
  const full=pnameWith(p.name,ex);
  p.name=full; p.print_settings_id=full;                // ten trong Bambu = model + che do
  _saveJson(p, full+".json");
  toast("Đã tải: "+full+" — Import xong nhớ CHỌN preset ở dropdown Process");
}
/* Bước 1: SLICE LẠI với thông số đã chỉnh (giữ file, KHÔNG đẩy) -> hiện thời gian/nhựa mới */
async function sliceReview(){
  if(!FILE){ toast("Chọn lại file"); return; }
  const m=(document.getElementById("smode")||{value:""}).value;
  const box=document.getElementById("sliceresult");
  if(box) box.innerHTML='<div class="mut">⏳ Đang slice lại (dùng cấu hình A1 thật)… có thể mất ~15-40s.</div>';
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/upload?name="+encodeURIComponent(FILE.name)+(m?"&mode="+m:"")+"&download=1"
    +(window.__platesN>1?("&plate="+(window.__plate||1)):"")+filQS()+ovQS());
  xhr.onload=()=>{ let j={}; try{j=JSON.parse(xhr.responseText);}catch(e){}
    if(j.ok&&j.queued){ pollReview(); } else { if(box) box.innerHTML='<div class="iss">Lỗi: '+esc(j.msg||xhr.status)+'</div>'; } };
  xhr.onerror=()=>{ if(box) box.innerHTML='<div class="iss">Mất kết nối</div>'; };
  xhr.send(FILE);
}
async function pollReview(){
  const box=document.getElementById("sliceresult");
  try{ const j=await (await fetch("/api/upstatus",{cache:"no-store"})).json();
    if(j.state==="slicing"||j.state==="pushing"){ if(box) box.innerHTML='<div class="mut">⏳ '+esc(j.msg||"Đang xử lý…")+'</div>'; setTimeout(pollReview,2500); return; }
    if(j.state==="done"){ const s=j.stats||{};
      const t=esc(s.time||"?"), w=(s.weight_g!=null?s.weight_g:"?"), L=(s.layers!=null?s.layers:"?");
      if(box) box.innerHTML='<div class="tip" style="font-size:14px">✔ Slice xong — <b>⏱ '+t+'</b> · <b>🎨 '+w+' g</b> · <b>🧱 '+L+' lớp</b></div>'
        +'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">'
        +'<button class="btn go" style="margin-top:0;flex:1;min-width:180px" onclick="pushLast()">🖨 Đẩy xuống máy in</button>'
        +'<a class="btn" style="margin-top:0;flex:1;min-width:150px;text-align:center;text-decoration:none;background:linear-gradient(160deg,#a78bfa,#7c3aed)" href="/api/sliced-download">⬇ Tải .gcode.3mf</a>'
        +'</div><div class="mut" style="font-size:11.5px;margin-top:6px">Số này là kết quả slice THẬT với thông số bạn đã chỉnh — khớp Bambu Studio. Sửa tiếp thì bấm Slice lại.</div>';
    }
    else if(j.state==="error"){ if(box) box.innerHTML='<div class="iss">Lỗi slice: '+esc(j.msg)+'</div>'; }
  }catch(e){ setTimeout(pollReview,4000); }
}
/* Bước 2: ĐẨY file .gcode.3mf vừa slice xuống máy in (không slice lại) */
async function pushLast(){
  const box=document.getElementById("sliceresult");
  toast("Đang đẩy xuống máy in…");
  try{ const j=await (await fetch("/api/push-last",{method:"POST"})).json();
    if(j.ok){ toast("✔ Đã đẩy xuống máy: "+(j.name||"")); if(box) box.innerHTML+='<div class="tip" style="margin-top:6px">✔ Đã gửi <b>'+esc(j.name||"")+'</b> xuống máy in — mở Bambu Handy/màn hình máy để bấm In.</div>'; }
    else toast("Lỗi đẩy: "+(j.msg||"?"));
  }catch(e){ toast("Mất kết nối khi đẩy"); }
}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _qs_path(self):
        """Lay tham so ?path= da giai ma."""
        from urllib.parse import urlparse, parse_qs, unquote
        return unquote(parse_qs(urlparse(self.path).query).get("path", [""])[0])

    def _send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/status"):
            with LOCK:
                payload = {"connected": STATE["connected"], "ts": STATE["ts"], "rc": STATE["rc"],
                           "name": PRINTER_NAME, "data": STATE["data"]}
            payload["filament"] = build_filament()
            with JOB_LOCK:
                payload["job_weight"] = JOB["weight"]
                payload["has_thumb"] = bool(JOB["thumb"])
                # Mau nhua THUC SU dung trong ban in nay (tu slice_info: color + used_g)
                info = JOB.get("info") or {}
                payload["job_filaments"] = (info.get("slice") or {}).get("filaments") or []
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        elif path.startswith("/api/filament"):
            self._send(200, json.dumps({"filament": build_filament()}), "application/json; charset=utf-8")
        elif path.startswith("/api/jobinfo"):
            with JOB_LOCK:
                payload = {"file": JOB["file"], "weight": JOB["weight"],
                           "fetching": JOB["fetching"], "info": JOB["info"]}
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        elif path.startswith("/api/gcodedict"):
            self._send(200, json.dumps(GCODE_DICT), "application/json; charset=utf-8")
        elif path.startswith("/api/files"):
            self._send(200, json.dumps({"files": get_files(), "busy": is_busy()}), "application/json; charset=utf-8")
        elif path.startswith("/api/filethumb"):
            fpath = self._qs_path()
            if not fpath:
                self._send(400, "no path", "text/plain")
                return
            thumb, _ = ensure_file_meta(fpath)
            if thumb:
                self._send(200, thumb, "image/png")
            else:
                self._send(404, "no thumb", "text/plain")
        elif path.startswith("/api/printer-config"):
            with LOCK:
                conn = STATE.get("connected", False)
            self._send(200, json.dumps({
                "host": IP,
                "serial_set": bool(SERIAL),      # chi bao CO/CHUA — khong lo ky tu that
                "code_set": bool(CODE),
                "connected": conn,
            }), "application/json; charset=utf-8")
        elif path.startswith("/api/anstatus"):
            with ANJOB_LOCK:
                self._send(200, json.dumps(ANJOB, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif path.startswith("/api/optstatus"):
            with OPTJOB_LOCK:
                self._send(200, json.dumps(OPTJOB, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif path.startswith("/api/upstatus"):
            with UPJOB_LOCK:
                self._send(200, json.dumps(UPJOB), "application/json; charset=utf-8")
        elif path.startswith("/api/sliced-download"):
            with UPJOB_LOCK:
                fp, fn = LAST_SLICED.get("path"), LAST_SLICED.get("name")
            if not fp or not os.path.isfile(fp):
                self._send(404, json.dumps({"ok": False, "msg": "Chưa có file slice để tải"}),
                           "application/json; charset=utf-8")
                return
            with open(fp, "rb") as f:
                blob = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(fn or "sliced.gcode.3mf")}"')
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)
        elif path.startswith("/api/notify-test"):
            # Bam nut test tren dashboard -> gui thu 1 tin toi MOI kenh da cau hinh
            chs = notify.channels()
            if not chs:
                self._send(200, json.dumps({"ok": False, "msg":
                    "Chưa cấu hình kênh nào — điền NTFY_TOPIC / TELEGRAM_BOT_TOKEN+CHAT_ID / "
                    "DISCORD_WEBHOOK vào file .env rồi bấm lại (không cần restart)."},
                    ensure_ascii=False), "application/json; charset=utf-8")
            else:
                res = notify.send_sync("Bambu A1: test chuông 🔔",
                                       "Hub gửi thử — nhận được là cấu hình OK.")
                self._send(200, json.dumps({"ok": True, "sent": res}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif path.startswith("/api/vision-check"):
            # Nut 'AI soi ban in' tren dashboard — chup camera + vision, tra verdict
            jpg = camera_stream.get_frame(IP, CODE, wait_s=10)
            if not jpg:
                self._send(200, json.dumps({"ok": False, "answer":
                    "Không lấy được ảnh camera (máy tắt / đang kết nối)."},
                    ensure_ascii=False), "application/json; charset=utf-8")
                return
            a = ai_chat.ask_vision(
                "Đây là ảnh camera bàn in đang chạy. Soi kỹ các lỗi: spaghetti (nhựa "
                "rối), nhựa RỦ/chảy xệ, LỆCH TRỤC, XƠ/kéo sợi, cong vênh mép. DÒNG ĐẦU: "
                "'KQ: ON' / 'KQ: NGHI NGO' / 'KQ: HONG'. Chỉ NGHI NGO/HONG khi THẤY RÕ "
                "lỗi cụ thể; không thấy thì PHẢI 'KQ: ON'. Sau đó 1-3 dòng lý do ngắn.",
                [jpg], context=_status_text())
            self._send(200, json.dumps(
                {"ok": bool(a), "answer": a or "AI vision không phản hồi — thử lại."},
                ensure_ascii=False), "application/json; charset=utf-8")
        elif path.startswith("/api/camera.jpg"):
            # 1 frame moi nhat tu camera tich hop A1 (cong 6000) — fallback/thumbnail
            f = camera_stream.get_frame(IP, CODE, wait_s=8)
            if f:
                self._send(200, f, "image/jpeg")
            else:
                self._send(503, "Camera chưa có hình: " + (camera_stream.last_error() or
                           "đang kết nối — thử lại sau vài giây"), "text/plain; charset=utf-8")
        elif path.startswith("/api/camera"):
            # MJPEG stream lien tuc (multipart/x-mixed-replace) — nhung <img> la chay.
            # n tab cung xem van chi 1 ket noi toi may in (camera_stream cache frame).
            try:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=lpcam")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for f in camera_stream.mjpeg_frames(IP, CODE):
                    self.wfile.write(b"--lpcam\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(f)).encode()
                                     + b"\r\n\r\n" + f + b"\r\n")
            except (ConnectionError, OSError):
                pass                        # user dong tab/chuyen trang — binh thuong
        elif path.startswith("/api/plateimg"):
            # Anh khay (T2) — png Bambu Studio render san, _run_analyze da boc ra dia
            # LUU Y: `path` da bi cat query o dau do_GET -> phai parse tu self.path
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            nm = os.path.basename(unquote(q.get("name", [""])[0]).replace("\\", "/")).strip()
            try:
                pl = int(q.get("plate", ["1"])[0])
            except ValueError:
                pl = 1
            fp = os.path.join(SLICE_DIR, "plateimg", f"{nm}.plate_{pl}.png")
            if nm and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, "no plate image", "text/plain")
        elif path.startswith("/api/filpreset"):
            # Preset filament an toan (T3) — combo box chon nhua -> tai .json rieng.
            # slot=N (khe AMS that): ten preset KEM MAU (user chot 2026-07-17 —
            # 'phai luu thanh mau chu': Matte DEN 230/12 khac Matte trang) + nhung
            # dung ma mau cuon vao filament_colour.
            # LUU Y: `path` da bi cat query o dau do_GET -> phai parse tu self.path
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            fil = unquote(q.get("fil", [""])[0]).strip()
            custom = unquote(q.get("custom", [""])[0]).strip()
            hexcol = ""
            try:
                slot = int(q.get("slot", ["0"])[0])
            except ValueError:
                slot = 0
            if slot:
                t = next((x for x in _ams_filament_presets() if x["slot"] == slot), None)
                if t:
                    fil = t["sub"]
                    hexcol = t["color"]
                    custom = custom or _color_name(hexcol)
            r = analyzer.filament_preset(fil, custom)
            if r and hexcol:
                r["preset"]["filament_colour"] = [hexcol]
                r["preset"]["default_filament_colour"] = [hexcol]
            if not r:
                self._send(404, json.dumps(
                    {"ok": False, "msg": f"Không có preset an toàn cho '{fil}'"},
                    ensure_ascii=False), "application/json; charset=utf-8")
            else:
                self._send(200, json.dumps({"ok": True, **r}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif path.startswith("/api/filemeta"):
            fpath = self._qs_path()
            if not fpath:
                self._send(400, json.dumps({"ok": False}), "application/json")
                return
            # NHANH: doc 96KB cuoi file qua FTP REST (muc luc zip) thay vi tai ca file
            # 30MB. Cache .json de lan sau tuc thi. Thumb van tai day du o /api/filethumb.
            key = _cache_key(os.path.basename(fpath))
            meta = os.path.join(CACHE_DIR, key + ".json")
            sliced = None
            if os.path.isfile(meta):
                try:
                    with open(meta, encoding="utf-8") as f:
                        sliced = json.load(f).get("sliced")
                except (OSError, ValueError):
                    pass
            if sliced is None:
                sliced = filament_ftp.probe_sliced(IP, CODE, fpath)
                if sliced is not None:
                    try:
                        os.makedirs(CACHE_DIR, exist_ok=True)
                        with open(meta, "w", encoding="utf-8") as f:
                            json.dump({"sliced": bool(sliced)}, f)
                    except OSError:
                        pass
                else:
                    # May Bambu KHONG ho tro FTP REST (502) -> probe nhanh bat luc.
                    # Roi ve tai DAY DU (cham lan dau, ensure_file_meta tu cache .json+.png)
                    _, sliced = ensure_file_meta(fpath)
            if sliced is None:
                self._send(200, json.dumps({"ok": False, "sliced": False}),
                           "application/json; charset=utf-8")
            else:
                self._send(200, json.dumps({"ok": True, "sliced": bool(sliced)}),
                           "application/json; charset=utf-8")
        elif path == "/a1.jpg":
            if A1_IMG:
                self._send(200, A1_IMG, "image/jpeg")
            else:
                self._send(404, "no image", "text/plain")
        elif path == "/ams.jpg":
            if AMS_IMG:
                self._send(200, AMS_IMG, "image/jpeg")
            else:
                self._send(404, "no image", "text/plain")
        elif path == "/thumb.png":
            with JOB_LOCK:
                thumb = JOB["thumb"]
            if thumb:
                self._send(200, thumb, "image/png")
            else:
                self._send(404, "no thumb", "text/plain")
        elif path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/info":
            self._send(200, INFO_PAGE, "text/html; charset=utf-8")
        elif path == "/files":
            self._send(200, FILES_PAGE, "text/html; charset=utf-8")
        elif path == "/analyze":
            self._send(200, ANALYZE_PAGE, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return {}

    MAX_UPLOAD = 300 * 1024 * 1024      # 300 MB — .gcode.3mf that hiem khi qua 100

    def _do_upload(self):
        """Nhan raw bytes tu trinh duyet -> STOR len the SD cua may qua FTPS.

        Nguoi dung bam nut tren web moi chay. Chot chan: chi .3mf, cat ve
        basename (chan path traversal), gioi han dung luong.
        """
        from urllib.parse import urlparse, parse_qs, unquote
        raw = unquote(parse_qs(urlparse(self.path).query).get("name", [""])[0])
        name = os.path.basename(raw.replace("\\", "/")).strip()
        if not name or name in (".", ".."):
            self._send(400, json.dumps({"ok": False, "msg": "Thiếu tên file"}),
                       "application/json; charset=utf-8")
            return
        if not name.lower().endswith((".3mf", ".stl")):
            self._send(400, json.dumps({"ok": False, "msg": "Chỉ nhận file .3mf hoặc .stl"}),
                       "application/json; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0:
            self._send(400, json.dumps({"ok": False, "msg": "File rỗng"}),
                       "application/json; charset=utf-8")
            return
        if n > self.MAX_UPLOAD:
            self._send(413, json.dumps({"ok": False, "msg": "File quá lớn (>300 MB)"}),
                       "application/json; charset=utf-8")
            return
        try:
            data = self.rfile.read(n)
        except OSError as e:
            self._send(400, json.dumps({"ok": False, "msg": f"Đọc file lỗi: {e}"}),
                       "application/json; charset=utf-8")
            return

        # Phan luong: file DA slice -> chuyen thang xuong may in nhu truoc.
        # File CHUA slice (du an tho) -> slice bang Bambu Studio CLI truoc (chay nen).
        os.makedirs(SLICE_DIR, exist_ok=True)
        src = os.path.join(SLICE_DIR, "in_" + name)
        try:
            with open(src, "wb") as f:
                f.write(data)
        except OSError as e:
            self._send(500, json.dumps({"ok": False, "msg": f"Ghi file tạm lỗi: {e}"}),
                       "application/json; charset=utf-8")
            return

        if filament_ftp.parse_is_sliced(src):
            try:
                os.remove(src)
            except OSError:
                pass
            with THUMB_LOCK:            # dung chung khoa FTP: khong tai/day song song
                ok, msg = filament_ftp.upload_file(IP, CODE, data, name)
            if ok:
                FILES_CACHE["ts"] = 0   # ep lam moi danh sach file
                self._send(200, json.dumps({"ok": True, "path": msg, "name": name,
                                            "sliced": True}),
                           "application/json; charset=utf-8")
            else:
                self._send(502, json.dumps({"ok": False, "msg": f"FTP lỗi: {msg}"}),
                           "application/json; charset=utf-8")
            return

        # Chua slice -> can CLI + chi 1 job mot luc
        if not slicer_cli.find_exe():
            self._send(501, json.dumps({"ok": False, "msg":
                "File CHƯA slice và máy chủ không có Bambu Studio — hãy slice rồi upload lại"}),
                "application/json; charset=utf-8")
            return
        with UPJOB_LOCK:
            if UPJOB["state"] in ("slicing", "pushing"):
                self._send(409, json.dumps({"ok": False, "msg":
                    f"Đang slice file khác ({UPJOB['name']}) — chờ xong đã"}),
                    "application/json; charset=utf-8")
                return
            UPJOB.update(state="slicing", name=name, msg="Bắt đầu slice…", stats=None)
        q = parse_qs(urlparse(self.path).query)
        mode = unquote(q.get("mode", [""])[0]).strip().lower()
        if mode not in ("fast", "balanced", "quality"):
            mode = None
        # download=1 -> CHI slice de tai ve (khong day xuong may)
        push = q.get("download", ["0"])[0] not in ("1", "true", "yes")
        # plate=N (file nhieu khay, tab dang chon): slice DUNG khay do — stats/gcode
        # khop khay user nhin, khong con canh "so cua khay 1 nhung in khay khac"
        # (review HIGH-3). Khong truyen -> 0 = slice het nhu cu.
        try:
            plate = int(q.get("plate", ["0"])[0])
        except ValueError:
            plate = 0
        # Nhua chon (dong bo voi analyze) + chinh sua truoc in (#2/#3/#4 2026-07-19)
        try:
            _slot = int(q.get("slot", ["0"])[0]) or None
        except ValueError:
            _slot = None
        sel = {"slot": _slot, "fil": unquote(q.get("fil", [""])[0]).strip(),
               "color": unquote(q.get("color", [""])[0]).strip()}
        overrides = {k: unquote(q.get("ov_" + k, [""])[0]).strip() for k in
                     ("layer", "init_layer", "outer", "brim", "brim_w", "skirt",
                      "support", "sup_type", "sup_style", "sup_angle", "sup_onplate",
                      "sup_ztop", "sup_zbot", "sup_itop", "sup_ibot", "sup_ispacing",
                      "sup_ipattern", "sup_ifil", "sup_basefil")}
        overrides = {k: v for k, v in overrides.items() if v != ""}
        _ss = unquote(q.get("sup_strat", [""])[0]).strip()   # cach lam support user chon
        if _ss:
            overrides["sup_strat"] = _ss
        threading.Thread(target=_slice_and_push,
                         args=(name, src, mode, push, plate, sel, overrides),
                         daemon=True).start()
        self._send(200, json.dumps({"ok": True, "queued": True, "name": name}),
                   "application/json; charset=utf-8")

    def _do_analyze(self):
        """Nhan file -> tra ve NGAY (queued) -> thread nen phan tich -> UI poll."""
        from urllib.parse import urlparse, parse_qs, unquote
        q = parse_qs(urlparse(self.path).query)
        raw = unquote(q.get("name", [""])[0])
        name = os.path.basename(raw.replace("\\", "/")).strip()
        try:
            plate = int(q.get("plate", ["0"])[0]) or None   # tab khay goi lai voi plate=N
        except ValueError:
            plate = None
        try:
            _slot = int(q.get("slot", ["0"])[0]) or None     # nhua chon: khe AMS cu the
        except ValueError:
            _slot = None
        sel = {"slot": _slot, "fil": unquote(q.get("fil", [""])[0]).strip(),
               "color": unquote(q.get("color", [""])[0]).strip()}
        if not name.lower().endswith((".3mf", ".stl")):
            self._send(400, json.dumps({"ok": False, "msg": "Chỉ phân tích .3mf hoặc .stl"}),
                       "application/json; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0 or n > self.MAX_UPLOAD:
            self._send(400, json.dumps({"ok": False, "msg": "File rỗng hoặc quá lớn"}),
                       "application/json; charset=utf-8")
            return
        with ANJOB_LOCK:
            if ANJOB["state"] == "running":
                self._send(409, json.dumps({"ok": False, "msg":
                    f"Đang phân tích file khác ({ANJOB['name']}) — chờ chút"}),
                    "application/json; charset=utf-8")
                return
            ANJOB.update(state="running", name=name,
                         msg="Đang đọc mesh + tính toán…", result=None)
        os.makedirs(SLICE_DIR, exist_ok=True)
        tmp = os.path.join(SLICE_DIR, "an_" + name)
        try:
            with open(tmp, "wb") as f:
                f.write(self.rfile.read(n))
        except OSError as e:
            with ANJOB_LOCK:
                ANJOB.update(state="error", msg=str(e))
            self._send(500, json.dumps({"ok": False, "msg": f"Ghi file lỗi: {e}"}),
                       "application/json; charset=utf-8")
            return
        threading.Thread(target=_run_analyze, args=(name, tmp, plate, sel), daemon=True).start()
        self._send(200, json.dumps({"ok": True, "queued": True}),
                   "application/json; charset=utf-8")

    def _do_optimize(self):
        from urllib.parse import urlparse, parse_qs, unquote
        q = parse_qs(urlparse(self.path).query)
        raw = unquote(q.get("name", [""])[0])
        name = os.path.basename(raw.replace("\\", "/")).strip()
        try:
            plate = int(q.get("plate", ["0"])[0]) or None   # khay dang chon tren tab
        except ValueError:
            plate = None
        try:
            _slot = int(q.get("slot", ["0"])[0]) or None     # nhua chon (dong bo voi analyze)
        except ValueError:
            _slot = None
        sel = {"slot": _slot, "fil": unquote(q.get("fil", [""])[0]).strip(),
               "color": unquote(q.get("color", [""])[0]).strip()}
        if not name.lower().endswith((".3mf", ".stl")):
            self._send(400, json.dumps({"ok": False, "msg": "Chỉ nhận .3mf / .stl"}),
                       "application/json; charset=utf-8"); return
        with OPTJOB_LOCK:
            if OPTJOB["state"] == "running":
                self._send(409, json.dumps({"ok": False, "msg": "Đang tối ưu file khác"}),
                           "application/json; charset=utf-8"); return
            OPTJOB.update(state="running", name=name, msg="Bắt đầu…", report=None)
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        os.makedirs(SLICE_DIR, exist_ok=True)
        tmp = os.path.join(SLICE_DIR, "opt_" + name)
        with open(tmp, "wb") as f:
            f.write(self.rfile.read(n))
        threading.Thread(target=_run_optimize, args=(name, tmp, plate, sel), daemon=True).start()
        self._send(200, json.dumps({"ok": True, "queued": True}),
                   "application/json; charset=utf-8")

    def _same_origin(self) -> bool:
        """Chan CSRF: trinh duyet LUON gui Origin voi cross-site POST fetch —
        khac Host la request tu trang web la, tu choi. curl/script noi bo khong
        gui Origin -> cho qua (khong phai vector CSRF)."""
        raw = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not raw:
            return True
        from urllib.parse import urlparse
        return urlparse(raw).netloc == (self.headers.get("Host") or "")

    def do_POST(self):
        # /api/agent = BACKEND API CONG KHAI (webhook / Cloudflare Tunnel / domain rieng
        # goi toi). KHONG dung CSRF same-origin (caller o ngoai) ma dung KHOA API. Router
        # phan loai domain: bambu (may in) vs assistant (viec/note/chi tieu). Dat TRUOC
        # check same-origin.
        if self.path == "/api/agent":
            key = (notify._env().get("AGENT_API_KEY") or "").strip()   # noqa: SLF001
            got = (self.headers.get("X-API-Key")
                   or self.headers.get("Authorization", "").replace("Bearer", "")).strip()
            if not key or got != key:
                self._send(401, json.dumps({"ok": False, "msg": "Sai/thiếu API key"}),
                           "application/json; charset=utf-8"); return
            body = self._read_json()
            text = (body.get("text") or body.get("q") or "").strip()[:2000]
            if not text:
                self._send(400, json.dumps({"ok": False, "msg": "Thiếu 'text'"}),
                           "application/json; charset=utf-8"); return
            try:
                dom, reply = agent.handle(
                    text, printer_ctx=_status_text() + "\n" + _temps_text())
                self._send(200, json.dumps({"ok": True, "domain": dom, "reply": reply},
                           ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as ex:                       # noqa: BLE001
                self._send(500, json.dumps({"ok": False,
                           "msg": f"{type(ex).__name__}: {str(ex)[:200]}"},
                           ensure_ascii=False), "application/json; charset=utf-8")
            return
        if self.path == "/api/clientlog":               # CHAN DOAN: log loi JS client
            n = int(self.headers.get("Content-Length", 0) or 0)
            msg = self.rfile.read(n).decode("utf-8", "ignore")[:500] if n else ""
            notify._log(f"[clientlog] {msg}")            # noqa: SLF001
            self._send(200, "{}", "application/json")
            return
        if self.path == "/api/preset-dl":
            # TAI kieu SERVER (bulletproof — khong dinh quirk blob/revokeURL cua trinh
            # duyet lam "tai treo"). Form POST tu trang -> tra attachment. Chi echo data
            # user vua gui (JSON preset cua chinh ho) nen an toan.
            import urllib.parse as _up
            n = int(self.headers.get("Content-Length", 0) or 0)
            q = _up.parse_qs(self.rfile.read(n).decode("utf-8", "ignore")) if n else {}
            raw_nm = (q.get("name", ["preset.json"]) or ["preset.json"])[0]
            nm = ("".join(c for c in raw_nm if c.isalnum() or c in "._- ")[:120]) or "preset.json"
            b = ((q.get("data", ["{}"]) or ["{}"])[0]).encode("utf-8")
            notify._log(f"[preset-dl] hit -> {nm} ({len(b)} bytes)")   # noqa: SLF001 — chan doan
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{nm}"')
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if not self._same_origin():
            self._send(403, json.dumps({"ok": False, "msg": "Origin không khớp — chặn CSRF"}),
                       "application/json; charset=utf-8")
            return
        if self.path == "/api/ai-chat":
            # Hoi dap AI (Nemotron/OpenRouter) — biet trang thai may THAT + kho so hub
            body = self._read_json()
            q = (body.get("q") or "").strip()[:2000]
            if not q:
                self._send(400, json.dumps({"ok": False, "msg": "Thiếu câu hỏi"}),
                           "application/json; charset=utf-8"); return
            if not ai_chat.enabled():
                self._send(200, json.dumps({"ok": False, "answer":
                    "Chưa cấu hình OPENROUTER_API_KEY trong .env."}, ensure_ascii=False),
                    "application/json; charset=utf-8"); return
            with LOCK:
                dd = dict(STATE["data"])
            rem = 0
            try:
                rem = int(dd.get("mc_remaining_time") or 0)
            except (TypeError, ValueError):
                pass
            ctx = (f"Trạng thái: {dd.get('gcode_state')} · file "
                   f"{dd.get('subtask_name') or dd.get('gcode_file')} · "
                   f"{dd.get('mc_percent')}% · lớp {dd.get('layer_num')}/"
                   f"{dd.get('total_layer_num')} · còn ~{rem // 60}h{rem % 60:02d}m · "
                   f"nozzle {dd.get('nozzle_temper')}→{dd.get('nozzle_target_temper')}°C · "
                   f"bàn {dd.get('bed_temper')}→{dd.get('bed_target_temper')}°C · "
                   f"khay AMS: {', '.join(_ams_tray_types()) or 'chưa sync'}")
            a = ai_chat.ask(q, context=ctx)
            self._send(200, json.dumps(
                {"ok": bool(a), "answer": a or "AI không phản hồi (model free có thể hết "
                 "lượt hôm nay) — thử lại sau."}, ensure_ascii=False),
                "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/optimize"):
            self._do_optimize(); return
        if self.path.startswith("/api/analyze"):
            self._do_analyze()
            return
        if self.path == "/api/cmd/pause":
            ok, msg = cmd_print("pause")
        elif self.path == "/api/cmd/resume":
            ok, msg = cmd_print("resume")
        elif self.path == "/api/cmd/stop":
            ok, msg = cmd_print("stop")
        elif self.path == "/api/print":
            body = self._read_json()
            name = (body.get("name") or "").strip()
            fpath = (body.get("path") or "").strip()
            if not name:
                self._send(400, json.dumps({"ok": False, "msg": "thieu ten file"}), "application/json")
                return
            if is_busy():
                self._send(409, json.dumps({"ok": False, "msg": "Máy đang bận (đang in) — không thể in file mới"}), "application/json; charset=utf-8")
                return
            ok, msg = cmd_project_file(name, fpath)
            self._send(200, json.dumps({"ok": ok, "msg": msg}), "application/json; charset=utf-8")
            return
        elif self.path.startswith("/api/upload"):
            self._do_upload()
            return
        elif self.path.startswith("/api/push-last"):
            # Day file .gcode.3mf slice GAN NHAT xuong may in (buoc 2: da Slice lai xong)
            with UPJOB_LOCK:
                fp, fn = LAST_SLICED.get("path"), LAST_SLICED.get("name")
            if not fp or not os.path.isfile(fp):
                self._send(409, json.dumps({"ok": False, "msg":
                    "Chưa có file slice — bấm Slice lại trước"}, ensure_ascii=False),
                    "application/json; charset=utf-8")
                return
            try:
                with open(fp, "rb") as f:
                    data = f.read()
                with THUMB_LOCK:                       # dung chung khoa FTP
                    ok, msg = filament_ftp.upload_file(IP, CODE, data, fn)
            except OSError as e:
                ok, msg = False, str(e)
            if ok:
                FILES_CACHE["ts"] = 0                  # lam moi danh sach file may
                self._send(200, json.dumps({"ok": True, "name": fn}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(502, json.dumps({"ok": False, "msg": f"FTP lỗi: {msg}"},
                           ensure_ascii=False), "application/json; charset=utf-8")
            return
        elif self.path == "/api/printer-config":
            body = self._read_json()
            host = (body.get("host") or "").strip() or IP
            serial = (body.get("serial") or "").strip() or SERIAL
            code = (body.get("code") or "").strip()
            # Doi HOST bat buoc nhap lai access code — chan viec lai hub tro sang server
            # la roi tu dong dem code cu theo (phat hien boi code-review CSRF).
            if host != IP and not code:
                self._send(400, json.dumps({"ok": False, "msg": "Đổi IP thì phải nhập lại Access Code (chống trỏ nhầm/tấn công)"}), "application/json; charset=utf-8")
                return
            code = code or CODE
            if re.fullmatch(r"[\d.]+", host or ""):
                import ipaddress
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    self._send(400, json.dumps({"ok": False, "msg": "IP không hợp lệ (octet 0-255)"}), "application/json; charset=utf-8")
                    return
            elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", host or ""):
                self._send(400, json.dumps({"ok": False, "msg": "IP/host không hợp lệ"}), "application/json; charset=utf-8")
                return
            if not re.fullmatch(r"[A-Za-z0-9]{8}", code or ""):
                self._send(400, json.dumps({"ok": False, "msg": "Access code phải đúng 8 ký tự chữ/số (xem màn hình máy in: Cài đặt → WLAN)"}), "application/json; charset=utf-8")
                return
            if not re.fullmatch(r"[A-Za-z0-9]{10,20}", serial or ""):
                self._send(400, json.dumps({"ok": False, "msg": "Serial không hợp lệ (10-20 ký tự chữ/số)"}), "application/json; charset=utf-8")
                return
            try:
                update_printer_config(host, serial, code)
            except Exception as e:                       # noqa: BLE001 — ghi file loi (quyen/dia)
                self._send(500, json.dumps({"ok": False, "msg": f"Lưu cấu hình lỗi: {e}"}), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({"ok": True, "msg": "Đã lưu vào .env — đang kết nối lại máy in..."}), "application/json; charset=utf-8")
            return
        elif self.path == "/api/filament":
            body = self._read_json()
            tag = (body.get("tag_uid") or "").strip()
            try:
                rem = float(body.get("remaining"))
            except (TypeError, ValueError):
                self._send(400, json.dumps({"ok": False, "msg": "remaining khong hop le"}), "application/json")
                return
            if not tag:
                self._send(400, json.dumps({"ok": False, "msg": "thieu tag_uid"}), "application/json")
                return
            rec = filament_store.set_remaining(tag, rem, body.get("net"))
            self._send(200, json.dumps({"ok": True, "rec": rec}), "application/json; charset=utf-8")
            return
        else:
            self._send(404, json.dumps({"ok": False, "msg": "unknown cmd"}), "application/json")
            return
        self._send(200, json.dumps({"ok": ok, "msg": msg}), "application/json; charset=utf-8")


def _job_weight() -> float | None:
    with JOB_LOCK:
        return JOB.get("weight")


def _job_thumb() -> bytes | None:
    """Anh render model dang in (Bambu nhung san trong file, cache qua FTP)."""
    with JOB_LOCK:
        return JOB.get("thumb")


def _status_text() -> str:
    """Trang thai gon (text tho) cho AI context + caption anh."""
    with LOCK:
        d = dict(STATE["data"])
        on = STATE["connected"]
    if not on and not d:
        return "Chưa kết nối được máy in (máy tắt?)."
    try:
        rem = int(d.get("mc_remaining_time") or 0)
    except (TypeError, ValueError):
        rem = 0
    st = {"IDLE": "Đang rảnh", "RUNNING": "ĐANG IN", "PAUSE": "TẠM DỪNG",
          "FINISH": "In XONG", "FAILED": "In LỖI"}.get(d.get("gcode_state"), d.get("gcode_state") or "?")
    w = _job_weight()
    return (f"{st} · {d.get('mc_percent', '?')}% · lớp {d.get('layer_num', '?')}/"
            f"{d.get('total_layer_num', '?')} · còn ~{rem // 60}h{rem % 60:02d}m"
            + (f" · ~{w}g nhựa" if w else "") + "\n"
            f"File: {d.get('subtask_name') or d.get('gcode_file') or '—'}")


def _status_html() -> str:
    """The TRANG THAI cho Telegram — dung ui_tg (1 nguon UI duy nhat cho moi tin)."""
    with LOCK:
        d = dict(STATE["data"])
        on = STATE["connected"]
    return ui_tg.status_card(d, on, weight=_job_weight(), hub=notify.hub_url())


def _err_code() -> int:
    return int(MILE.get("err") or 0)


def _hms_hex(n: int) -> str:
    """302022663 -> '1200-8007' — dinh dang HEX y het man hinh may in."""
    s = f"{n & 0xFFFFFFFF:08X}"
    return f"{s[:4]}-{s[4:]}"


# Ma HMS da XAC MINH tren may that (chi ghi ma chac chan — con lai de AI/wiki):
HMS_VN = {
    "1200-8007": "ĐÙN NHỰA THẤT BẠI — kẹt extruder / kẹt sợi (màn hình máy: 'Failed "
                 "to extrude'). Đúng ca Matte đen hay gặp: kiểm tra sợi ở extruder, "
                 "nặng thì nâng nozzle 280°C hoá lỏng cặn rồi rút (cold pull).",
}


def _temps_text() -> str:
    """The NHIET & KHAY — bo cuc kieu man hinh Device cua Studio: nhiet hien/dich,
    quat, so do 4 khe co CHAM MAU + danh dau khe dang dung."""
    with LOCK:
        d = dict(STATE["data"])
        ams = (STATE["data"].get("ams") or {})
    try:
        now = int(ams.get("tray_now", 255))
        now = now if now < 4 else -1                 # 254/255 = khong khe nao
    except (TypeError, ValueError):
        now = -1
    return ui_tg.temps_card(d, _ams_tray_types(), _ams_tray_colors(), now=now)


def main():
    threading.Thread(target=mqtt_loop, daemon=True).start()
    # Bot 2 chieu (nut nhanh + AI vision + dieu khien may in) — CUNG mot `hooks` cho ca
    # Telegram va Slack (nao AI = ai_chat chung). Bat/tat tung kenh qua .env:
    # ENABLE_TELEGRAM / ENABLE_SLACK (mac dinh '1'=bat; '0'=tat, khong can xoa token).
    hooks = {
        "status": _status_text, "status_html": _status_html, "temps": _temps_text,
        "frame": lambda: camera_stream.get_frame(IP, CODE, wait_s=8),
        "thumb": _job_thumb, "cmd": cmd_print, "err": _err_code,
        "burst": _burst_frames,
    }
    if notify._on("TELEGRAM"):                               # noqa: SLF001
        telegram_bot.start(hooks)
    if notify._on("SLACK"):                                  # noqa: SLF001
        slack_bot.start(hooks)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("=" * 56)
    print("  BAMBU WEB DASHBOARD + DIEU KHIEN dang chay")
    print(f"  May in : {IP}  serial {SERIAL}")
    print(f"  Tren PC : http://localhost:{PORT}")
    print(f"  Dien thoai (cung LAN): http://<IP-PC>:{PORT}")
    print("  Nut Pause/Resume/Stop = NGUOI DUNG bam (AI khong dieu khien).")
    print("  Ctrl+C de dung.")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDung.")


if __name__ == "__main__":
    main()
