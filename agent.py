#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROUTER DOMAIN — backend core cua hub agent.

Front-end (Slack / Telegram / web) chi goi `agent.handle(text, ...)`. Router phan loai
DOMAIN roi dieu phoi:
  • BAMBU     — may in (AI hien tai: SYSTEM printer + boi canh trang thai + nho ngu canh)
  • ASSISTANT — cong viec / ghi chu / chi tieu (assistant.py -> Notion)

Them domain moi = them 1 nhanh (y "moi thu la plugin" cua dsh). Vong Gauntlet (generate ->
critic -> refine) se boc rieng cho tac vu chat luong cao — chua gan o day.
"""
from __future__ import annotations

import ai_chat
import assistant

# Tu khoa nghieng ve TRO LY (viec/note/tien). Con lai -> BAMBU (goc hub).
_ASSIST = ("việc", "viec", "task", "deadline", "nhắc", "nhac", "lịch làm", "ghi chú",
           "ghi chu", "note", "chi tiêu", "chi tieu", "tiêu tiền", "tiêu bao", "ngân sách",
           "ngan sach", "chi phí", "chi phi", "mua ", "trả tiền", "đóng tiền", "cà phê",
           "ca phe", "xăng", "xang", "ăn trưa", "an trua", "ăn sáng", "lương", "hoá đơn",
           "hoa don", "tổng chi", "hôm nay tiêu", "tuần này", "việc hôm nay", "todo")
_ASSIST_PREFIX = ("/task", "/note", "/chi", "/viec", "/việc", "/tieu", "/tiêu", "/todo")
# Tu khoa nghieng ve MAY IN.
_BAMBU = ("nhựa", "nhua", "nhiệt", "nhiet", "lớp", " lop", "support", "camera", "máy in",
          "may in", "bàn in", "ban in", " pla", "petg", "slice", "spaghetti", "bong lớp",
          " flow", "ironing", "vòi", "nozzle", " bed", "in xong", "đang in", "gcode",
          "cong vênh", "cong venh", "xơ nhựa")


def classify(text: str) -> str:
    """Tra 'assistant' | 'bambu'. Prefix > dem tu khoa; MO HO (0-0) -> nho LLM phan loai
    theo y dinh (task/note/expense/query -> assistant), tranh day nham cau viec sang may in."""
    t = (text or "").strip().lower()
    if any(t.startswith(p) for p in _ASSIST_PREFIX):
        return "assistant"
    a = sum(1 for k in _ASSIST if k in t)
    b = sum(1 for k in _BAMBU if k in t)
    if a > b:
        return "assistant"
    if b > a:
        return "bambu"
    if a == 0:                                    # 0-0: khong ro -> hoi LLM y dinh
        try:
            if assistant.parse_intent(text).get("intent") in (
                    "task", "note", "expense", "query"):
                return "assistant"
        except Exception:                         # noqa: BLE001
            pass
    return "bambu"                                # mac dinh / hoa -> BAMBU (goc hub)


def handle(text: str, printer_ctx: str = "", history: list | None = None) -> tuple[str, str]:
    """Dieu phoi 1 tin -> (domain, tra_loi). printer_ctx = trang thai may (domain bambu);
    history = lich su hoi thoai (domain bambu). Domain assistant tu lo Notion."""
    dom = classify(text)
    if dom == "assistant":
        return "assistant", assistant.handle(text)
    reply = ai_chat.ask(text, context=printer_ctx, history=history) \
        or "AI không phản hồi — thử lại."
    return "bambu", reply
