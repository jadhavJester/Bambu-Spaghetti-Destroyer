#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRO LY CONG VIEC — persona thu 2 cua hub (ngoai AI may in Bambu).

Quan ly CONG VIEC / GHI CHU / CHI TIEU, luu vao Notion (notion.py — se noi sau).
Dung chung engine: ai_chat.ask(question, system=assistant.SYSTEM, ...) + Slack/Telegram.

2 system prompt:
  SYSTEM        — hoi dap tu nhien voi anh Long (giong tro ly).
  INTENT_SYSTEM — TRICH Y DINH ra JSON de route sang Notion (task/note/expense/query).
"""
from __future__ import annotations

import datetime
import json
import re

import ai_chat
import notion

# ─────────────────────────── 1) HOI DAP TU NHIEN ───────────────────────────
SYSTEM = """Bạn là TRỢ LÝ CÁ NHÂN của anh Long — kỹ sư BIM/MEP ở Newtecons, có làm in 3D \
(máy Bambu A1). Nói TIẾNG VIỆT, xưng "em", gọi user là "anh". NGẮN GỌN, chủ động, thực \
dụng — anh Long bận, ghét dài dòng và lý thuyết.

EM LO 3 MẢNG (lưu vào Notion):
① CÔNG VIỆC — tạo/sửa/liệt kê việc; deadline; ưu tiên (Cao/TB/Thấp); trạng thái \
(Chưa/Đang/Xong). Nhắc việc tới hạn.
② GHI CHÚ — lưu ý tưởng/thông tin/link: tiêu đề + nội dung + nhãn.
③ CHI TIÊU — ghi khoản chi (số tiền + hạng mục + phân loại: Ăn uống/Đi lại/Vật tư/Cà \
phê/Khác); tổng hợp ngày/tuần/tháng; cảnh báo khi vượt ngân sách.

CÁCH LÀM:
- Hiểu Ý ĐỊNH từ lời nói tự nhiên:
  · "mai họp BIM 9h" → CÔNG VIỆC (hạn mai 9h).
  · "ghi chú: dùng PETG cho khay" → GHI CHÚ.
  · "cà phê 35k", "đổ xăng 100 nghìn" → CHI TIÊU (35.000 Cà phê / 100.000 Đi lại).
  · "hôm nay tiêu bao nhiêu", "việc tuần này" → TRUY VẤN → tổng hợp.
- Tiền: "k"=nghìn, "tr"/"củ"/"triệu"=triệu. LUÔN xác nhận lại con số đã hiểu.
- THIẾU thông tin quan trọng (số tiền / deadline / hạng mục) → HỎI LẠI đúng 1 câu ngắn, \
KHÔNG đoán bừa.
- Sau mỗi thao tác: xác nhận NGẮN, ví dụ "✅ Đã lưu việc: Họp BIM · hạn mai 9h · ưu tiên TB".
- KHÔNG bịa số liệu — việc/tiền phải lấy từ Notion THẬT. Chưa nối được Notion thì nói \
thẳng "em chưa nối được Notion".

GIỌNG: 1–3 dòng, emoji trạng thái (✅ ⏳ ⚠️ 💸 📝 🔔). Không dài dòng.

RANH GIỚI: câu hỏi về MÁY IN Bambu (nhiệt, nhựa, support, lỗi in, slice…) KHÔNG thuộc \
phần của em — nhắc anh chuyển sang chế độ Máy in. Em chỉ lo công việc / ghi chú / chi tiêu.
"""

# ─────────────────────── 2) TRICH Y DINH -> JSON (route) ───────────────────────
# Dung khi muon BIEN loi noi thanh HANH DONG co cau truc de ghi Notion. Goi rieng
# (ai_chat.ask(..., system=INTENT_SYSTEM, max_tokens nho)) roi json.loads ket qua.
INTENT_SYSTEM = """Bạn là bộ PHÂN TÍCH Ý ĐỊNH cho trợ lý cá nhân. Đọc câu của người dùng \
(tiếng Việt) và TRẢ VỀ DUY NHẤT một JSON hợp lệ (không chữ nào ngoài JSON), theo schema:

