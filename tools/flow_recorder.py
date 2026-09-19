"""Record what Isaac and Doom show, side by side, while you start a run or change floors:

    python tools\\flow_recorder.py

Grabs both windows several times a second (both must be visible on screen - Doom windowed
or borderless, not exclusive fullscreen), and keeps a frame whenever either picture or
the pipe state changes. Each kept frame is a row: time, Isaac, Doom, and what the mod,
the bridge and Doom were saying at that moment (state file, isaac_in.cfg, the bridge log,
Doom's log). Rows are written as pages of images in tools\\flow\\ plus timeline.txt.

Start it, then do the thing that looks wrong (launch, New Run, a trapdoor...). Ctrl+C
stops it. It gives up on its own after 6 minutes.
"""
import ctypes, io, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT

try:
    import numpy as np
    from PIL import Image, ImageGrab, ImageDraw
except ImportError:
    print("needs Pillow and numpy:  pip install pillow numpy"); sys.exit(1)

PIPE = ROOT / "pipe"
OUT = ROOT / "tools" / "flow"
THUMB = (320, 180)
ROWS_PER_PAGE = 10
MAX_SECONDS = 360
STATE_TAGS = ("SEQ", "FRAME", "ROOM", "NEWROOM", "RUN", "DEAD", "PAUSED", "FROZEN", "INTRO", "FALL", "MUSIC", "BACKDROP")


def windows():
    """{'isaac': rect, 'doom': rect} client areas on screen"""
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    found = {}
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h) and not u.IsIconic(h):
            n = u.GetWindowTextLengthW(h)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, buf, n + 1)
                t = buf.value.lower()
                if t.startswith("binding of isaac") and "isaac" not in found:
                    found["isaac"] = h
                # GZDoom / UZDoom title their window after the game ("Freedoom: Phase 2", "DOOM 2"...);
                # the bridge's console and explorer windows mention IsaacDoom, so those are skipped
                elif "doom" in t and "isaac" not in t and "bridge" not in t and "doom" not in found:
                    found["doom"] = h
        return True
    u.EnumWindows(proc(cb), 0)
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    out = {}
    for k, h in found.items():
        rc = RECT(); u.GetClientRect(h, ctypes.byref(rc))
        pt = POINT(0, 0); u.ClientToScreen(h, ctypes.byref(pt))
        w, hh = rc.r - rc.l, rc.b - rc.t
        if w > 50 and hh > 30:
            out[k] = (pt.x, pt.y, pt.x + w, pt.y + hh)
    return out


def grab(rect):
    try:
        img = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")
        img.thumbnail(THUMB)
        canvas = Image.new("RGB", THUMB, (30, 30, 30))
        canvas.paste(img, ((THUMB[0] - img.width) // 2, (THUMB[1] - img.height) // 2))
        return canvas
    except Exception:
        return None


def read_text(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def state_summary():
    txt = read_text(PIPE / "isaac_state.txt")
    parts = []
    for ln in txt.splitlines():
        tag, _, rest = ln.partition(" ")
        if tag in STATE_TAGS:
            if tag == "ROOM":
                p = rest.split()
                rest = f"idx={p[0]} type={p[2]} stage={p[6]}/{p[7]}" if len(p) > 7 else rest
            parts.append(f"{tag}={rest.strip()[:40]}")
    try:
        alive_age = time.time() - (PIPE / "isaac_alive.txt").stat().st_mtime
        parts.append(f"alive={alive_age:.1f}s")
    except OSError:
        parts.append("alive=none")
    cfg = read_text(PIPE / "isaac_in.cfg")
    for ln in cfg.splitlines():
        if ln.startswith(("set isaac_room ", "set isaac_intro ", "set isaac_fall ", "set isaac_frozen ", "set isaac_stageseq ", "set isaac_seq ")):
            parts.append("cfg:" + ln[4:][:60])
    return " | ".join(parts)


class Tail:
    def __init__(self, path):
        self.path = path
        try:
            self.pos = path.stat().st_size
        except OSError:
            self.pos = 0
    def new_lines(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos); data = f.read(size - self.pos); self.pos = size
        return [l.strip() for l in data.decode("utf-8", "replace").splitlines() if l.strip()]


def diff(a, b):
    if a is None or b is None:
        return 999
    return float(np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16)).mean())


def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*"):
        try: old.unlink()
        except OSError: pass
    bridge_tail = Tail(PIPE / "bridge_log.txt")
    doom_log = PIPE / "log-doom_out-log.txt"
    if not doom_log.exists():
        doom_log = PIPE / "doom_out.log"
    doom_tail = Tail(doom_log)
    timeline = open(OUT / "timeline.txt", "w", encoding="utf-8")
    t0 = time.time()
    last_isaac = last_doom = None
    last_state = ""
    last_kept = 0.0
    rows = []
    page = 0
    print("recording... do the thing now (Ctrl+C to stop)")

    def flush_page():
        nonlocal rows, page
        if not rows:
            return
        rh = THUMB[1] + 46
        sheet = Image.new("RGB", (THUMB[0] * 2 + 30, rh * len(rows)), (16, 16, 16))
        d = ImageDraw.Draw(sheet)
        for i, (t, a, b, text) in enumerate(rows):
            y = i * rh
            d.text((6, y + 4), f"t={t:6.2f}s", fill=(255, 220, 90))
            d.text((90, y + 4), text[:190], fill=(220, 220, 220))
            d.text((6, y + 18), text[190:380], fill=(180, 180, 180))
            d.text((6, y + 32), text[380:570], fill=(180, 180, 180))
            if a: sheet.paste(a, (6, y + 46))
            if b: sheet.paste(b, (THUMB[0] + 18, y + 46))
        page += 1
        sheet.save(OUT / f"page_{page:02d}.png")
        print(f"  wrote page_{page:02d}.png ({len(rows)} frames)")
        rows = []

    try:
        while time.time() - t0 < MAX_SECONDS:
            t = time.time() - t0
            w = windows()
            a = grab(w["isaac"]) if "isaac" in w else None
            b = grab(w["doom"]) if "doom" in w else None
            st = state_summary()
            bl = bridge_tail.new_lines()
            dl = [l for l in doom_tail.new_lines() if not l.startswith(("IS ", "ISA ", "ISK "))][-3:]
            changed = diff(a, last_isaac) > 3.5 or diff(b, last_doom) > 3.5 or st != last_state or bl or dl
            if changed or t - last_kept > 4.0:
                text = st
                if bl: text += " || bridge: " + " ; ".join(bl[-4:])
                if dl: text += " || doom: " + " ; ".join(dl)
                rows.append((t, a, b, text))
                timeline.write(f"{t:7.2f}  {text}\n"); timeline.flush()
                last_isaac, last_doom, last_state, last_kept = a, b, st, t
                if len(rows) >= ROWS_PER_PAGE:
                    flush_page()
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    flush_page()
    timeline.close()
    print(f"done: {OUT}")


if __name__ == "__main__":
    main()
