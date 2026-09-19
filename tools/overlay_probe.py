"""Measure how Isaac actually uses its room shadow overlays:  python tools\\overlay_probe.py

Run it while playing (Isaac's window must be visible on screen, not covered; the bridge
and Doom can be running or not). Every time you enter a 1x1 room it grabs Isaac's window,
looks at the floor, and works out which of the stage's five overlay images (if any) the
game multiplied over it and how strongly. Each room is appended to tools\\overlay_log.csv
and a running summary is printed: how many rooms had an overlay, how dark, and whether
the pick can be predicted from the room's seeds (so IsaacDoom can make the same pick).

Stop it with Ctrl+C: it prints the final summary. Send overlay_log.csv along if asked.
"""
import csv, ctypes, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT, gfx_dirs

try:
    import numpy as np
    from PIL import Image, ImageGrab
except ImportError:
    print("needs Pillow and numpy:  pip install pillow numpy"); sys.exit(1)

PIPE = ROOT / "pipe"
STATE = PIPE / "isaac_state.txt"
LOG = ROOT / "tools" / "overlay_log.csv"

# backdrop id -> overlay folder (same table as build_sprites.py)
OVERLAY_DIRS = {
    1: "basement", 2: "basement", 3: "basement", 4: "caves", 5: "caves", 6: "caves",
    7: "depths", 8: "depths", 9: "depths", 10: "womb", 11: "womb", 12: "womb", 13: "womb",
    14: "sheol", 16: "sheol", 15: "cathedral", 17: "chest",
    31: "downpour", 45: "downpour", 32: "mines", 46: "mines", 33: "mausoleum", 40: "mausoleum",
    41: "mausoleum", 47: "gehenna", 34: "corpse", 43: "corpse", 44: "corpse",
    48: "home", 49: "home", 50: "home",
}
# Isaac's 1x view: 480x270, the 15x9-cell room (26 px cells) centred in it
VIEW_W, VIEW_H, CELL = 480, 270, 26
ROOM_X, ROOM_Y = (VIEW_W - 15 * CELL) // 2, (VIEW_H - 9 * CELL) // 2


def read_state():
    try:
        txt = STATE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    st = {"ents": []}
    for ln in txt.splitlines():
        tag, _, rest = ln.partition(" ")
        p = rest.split()
        try:
            if tag == "ROOM":
                st["idx"] = int(p[0]); st["shape"] = int(p[1]); st["type"] = int(p[2]); st["w"] = int(p[4]); st["h"] = int(p[5])
                st["stage"] = int(p[6]); st["tl"] = (float(p[8]), float(p[9]))
            elif tag == "BACKDROP":
                st["backdrop"] = int(p[0])
            elif tag == "GRID":
                st["grid"] = rest.strip()
            elif tag == "SEEDS":
                st["seeds"] = [int(v) for v in p[:4]]
            elif tag == "OVERLAY":
                st["guess"] = int(p[0])
            elif tag == "ENT":
                st["ents"].append((float(p[5]), float(p[6]), float(p[9]) if len(p) > 9 else 20.0))
            elif tag == "SEQ":
                st["seq"] = int(p[0])
        except (ValueError, IndexError):
            pass
    return st if "idx" in st and "grid" in st else None


def isaac_window():
    """screen rect of Isaac's client area, or None"""
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, buf, n + 1)
                if buf.value.lower().startswith("binding of isaac"):
                    found.append(h)
        return True
    u.EnumWindows(proc(cb), 0)
    hwnd = found[0] if found else None
    if not hwnd:
        return None
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    rc = RECT(); u.GetClientRect(hwnd, ctypes.byref(rc))
    pt = POINT(0, 0); u.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rc.r - rc.l, rc.b - rc.t
    if w < 100 or h < 60:
        return None
    if u.IsIconic(hwnd):
        return None
    return (pt.x, pt.y, pt.x + w, pt.y + h)


