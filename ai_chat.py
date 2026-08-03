#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hoi dap + soan tin bao bang AI qua OpenRouter.

Model mac dinh: NVIDIA Nemotron 3 Nano Omni (FREE — user chon 2026-07-16).
Doi model: them OPENROUTER_MODEL vao .env. Key: OPENROUTER_API_KEY trong .env.

Nguyen tac: AI la LOP TRANG TRI — moi cho goi deu phai co fallback khi AI
loi/cham/het quota (tra None, caller tu dung text thuong). KHONG de AI chan
luong bao chuong.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.request

import printer_config

# Bo dem SU DUNG local (ai_usage.json canh file nay — gitignore): dem so lan
# chat/vision/roi-xuong-tra-phi de bao cao "con uoc bao nhieu lan" tren Telegram.
_USAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_usage.json")
_USAGE_LOCK = threading.Lock()


def _count(kind: str) -> None:
    with _USAGE_LOCK:
        try:
            d = json.load(open(_USAGE_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[kind] = int(d.get(kind, 0)) + 1
        try:
            json.dump(d, open(_USAGE_PATH, "w", encoding="utf-8"))
        except OSError:
            pass


def _counts() -> dict:
    try:
        return json.load(open(_USAGE_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}

DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

# Model THUC SU tra loi lan gan nhat (chuoi fallback nen khong doan duoc truoc).
# Bot Telegram in kem duoi cau tra loi de biet dang xai model nao, free hay tra tien.
LAST_MODEL = ""

# Gia THAT 1 cau hoi cua hub. Tinh: system prompt ~3130 token (bang nhua + dac tinh
# + vat cao + vi tri option + nguong hinh hoc + ma HMS — rut tu analyzer/bambu_web)
# + cau hoi ~60 + tra loi ~120. Bang gia OpenRouter 2026-07-17. Dung cho /usage.
# DeepSeek KHONG co ban free tren OpenRouter (tra 11 model) — v4-flash re nhat va
# do that tra loi dung + nhanh nhat (5s vs 15s cua Nemotron free).
_IN_TOK, _OUT_TOK = 4230, 130
CHAT_COST = {
    "deepseek/deepseek-v4-flash": (_IN_TOK * 0.098 + _OUT_TOK * 0.20) / 1e6,   # ~$0.00034
    "deepseek/deepseek-v4-pro": (_IN_TOK * 0.435 + _OUT_TOK * 0.87) / 1e6,     # ~$0.0015
    "deepseek/deepseek-v3.2": (_IN_TOK * 0.269 + _OUT_TOK * 0.40) / 1e6,
    "openai/gpt-5-nano": (_IN_TOK * 0.05 + _OUT_TOK * 0.40) / 1e6,
}


def _knowledge() -> str:
    """Kho so DA KIEM CHUNG cua hub — nhung vao system prompt de AI KHONG bia so
    nguoc voi ground truth (test that: model free tra loi 'Matte 190°C' — sai bet,
    hub da chot 230°C chong ket). Import tre de khoi vong lap module.

    RUT TU CHINH CODE (khong chep tay) -> sua analyzer la prompt tu doi, khong the
    lech nhau — cung nguyen tac 'mot nguon duy nhat' nhu tall_rules().
    """
    try:
        import analyzer
    except Exception:                                   # noqa: BLE001
        return ""
    out: list[str] = []
    # 1. Bang XUAT preset an toan (nhiet/tran chay/flow/ban) + LY DO tung dong
    out.append("[A] SỐ AN TOÀN THEO CUỘN (hub xuất preset bằng đúng bảng này):")
    for k, v in analyzer.FIL_EXPORT.items():
        s = v["safe"]
        out.append(f"- {k}: {s['nozzle_temperature']}°C · trần chảy "
                   f"{s['filament_max_volumetric_speed']} mm³/s · flow "
                   f"{s['filament_flow_ratio']} · bàn {s['hot_plate_temp']}°C"
                   f"{' · ' + v['why'] if v.get('why') else ''}")
    # 2. Thu vien CANH BAO tung loai nhua (kẹt/ẩm/vênh/kéo sợi) — kien thuc van hanh
    out.append("\n[B] ĐẶC TÍNH & RỦI RO TỪNG LOẠI NHỰA (tư vấn sự cố dựa vào đây):")
    for k, v in analyzer.FILAMENT_REF.items():
        out.append(f"- {k} ({v['temp']}, flow {v['flow']}): {v['note']}")
    # 3. Luat VAT CAO — doc tu tall_rules() (nguon duy nhat), theo tung che do
    out.append("\n[C] VẬT CAO ≥120mm (chống lệch trục — bed-slinger A1):")
    for mode in ("fast", "balanced", "quality"):
        rs = analyzer.tall_rules(130, mode)
        out.append(f"- Chế độ {mode}: " +
                   "; ".join(f"{r['en'].split('▸')[-1].strip()} {r['base']}→{r['val']}"
                             for r in rs if r["val"] != r["base"]))
    if analyzer.tall_rules(130, "balanced"):
        out.append(f"  Lý do: {analyzer.tall_rules(130, 'balanced')[0]['why']}")
    # 4. VI TRI tung option trong Bambu Studio — de chi user bam o dau
    out.append("\n[D] VỊ TRÍ OPTION TRONG BAMBU STUDIO (chỉ đúng đường cho user):")
    for key, (tab, sec, en) in analyzer.BS_LOC.items():
        out.append(f"- {key} = {tab} ▸ {sec} ▸ {en}")
    # 5. Nguong hinh hoc + bridge flow theo nhua (tu hang so that trong analyzer)
    out.append(f"\n[E] NGƯỠNG HÌNH HỌC: bridge ≤{analyzer.BRIDGE_MM:g}mm là bắc cầu "
               f"được (không cần support); thành <{analyzer.WALL_HARD_MM}mm KHÔNG in "
               f"đặc được, <{analyzer.WALL_SOFT_MM}mm là mỏng (nên ≥1.5mm nếu chịu "
               f"lực); vật ≥{analyzer.TALL_MM}mm = vật cao.")
    # 6. SU CO -> CACH SUA (bang nguon-duy-nhat trong analyzer). Thieu muc nay AI
    #    tra loi sai: hoi 'mat tren lam tam' no khuyen chinh ban (test that 2026-07-17).
    out.append("\n[G] SỰ CỐ → SỬA THEO THỨ TỰ (đã kiểm chứng — bám đúng thứ tự này):")
    for sym, steps in analyzer.TROUBLESHOOT.items():
        out.append(f"* {sym}:")
        out += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
    # 7. Ma loi HMS -> nghia tieng Viet (tu bang da xac minh trong bambu_web)
    try:
        import bambu_web
        out.append("\n[F] MÃ LỖI HMS ĐÃ XÁC MINH:")
        for code, mean in bambu_web.HMS_VN.items():
            out.append(f"- [{code}] {mean}")
    except Exception:                                   # noqa: BLE001
        pass
    return "\n".join(out)


SYSTEM = (
    # ===== VAI TRO =====
    "Bạn là KỸ SƯ VẬN HÀNH máy in 3D Bambu Lab A1 của xưởng này — không phải trợ lý "
    "chung chung. Bạn nói chuyện với CHỦ MÁY (đã có kinh nghiệm, ghét vòng vo).\n\n"
    # ===== MAY CU THE =====
    "MÁY: Bambu Lab A1, khung HỞ (không buồng kín), bed-slinger (bàn chạy trục Y — "
    "đây là lý do vật cao dễ lệch trục), 1 ray Z, nozzle 0.4 thép, AMS Lite 4 khe "
    "(KHÔNG sấy nhựa), bàn Textured PEI.\n\n"
    # ===== SU THAT DA KIEM CHUNG (khong duoc bia khac) =====
    "BẢNG SỐ ĐÃ AUDIT (official Bambu 2 tầng profile @base + @BBL A1, đối chiếu cộng "
    "đồng). Hỏi về nhiệt/trần chảy/flow/bàn PHẢI dùng đúng bảng này:\n" + _knowledge() +
    "\n\nLUẬT KỸ THUẬT ĐÃ KIỂM CHỨNG BẰNG SLICE THẬT (ưu tiên hơn kiến thức chung):\n"
    "• PLA Matte/Metal có HẠT ĐỘN → dễ kẹt: 230°C + hạ trần chảy 22→12. Màu ĐEN nặng "
    "nhất (bột carbon tích cặn) — bàn hạ 55°C chống heat-creep.\n"
    "• Vật cao ≥120mm: accel 3000–5000 + travel 380 (giá đo thật: +7.8%~+11.9% thời "
    "gian). Gia tốc là thủ phạm CHÍNH khi bàn đảo chiều, không phải tốc độ.\n"
    "• Gờ/rãnh nhịp ngắn: bật 'Don't support bridges' + 'On build plate only' thay vì "
    "để support chống lên thân.\n"
    "• MẶT TRÊN đẹp (chống 'thiếu lớp'/pillowing + 'không đều'/sọc) — RẤT QUAN TRỌNG, trả "
    "ĐỦ các lever, đừng chỉ khuyên tăng lớp: ① TỐC ĐỘ top ≤60% trần chảy (PLA ~150, PETG "
    "~100, Matte ~85) — chạy sát trần thì nhựa ra không kịp, line hở → SỌC (đây là thủ phạm "
    "#1, hub đã cap). ② Số lớp top ≥4 (Bambu lấy MAX(lớp×layer_height, top shell thickness); "
    "hub để 6 lớp + chốt 1mm ⇒ luôn ≥1mm, dư chống pillowing/lỗ). ③ top_surface_pattern = "
    "monotonic line (mặc định Bambu, đều); còn sọc dọc thì đổi 'monotonic'. ④ top line width "
    "0.42 (không để mỏng 0.25 → telegraphing). ⑤ Ruột ≥15% để lớp top không võng vào khe "
    "infill. ⑥ Calib Flow Ratio + Pressure Advance — lệch flow là top rỗ/gồ. Ủi (ironing) là "
    "phương án CUỐI: PLA ổn, PETG dễ kéo sợi/blob nên cân nhắc, ưu tiên sửa tốc độ+flow trước.\n"
    "• SUPPORT theo vật liệu (hub tự sinh 'Cách làm support'). Hỏi support LUÔN trả ĐỦ "
    "BỘ 3 THÔNG SỐ: ① Top Z distance ② Interface spacing ③ Interface top layers (+ pattern) "
    "— cho CẢ 2 biến thể 'mặt đẹp' và 'dễ gỡ', đừng chỉ nói mỗi Top Z.\n"
    "  (a) CÙNG loại (Z NHỎ = mặt đẹp gỡ chặt; Z LỚN = dễ gỡ mặt hơi rỗ):\n"
    "    · 'mặt đẹp' = Top Z NHỎ (PLA 0.15 / PETG 0.3) + interface spacing 0 + 2 lớp concentric.\n"
    "    · 'dễ gỡ'  = Top Z LỚN (PLA 0.25 / PETG 0.4) + spacing 0.3 + 1 lớp rectilinear.\n"
    "    PETG PHẢI để Z 0.3–0.4 (1.5–2× layer) vì hàn chính nó mạnh hơn PLA, để 0.15 là DÍNH "
    "CHẾT. PETG thêm Base pattern Gyroid (zig-zag PETG khó bẻ) + interface speed 30–40mm/s.\n"
    "    ⚠️ CẢNH BÁO (luôn nhắc khi hỏi support cùng loại): CÙNG loại mà để Top Z=0 (hoặc "
    "<0.1) = support HÀN CHẾT vào model, gỡ ra là VỠ/rách mặt, hỏng cả bản in. Z=0 CHỈ dùng "
    "cho KHÁC loại (PLA↔PETG không dính hoá học). Mở đầu cảnh báo bằng '⚠️' để hub tô ĐỎ.\n"
    "  (b) KHÁC loại (PLA↔PETG KHÔNG dính hoá học): interface = nhựa kia, Top Z=0, spacing 0, "
    "1 lớp concentric → bóc SẠCH, mặt dưới nhẵn như mặt trên; nhớ FLUSH nhiều khi đổi "
    "(PLA→PETG ~650, PETG→PLA ~250).\n"
    "• Tốc độ đặt cao hơn TRẦN CHẢY (mvs ÷ (layer × line width)) là số ảo — máy tự hãm.\n"
    "• Ngân sách preset của hub: mục tiêu +1h30, sai số +2h so với default 0.20mm; "
    "KHÔNG bao giờ cắt lever chống lỗi để đổi lấy thời gian.\n"
    "• Giá đo thật (BUCKET khay 1, base 3h22m): tường 3 +32m · layer 0.16 +45m · "
    "accel 3000 +24m · ủi +19m · support bật thêm +0s (không phải thủ phạm).\n\n"
    # ===== CACH TRA LOI (ECC: format bat buoc) =====
    "CÁCH TRẢ LỜI:\n"
    "1. Câu ĐẦU = câu trả lời thẳng (số cụ thể / có-không). Không mở bài.\n"
    "2. Sau đó tối đa 3 dòng: vì sao + đánh đổi. Mỗi dòng 1 ý, bắt đầu bằng '•'.\n"
    "3. Có bối cảnh máy (đang in gì, %, nhựa nào) thì DÙNG nó, đừng hỏi lại.\n"
    "4. Chỉ nói điều bạn chắc. KHÔNG chắc thì nói thẳng 'chưa chắc' + cách kiểm chứng "
    "(wiki.bambulab.com / in thử mẫu nhỏ / chạy calibration). Bịa số là lỗi nặng nhất.\n"
    "5. Đơn vị luôn kèm số (°C, mm/s, mm³/s, %). Tiếng Việt, không chêm tiếng Anh trừ "
    "tên option trong Bambu Studio (giữ nguyên để user tìm được trên UI).\n"
    "6. Sửa lỗi thì xếp theo THỨ TỰ ƯU TIÊN đã đồng thuận: Hướng in > Số thành > "
    "Nhựa + calib > Ruột. Đừng khuyên tăng infill khi vấn đề là hướng in.")


def _cfg() -> tuple[str | None, str]:
    try:
        env = printer_config._parse_dotenv(printer_config.env_path())  # noqa: SLF001
    except Exception:                                  # noqa: BLE001
        env = {}
    return env.get("OPENROUTER_API_KEY"), env.get("OPENROUTER_MODEL") or DEFAULT_MODEL


def enabled() -> bool:
    return bool(_cfg()[0])


def _call(model: str, key: str, messages: list, max_tokens: int, timeout: int) -> str | None:
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/Long0308/LP-BambuLab",
                 "X-Title": "LP-BambuLab Hub"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        out = (msg.get("content") or "").strip()
        return out or None
    except Exception:                                   # noqa: BLE001 — AI chi la trang tri
        return None


def _chain(primary: str, vision: bool = False) -> list[str]:
    """Chuoi FALLBACK model. Thu tu: model chinh (.env) -> free du phong -> paid.

    Chat text mac dinh (user chot 2026-07-17 lan 2): deepseek-v4-flash TRA PHI
    ($0.098/1M in, ~$0.0002/cau) — do that tren cau ky thuat: dung so 230/12,
    5s (Nemotron free 15s), tieng Viet sach (free viet sai 'tran chay'/'ket khiet').
    DeepSeek KHONG co ban free tren OpenRouter (tra 11 model, 2026-07-17) va KHONG
    co vision -> vision van dung gemini-2.5-flash-lite, du phong Nano Omni free.
    """
    try:
        env = printer_config._parse_dotenv(printer_config.env_path())  # noqa: SLF001
    except Exception:                                   # noqa: BLE001
        env = {}
    # Vision: gpt-5-nano DO THAT khong tra loi anh -> du phong vision la Nano Omni
    # free (co vision, da test OK), roi moi toi paid text-capable cuoi chuoi.
    paid = env.get("OPENROUTER_PAID_MODEL") or "openai/gpt-5-nano"
    chain = [primary]
    if DEFAULT_MODEL not in chain:
        chain.append(DEFAULT_MODEL)                     # Nano Omni free du phong (co vision)
    if not vision and paid not in chain:
        chain.append(paid)
    return chain


def ask(question: str, context: str = "", system: str = SYSTEM,
        timeout: int = 45, max_tokens: int = 700) -> str | None:
    """Hoi 1 cau -> tra loi text; tu FALLBACK qua chuoi model, None khi het chuoi."""
    key, model = _cfg()
    if not key or not question.strip():
        return None
    user = question if not context else f"{question}\n\n[Bối cảnh máy in hiện tại]\n{context}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for m in _chain(model):
        out = _call(m, key, msgs, max_tokens, timeout)
        if out:
            global LAST_MODEL                               # noqa: PLW0603
            LAST_MODEL = m
            _count("chat")
            if not m.endswith(":free"):
                _count("chat_paid")
            return out
    return None


def ask_vision(question: str, images: list[bytes], context: str = "",
               timeout: int = 90, max_tokens: int = 800) -> str | None:
    """Hoi kem ANH (frame camera / render model) — model VISION rieng.

    Mac dinh Nemotron 3 Nano Omni free (co vision) — model hoi dap text (Super 120B)
    KHONG nhin duoc anh nen phai tach. Doi bang OPENROUTER_VISION_MODEL trong .env.
    """
    key, _ = _cfg()
    if not key or not images:
        return None
    try:
        env = printer_config._parse_dotenv(printer_config.env_path())  # noqa: SLF001
    except Exception:                                   # noqa: BLE001
        env = {}
    model = env.get("OPENROUTER_VISION_MODEL") or DEFAULT_MODEL
    user = question if not context else f"{question}\n\n[Bối cảnh máy in hiện tại]\n{context}"
    content: list = [{"type": "text", "text": user}]
    for jpg in images[:3]:                              # toi da 3 anh (loat chong FP ban chay)
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")}})
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": content}]
    for m in _chain(model, vision=True):                # vision free -> paid co vision
        out = _call(m, key, msgs, max_tokens, timeout)
        if out:
            global LAST_MODEL                               # noqa: PLW0603
            LAST_MODEL = m
            _count("vision")
            if not m.endswith(":free"):
                _count("vision_paid")
            return out
    return None


def usage_report() -> str:
    """Bao cao chi phi AI cho Telegram: so du, hub da dung, uoc con bao nhieu lan.

    So tien tu OpenRouter API that (/auth/key = chi tieu cua KEY nay, /credits =
    tai khoan); so LAN tu bo dem local. Gia vision ~$0.0004/lan (gemini-2.5-flash-
    lite $0.10/1M, ~3 anh/lan — do that 2026-07-17: 12 lan soi + chat = $0.0032).
    """
    key, _ = _cfg()
    if not key:
        return "Chưa cấu hình OPENROUTER_API_KEY."
    bal = spent_key = None
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                                     headers={"Authorization": f"Bearer {key}"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("data") or {}
        spent_key = float(d.get("usage") or 0)
        req = urllib.request.Request("https://openrouter.ai/api/v1/credits",
                                     headers={"Authorization": f"Bearer {key}"})
        c = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("data") or {}
        bal = float(c.get("total_credits") or 0) - float(c.get("total_usage") or 0)
    except Exception:                                   # noqa: BLE001
        pass
    n = _counts()
    nv, nc = int(n.get("vision", 0)), int(n.get("chat", 0))
    vp, cp = int(n.get("vision_paid", 0)), int(n.get("chat_paid", 0))
    # gia vision trung binh THAT cua hub: tien key / so lan tra phi (fallback 0.0004)
    v_cost = (spent_key / vp) if (spent_key and vp) else 0.0004
    lines = ["💰 <b>Chi phí AI (OpenRouter)</b>"]
    if bal is not None:
        lines.append(f"Số dư tài khoản: <b>${bal:.2f}</b>")
    if spent_key is not None:
        lines.append(f"Hub đã tiêu (key này): <b>${spent_key:.4f}</b>")
    lines.append(f"Đã gọi: {nc} chat ({cp} lần rơi xuống trả phí) · {nv} vision "
                 f"({vp} trả phí)")
    lines.append(f"Giá vision TB: ~${v_cost:.4f}/lần soi")
    if bal is not None and v_cost > 0:
        lines.append(f"→ Còn ước <b>~{int(bal / v_cost):,} lần phân tích vision</b>")
    _, cm = _cfg()
    if cm.endswith(":free"):
        lines.append(f"Chat text: FREE $0 ({cm.split('/')[-1]}).")
    else:
        # Gia/cau tinh THAT theo do dai prompt hub (system + cau hoi + tra loi)
        c = CHAT_COST.get(cm, 0.0002)
        lines.append(f"Chat text: {cm.split('/')[-1]} ~${c:.6f}/câu")
        if bal is not None:
            lines.append(f"→ Hoặc ~{int(bal / c):,} câu chat")
    return "\n".join(lines)