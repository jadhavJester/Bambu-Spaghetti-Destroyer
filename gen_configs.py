# -*- coding: utf-8 -*-
"""Sinh bộ config QUỐC DÂN (Bambu preset) cho 3 nhựa — CÓ CẤU TRÚC THƯ MỤC.
Tái dùng analyzer.make_preset + filament_preset (KHÔNG bịa số).
Xuất ra D:\\16.Sharp3D\\Congfig\\LP_QuocDan\\. Chạy: python gen_configs.py
"""
import json, io, os, copy, glob
import analyzer

ROOT = r"D:\16.Sharp3D\Congfig"
OUT = os.path.join(ROOT, "LP_QuocDan")
BASE_FILE = r"C:\Users\Admin\Downloads\Box+with+Slide+Lid+(100x60x30+mm)+Single+Color.3mf"

SUB_FIL  = os.path.join(OUT, "1.Filament")
SUB_BASE = os.path.join(OUT, "2.Process_Base")
SUB_SCUNG = os.path.join(OUT, "3.Support_CungLoai")
SUB_SKHAC = os.path.join(OUT, "4.Support_KhacLoai")

MATS = [  # (tag ten file, ten nhua analyzer, doi ung khac loai, HO NHUA = thu muc con)
    ("PLA_LITE",  "PLA LITE",  "PETG ECO", "PLA"),
    ("PLA_MATTE", "PLA MATTE", "PETG ECO", "PLA"),
    ("PETG",      "PETG ECO",  "PLA LITE", "PETG"),
]
IRONING_ON = {"ironing_type": "top", "ironing_flow": "25%",
              "ironing_spacing": "0.1", "ironing_speed": "30"}


def _clean_old():
    """Xoa file PHANG cu tôi từng tạo o ROOT (LP_*.json + _README_LP_CONFIG.md).
    KHONG dung folder cua user (01.Filament_* ...)."""
    for f in glob.glob(os.path.join(ROOT, "LP_*.json")):
        os.remove(f)
    old = os.path.join(ROOT, "_README_LP_CONFIG.md")
    if os.path.exists(old):
        os.remove(old)
    # xoa toan bo OUT cu roi tao lai (idempotent)
    if os.path.isdir(OUT):
        for dp, _, fs in os.walk(OUT, topdown=False):
            for fn in fs:
                os.remove(os.path.join(dp, fn))
            os.rmdir(dp)


def base_process(fil_sel: str) -> dict:
    r = analyzer.analyze(BASE_FILE, mode="balanced", fil_sel=fil_sel)
    p = copy.deepcopy((analyzer.make_preset(r, mode="balanced") or {}).get("preset") or {})
    p["enable_support"] = "0"
    p["ironing_type"] = "no ironing"
    p.pop("ironing_flow", None); p.pop("ironing_spacing", None); p.pop("ironing_speed", None)
    return p


def named(p, name, extra=None):
    q = copy.deepcopy(p); q["name"] = name; q["print_settings_id"] = name
    for k, v in (extra or {}).items():
        q[k] = v
    return q


def write(folder, name, p, tag=""):
    os.makedirs(folder, exist_ok=True)
    io.open(os.path.join(folder, name + ".json"), "w", encoding="utf-8").write(
        json.dumps(p, ensure_ascii=False, indent=2))
    rel = os.path.relpath(os.path.join(folder, name + ".json"), OUT)
    print(f"  ✓ {rel} {tag}")


