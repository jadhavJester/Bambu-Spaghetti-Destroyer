#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot Slack 2 CHIEU — song song Telegram, DUNG LAI 100% nao AI + hooks may in.

Kien truc: bambu_web.py bom cung mot `hooks` (status/status_html/temps/frame/thumb/
burst/cmd/err) cho CA telegram_bot va slack_bot. Nao AI la ai_chat (chung). File nay
CHI la lop TRUYEN TIN cho Slack (Socket Mode = WebSocket ra Slack, khong can URL public).

Nhan:
  - Tin nhan DM / @mention  -> lenh nhanh hoac cau hoi tu do (AI, tu kem anh khi hoi ve anh)
  - BAM NUT (Block Kit)     -> act:<lenh> (tinh hinh/anh/nhiet/phan tich/meo/loi/model/...)
                               va m:<so> (chon-khoa model AI)
Gui:
  - chat.postMessage (text mrkdwn / blocks)  +  files_upload_v2 (anh camera)

Lenh go: tinh hinh · nhiet · anh · phan tich · meo · loi · chi phi · pause · resume ·
         stop (xac nhan 'DUNG XAC NHAN') · model · reset · menu/help. Con lai -> AI.

BAO MAT: token doc tu .env (SLACK_BOT_TOKEN xoxb- / SLACK_APP_TOKEN xapp-), KHONG commit.
Kenh DM cua user duoc ghi ra slack_target.txt de notify.py day CANH BAO (in xong/loi) vao.
"""
from __future__ import annotations

import os
import re
import threading
import time

import agent
import ai_chat
import notify
import ui_tg

# ── lich su hoi thoai theo kenh (giong telegram_bot: AI nho ngu canh 5 luot gan nhat)
_HIST: dict = {}
_HIST_TURNS = 5

# tu khoa -> cau hoi tu do se duoc kem ANH (camera + render model) cho AI vision
_VISION_WORDS = ("ảnh", "anh ban in", "hình", "hinh", "nhìn", "nhin", "camera",
                 "sản phẩm", "san pham", "spaghetti", "hỏng", "hong", "bong",
                 "lệch", "lech", "xơ", "xo", "bề mặt", "be mat")

STOP_WORD = "DUNG XAC NHAN"
_PEND = {"stop_until": 0.0}

# "Cuoc goi ngoai" Slack bat buoc join_url tu dich vu g.oi ben 3 -> dung Jitsi Meet
# (mien phi, khong can tai khoan). Phong co dinh de moi nguoi vao cung 1 cho.
_JITSI_ROOM = "LPBambuHub-A1-59f2c1"

_DIR = os.path.dirname(os.path.abspath(__file__))
_TARGET_PATH = os.path.join(_DIR, "slack_target.txt")

# lenh go: chuoi CHINH XAC (khong quet giua cau) -> tranh nham cau hoi thanh lenh.
_CMD = {
    "menu":   {"", "hi", "hello", "menu", "start", "/start", "help", "/help",
               "trợ giúp", "tro giup", "?"},
    "status": {"status", "/status", "tình hình", "tinh hinh", "trạng thái",
               "trang thai", "tt", "📊"},
    "temp":   {"temp", "nhiệt", "nhiet", "khay", "nhiệt & khay", "🌡️"},
    "photo":  {"photo", "/photo", "ảnh", "anh", "hình", "hinh", "camera",
               "chụp", "chup", "📷"},
    "analyze": {"analyze", "phân tích", "phan tich", "soi", "kiểm tra", "kiem tra", "🔍"},
    "tip":    {"tip", "mẹo", "meo", "mẹo in", "💡"},
    "err":    {"err", "lỗi", "loi", "error", "mã lỗi", "ma loi", "hỏi lỗi", "🧯"},
    "usage":  {"usage", "/usage", "chi phí", "chi phi", "chi phí ai", "💰"},
    "pause":  {"pause", "tạm dừng", "tam dung", "⏸"},
    "resume": {"resume", "tiếp tục", "tiep tuc", "tiếp", "tiep", "▶️"},
    "stop":   {"stop", "dừng hẳn", "dung han", "dừng hắn", "⏹"},
    "reset":  {"/reset", "reset", "/moi", "mới", "moi", "quên", "quen", "🧹"},
    "call":   {"call", "/call", "gọi", "goi", "gọi video", "video", "📞"},
}

_TAGRE = re.compile(r"<[^>]+>")
_MENTION = re.compile(r"<@[A-Z0-9]+>")


# ─────────────────────────────── tien ich ────────────────────────────────
def _hist_get(chat: str) -> list:
    return _HIST.get(str(chat), [])


def _hist_add(chat: str, q: str, a: str) -> None:
    h = _HIST.setdefault(str(chat), [])
    h.append({"role": "user", "content": q})
    h.append({"role": "assistant", "content": a})
    del h[: max(0, len(h) - _HIST_TURNS * 2)]


def _hist_clear(chat: str) -> None:
    _HIST.pop(str(chat), None)


def _md(s: str) -> str:
    """HTML cua ui_tg (Telegram) -> mrkdwn Slack: <b>->*  <i>->_  <code>->`. Cac the
    khac (blockquote/pre/a) lược bo. Giu nguyen &lt; &gt; &amp; — Slack escape y het."""
    s = s or ""
    s = s.replace("<b>", "*").replace("</b>", "*")
    s = s.replace("<i>", "_").replace("</i>", "_")
    s = s.replace("<code>", "`").replace("</code>", "`")
    return _TAGRE.sub("", s)


def _clean(text: str) -> str:
    """Bo <@BOTID> (mention) khoi text cua app_mention + trim."""
    return _MENTION.sub("", text or "").strip()


def _sig(answer: str | None, fallback: str) -> str:
    """Gan TEN MODEL duoi cau tra loi (nhu Telegram) — biet dang xai model gi."""
    if not answer:
        return fallback
    m = ai_chat.LAST_MODEL or "?"
    return f"{answer}\n\n— 🤖 {m} ({'miễn phí' if m.endswith(':free') else 'trả phí'})"


def _images(hooks: dict) -> list[bytes]:
    out = []
    cam = hooks["frame"]()
    if cam:
        out.append(cam)
    th = (hooks.get("thumb") or (lambda: None))()
    if th:
        out.append(th)
    return out


def _remember_target(chat: str) -> None:
    """Ghi kenh de notify.py day CANH BAO vao. Uu tien DM (kenh 'D...'); kenh khac chi
    ghi khi chua co dich nao."""
    if not chat:
        return
    try:
        cur = open(_TARGET_PATH, encoding="utf-8").read().strip()
    except OSError:
        cur = ""
    if chat == cur:
        return
    if chat.startswith("D") or not cur:
        try:
            open(_TARGET_PATH, "w", encoding="utf-8").write(chat)
        except OSError:
            pass


# ─────────────────────────────── gui tin ─────────────────────────────────
def _send(web, chat: str, text: str, blocks: list | None = None) -> None:
    try:
        web.chat_postMessage(channel=chat, text=(text or " ")[:3900],
                             blocks=blocks, mrkdwn=True)
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[slack] send loi: {type(e).__name__}: {str(e)[:160]}")  # noqa: SLF001


def _upload(web, chat: str, jpg: bytes, caption: str = "") -> bool:
    try:
        web.files_upload_v2(channel=chat, file=jpg, filename="cam.jpg",
                            initial_comment=caption[:1500])
        return True
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[slack] upload loi: {type(e).__name__}: {str(e)[:160]}")  # noqa: SLF001
        return False


def _upload_many(web, chat: str, jpgs: list, caption: str = "") -> bool:
    """Gui CA LOAT anh (album) — parity voi Telegram sendMediaGroup, de user doi chieu
    dung cac frame AI da nhin."""
    jpgs = [j for j in (jpgs or []) if j]
    if not jpgs:
        return False
    if len(jpgs) == 1:
        return _upload(web, chat, jpgs[0], caption)
    try:
        ups = [{"file": j, "filename": f"cam{i}.jpg"} for i, j in enumerate(jpgs[:10])]
        web.files_upload_v2(channel=chat, file_uploads=ups, initial_comment=caption[:1500])
        return True
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[slack] upload_many loi: {type(e).__name__}: {str(e)[:160]}")  # noqa: SLF001
        return _upload(web, chat, max(jpgs, key=len), caption)


def _btn(label: str, action_id: str, style: str = "") -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": label, "emoji": True},
         "action_id": action_id, "value": action_id}
    if style:
        b["style"] = style                                   # 'primary' / 'danger'
    return b


# ─────────────── KHO MEO TINH (bam la doc ngay, KHONG can AI) ───────────────
# User chot: "cho meo in phai hien option de chon doc" — AI free chan/rong nen
# meo co dinh (kien thuc may-doc-lap) tra tuc thi; con meo theo NHUA/BAN IN dang
# chay thi de nut rieng '🤖 Meo theo may (AI)'.
_TIPS = {
    "sup_dual": (
        "🧊 Support 2 nhựa (PLA+PETG)",
        "*🧊 Support bóc sạch — mặt dưới nhẵn như mặt trên (2 nhựa)*\n"
        "PLA và PETG *không dính hoá học* → ép sát 0mm vẫn bóc rời.\n"
        "• Support/raft interface = *nhựa đối ứng* (thân PLA → interface PETG, và ngược lại)\n"
        "• Top Z distance = *0* · Bottom Z = 0\n"
        "• Top interface spacing = *0* (đặc 100%)\n"
        "• Interface pattern = Rectilinear Interlaced · 2–3 lớp\n"
        "• Tắt Independent support layer height\n"
        "• Nhớ *Flush nhiều* khi đổi: PLA→PETG ~650 · PETG→PLA ~250\n"
        "⚠️ CÙNG loại mà để Z=0 = hàn chết, gỡ ra vỡ mặt."),
    "sup_pla": (
        "🟢 Support 1 nhựa PLA",
        "*🟢 Full support bằng PLA (1 nhựa)*\n"
        "Không có nhựa đối ứng thì chọn 1 trong 2 bộ (3 thông số đi kèm nhau):\n"
        "• *Mặt đẹp*: Top Z = *0.15* · spacing 0 · 2 lớp Concentric\n"
        "• *Dễ gỡ*: Top Z = *0.25* · spacing 0.3 · 1 lớp Rectilinear\n"
        "💡 *Mẹo ít vết mà khỏi đổi nhựa*: Interface pattern = *Grid (lưới)* + Top Z ~*0.04* "
        "— lưới ít điểm tiếp xúc nên bóc nhẹ, để lại rất ít dấu.\n"
        "⚠️ 0.04 mấp mé hàn dính → in thử 1 miếng có overhang canh theo máy trước."),
    "fit": (
        "🔩 Lắp khít/lỏng (âm dương)",
        "*🔩 Lắp 2 part khít/lỏng — đầu âm dương*\n"
        "Chỉnh *X-Y size compensation* (KHÔNG dùng Scale — Scale méo hoa văn). "
        "Vị trí: Quality ▸ Precision.\n"
        "• X-Y contour compensation: *ÂM* = co nhỏ → lọt dễ · *DƯƠNG* = nở → chặt "
        "(hoa khít quá đặt -0.2 ≈ giảm 0.4mm đường kính)\n"
        "• X-Y hole compensation: *DƯƠNG* = lỗ to ra\n"
        "• Khe hở chuẩn: khít 0.1 · êm 0.2 · lỏng 0.3mm\n"
        "🎯 Thiết kế: chừa sẵn ~0.2mm trong model + in *clearance test*; *bo góc 1–2mm* "
        "chống phồng/lồi (elephant foot) = thủ phạm #1 gây kẹt.\n"
        "Chỉnh RIÊNG 1 part: chuột phải object ▸ Add settings."),
}

# Nhan nut NGAN cho tung nhom TROUBLESHOOT (nguon = analyzer, 1 su that). Key la
# thu tu (symptom string) trong analyzer.TROUBLESHOOT; thieu -> tu cat 24 ky tu.
_TS_LABEL = {
    "Mặt trên lấm tấm / lỗ li ti / vân thưa": "✨ Mặt trên đẹp",
    "Kéo sợi / xù lông / mặt rỗ li ti": "🕸️ Kéo sợi/xù lông",
    "Overhang rủ / xệ": "🌉 Overhang rủ/xệ",
    "Lớp 1 bám kém / in quá nhanh (first layer & tốc độ)": "🧱 Lớp 1 & tốc độ",
    "PETG lớp đầu bông / tróc (in cao hoặc nhỏ)": "🫧 PETG lớp đầu bông",
    "Vênh / bong mép (warping)": "🪵 Vênh/bong mép",
    "Kẹt nhựa / thiếu đùn (mã 1200-8007)": "🔥 Kẹt/thiếu đùn",
    "Lệch trục / nghiêng ~2/3 chiều cao": "🗼 Lệch trục",
    "Brim khó gỡ / để lại via": "🩹 Brim khó gỡ",
    "Support xấu / khó bóc / sẹo mặt": "🧊 Support khó bóc",
}


def _ts_items() -> list:
    """Bang su co->sua (analyzer.TROUBLESHOOT) — kho meo DA KIEM CHUNG, 1 nguon."""
    try:
        import analyzer
        return list(analyzer.TROUBLESHOOT.items())
    except Exception:                                        # noqa: BLE001
        return []


def _ts_text(i: int) -> str | None:
    items = _ts_items()
    if not (0 <= i < len(items)):
        return None
    sym, steps = items[i]
    return f"*💡 {sym}*\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1))


def _tip_menu_blocks() -> list:
    # TOAN BO meo tu kho tai lieu (TROUBLESHOOT) + 3 card recipe rieng cua hub
    ts_btns = [_btn(_TS_LABEL.get(sym, sym[:24]), f"act:ts:{i}")
               for i, (sym, _s) in enumerate(_ts_items())]
    extra_btns = [_btn(lbl, f"act:tip:{k}") for k, (lbl, _t) in _TIPS.items()]
    all_btns = ts_btns + extra_btns
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
              "text": "*💡 Mẹo in — kho đã kiểm chứng* · bấm 1 mục để ĐỌC ngay "
                      "(không cần AI):"}}]
    for j in range(0, len(all_btns), 5):                     # <=5 nut / actions block
        blocks.append({"type": "actions", "elements": all_btns[j:j + 5]})
    blocks.append({"type": "actions", "elements": [
        _btn("🤖 Mẹo theo máy (AI)", "act:tip:ai", "primary"),
        _btn("⬅️ Menu", "act:menu")]})
    return blocks


def _menu_blocks() -> list:
    return [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": "*🖨 Bảng điều khiển máy in A1* — bấm nút, hoặc gõ câu hỏi bất kỳ "
                 "(AI trả lời; hỏi về ảnh/sản phẩm sẽ tự kèm ảnh camera)."}},
        {"type": "actions", "elements": [
            _btn("📊 Tình hình", "act:status"), _btn("📷 Ảnh bàn in", "act:photo"),
            _btn("🌡️ Nhiệt & khay", "act:temp"), _btn("🔍 Phân tích AI", "act:analyze"),
            _btn("💡 Mẹo", "act:tip")]},
        {"type": "actions", "elements": [
            _btn("🧯 Hỏi lỗi", "act:err"), _btn("💰 Chi phí AI", "act:usage"),
            _btn("🤖 Model", "act:model"), _btn("⏸ Tạm dừng", "act:pause"),
            _btn("▶️ Tiếp tục", "act:resume")]},
        {"type": "actions", "elements": [
            _btn("📞 Gọi video", "act:call"), _btn("⏹ DỪNG HẲN", "act:stop", "danger"),
            _btn("🧹 Xoá ngữ cảnh", "act:reset")]},
    ]


def _model_blocks() -> list:
    models = ai_chat.MODELS
    cur = ai_chat.pinned_model() or "auto"
    els = []
    for i, (mid, name, _note) in enumerate(models, 1):
        mark = " ✅" if mid == cur else ""
        els.append(_btn(f"{i}. {name}{mark}", f"m:{i}"))
    cur_name = next((n for mid, n, _ in models if mid == cur), cur)
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
              "text": f"*🤖 Chọn model AI* — bấm 1 nút để KHOÁ (hết đổi liên tục).\n"
                      f"Đang dùng: *{cur_name}*"}}]
    for j in range(0, len(els), 5):                          # <=5 nut / actions block
        blocks.append({"type": "actions", "elements": els[j:j + 5]})
    return blocks


# ─────────────────────────────── xu ly lenh ──────────────────────────────
def _do(web, chat: str, key: str, hooks: dict) -> None:      # noqa: PLR0912
    if key == "menu":
        _send(web, chat, "Bảng điều khiển máy in A1", _menu_blocks())
    elif key == "status":
        _send(web, chat, _md(hooks["status_html"]()))
    elif key == "temp":
        _send(web, chat, _md(hooks["temps"]()))
    elif key == "photo":
        jpg = hooks["frame"]()
        if jpg:
            _upload(web, chat, jpg, caption=hooks["status"]())
        else:
            _send(web, chat, "Camera chưa có hình (máy tắt / đang kết nối) — thử lại.")
    elif key == "analyze":
        _send(web, chat, "🔍 Đang chụp LOẠT 3 ảnh (cách 4s) + AI vision phân tích…")
        imgs = (hooks.get("burst") or (lambda: []))() or _images(hooks)
        if not imgs:
            _send(web, chat, "Không lấy được ảnh camera — máy tắt?")
            return
        a = ai_chat.ask_vision(
            "Đây là LOẠT ảnh camera bàn in chụp cách nhau ~4 giây (máy A1 bed-slinger, "
            "bàn di chuyển liên tục — khối in có thể TRÔNG nghiêng ở 1 ảnh do bàn đang "
            "chạy; chỉ kết luận LỆCH TRỤC khi nghiêng NHẤT QUÁN ở mọi ảnh). Kiểm tra: "
            "spaghetti/bong lớp/lệch trục/xơ nhựa/cong vênh? Nhận xét ngắn từng ý, "
            "chốt 1 dòng: ✅ ỔN / ⚠️ NGHI NGỜ / ❌ HỎNG.",
            imgs, context=hooks["status"]()) \
            or "AI vision không phản hồi (hết lượt free hôm nay?) — xem ảnh bằng 📷."
        ic, lab = ui_tg.verdict_of(a)
        cap = f"{ic} AI VISION: {lab}\n{a}"[:1400]
        if not _upload_many(web, chat, imgs, cap):           # CA LOAT anh AI da nhin
            _send(web, chat, a)
    elif key == "tip":
        _send(web, chat, "💡 Chọn mẹo để đọc", _tip_menu_blocks())
    elif key.startswith("ts:"):                              # meo tu kho TROUBLESHOOT
        try:
            txt = _ts_text(int(key[3:]))
        except ValueError:
            txt = None
        _send(web, chat, txt or "Mẹo không có — bấm 💡 Mẹo để chọn lại.")
    elif key.startswith("tip:"):
        tk = key[4:]
        if tk == "ai":                                        # meo DONG theo nhua/ban in dang chay
            a = ai_chat.ask("Cho 3 mẹo NGẮN, cụ thể, đúng với nhựa và bản in đang chạy "
                            "(theo bối cảnh). Mỗi mẹo 1 dòng bắt đầu bằng 💡.",
                            context=hooks["status"]() + "\n" + hooks["temps"]()) \
                or "AI không phản hồi — thử lại sau, hoặc bấm 1 mẹo cố định ở trên."
            _send(web, chat, a)
        else:                                                 # meo TINH: doc tuc thi
            item = _TIPS.get(tk)
            _send(web, chat, item[1] if item else "Mẹo không có — bấm 💡 Mẹo để chọn lại.")
    elif key == "err":
        err = (hooks.get("err") or (lambda: 0))()
        if not err:
            _send(web, chat, "✅ Máy KHÔNG báo mã lỗi nào. In xấu thì bấm 🔍 Phân tích AI.")
        else:
            a = ai_chat.ask(f"Máy Bambu A1 đang báo mã lỗi {err} (hex {err:X}). Giải thích "
                            f"ngắn nguyên nhân + cách xử lý theo mã HMS Bambu.",
                            context=hooks["status"]()) or ""
            _send(web, chat, f"🚨 Mã lỗi *{err}* (hex {err:X})\n{a}\n"
                             f"Tra chính thức: wiki.bambulab.com · {notify.hub_url()}")
    elif key == "usage":
        _send(web, chat, _md(ai_chat.usage_report()))
    elif key == "pause":
        ok, msg = hooks["cmd"]("pause")
        _send(web, chat, "⏸ Đã gửi lệnh TẠM DỪNG." if ok else f"Lỗi: {msg}")
    elif key == "resume":
        ok, msg = hooks["cmd"]("resume")
        _send(web, chat, "▶️ Đã gửi lệnh TIẾP TỤC." if ok else f"Lỗi: {msg}")
    elif key == "stop":
        _PEND["stop_until"] = time.time() + 60
        _send(web, chat, f"⚠️ DỪNG HẲN sẽ HỦY bản in, không tiếp tục lại được.\n"
                         f"Chắc chắn thì gõ đúng: *{STOP_WORD}* (trong 60 giây).")
    elif key == "reset":
        _hist_clear(chat)
        _send(web, chat, "🧹 Đã XOÁ ngữ cảnh hội thoại. Câu sau bắt đầu chủ đề mới.")
    elif key == "model":
        _send(web, chat, "Chọn model AI", _model_blocks())
    elif key == "call":
        url = f"https://meet.jit.si/{_JITSI_ROOM}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": "📞 *Gọi video giám sát máy in* — bấm để vào phòng (Jitsi Meet, "
                     "MIỄN PHÍ, không cần tài khoản). Mở camera điện thoại soi bàn in "
                     "trực tiếp, hoặc gọi nhóm."}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "📞 Vào phòng gọi",
                 "emoji": True}, "style": "primary", "url": url, "action_id": "call:open"}]},
        ]
        _send(web, chat, f"Phòng gọi video: {url}", blocks)


def _set_model(web, chat: str, i: int) -> None:
    models = ai_chat.MODELS
    if not (1 <= i <= len(models)):
        return
    pick = models[i - 1]
    ai_chat.set_model(pick[0])
    tail = ("auto = đổi theo cái nào đáp trước" if pick[0] == "auto"
            else "đã KHOÁ 1 model — không đổi nữa")
    _send(web, chat, f"✅ Ghim model: *{pick[1]}*\n_{pick[2]}_\n({tail})")


def _handle(web, chat: str, text: str, hooks: dict) -> None:
    t = (text or "").strip()
    tl = t.lower()
    # xac nhan DUNG (bo dau) — giong telegram
    flat = tl.replace("ừ", "u").replace("ậ", "a").replace("dừng", "dung").upper().replace("Đ", "D")
    if t.upper() == STOP_WORD or flat == STOP_WORD:
        if time.time() <= _PEND["stop_until"]:
            _PEND["stop_until"] = 0
            ok, msg = hooks["cmd"]("stop")
            _send(web, chat, "⏹ Đã gửi lệnh DỪNG HẲN." if ok else f"Lỗi: {msg}")
        else:
            _send(web, chat, "Hết hạn xác nhận — bấm ⏹ DỪNG HẲN lại nếu vẫn muốn dừng.")
        return
    if tl.startswith("/model") or tl == "model":
        arg = t.split(maxsplit=1)
        if len(arg) > 1 and arg[1].strip().isdigit():
            _set_model(web, chat, int(arg[1].strip()))
        else:
            _send(web, chat, "Chọn model AI", _model_blocks())
        return
    # lenh CHINH XAC (khong quet giua cau) -> nut/hanh dong
    for key, words in _CMD.items():
        if tl in words:
            _do(web, chat, key, hooks)
            return
    if t.startswith("/"):
        _send(web, chat, "Lệnh không có. Dùng: menu · tình hình · ảnh · phân tích · "
                         "nhiệt · mẹo · lỗi · chi phí · model · reset — hoặc bấm nút.")
        return
    # cau hoi tu do — nhac toi anh/nhin/san pham thi kem ANH cho AI vision
    if any(w in tl for w in _VISION_WORDS):
        imgs = _images(hooks)
        if imgs:
            a = ai_chat.ask_vision(
                t + "\n(Ảnh 1 = camera bàn in thật; ảnh 2 nếu có = render model.)",
                imgs, context=hooks["status"]())
            if a:
                _hist_add(chat, t, a)
            _send(web, chat, _sig(a, "AI vision không phản hồi — thử lại sau."))
            return
    # ROUTER: tu do -> domain BAMBU (may in, kem ngu canh + nho hoi thoai) hoac
    # ASSISTANT (viec/note/chi tieu -> Notion). Chi gan ten model cho cau AI may in.
    dom, a = agent.handle(t, printer_ctx=hooks["status"]() + "\n" + hooks["temps"](),
                          history=_hist_get(chat))
    if a:
        _hist_add(chat, t, a)
    _send(web, chat, _sig(a, "AI không phản hồi — thử lại.") if dom == "bambu" else a)


def _handle_safe(web, chat: str, text: str, hooks: dict) -> None:
    try:
        _handle(web, chat, text, hooks)
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[slack] loi xu ly {text[:40]!r}: {type(e).__name__}: {str(e)[:200]}")  # noqa: SLF001
        try:
            _send(web, chat, f"⚠️ Bot lỗi khi xử lý: {type(e).__name__}: {str(e)[:180]}\n"
                             "Đã ghi notify.log. Thử lại hoặc bấm nút.")
        except Exception:                                    # noqa: BLE001
            pass


def _on_action(web, chat: str, action: dict, hooks: dict) -> None:
    aid = action.get("action_id") or action.get("value") or ""
    if aid.startswith("m:"):
        try:
            _set_model(web, chat, int(aid[2:]))
        except ValueError:
            pass
    elif aid.startswith("act:"):
        _do(web, chat, aid[4:], hooks)


def _action_safe(web, chat: str, action: dict, hooks: dict) -> None:
    try:
        _on_action(web, chat, action, hooks)
    except Exception as e:                                   # noqa: BLE001
        notify._log(f"[slack] callback loi: {type(e).__name__}: {str(e)[:160]}")  # noqa: SLF001


# ─────────────────────────── Socket Mode loop ────────────────────────────
def _route(web, req, hooks: dict, bot_id: str) -> None:
    """Dinh tuyen 1 su kien Socket Mode -> lenh/cau hoi/bam nut."""
    typ = getattr(req, "type", "")
    payload = getattr(req, "payload", None) or {}
    if typ == "events_api":
        ev = payload.get("event") or {}
        if ev.get("type") not in ("message", "app_mention"):
            return
        # BO qua tin cua BOT / tin sua-xoa (subtype) -> tranh vong lap tu tra loi
        if ev.get("bot_id") or ev.get("subtype") or ev.get("user") == bot_id:
            return
        chat = ev.get("channel") or ""
        _remember_target(chat)
        _handle_safe(web, chat, _clean(ev.get("text")), hooks)
    elif typ == "interactive":
        if payload.get("type") != "block_actions":
            return
        chat = ((payload.get("channel") or {}).get("id")
                or (payload.get("container") or {}).get("channel_id") or "")
        _remember_target(chat)
        for a in (payload.get("actions") or []):
            _action_safe(web, chat, a, hooks)
    elif typ == "slash_commands":
        chat = payload.get("channel_id") or ""
        _remember_target(chat)
        txt = (payload.get("text") or "").strip() or "menu"
        _handle_safe(web, chat, txt, hooks)


def loop(hooks: dict) -> None:
    from slack_sdk.web import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    env = notify._env()                                      # noqa: SLF001 — cung nguon .env
    bot = (env.get("SLACK_BOT_TOKEN") or "").strip()
    app = (env.get("SLACK_APP_TOKEN") or "").strip()
    if not bot or not app:
        notify._log("[slack] thieu SLACK_BOT_TOKEN / SLACK_APP_TOKEN — cho .env")  # noqa: SLF001
        time.sleep(30)
        return
    web = WebClient(token=bot)
    who = web.auth_test()                                    # xac thuc + lay bot user id
    bot_id = who.get("user_id") or ""
    notify._log(f"[slack] ket noi OK — team={who.get('team')} bot=@{who.get('user')}")  # noqa: SLF001

    sm = SocketModeClient(app_token=app, web_client=web)

    def _on(client, req):
        try:                                                # ACK NGAY (Slack retry sau 3s)
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:                                   # noqa: BLE001
            pass
        threading.Thread(target=_route, args=(web, req, hooks, bot_id),
                         daemon=True).start()

    sm.socket_mode_request_listeners.append(_on)
    sm.connect()
    notify._log("[slack] Socket Mode dang lang nghe")
    hb = 0.0
    while True:                                              # giu thread song + nhip tim
        time.sleep(60)
        try:
            if not sm.is_connected():
                notify._log("[slack] mat ket noi — noi lai")  # noqa: SLF001
                sm.connect()
        except Exception as e:                              # noqa: BLE001
            notify._log(f"[slack] noi lai loi: {type(e).__name__}: {str(e)[:120]}")  # noqa: SLF001
        now = time.time()
        if now - hb > 1800:
            hb = now
            notify._log("[slack] poll song")                # noqa: SLF001


def _supervise(hooks: dict) -> None:
    """Canh loop(): chet vi bat ky ly do gi cung bat lai sau 10s (nhu telegram_bot)."""
    while True:
        try:
            loop(hooks)
            notify._log("[slack] loop() thoat — bat lai sau 10s")   # noqa: SLF001
        except BaseException as e:                          # noqa: BLE001
            notify._log(f"[slack] loop() CHET: {type(e).__name__}: "  # noqa: SLF001
                        f"{str(e)[:200]} — bat lai sau 10s")
        time.sleep(10)


def start(hooks: dict) -> None:
    try:
        import slack_sdk  # noqa: F401
    except ImportError:
        notify._log("[slack] slack_sdk chua cai (pip install slack_sdk) — bo qua Slack")  # noqa: SLF001
        return
    threading.Thread(target=_supervise, args=(hooks,), daemon=True).start()
