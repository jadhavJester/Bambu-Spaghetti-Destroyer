#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot Telegram 2 CHIEU v2 — nut nhanh + AI vision + DIEU KHIEN may in.

Ban phim thuong truc:
  📊 Tình hình in      → bao cao HTML dep (thanh tien do, lop, gio, GAM NHUA)
  📷 Ảnh bàn in        → chup camera A1 gui vao chat
  🔍 Phân tích bản in  → AI VISION nhin anh camera + anh render model -> danh gia
  🌡️ Nhiệt & khay      → nozzle/bed + 4 khe AMS
  💡 Mẹo in            → AI meo theo nhua/model dang in
  🧯 Hỏi lỗi           → ma loi hien tai + AI giai thich
  ⏸ Tạm dừng / ▶️ Tiếp tục → lenh truc tiep
  ⏹ DỪNG HẲN           → 2 BUOC: bot hoi lai, phai go 'DUNG XAC NHAN' trong 60s
  Go cau hoi bat ky    → AI; cau hoi nhac anh/nhin/san pham -> tu dinh kem anh (vision)

BAO MAT: chi tra loi TELEGRAM_CHAT_ID trong .env — nguoi la im lang tuyet doi.
hooks tiem tu bambu_web (khong import vong): status_html/status/temps/frame/thumb/
cmd(pause|resume|stop)/err.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid

import ai_chat
import notify
import ui_tg

B_STATUS, B_PHOTO = "📊 Tình hình in", "📷 Ảnh bàn in"
B_ANALYZE, B_TEMP = "🔍 Phân tích bản in qua AI Vision", "🌡️ Nhiệt & khay"
B_ANALYZE_OLD = "🔍 Phân tích bản in"   # ban phim cu con cache tren may user van an
B_TIP, B_ERR = "💡 Mẹo in", "🧯 Hỏi lỗi"
B_USAGE = "💰 Chi phí AI"
B_PAUSE, B_RESUME, B_STOP = "⏸ Tạm dừng", "▶️ Tiếp tục", "⏹ DỪNG HẲN"
STOP_WORD = "DUNG XAC NHAN"

_KEYBOARD = {"keyboard": [
    [{"text": B_STATUS}, {"text": B_PHOTO}],
    [{"text": B_ANALYZE}, {"text": B_TEMP}],
    [{"text": B_TIP}, {"text": B_ERR}, {"text": B_USAGE}],
    [{"text": B_PAUSE}, {"text": B_RESUME}, {"text": B_STOP}],
], "resize_keyboard": True, "is_persistent": True}

# tu khoa -> cau hoi tu do se duoc dinh kem ANH (camera + render model) cho AI vision
_VISION_WORDS = ("ảnh", "anh ban in", "hình", "hinh", "nhìn", "nhin", "camera",
                 "sản phẩm", "san pham", "spaghetti", "hỏng", "hong", "bong",
                 "lệch", "lech", "xơ", "xo", "bề mặt", "be mat")

_PEND = {"stop_until": 0.0}

# ── LICH SU HOI THOAI theo chat (bug 2026-08-03: bot "mat ngu canh/context"). Giu
#    toi da _HIST_TURNS luot gan nhat (moi luot = 1 user + 1 assistant) de AI nho
#    mach hoi thoai. Reset bang /reset hoac /moi.
_HIST: dict = {}
_HIST_TURNS = 5                                          # 5 luot ~ 10 tin, du ngu canh


def _hist_get(chat: str) -> list:
    return _HIST.get(str(chat), [])


def _hist_add(chat: str, q: str, a: str) -> None:
    h = _HIST.setdefault(str(chat), [])
    h.append({"role": "user", "content": q})
    h.append({"role": "assistant", "content": a})
    del h[: max(0, len(h) - _HIST_TURNS * 2)]           # cat con _HIST_TURNS luot


def _hist_clear(chat: str) -> None:
    _HIST.pop(str(chat), None)