def grab_view(rect):
    """Isaac's 480x270 view, resampled from the window (letterboxed if the window isn't 16:9)"""
    img = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")
    w, h = img.size
    # the game keeps 16:9 inside the client area
    if w / h > 16 / 9:
        vw = int(h * 16 / 9); x0 = (w - vw) // 2; img = img.crop((x0, 0, x0 + vw, h))
    else:
        vh = int(w * 9 / 16); y0 = (h - vh) // 2; img = img.crop((0, y0, w, y0 + vh))
    return img.resize((VIEW_W, VIEW_H), Image.BOX)


_overlays = {}
def overlays_for(bid):
    folder = OVERLAY_DIRS.get(bid)
    if not folder:
        return []
    if folder in _overlays:
        return _overlays[folder]
    dlc, base = gfx_dirs()
    out = []
    for k in range(1, 6):
        for root in (dlc, base):
            p = root / "overlays" / folder / f"1x1_overlay_{k}.png"
            if p.exists():
                g = Image.open(p).convert("L").resize((15 * CELL, 9 * CELL), Image.BILINEAR)
                out.append((k, np.asarray(g, dtype=np.float32) / 255.0))
                break
    _overlays[folder] = out
    return out


def floor_mask(st):
    """which pixels of the 15x9-cell room image are plain floor with nothing standing on them"""
    w, h, g = st["w"], st["h"], st["grid"]
    m = np.zeros((9 * CELL, 15 * CELL), dtype=bool)
    for r in range(h):
        for c in range(w):
            i = r * w + c
            if i < len(g) and g[i] == ".":
                m[r * CELL + 3:(r + 1) * CELL - 3, c * CELL + 3:(c + 1) * CELL - 3] = True
    tlx, tly = st["tl"]
    for ex, ey, size in st["ents"]:
        cx = (1 + (ex - tlx) / 40.0) * CELL
        cy = (1 + (ey - tly) / 40.0) * CELL
        rad = max(18.0, size * 0.8 * CELL / 40.0)
        y0, y1 = int(max(0, cy - rad * 1.6)), int(min(m.shape[0], cy + rad * 0.6))
        x0, x1 = int(max(0, cx - rad)), int(min(m.shape[1], cx + rad))
        m[y0:y1, x0:x1] = False
    # the tile just past the walls carries the wall shadow: keep clear of it
    m[:CELL + 6, :] = False; m[-CELL - 6:, :] = False; m[:, :CELL + 6] = False; m[:, -CELL - 6:] = False
    return m


def analyse(view, st):
    """(overlay k or 0, alpha, correlation) for the room in this view"""
    room = np.asarray(view, dtype=np.float32)[ROOM_Y:ROOM_Y + 9 * CELL, ROOM_X:ROOM_X + 15 * CELL]
    lum = room.mean(axis=2)
    mask = floor_mask(st)
    if mask.sum() < 800:
        return None
    L = lum[mask]
    best = (0, 0.0, 0.0)
    for k, g in overlays_for(st["backdrop"]):
        G = g[mask]
        if G.std() < 0.02:
            continue
        r = float(np.corrcoef(L, G)[0, 1])
        # L = a + b*G  ->  base = a + b, alpha = b / base
        b, a = np.polyfit(G, L, 1)
        base = a + b
        alpha = float(b / base) if base > 1 else 0.0
        if r > best[2]:
            best = (k, alpha, r)
    k, alpha, r = best
    # a real overlay fits the floor well; the floor's own blotches never correlate above ~0.3
    if r < 0.38 or alpha < 0.06:
        return (0, alpha, r)
    return (k, alpha, r)


def seed_formulas(seed_names):
    """candidate ways the game might turn a seed into an overlay index 1..5"""
    out = []
    for si, nm in enumerate(seed_names):
        out.append((f"{nm} % 5 + 1", lambda s, si=si: s[si] % 5 + 1))
        out.append((f"{nm} % 6 (0 = none)", lambda s, si=si: s[si] % 6))
        for sh in (8, 16, 24):
            out.append((f"({nm} >> {sh}) % 5 + 1", lambda s, si=si, sh=sh: (s[si] >> sh) % 5 + 1))
            out.append((f"({nm} >> {sh}) % 6", lambda s, si=si, sh=sh: (s[si] >> sh) % 6))
    return out