README = """# Bộ config QUỐC DÂN — LP (Bambu Lab A1 · nozzle 0.4 · balanced 0.20mm)

Sinh tự động từ hub (`gen_configs.py`) — số đã kiểm chứng, KHÔNG bịa.
Mỗi nhóm chia thư mục con **PLA / PETG**. Import Studio: **Filament / Process ▸ ⋯ ▸ Import preset**.
**Nạp NHỰA trước (1.Filament), rồi Process.**

```
LP_QuocDan\\
├── 1.Filament\\{PLA,PETG}\\          3 nhựa (nhiệt/flow/mvs/retraction)
├── 2.Process_Base\\{PLA,PETG}\\      base + _IRONING (support TẮT)
├── 3.Support_CungLoai\\{PLA,PETG}\\  mặt đẹp / dễ gỡ
└── 4.Support_KhacLoai\\{PLA,PETG}\\  interface nhựa khác (Z=0 bóc sạch)
```

## 1.Filament — 3 nhựa
| File | Vòi | Bàn | mvs | Flow | Retract |
|---|---|---|---|---|---|
| PLA\\LP_PLA_LITE_FILAMENT  | 220 | 65 | 21 | 0.98 | mặc định |
| PLA\\LP_PLA_MATTE_FILAMENT | 230 | 55 | 12 | 0.98 | mặc định |
| PETG\\LP_PETG_FILAMENT     | 240 | 80 | 14 | 0.94 | **1.2/30** |

## 2.Process_Base — support TẮT
**MẶT ĐẸP KHÔNG CẦN IRONING** (base `_DEFAUT` đã đạt nhờ 6 lever, ironing chỉ là đánh bóng THÊM, không bắt buộc):
① top_surface ≤60% trần chảy (PLA Lite **149** · Matte **85** · PETG **100** mm/s — line liền, KHÔNG sọc) · ② monotonic line · ③ top line width 0.42 · ④ ≥5 lớp top + chốt 1mm (chống pillowing) · ⑤ ruột 15% (top không võng) · ⑥ tường 3 + Arachne. *Thiếu đều thật thì calib Flow Ratio + PA.*
- `_DEFAUT` = mặt đẹp không ironing.
- `_DEFAUT_IRONING` = bật ủi thêm cho bóng: `ironing_type = top` (Studio hiện **"Top surfaces"** dưới **Quality ▸ Ironing** — KHÔNG chọn "Topmost"), flow 25% · spacing 0.1 · speed 30. *PETG ủi dễ tơ/blob — cân nhắc, PLA an toàn.*
> Không thấy ironing? → bạn import file `_DEFAUT` (đúng là TẮT); file `_DEFAUT_IRONING` mới có, xem ở **Quality ▸ Ironing ▸ Ironing Type = Top surfaces**.

## 3.Support_CungLoai (1 nhựa · 1 màu)
| Đuôi | Top Z | Interface |
|---|---|---|
| `_SUP_CUNG_MATDEP` | PLA **0.15** / PETG **0.3** | spacing 0 · 2 lớp concentric — mặt đẹp |
| `_SUP_CUNG_DEGO`  | PLA **0.25** / PETG **0.4** | spacing 0.3 · 1 lớp rectilinear — dễ gỡ |
> ⚠️ PETG Top Z 0.3–0.4 (KHÔNG 0.15 như PLA) — PETG hàn chính nó, Z nhỏ = dính chết vỡ mặt.

## 4.Support_KhacLoai (2 nhựa · 2 màu · Z=0 bóc sạch)
PLA↔PETG không dính hoá học → Z=0 vẫn bóc sạch, mặt dưới nhẵn.
| File | Đế (khe 1) | Interface (khe 2) |
|---|---|---|
| PLA\\LP_PLA_LITE_SUP_KHAC_PETG | PLA Lite | PETG |
| PLA\\LP_PLA_MATTE_SUP_KHAC_PETG| PLA Matte | PETG |
| PETG\\LP_PETG_SUP_KHAC_PLA | PETG | PLA |

**MÀU/KHE:** `support_filament=0` → đế = nhựa model (khe 1); `support_interface_filament=2` → lớp tiếp xúc = nhựa KHÁC ở **khe 2**. Kéo đúng cuộn vào khe 2. FLUSH nhiều khi đổi (PLA→PETG ~650 · PETG→PLA ~250).

---
*18 config · sinh lại: `python gen_configs.py` (d:\\15.BambuStudio)*
"""


def main():
    _clean_old()
    os.makedirs(OUT, exist_ok=True)
    io.open(os.path.join(OUT, "00_README.md"), "w", encoding="utf-8").write(README)
    n_fil = n_proc = 0
    for tag, fil, partner, fam in MATS:
        # moi nhom chia thu muc con theo HO NHUA (PLA / PETG) — user 2026-08-03
        d_fil = os.path.join(SUB_FIL, fam)
        d_base = os.path.join(SUB_BASE, fam)
        d_scung = os.path.join(SUB_SCUNG, fam)
        d_skhac = os.path.join(SUB_SKHAC, fam)
        # ---- FILAMENT (nhiet/mvs/flow/retraction) ----
        fp = analyzer.filament_preset(fil)
        if fp:
            fpre = copy.deepcopy(fp["preset"])
            fn = f"LP_{tag}_FILAMENT"
            fpre["name"] = fn; fpre["filament_settings_id"] = [fn]
            write(d_fil, fn, fpre,
                  f"(temp {fpre.get('nozzle_temperature',['?'])[0]} · mvs {fpre.get('filament_max_volumetric_speed',['?'])[0]})")
            n_fil += 1
        # ---- PROCESS base + ironing ----
        p = base_process(fil)
        write(d_base, f"LP_{tag}_DEFAUT", named(p, f"LP_{tag}_DEFAUT"),
              f"(top {p.get('top_surface_speed',['?'])[0]} · support OFF)"); n_proc += 1
        write(d_base, f"LP_{tag}_DEFAUT_IRONING", named(p, f"LP_{tag}_DEFAUT_IRONING", IRONING_ON),
              "(ironing top)"); n_proc += 1
        # ---- Support CUNG loai ----
        for s in analyzer.support_strategy(fil, [fil]):
            suf = "MATDEP" if s["id"] == "same_smooth" else "DEGO"
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            write(d_scung, f"LP_{tag}_SUP_CUNG_{suf}", named(p, f"LP_{tag}_SUP_CUNG_{suf}", keys),
                  f"(Top Z {keys['support_top_z_distance']})"); n_proc += 1
        # ---- Support KHAC loai ----
        for s in analyzer.support_strategy(fil, [fil, partner]):
            if s["id"] != "diff":
                continue
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            pn = partner.split()[0]
            write(d_skhac, f"LP_{tag}_SUP_KHAC_{pn}", named(p, f"LP_{tag}_SUP_KHAC_{pn}", keys),
                  f"(interface {pn} · Top Z 0)"); n_proc += 1
    return n_fil, n_proc


if __name__ == "__main__":
    print("Sinh config quoc dan ->", OUT)
    nf, npr = main()
    print(f"\nXONG: {nf} filament + {npr} process.")