def _cmd_model(token: str, chat: str, text: str) -> None:
    """/model = liet ke + hien model dang dung. /model <so> = GHIM 1 model (on dinh)."""
    models = ai_chat.MODELS
    cur = ai_chat.pinned_model() or "auto"
    cur_name = next((n for mid, n, _ in models if mid == cur), cur)
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        lines = ["🤖 <b>Model AI</b> — gõ <code>/model &lt;số&gt;</code> để ghim (khoá 1 model, "
                 "hết đổi liên tục):"]
        for i, (mid, name, note) in enumerate(models, 1):
            mark = " ✅" if mid == cur else ""
            lines.append(f"<b>{i}. {name}</b>{mark}\n   <i>{note}</i>")
        lines.append(f"\nĐang dùng: <b>{cur_name}</b>")
        _send(token, chat, "\n".join(lines))
        return
    pick = None
    if arg.isdigit() and 1 <= int(arg) <= len(models):
        pick = models[int(arg) - 1]
    else:
        al = arg.lower()
        pick = next((m for m in models if al in m[0].lower() or al in m[1].lower()), None)
    if not pick:
        _send(token, chat, "Số/tên model không đúng. Gõ <b>/model</b> để xem danh sách.")
        return
    ai_chat.set_model(pick[0])
    tail = ("\n(auto = model đổi theo cái nào đáp trước)" if pick[0] == "auto"
            else "\n(đã KHOÁ 1 model — không đổi nữa)")
    _send(token, chat, f"✅ Ghim model: <b>{pick[1]}</b>\n<i>{pick[2]}</i>{tail}")


def _cfg() -> tuple[str | None, str | None]:
    e = notify._env()                                    # noqa: SLF001 — cung nguon .env
    return e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_CHAT_ID")


def _api(token: str, method: str, payload: dict, timeout: int = 65) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _send(token: str, chat: str, text: str, html: bool = True) -> None:
    p = {"chat_id": chat, "text": text, "reply_markup": _KEYBOARD}
    if html:
        p["parse_mode"] = "HTML"
    try:
        _api(token, "sendMessage", p, timeout=20)
    except Exception:                                    # noqa: BLE001
        if html:                                         # HTML loi (ky tu la) -> gui tho
            _send(token, chat, text, html=False)


def _send_photo(token: str, chat: str, jpg: bytes, caption: str = "") -> None:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"photo\"; "
            f"filename=\"cam.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode("utf-8")
    body += jpg + f"\r\n--{b}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def _sig(answer: str | None, fallback: str) -> str:
    """Gan TEN MODEL duoi cau tra loi (user yeu cau 2026-07-22). Chuoi fallback nhieu
    model nen khong doan truoc duoc con nao thuc su tra loi — hien ra de biet dang xai
    model gi, mien phi hay dang tieu tien."""
    if not answer:
        return fallback
    m = ai_chat.LAST_MODEL or "?"
    return f"{answer}\n\n— 🤖 {m} ({'miễn phí' if m.endswith(':free') else 'trả phí'})"


def _images(hooks: dict) -> list[bytes]:
    """Anh cho AI vision: [camera that, render model] — cai nao co thi lay."""
    out = []
    cam = hooks["frame"]()
    if cam:
        out.append(cam)
    th = (hooks.get("thumb") or (lambda: None))()
    if th:
        out.append(th)
    return out


