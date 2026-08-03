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

MATS = [  # (tag ten file, ten nhua analyzer, doi ung khac loai)
    ("PLA_LITE",  "PLA LITE",  "PETG ECO"),
    ("PLA_MATTE", "PLA MATTE", "PETG ECO"),
    ("PETG",      "PETG ECO",  "PLA LITE"),
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
    print(f"  ✓ {os.path.basename(folder)}/{name}.json {tag}")


def main():
    _clean_old()
    n_fil = n_proc = 0
    for tag, fil, partner in MATS:
        # ---- FILAMENT (nhiet/mvs/flow/retraction) ----
        fp = analyzer.filament_preset(fil)
        if fp:
            fpre = copy.deepcopy(fp["preset"])
            fn = f"LP_{tag}_FILAMENT"
            fpre["name"] = fn; fpre["filament_settings_id"] = [fn]
            write(SUB_FIL, fn, fpre,
                  f"(temp {fpre.get('nozzle_temperature',['?'])[0]} · mvs {fpre.get('filament_max_volumetric_speed',['?'])[0]})")
            n_fil += 1
        # ---- PROCESS base + ironing ----
        p = base_process(fil)
        write(SUB_BASE, f"LP_{tag}_DEFAUT", named(p, f"LP_{tag}_DEFAUT"),
              f"(top {p.get('top_surface_speed',['?'])[0]} · support OFF)"); n_proc += 1
        write(SUB_BASE, f"LP_{tag}_DEFAUT_IRONING", named(p, f"LP_{tag}_DEFAUT_IRONING", IRONING_ON),
              "(ironing top)"); n_proc += 1
        # ---- Support CUNG loai ----
        for s in analyzer.support_strategy(fil, [fil]):
            suf = "MATDEP" if s["id"] == "same_smooth" else "DEGO"
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            write(SUB_SCUNG, f"LP_{tag}_SUP_CUNG_{suf}", named(p, f"LP_{tag}_SUP_CUNG_{suf}", keys),
                  f"(Top Z {keys['support_top_z_distance']})"); n_proc += 1
        # ---- Support KHAC loai ----
        for s in analyzer.support_strategy(fil, [fil, partner]):
            if s["id"] != "diff":
                continue
            keys = dict(s["keys"]); keys["enable_support"] = "1"
            pn = partner.split()[0]
            write(SUB_SKHAC, f"LP_{tag}_SUP_KHAC_{pn}", named(p, f"LP_{tag}_SUP_KHAC_{pn}", keys),
                  f"(interface {pn} · Top Z 0)"); n_proc += 1
    return n_fil, n_proc


if __name__ == "__main__":
    print("Sinh config quoc dan ->", OUT)
    nf, npr = main()
    print(f"\nXONG: {nf} filament + {npr} process.")