{
  "intent": "task" | "note" | "expense" | "query" | "chat",
  "title":    string|null,      // tên việc / tiêu đề note / tên khoản chi
  "body":     string|null,      // nội dung note (nếu có)
  "amount":   number|null,      // CHI/THU: số tiền VND (35k=35000, 2tr/2củ=2000000, 550k=550000)
  "category": string|null,      // Điện|Nước|Wifi/Internet|Thuê nhà|Lương|Thưởng|Ăn uống|Cà phê|Đi lại|Vật tư|Mua sắm|Hoá đơn|Y tế|Giải trí|Khác
  "loai":     "Chi"|"Thu"|null, // Lương/Thưởng/nhận tiền = Thu; còn lại = Chi
  "dinh_ky":  true|false,       // hoá đơn hàng tháng (điện/nước/wifi/thuê nhà/lương/thưởng) = true
  "due":      string|null,      // deadline ISO 8601 suy từ HÔM NAY (vd "2026-08-17T09:00"), else null
  "priority": "Cao"|"TB"|"Thấp"|null,
  "nhom":     "BIM"|"In 3D"|"Cá nhân"|"Việc nhà"|"Học"|"Khác"|null,  // task: có nhắc "BIM" => "BIM"
  "project":  string|null,      // task: tên dự án nếu nêu (vd "KSDA 2025")
  "query":    "expense_today"|"expense_week"|"expense_month"|"task_today"|"task_week"|"task_open"|null,
  "missing":  string|null       // 1 câu hỏi lại nếu THIẾU (số tiền/deadline), else null
}