def _handle(token: str, chat: str, text: str, hooks: dict) -> None:  # noqa: PLR0912
    t = (text or "").strip()
    tl = t.lower()
    if t in ("/start", "/help"):
        _send(token, chat, "🖨 <b>Bot máy in A1</b> — bấm nút bên dưới, hoặc gõ câu hỏi "
                           "bất kỳ (AI trả lời; câu hỏi nhắc tới ảnh/sản phẩm sẽ tự kèm "
                           "ảnh camera cho AI nhìn).")
    elif t in (B_STATUS, "/status"):
        _send(token, chat, hooks["status_html"]())
    elif t == B_TEMP:
        _send(token, chat, hooks["temps"]())
    elif t in (B_PHOTO, "/photo"):
        jpg = hooks["frame"]()
        if jpg:
            _send_photo(token, chat, jpg, caption=hooks["status"]())
        else:
            _send(token, chat, "Camera chưa có hình (máy tắt / đang kết nối) — thử lại.")
    elif t in (B_ANALYZE, B_ANALYZE_OLD):
        _send(token, chat, "🔍 Đang chụp LOẠT 3 ảnh (cách 4s, chống nhầm do bàn đang "
                           "chạy) + AI vision phân tích…", html=False)
        # burst 3 frame tu bambu_web (chong FP ban bed-slinger dang di chuyen);
        # khong co hook thi fallback 1 frame + render thumb
        imgs = (hooks.get("burst") or (lambda: []))() or _images(hooks)
        if not imgs:
            _send(token, chat, "Không lấy được ảnh camera — máy tắt?")
            return
        a = ai_chat.ask_vision(
            "Đây là LOẠT ảnh camera bàn in chụp cách nhau ~4 giây (máy A1 bed-slinger, "
            "bàn di chuyển liên tục — khối in có thể TRÔNG nghiêng ở 1 ảnh do bàn đang "
            "chạy; chỉ kết luận LỆCH TRỤC khi nghiêng NHẤT QUÁN ở mọi ảnh). Kiểm tra: "
            "spaghetti/bong lớp/lệch trục/xơ nhựa/cong vênh? Nhận xét ngắn từng ý, "
            "chốt 1 dòng: ✅ ỔN / ⚠️ NGHI NGỜ / ❌ HỎNG.",
            imgs, context=hooks["status"]()) \
            or "AI vision không phản hồi (hết lượt free hôm nay?) — xem ảnh bằng nút 📷."
        # PHAN TICH VISION LUON KEM ANH (user chot 2026-07-17) — chon frame NET
        # nhat trong loat (JPEG lon nhat; frame mo do ban chay nen nho hon han)
        ic, lab = ui_tg.verdict_of(a)                    # ket luan -> icon + nhan
        cap = f"{ic} AI VISION: {lab}\n{a}"[:1000]       # caption anh: ket qua len dau
        # Gui CA LOAT anh AI da nhin (album) de user doi chieu
        if not notify.send_photos_telegram(imgs, caption=cap):
            _send(token, chat, a, html=False)            # gui anh loi -> van co text
    elif t == B_TIP:
        a = ai_chat.ask("Cho 3 mẹo NGẮN, cụ thể, đúng với nhựa và bản in đang chạy "
                        "(theo bối cảnh). Mỗi mẹo 1 dòng bắt đầu bằng 💡.",
                        context=hooks["status"]() + "\n" + hooks["temps"]()) \
            or "AI không phản hồi — thử lại sau."
        _send(token, chat, a, html=False)
    elif t == B_ERR:
        err = (hooks.get("err") or (lambda: 0))()
        if not err:
            _send(token, chat, "✅ Máy KHÔNG báo mã lỗi nào. Nếu thấy in xấu, bấm "
                               "🔍 Phân tích bản in để AI soi ảnh camera.")
        else:
            a = ai_chat.ask(f"Máy Bambu A1 đang báo mã lỗi {err} (hex {err:X}). Giải thích "
                            f"ngắn khả năng nguyên nhân + cách xử lý theo mã HMS Bambu.",
                            context=hooks["status"]()) or ""
            _send(token, chat, f"🚨 Mã lỗi <b>{err}</b> (hex {err:X})\n{a}\n"
                               f"Tra chính thức: wiki.bambulab.com · {notify.hub_url()}")
    elif t in (B_USAGE, "/usage"):
        _send(token, chat, ai_chat.usage_report())
    elif t == B_PAUSE:
        ok, msg = hooks["cmd"]("pause")
        _send(token, chat, "⏸ Đã gửi lệnh TẠM DỪNG." if ok else f"Lỗi: {msg}")
    elif t == B_RESUME:
        ok, msg = hooks["cmd"]("resume")
        _send(token, chat, "▶️ Đã gửi lệnh TIẾP TỤC." if ok else f"Lỗi: {msg}")
    elif t == B_STOP:
        _PEND["stop_until"] = time.time() + 60
        _send(token, chat, f"⚠️ DỪNG HẲN sẽ HỦY bản in, không tiếp tục lại được.\n"
                           f"Chắc chắn thì gõ đúng: <b>{STOP_WORD}</b> (trong 60 giây).")
    elif tl.replace("ừ", "u").replace("ậ", "a").replace("dừng", "dung").upper().replace("Đ", "D") == STOP_WORD \
            or t.upper() == STOP_WORD:
        if time.time() <= _PEND["stop_until"]:
            _PEND["stop_until"] = 0
            ok, msg = hooks["cmd"]("stop")
            _send(token, chat, "⏹ Đã gửi lệnh DỪNG HẲN." if ok else f"Lỗi: {msg}")
        else:
            _send(token, chat, "Hết hạn xác nhận — bấm ⏹ DỪNG HẲN lại nếu vẫn muốn dừng.")
    elif t in ("/reset", "/moi", "/quen"):
        _hist_clear(chat)
        _send(token, chat, "🧹 Đã XOÁ ngữ cảnh hội thoại. Câu sau bắt đầu chủ đề mới.")
    elif tl.startswith("/model"):
        _cmd_model(token, chat, t)
    elif t.startswith("/"):
        # lenh go sai (vd /strart) — nhac lenh dung, khong dot luot AI vo ich
        _send(token, chat, "Lệnh không có. Dùng: /start · /status · /photo · /model · "
                           "/reset · /help — hoặc bấm nút bên dưới.")
    else:
        # cau hoi tu do — nhac toi anh/nhin/san pham thi kem ANH cho AI vision
        if any(w in tl for w in _VISION_WORDS):
            imgs = _images(hooks)
            if imgs:
                a = ai_chat.ask_vision(
                    t + "\n(Ảnh 1 = camera bàn in thật; ảnh 2 nếu có = render model.)",
                    imgs, context=hooks["status"]())
                if a:
                    _hist_add(chat, t, a)                # anh cung vao mach hoi thoai
                _send(token, chat, _sig(a, "AI vision không phản hồi — thử lại sau."),
                      html=False)
                return
        # NHO NGU CANH: gui kem lich su cac luot truoc cua chat nay
        a = ai_chat.ask(t, context=hooks["status"]() + "\n" + hooks["temps"](),
                        history=_hist_get(chat))
        if a:
            _hist_add(chat, t, a)
        _send(token, chat, _sig(a, "AI không phản hồi (model free có thể hết lượt hôm "
                                   "nay) — thử lại sau."), html=False)


