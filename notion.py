#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notion client (urllib, khong dep) — luu CONG VIEC / GHI CHU / CHI TIEU cho tro ly.

CAU HINH .env (KHONG commit):
  NOTION_TOKEN=ntn_...            # Internal Integration Secret (notion.so/my-integrations)
  NOTION_DB_TASKS=<database_id>   # DB Cong viec
  NOTION_DB_NOTES=<database_id>   # DB Ghi chu
  NOTION_DB_EXPENSES=<database_id># DB Chi tieu

SCHEMA DB phai KHOP ten thuoc tinh (tao dung ten nay o Notion):
  Tasks    : "Name"(title) · "Deadline"(date) · "Ưu tiên"(select: Cao/TB/Thấp) · "Trạng thái"(select: Chưa/Đang/Xong)
  Notes    : "Name"(title) · "Nội dung"(rich_text) · "Nhãn"(multi_select)
  Expenses : "Name"(title) · "Số tiền"(number) · "Phân loại"(select) · "Ngày"(date)

Chia se 3 DB voi Integration (Notion: ... > Connections > add integration) thi API moi thay.
"""
from __future__ import annotations

import json
import urllib.request

import printer_config

_API = "https://api.notion.com/v1"
_VER = "2022-06-28"


def _env() -> dict:
    try:
        return printer_config._parse_dotenv(printer_config.env_path())  # noqa: SLF001
    except Exception:                                   # noqa: BLE001
        return {}


def _hdr() -> dict:
    tok = (_env().get("NOTION_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
            "Notion-Version": _VER}


def enabled() -> bool:
    return bool((_env().get("NOTION_TOKEN") or "").strip())


def _req(method: str, path: str, payload: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(_API + path, data=data, headers=_hdr(), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _title(s: str) -> dict:
    return {"title": [{"text": {"content": (s or "")[:1900]}}]}


def _text(s: str) -> dict:
    return {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}


# ──────────────────────────── GHI (create) ────────────────────────────
def add_task(title: str, due: str | None = None, priority: str = "TB",
             nhom: str | None = None, project: str | None = None) -> str | None:
    """Tao CONG VIEC. due = ISO. nhom = BIM/In 3D/... project = ten du an. -> id / None."""
    db = _env().get("NOTION_DB_TASKS")
    if not (enabled() and db and title):
        return None
    props = {"Name": _title(title), "Trạng thái": {"select": {"name": "Chưa"}}}
    if priority:
        props["Ưu tiên"] = {"select": {"name": priority}}
    if due:
        props["Deadline"] = {"date": {"start": due}}
    if nhom:
        props["Nhóm"] = {"select": {"name": nhom}}
    if project:
        props["Dự án"] = _text(project)
    try:
        return _req("POST", "/pages",
                    {"parent": {"database_id": db}, "properties": props}).get("id")
    except Exception:                                   # noqa: BLE001
        return None


def add_note(title: str, body: str = "", tags: list | None = None) -> str | None:
    db = _env().get("NOTION_DB_NOTES")
    if not (enabled() and db and title):
        return None
    props = {"Name": _title(title)}
    if body:
        props["Nội dung"] = _text(body)
    if tags:
        props["Nhãn"] = {"multi_select": [{"name": t} for t in tags[:5]]}
    try:
        return _req("POST", "/pages",
                    {"parent": {"database_id": db}, "properties": props}).get("id")
    except Exception:                                   # noqa: BLE001
        return None


def add_expense(amount: float, item: str, category: str = "Khác",
                date: str | None = None, loai: str = "Chi",
                dinh_ky: bool = False) -> str | None:
    """Ghi CHI/THU. amount = VND. loai = Chi/Thu. dinh_ky = hoa don hang thang."""
    db = _env().get("NOTION_DB_EXPENSES")
    if not (enabled() and db and amount):
        return None
    props = {"Name": _title(item or "Chi"), "Số tiền": {"number": float(amount)},
             "Phân loại": {"select": {"name": category or "Khác"}},
             "Loại": {"select": {"name": loai or "Chi"}},
             "Định kỳ": {"checkbox": bool(dinh_ky)}}
    if date:
        props["Ngày"] = {"date": {"start": date}}
    try:
        return _req("POST", "/pages",
                    {"parent": {"database_id": db}, "properties": props}).get("id")
    except Exception:                                   # noqa: BLE001
        return None


# ──────────────────────────── DOC (query) ────────────────────────────
def query_tasks(status: str | None = "Chưa", limit: int = 20) -> list[dict]:
    """Liet ke CONG VIEC (mac dinh chua xong). -> [{title, due, priority, status}]."""
    db = _env().get("NOTION_DB_TASKS")
    if not (enabled() and db):
        return []
    body: dict = {"page_size": limit, "sorts": [{"property": "Deadline",
                                                  "direction": "ascending"}]}
    if status:
        body["filter"] = {"property": "Trạng thái", "select": {"equals": status}}
    try:
        rows = _req("POST", f"/databases/{db}/query", body).get("results", [])
    except Exception:                                   # noqa: BLE001
        return []
    out = []
    for p in rows:
        pr = p.get("properties", {})
        out.append({
            "title": _plain_title(pr.get("Name")),
            "due": ((pr.get("Deadline") or {}).get("date") or {}).get("start"),
            "priority": ((pr.get("Ưu tiên") or {}).get("select") or {}).get("name"),
            "status": ((pr.get("Trạng thái") or {}).get("select") or {}).get("name"),
        })
    return out


def sum_expenses(start: str, end: str) -> tuple[float, list[dict]]:
    """Tong CHI TIEU trong [start, end] (YYYY-MM-DD). -> (tong_VND, [{item,amount,cat}])."""
    db = _env().get("NOTION_DB_EXPENSES")
    if not (enabled() and db):
        return 0.0, []
    body = {"page_size": 100, "filter": {"and": [
        {"property": "Ngày", "date": {"on_or_after": start}},
        {"property": "Ngày", "date": {"on_or_before": end}}]}}
    try:
        rows = _req("POST", f"/databases/{db}/query", body).get("results", [])
    except Exception:                                   # noqa: BLE001
        return 0.0, []
    total, items = 0.0, []
    for p in rows:
        pr = p.get("properties", {})
        amt = (pr.get("Số tiền") or {}).get("number") or 0
        total += amt
        items.append({"item": _plain_title(pr.get("Name")), "amount": amt,
                      "cat": ((pr.get("Phân loại") or {}).get("select") or {}).get("name")})
    return total, items


def _plain_title(prop: dict | None) -> str:
    try:
        return "".join(t.get("plain_text", "") for t in (prop or {}).get("title", []))
    except Exception:                                   # noqa: BLE001
        return ""


def ping() -> tuple[bool, str]:
    """Kiem tra token + quyen: goi /users/me. -> (ok, thong_bao)."""
    if not enabled():
        return False, "Chưa có NOTION_TOKEN trong .env"
    try:
        me = _req("GET", "/users/me")
        return True, f"OK — bot: {me.get('name') or me.get('bot', {}).get('owner', {})}"
    except Exception as e:                               # noqa: BLE001
        return False, f"Lỗi: {type(e).__name__}: {str(e)[:160]}"
