# -*- coding: utf-8 -*-
"""Sinh bộ config QUỐC DÂN (process preset Bambu) cho 3 nhựa + dãy support.
Tái dùng analyzer.make_preset (KHÔNG bịa số) — chỉ đổi tên + bật/tắt support + ironing.
Xuất ra D:\\16.Sharp3D\\Congfig. Chạy: python gen_configs.py
"""
import json, io, os, copy
import analyzer

OUT = r"D:\16.Sharp3D\Congfig"
BASE_FILE = r"C:\Users\Admin\Downloads\Box+with+Slide+Lid+(100x60x30+mm)+Single+Color.3mf"

# (ten trong file, ten hien thi nhua, doi ung khac loai)
MATS = [
    ("PLA_LITE",  "PLA LITE",  "PETG ECO"),
    ("PLA_MATTE", "PLA MATTE", "PETG ECO"),
    ("PETG",      "PETG ECO",  "PLA LITE"),
]
IRONING_ON = {"ironing_type": "top", "ironing_flow": "25%",
              "ironing_spacing": "0.1", "ironing_speed": "30"}


def base_preset(fil_sel: str) -> dict:
    """Preset balanced 0.2 cho 1 nhua tren vat nen trung tinh — support TAT."""
    r = analyzer.analyze(BASE_FILE, mode="balanced", fil_sel=fil_sel)
    p = copy.deepcopy((analyzer.make_preset(r, mode="balanced") or {}).get("preset") or {})
    # QUOC DAN = base sach: support TAT (user 2026-08-03), ironing TAT mac dinh
    p["enable_support"] = "0"
    p["ironing_type"] = "no ironing"
    p.pop("ironing_flow", None); p.pop("ironing_spacing", None); p.pop("ironing_speed", None)
    return p


def named(p: dict, name: str, extra: dict | None = None) -> dict:
    q = copy.deepcopy(p)
    q["name"] = name
    q["print_settings_id"] = name
    for k, v in (extra or {}).items():
        q[k] = v
    return q


def write(name: str, p: dict) -> None:
    path = os.path.join(OUT, name + ".json")
    io.open(path, "w", encoding="utf-8").write(json.dumps(p, ensure_ascii=False, indent=2))
    print("  ✓", name + ".json",
          f"(support={p.get('enable_support')} · topZ={p.get('support_top_z_distance','-')} · "
          f"ironing={p.get('ironing_type')} · top_speed={(p.get('top_surface_speed') or ['?'])[0]})")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    made = []
    for tag, fil, partner in MATS:
        p = base_preset(fil)
        # 1) BASE QUOC DAN (support tat, ironing tat)
        n = f"LP_{tag}_DEFAUT"; write(n, named(p, n)); made.append(n)
        # 2) BAN IRONING (support tat, ironing bat) — user 2026-08-03
        n = f"LP_{tag}_DEFAUT_IRONING"; write(n, named(p, n, IRONING_ON)); made.append(n)
        # 3) DAY SUPPORT CUNG LOAI (mat dep / de go) — Top Z theo nhua (PETG 0.3/0.4)
        for s in analyzer.support_strategy(fil, [fil]):
            suf = "MATDEP" if s["id"] == "same_smooth" else "DEGO"
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            n = f"LP_{tag}_SUP_CUNG_{suf}"; write(n, named(p, n, keys)); made.append(n)
        # 4) DAY SUPPORT KHAC LOAI (interface = nhua doi ung, Z=0 boc sach)
        for s in analyzer.support_strategy(fil, [fil, partner]):
            if s["id"] != "diff":
                continue
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            pn = partner.split()[0]           # PETG / PLA
            n = f"LP_{tag}_SUP_KHAC_{pn}"; write(n, named(p, n, keys)); made.append(n)
    return made


if __name__ == "__main__":
    print("Sinh config quoc dan ->", OUT)
    m = main()
    print(f"\nXONG {len(m)} file.")