def _handle_safe(token: str, chat: str, text: str, hooks: dict) -> None:
    """Boc _handle. BUG THAT (user 2026-07-20 'bot im ru'): _handle chay o thread rieng
    KHONG bat loi — bat ky exception nao (hook status/temps, ai_chat, mang) la thread CHET
    IM, user gui tin khong he co hoi am, cung khong co log de biet duong sua. Gio: ghi log
    + BAO LOI ra Telegram de con biet la bot con song va hong o dau."""
    try:
        _handle(token, chat, text, hooks)
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[bot] loi xu ly {text[:40]!r}: {type(e).__name__}: {str(e)[:200]}")  # noqa: SLF001
        try:
            _send(token, chat,
                  f"⚠️ Bot lỗi khi xử lý tin này: {type(e).__name__}: {str(e)[:180]}\n"
                  "Đã ghi vào notify.log. Thử lại, hoặc bấm nút bên dưới.", html=False)
        except Exception:                                    # noqa: BLE001
            pass


def _register_commands(token: str) -> None:
    """Dang ky menu lenh '/' cua Telegram (hien goi y khi go /) — tu lanh moi lan chay."""
    try:
        _api(token, "setMyCommands", {"commands": [
            {"command": "start", "description": "Mở bàn phím nút nhanh"},
            {"command": "status", "description": "📊 Tình hình in"},
            {"command": "photo", "description": "📷 Ảnh bàn in từ camera"},
            {"command": "usage", "description": "💰 Chi phí AI / số dư / còn bao nhiêu lần"},
            {"command": "model", "description": "🤖 Chọn / khoá model AI"},
            {"command": "reset", "description": "🧹 Xoá ngữ cảnh hội thoại"},
            {"command": "help", "description": "Hướng dẫn dùng bot"},
        ]}, timeout=20)
    except Exception:                                    # noqa: BLE001
        pass


