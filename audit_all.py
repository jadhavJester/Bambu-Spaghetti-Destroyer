"""Audit toan bo file 3mf x 3 loai nhua -> ghi JSONL.
CO LAP tung FILE trong subprocess rieng: mesh lon segfault (loi da biet) chi lam
FAIL 1 file, KHONG giet ca me. Chay: python audit_all.py [worker <path>]."""
import glob, json, io, os, sys, subprocess

FILS = ["PLA LITE", "PLA MATTE", "PETG ECO"]
OUT = "audit_out.jsonl"
KEYS = ["layer_height", "initial_layer_print_height", "outer_wall_speed", "inner_wall_speed",
        "sparse_infill_speed", "internal_solid_infill_speed", "top_surface_speed",
        "initial_layer_speed", "travel_speed", "sparse_infill_density", "wall_loops",
        "top_surface_line_width", "brim_type", "brim_width", "enable_support",
        "support_type", "support_threshold_angle", "support_top_z_distance",
        "support_interface_top_layers", "ironing_type", "ironing_flow", "ironing_spacing",
        "bridge_flow", "infill_wall_overlap", "top_shell_layers", "bottom_shell_layers"]


def _analyze_one(path: str) -> list:
    """Chay trong subprocess: tra ve list rec cho 1 file x moi nhua."""
    import analyzer
    out = []
    for fs in FILS:
        rec = {"k": path + "||" + fs, "file": os.path.basename(path), "fil": fs}
        try:
            r = analyzer.analyze(path, mode="balanced", fil_sel=fs)
            p = (analyzer.make_preset(r, mode="balanced") or {}).get("preset") or {}
            mesh = r.get("mesh") or {}
            rec["dims"] = mesh.get("dims")
            rec["bed_cm2"] = mesh.get("bed_cm2")
            rec["height"] = mesh.get("height")
            rec["over_pct"] = mesh.get("overhang_pct")
            rec["mvs"] = ((r.get("flow") or {}).get("mvs"))

            def _flat(v):    # speed la list ['150'] -> lay so
                return v[0] if isinstance(v, list) and v else v
            rec["p"] = {x: _flat(p.get(x)) for x in KEYS if p.get(x) is not None}
            rec["ok"] = True
        except Exception as e:                              # noqa: BLE001
            rec["ok"] = False
            rec["err"] = f"{type(e).__name__}: {str(e)[:160]}"
        out.append(rec)
    return out


if len(sys.argv) >= 3 and sys.argv[1] == "worker":
    # Che do WORKER: phan tich dung 1 file, in JSONL ra stdout.
    # BUG: stdout Windows mac dinh cp1252 -> file ten TIENG TRUNG lam json.dumps
    # (ensure_ascii=False) VO khi ghi -> worker chet, dieu phoi tuong 'segfault'.
    # Ep UTF-8 cho stdout worker.
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # py3.7+
    except Exception:                                       # noqa: BLE001
        pass
    for rec in _analyze_one(sys.argv[2]):
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    sys.exit(0)

# Che do DIEU PHOI.
done = set()
if os.path.exists(OUT):
    for ln in io.open(OUT, encoding="utf-8"):
        try: done.add(json.loads(ln)["k"])
        except Exception: pass

files = sorted(glob.glob(r"C:\Users\Admin\Downloads\*.3mf")) + \
        sorted(glob.glob(r"D:\16.Sharp3D\**\*.3mf", recursive=True))

fh = io.open(OUT, "a", encoding="utf-8")
crashed = []
for f in files:
    if all((f + "||" + fs) in done for fs in FILS):
        continue
    try:
        _env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, __file__, "worker", f], env=_env,
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip().startswith("{")]
        if not lines:
            raise RuntimeError(f"rc={p.returncode} no output; stderr={(p.stderr or '')[:120]}")
        for l in lines:
            fh.write(l + "\n")
        if p.returncode != 0:            # segfault SAU khi in -> hiem, van danh dau
            crashed.append((os.path.basename(f), f"rc={p.returncode}"))
    except Exception as e:               # noqa: BLE001 — subprocess CHET (segfault/timeout)
        for fs in FILS:
            fh.write(json.dumps({"k": f + "||" + fs, "file": os.path.basename(f),
                                 "fil": fs, "ok": False,
                                 "err": f"SUBPROC {type(e).__name__}: {str(e)[:120]}"},
                                ensure_ascii=False) + "\n")
        crashed.append((os.path.basename(f), f"{type(e).__name__}"))
    fh.flush()
    print(".", end="", flush=True)
fh.close()
print(f"\nXONG {len(files)} file | subprocess chet: {len(crashed)}")
for name, why in crashed:
    print("   CRASH", name[:55], "->", why)