QUY TẮC:
- "intent":"expense" bắt buộc có amount; thiếu số tiền -> "missing" hỏi lại, amount=null.
- "intent":"task" nên có title; suy "due" từ "mai/ngày mai/thứ 5/9h..." theo ngày HÔM NAY được cung cấp trong bối cảnh.
- Câu hỏi tổng hợp ("tiêu bao nhiêu", "việc hôm nay") -> intent:"query" + đặt "query".
- Không phải việc/note/tiền/truy vấn -> intent:"chat" (để trả lời thường).
- CHỈ in JSON, không giải thích.
"""

# Danh muc phan loai chi tieu (dung cho UI/Notion select). Sua tuy y.
EXPENSE_CATEGORIES = ["Ăn uống", "Cà phê", "Đi lại", "Vật tư", "Mua sắm", "Hoá đơn", "Khác"]
TASK_PRIORITIES = ["Cao", "TB", "Thấp"]
TASK_STATUSES = ["Chưa", "Đang", "Xong"]


# ─────────────── 3) FRAMEWORK: hieu y dinh -> Notion -> tra loi ───────────────
def _extract_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s or "", re.S)          # model doi khi kem chu quanh JSON
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:                                   # noqa: BLE001
        return {}


def parse_intent(text: str, today: str | None = None) -> dict:
    """LLM trich Y DINH ra JSON (INTENT_SYSTEM). Rong khi loi."""
    today = today or datetime.date.today().isoformat()
    raw = ai_chat.ask(f"Hôm nay: {today}. Câu của anh Long: {text}",
                      system=INTENT_SYSTEM, max_tokens=350) or ""
    return _extract_json(raw)


def _vnd(n) -> str:
    try:
        return f"{float(n):,.0f}đ".replace(",", ".")
    except Exception:                                   # noqa: BLE001
        return str(n)


def _dmy(s: str) -> str:
    """ISO 'YYYY-MM-DD[THH:MM…]' -> 'dd/mm/yy' (+ ' HH:MM' neu co gio khac 00:00)."""
    if not s:
        return ""
    try:
        y, m, d = s[:10].split("-")
        out = f"{d}/{m}/{y[2:]}"
        if "T" in s:
            hm = s.split("T", 1)[1][:5]
            if hm and hm != "00:00":
                out += f" {hm}"
        return out
    except Exception:                                   # noqa: BLE001
        return s


def handle(text: str) -> str:
    """Xu ly 1 tin cho domain TRO LY. Chua co Notion token -> van xac nhan da hieu gi."""
    d = parse_intent(text)
    if d.get("missing"):
        return f"❓ {d['missing']}"
    intent = d.get("intent") or "chat"
    today = datetime.date.today().isoformat()

    if intent == "expense":
        amt, item, cat = d.get("amount"), (d.get("title") or "Chi"), (d.get("category") or "Khác")
        if not amt:
            return "❓ Anh chi bao nhiêu tiền ạ?"
        loai = d.get("loai") or "Chi"
        if not notion.enabled():
            return f"💸 (chưa nối Notion) Em hiểu: {loai.lower()} {_vnd(amt)} · {item} · {cat}"
        ok = notion.add_expense(amt, item, cat, today, loai, bool(d.get("dinh_ky")))
        icon = "💰" if loai == "Thu" else "💸"
        return (f"{icon} Đã lưu {loai.lower()}: {_vnd(amt)} · {item} · {cat}"
                + (" · định kỳ" if d.get("dinh_ky") else "") if ok
                else "⚠️ Ghi Notion lỗi — kiểm tra chia sẻ DB Chi tiêu cho integration.")

    if intent == "task":
        title = d.get("title") or text
        due = f" · hạn {_dmy(d['due'])}" if d.get("due") else ""
        tag = f" · {d['nhom']}" if d.get("nhom") else ""
        if not notion.enabled():
            return f"📝 (chưa nối Notion) Em hiểu việc: {title}{due}{tag}"
        ok = notion.add_task(title, d.get("due"), d.get("priority") or "TB",
                             d.get("nhom"), d.get("project"))
        return f"✅ Đã lưu việc: {title}{due}{tag}" if ok else "⚠️ Ghi Notion lỗi — DB Công việc."

    if intent == "note":
        title = d.get("title") or text[:60]
        if not notion.enabled():
            return f"📒 (chưa nối Notion) Em hiểu ghi chú: {title}"
        ok = notion.add_note(title, d.get("body") or "")
        return f"📒 Đã lưu ghi chú: {title}" if ok else "⚠️ Ghi Notion lỗi — DB Ghi chú."

    if intent == "query":
        if not notion.enabled():
            return "📊 Chưa nối Notion nên em chưa tổng hợp được."
        q = d.get("query") or "expense_today"
        if q.startswith("expense"):
            start = today
            if q == "expense_week":
                start = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
            elif q == "expense_month":
                start = today[:8] + "01"
            total, items = notion.sum_expenses(start, today)
            top = " · ".join(f"{i['item']} {_vnd(i['amount'])}" for i in items[:5]) or "chưa có"
            rng = _dmy(today) if start == today else f"{_dmy(start)}→{_dmy(today)}"
            return f"📊 Chi {rng}: *{_vnd(total)}* ({len(items)} khoản)\n{top}"
        rows = notion.query_tasks(status=None if q == "task_open" else "Chưa")
        if not rows:
            return "✅ Không có việc nào đang chờ."
        lines = "\n".join(f"• {r['title']}" + (f" · hạn {_dmy(r['due'])}" if r["due"] else "")
                          for r in rows[:10])
        return f"🗓️ Việc cần làm ({len(rows)}):\n{lines}"

    # chat thuong — NHUNG neu chua noi Notion thi CAM bia du lieu cua anh Long
    sys_p = SYSTEM
    if not notion.enabled():
        sys_p += ("\n\n[QUAN TRỌNG] Em CHƯA nối Notion → em KHÔNG có bất kỳ dữ liệu việc / "
                  "chi tiêu / lịch / tracking nào của anh. Nếu anh hỏi về việc/chi tiêu/lịch/"
                  "tracking/kế hoạch, CHỈ được trả lời đúng: 'Em chưa nối Notion nên chưa có "
                  "dữ liệu của anh — nối Notion rồi em quản lý giúp.' TUYỆT ĐỐI KHÔNG bịa hay "
                  "liệt kê việc/khoản chi mà anh chưa giao.")
    return ai_chat.ask(text, system=sys_p) or "Dạ, em nghe đây."