def summary(rows):
    n = len(rows)
    if not n:
        return
    with_ov = [r for r in rows if r["overlay"] > 0]
    print(f"\n--- {n} rooms measured: {len(with_ov)} with an overlay ({100 * len(with_ov) / n:.0f}%)")
    if with_ov:
        al = np.array([r["alpha"] for r in with_ov])
        print(f"    strength (how dark the black parts get): median {np.median(al):.2f}, range {al.min():.2f}..{al.max():.2f}")
        counts = {k: sum(1 for r in with_ov if r["overlay"] == k) for k in range(1, 6)}
        print("    which one: " + ", ".join(f"#{k} x{v}" for k, v in counts.items()))
    names = ["deco", "spawn", "award", "listidx"]
    if any(r.get("seeds") for r in rows):
        best = []
        for label, f in seed_formulas(names):
            hit = sum(1 for r in rows if r.get("seeds") and f(r["seeds"]) == r["overlay"])
            best.append((hit, label))
        best.sort(reverse=True)
        print("    seed formulas that predict the pick: " + ", ".join(f"{l}: {h}/{n}" for h, l in best[:4]))
        hit = sum(1 for r in rows if r.get("guess") == r["overlay"])
        print(f"    IsaacDoom's current guess matched {hit}/{n}")


def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    rows = []
    if LOG.exists():
        with open(LOG, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({"overlay": int(r["overlay"]), "alpha": float(r["alpha"]), "guess": int(r["guess"]),
                                 "seeds": [int(v) for v in r["seeds"].split("/")] if r["seeds"] else None})
                except (ValueError, KeyError):
                    pass
        print(f"{len(rows)} rooms already in {LOG.name}")
    print("watching... enter rooms in Isaac (1x1 rooms only are measured; keep the window visible)")
    seen = {}      # (stage, room idx) -> measured
    last_idx = None; entered_at = 0.0
    while True:
        st = read_state()
        if st and st["idx"] != last_idx:
            last_idx = st["idx"]; entered_at = time.time()
        # give the room a second to fade in, then measure once
        if st and st["idx"] == last_idx and time.time() - entered_at > 1.2:
            key = (st.get("stage"), st["idx"])
            if key not in seen and st["w"] == 15 and st["h"] == 9:
                seen[key] = True
                rect = isaac_window()
                if not rect:
                    print("  (Isaac's window not found / minimised)")
                else:
                    try:
                        res = analyse(grab_view(rect), st)
                    except Exception as e:
                        res = None; print("  (grab failed:", e, ")")
                    if res is None:
                        print(f"  room {st['idx']}: not enough clear floor to measure")
                    else:
                        k, alpha, r = res
                        row = {"overlay": k, "alpha": round(alpha, 3), "guess": st.get("guess", 0), "seeds": st.get("seeds")}
                        rows.append(row)
                        new = not LOG.exists()
                        with open(LOG, "a", newline="") as f:
                            wr = csv.writer(f)
                            if new:
                                wr.writerow(["stage", "room", "type", "backdrop", "overlay", "alpha", "corr", "guess", "seeds"])
                            wr.writerow([st.get("stage"), st["idx"], st.get("type"), st.get("backdrop"), k, round(alpha, 3), round(r, 3),
                                         st.get("guess", 0), "/".join(str(v) for v in st["seeds"]) if st.get("seeds") else ""])
                        print(f"  room {st['idx']} (stage {st.get('stage')}, backdrop {st.get('backdrop')}): "
                              + (f"overlay #{k}, strength {alpha:.2f} (fit {r:.2f})" if k else f"no overlay (best fit {r:.2f})")
                              + f"  [IsaacDoom guessed #{st.get('guess', 0)}]")
                        if len(rows) % 5 == 0:
                            summary(rows)
        time.sleep(0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        rows = []
        if LOG.exists():
            with open(LOG, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append({"overlay": int(r["overlay"]), "alpha": float(r["alpha"]), "guess": int(r["guess"]),
                                     "seeds": [int(v) for v in r["seeds"].split("/")] if r["seeds"] else None})
                    except (ValueError, KeyError):
                        pass
        summary(rows)