LAST_POLL = 0.0     # luc poll thanh cong gan nhat — de biet luong poll con song


def loop(hooks: dict) -> None:
    """Thread nen: long-poll getUpdates; moi tin xu ly o thread rieng (vision cham)."""
    global LAST_POLL                                         # noqa: PLW0603
    offset = 0
    registered = False
    hb = 0.0
    while True:
        # BUG THAT (user 2026-07-22 "ngon ngu tu nhien chua hoat dong"): TRUOC day
        # _cfg() va _register_commands() nam NGOAI try. Chi can MOT lan doc .env loi
        # hoac mang chet la exception thoat khoi loop() -> luong poll CHET VINH VIEN,
        # khong log, khong hoi am. Do la ly do that: khong phai AI, khong phai routing
        # (da do: ai_chat.ask tra loi 4.8s; getUpdates tu ngoai KHONG bi 409 Conflict
        # => chung minh khong con ai poll). Gio BOC TOAN BO than vong lap.
        try:
            token, me = _cfg()
            if not token or not me:
                time.sleep(30)
                continue
            if not registered:
                _register_commands(token)
                registered = True
            r = _api(token, "getUpdates",
                     {"timeout": 50, "offset": offset, "allowed_updates": ["message"]})
            LAST_POLL = time.time()
            if LAST_POLL - hb > 1800:            # nhip tim 30'/lan: log de biet con song
                hb = LAST_POLL
                notify._log(f"[bot] poll song (offset={offset})")   # noqa: SLF001
            for u in r.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                m = u.get("message") or {}
                chat = str((m.get("chat") or {}).get("id") or "")
                if chat != str(me):
                    continue                    # nguoi la — im lang tuyet doi
                threading.Thread(target=_handle_safe,
                                 args=(token, chat, m.get("text") or "", hooks),
                                 daemon=True).start()
        except Exception as e:                  # noqa: BLE001 — mang VN chap chon
            # TRUOC day nuot im -> khong biet bot dang loi gi. Gio GHI LOG (van ngu 5s).
            notify._log(f"[bot] getUpdates loi: {type(e).__name__}: {str(e)[:160]}")  # noqa: SLF001
            time.sleep(5)


def _supervise(hooks: dict) -> None:
    """Canh loop(): no thoat hay chet vi BAT KY ly do gi cung BAT LAI sau 10s.
    Bot im ru la loi ton tien nhat cua hub — mat het canh bao khi ban in dang hong."""
    while True:
        try:
            loop(hooks)
            notify._log("[bot] loop() thoat bat ngo — bat lai sau 10s")   # noqa: SLF001
        except BaseException as e:                           # noqa: BLE001
            notify._log(f"[bot] loop() CHET: {type(e).__name__}: "        # noqa: SLF001
                        f"{str(e)[:200]} — bat lai sau 10s")
        time.sleep(10)


def start(hooks: dict) -> None:
    threading.Thread(target=_supervise, args=(hooks,), daemon=True).start()