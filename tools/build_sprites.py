"""
IsaacDoom sprite converter
==========================
Turns Isaac's .anm2 animations + PNG sheets into Doom sprites.

    python tools\\build_sprites.py

Reads:   Documents\\IsaacDoom\\sprites\\          (Repentance DLC gfx tree, overlay)
         Documents\\IsaacDoom\\sprites\\base\\     (base game gfx tree)
Writes:  Documents\\IsaacDoom\\doom\\isaacsprites.pk3
         Documents\\IsaacDoom\\tools\\sprite_report.txt

For every animation of every .anm2 it evaluates each frame like the game does
(layer order, keyframe delays, interpolation, pivots, scale, rotation, tint,
alpha), composites the layers, dedupes identical frames, and stores each unique
frame as  sprites/<NAME><F>0.png  with a grAb chunk so the pivot lands on the
actor's origin. A text lump ISAACSPR maps  "<anm2 key>|<Animation>"  to the
sprite+frame token for every frame index, which the ZScript side reads.

Needs Pillow:  pip install pillow
"""
import io, os, re, struct, sys, zlib, zipfile, hashlib, math, time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow is required:  pip install pillow"); sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT as PROJ, gfx_dirs
SPR_DLC, SPR_BASE = gfx_dirs()      # the game's unpacked resources, else Documents\IsaacDoom\sprites
OUT_PK3 = PROJ / "doom" / "isaacsprites.pk3"
REPORT = PROJ / "tools" / "sprite_report.txt"

# folders under gfx we don't need in Doom
SKIP_DIRS = {"ui", "cutscenes", "backdrop", "overlays", "promo", "base", "characters", "menu",
             "achievements", "endings", "special", "controls", "boss_bar", "death screen"}
MAX_FRAMES_PER_NAME = 26          # A..Z
FRAME_BUDGET = 70000              # frames are drawn via picnum, so this is a startup-time/memory budget only
MAX_UNIQUE_PER_ANIM = 12          # subsample long animations (30 fps -> ~15 fps is invisible)
# conversion priority: what to keep if the budget runs out (first = most important)
PRIORITY = ["monsters", "bosses", "pickups", "collectibles", "grid", "tears", "bombs", "familiars",
            "projectiles", "slots", "other", "effects"]
# layers to leave out, by anm2 key prefix (the doors' dark interior must be see-through in 3D)
SKIP_LAYERS = {"grid/door": {"background"}}
# Isaac draws doors in fake perspective (narrow at the bottom). Widen the bottom so the
# door becomes a flat rectangle that can be hung squarely on a Doom wall.
KEYSTONE = {"grid/door": 0.30}    # bottom inset as a fraction of the width, per key prefix
# anm2s whose sheet the game swaps per id: render once per file in the folder, key "<anm2>#<id>"
VARIANT_SETS = {
    "005.100_collectible": ("items/collectibles", r"collectibles?_(\d+)_", ("Idle", "ShopIdle", "Empty", "PlayerPickup")),
    "005.350_trinket":     ("items/trinkets",     r"trinkets?_(\d+)_",     ("Idle", "ShopIdle", "PlayerPickup")),
}
# door anm2s the game re-skins by swapping the sheet (shop door, stage doors...):
# rendered once per sheet under the key "<anm2>@<sheet>" (the mod reports which one)
DOOR_SKINS = {
    "grid/door_01_normaldoor": ["door_00_shopdoor", "door_00_sacrificeroomdoor", "door_00_diceroomdoor",
        "door_13_librarydoor", "door_12_cellardoor", "door_01_burningbasement", "door_27_drownedcaves",
        "door_14_depthsdoor", "door_25_wombdoor", "door_28_scarredroomdoor", "door_01_bluewombdoor",
        "door_19_sheoldoor", "door_22_cathedraldoor", "door_21_darkroomdoor", "door_23_chestdoor",
        "door_01_corpsedoor", "door_01_corpse2door", "door_01_gehennadoor", "door_01_mausoleumdoor",
        "door_01_minesdoor", "door_26_paytoplaydoor"],
    "grid/door_08_holeinwall": ["door_08_holeinwall_caves", "door_08_holeinwall_depths", "door_08_holeinwall_womb",
        "door_08_holeinwall_utero", "door_08_holeinwall_cathedral", "door_08_holeinwall_darkroom",
        "door_08_holeinwall_corpse"],
    "grid/door_02_treasureroomdoor": ["door_02b_chestroomdoor"],
}
# animations the game plays as an overlay underneath another one (Repentance draws item
# pedestals this way): key prefix -> {main animation: overlay animation}
MERGE_OVERLAY = {}   # overlays (e.g. item pedestals) are drawn live by Doom from Isaac's overlay frame
CANVAS = 768                      # working canvas, origin at the centre
ARGS = set(a.lower() for a in sys.argv[1:])


# --------------------------------------------------------------------------
# sheet loading with overlay resolution (DLC tree wins, then base tree)
# --------------------------------------------------------------------------
_sheet_cache = {}
_lower_index = None


def _build_lower_index():
    global _lower_index
    _lower_index = {}
    for root in (SPR_BASE, SPR_DLC):
        if not root.exists():
            continue
        for p in root.rglob("*.png"):
            rel = p.relative_to(root).as_posix().lower()
            if rel.startswith("base/"):
                continue
            _lower_index[rel] = p    # DLC iterated last -> overrides base


def _norm(path):
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def load_sheet(rel, anm2_dir=""):
    """rel: path as written in the anm2; anm2_dir: the anm2's folder relative to the gfx root"""
    if _lower_index is None:
        _build_lower_index()
    raw = rel.replace("\\", "/").lower()
    if raw.startswith("gfx/"):
        raw = raw[4:]
    cands = [_norm(raw)]
    if anm2_dir:
        cands.insert(0, _norm(anm2_dir.lower() + "/" + raw))
    key = cands[0]
    if key in _sheet_cache:
        return _sheet_cache[key]
    p = None
    for c in cands:
        p = _lower_index.get(c)
        if p:
            break
    im = None
    if p:
        try:
            im = Image.open(p).convert("RGBA")
        except Exception:
            im = None
    _sheet_cache[key] = im
    return im


# --------------------------------------------------------------------------
# anm2 evaluation
# --------------------------------------------------------------------------
NUM = ("XPosition", "YPosition", "XPivot", "YPivot", "XCrop", "YCrop", "Width", "Height",
       "XScale", "YScale", "Delay", "RedTint", "GreenTint", "BlueTint", "AlphaTint",
       "RedOffset", "GreenOffset", "BlueOffset", "Rotation")
LERP = ("XPosition", "YPosition", "XScale", "YScale", "Rotation", "AlphaTint",
        "RedTint", "GreenTint", "BlueTint", "RedOffset", "GreenOffset", "BlueOffset")


def parse_frame(el):
    f = {}
    for k in NUM:
        v = el.get(k)
        if v is not None:
            try:
                f[k] = float(v)
            except ValueError:
                f[k] = 0.0
    f["Visible"] = el.get("Visible", "true").lower() == "true"
    f["Interpolated"] = el.get("Interpolated", "false").lower() == "true"
    return f


def frame_at(keyframes, i):
    """keyframes: list of dicts with Delay; returns interpolated frame dict at index i, or None"""
    if not keyframes:
        return None
    t = 0
    for k, kf in enumerate(keyframes):
        d = max(1, int(kf.get("Delay", 1)))
        if i < t + d:
            if kf["Interpolated"] and k + 1 < len(keyframes) and d > 1:
                nxt = keyframes[k + 1]
                u = (i - t) / d
                out = dict(kf)
                for key in LERP:
                    if key in kf and key in nxt:
                        out[key] = kf[key] + (nxt[key] - kf[key]) * u
                return out
            return kf
        t += d
    return keyframes[-1]


def total_frames(keyframes):
    return sum(max(1, int(kf.get("Delay", 1))) for kf in keyframes)


class Anm2:
    def __init__(self, path, key, sheet_override=None):
        self.path = path
        self.key = key
        self.sheet_override = sheet_override or {}   # sheetId -> path
        self.dir = key.rsplit("/", 1)[0] if "/" in key else ""
        tree = ET.parse(path)
        root = tree.getroot()
        self.sheets = {}
        for s in root.iter("Spritesheet"):
            self.sheets[int(s.get("Id"))] = s.get("Path", "")
        skip = set()
        for prefix, names in SKIP_LAYERS.items():
            if key.startswith(prefix):
                skip |= names
        self.layers = []   # (id, sheetId) in draw order (by Id)
        for l in root.iter("Layer"):
            lname = l.get("Name", "").lower()
            # drop-shadow layers belong to a top-down game, not to standing billboards
            if lname in skip or "shadow" in lname:
                continue
            self.layers.append((int(l.get("Id")), int(l.get("SpritesheetId", "0"))))
        self.layers.sort()
        self.default = root.find("Animations").get("DefaultAnimation", "") if root.find("Animations") is not None else ""
        self.anims = {}
        for a in root.iter("Animation"):
            name = a.get("Name")
            n = int(a.get("FrameNum", "1"))
            rootf = [parse_frame(f) for f in a.find("RootAnimation").findall("Frame")] if a.find("RootAnimation") is not None else []
            layers = {}
            order = []      # draw order = the order the LayerAnimations appear in the file (first = bottom)
            la = a.find("LayerAnimations")
            if la is not None:
                for lan in la.findall("LayerAnimation"):
                    if lan.get("Visible", "true").lower() != "true":
                        continue
                    lid = int(lan.get("LayerId"))
                    layers[lid] = [parse_frame(f) for f in lan.findall("Frame")]
                    order.append(lid)
            self.anims[name] = dict(n=n, root=rootf, layers=layers, order=order)

    def render(self, anim, i):
        """returns (RGBA image, xoff, yoff) with origin at (xoff, yoff) inside the image, or None"""
        canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        ox = oy = CANVAS // 2
        drew = False
        # overlay animation drawn underneath first (e.g. the pedestal under an item)
        for prefix, mp in MERGE_OVERLAY.items():
            if self.key.startswith(prefix) and anim in mp and mp[anim] in self.anims:
                ov = self.anims[mp[anim]]
                n = max(1, ov["n"])
                drew = self._draw_anim(canvas, ov, i % n, ox, oy) or drew
        a = self.anims[anim]
        rf = frame_at(a["root"], i) or {}
        if not rf.get("Visible", True):
            return None
        drew = self._draw_anim(canvas, a, i, ox, oy) or drew
        if not drew:
            return None
        bbox = canvas.getbbox()
        if not bbox:
            return None
        img = canvas.crop(bbox)
        xo, yo = ox - bbox[0], oy - bbox[1]
        for prefix, inset in KEYSTONE.items():
            if self.key.startswith(prefix):
                w, h = img.size
                ins = int(round(w * inset))
                # output rectangle samples the source trapezoid: TL, BL, BR, TR
                img = img.transform((w, h), Image.QUAD, (0, 0, ins, h, w - ins, h, w, 0), Image.NEAREST)
                break
        return img, xo, yo

    def _draw_anim(self, canvas, a, i, ox, oy):
        """draw one animation's frame i onto the canvas; returns True if anything was drawn"""
        rf = frame_at(a["root"], i) or {}
        if not rf.get("Visible", True):
            return False
        rx, ry = rf.get("XPosition", 0.0), rf.get("YPosition", 0.0)
        rsx, rsy = rf.get("XScale", 100.0) / 100.0, rf.get("YScale", 100.0) / 100.0
        drew = False
        sheet_of = dict(self.layers)
        for lid in a["order"]:
            if lid not in sheet_of:      # skipped layer (shadow etc.)
                continue
            sid = sheet_of[lid]
            kfs = a["layers"].get(lid)
            if not kfs:
                continue
            f = frame_at(kfs, i)
            if not f or not f.get("Visible", True):
                continue
            sheet = load_sheet(self.sheet_override.get(sid, self.sheets.get(sid, "")), self.dir)
            if sheet is None:
                continue
            w, h = int(f.get("Width", 0)), int(f.get("Height", 0))
            if w <= 0 or h <= 0:
                continue
            cx, cy = int(f.get("XCrop", 0)), int(f.get("YCrop", 0))
            crop = sheet.crop((cx, cy, cx + w, cy + h))
            sx, sy = f.get("XScale", 100.0) / 100.0 * rsx, f.get("YScale", 100.0) / 100.0 * rsy
            if abs(sx) < 0.01 or abs(sy) < 0.01:
                continue
            # tint / offset / alpha
            rt, gt, bt = f.get("RedTint", 255) / 255, f.get("GreenTint", 255) / 255, f.get("BlueTint", 255) / 255
            ro, go, bo = f.get("RedOffset", 0), f.get("GreenOffset", 0), f.get("BlueOffset", 0)
            alpha = f.get("AlphaTint", 255) / 255
            if (rt, gt, bt) != (1, 1, 1) or (ro, go, bo) != (0, 0, 0) or alpha != 1:
                r, g, b, al = crop.split()
                r = r.point(lambda v: max(0, min(255, int(v * rt + ro))))
                g = g.point(lambda v: max(0, min(255, int(v * gt + go))))
                b = b.point(lambda v: max(0, min(255, int(v * bt + bo))))
                al = al.point(lambda v: int(v * alpha))
                crop = Image.merge("RGBA", (r, g, b, al))
            px, py = f.get("XPivot", 0.0), f.get("YPivot", 0.0)
            flip_x = sx < 0
            flip_y = sy < 0
            sx, sy = abs(sx), abs(sy)
            nw, nh = max(1, int(round(w * sx))), max(1, int(round(h * sy)))
            if (nw, nh) != (w, h):
                crop = crop.resize((nw, nh), Image.NEAREST)
            px, py = px * sx, py * sy
            if flip_x:
                crop = crop.transpose(Image.FLIP_LEFT_RIGHT); px = nw - px
            if flip_y:
                crop = crop.transpose(Image.FLIP_TOP_BOTTOM); py = nh - py
            rot = f.get("Rotation", 0.0)
            if abs(rot) > 0.01:
                # rotate around the pivot; PIL rotates counter-clockwise, Isaac clockwise
                crop = crop.rotate(-rot, resample=Image.NEAREST, expand=True, center=(px, py))
                # after expand the pivot moves; recompute by rotating the pivot about the old centre
                ow, oh = nw, nh
                cxr, cyr = ow / 2, oh / 2
                ang = math.radians(rot)
                dx, dy = px - cxr, py - cyr
                rpx = cxr + dx * math.cos(ang) - dy * math.sin(ang)
                rpy = cyr + dx * math.sin(ang) + dy * math.cos(ang)
                px = rpx + (crop.width - ow) / 2
                py = rpy + (crop.height - oh) / 2
            dx = ox + rx + f.get("XPosition", 0.0) - px
            dy = oy + ry + f.get("YPosition", 0.0) - py
            canvas.alpha_composite(crop, (int(round(dx)), int(round(dy))))
            drew = True
        return drew


# --------------------------------------------------------------------------
# PNG with grAb offsets
# --------------------------------------------------------------------------
def png_with_grab(img, xoff, yoff):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    data = buf.getvalue()
    # insert grAb after IHDR (8 byte sig + 25 byte IHDR chunk)
    grab = struct.pack(">ii", int(xoff), int(yoff))
    chunk = struct.pack(">I", 8) + b"grAb" + grab
    chunk += struct.pack(">I", zlib.crc32(b"grAb" + grab) & 0xffffffff)
    return data[:33] + chunk + data[33:]


# --------------------------------------------------------------------------
# sprite name allocation
# --------------------------------------------------------------------------
B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class Namer:
    def __init__(self):
        self.n = 0

    def next(self):
        v = self.n
        self.n += 1
        s = ""
        for _ in range(3):
            s = B36[v % 36] + s
            v //= 36
        return "_" + s


def anm2_key(path, root):
    rel = path.relative_to(root).as_posix()
    rel = re.sub(r"\.anm2$", "", rel, flags=re.I)
    return re.sub(r"[^a-z0-9/_.\-]", "_", rel.lower())


def anim_key(name):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name)


def category(key):
    base = key.rsplit("/", 1)[-1]
    m = re.match(r"^(\d+)\.", base)
    if m:
        n = int(m.group(1))
        if n == 2: return "tears"
        if n == 3: return "familiars"
        if n == 4: return "bombs"
        if n == 5: return "pickups"
        if n == 6: return "slots"
        if n in (7, 8): return "other"
        if n == 9: return "projectiles"
        if n == 1000: return "effects"
        if 10 <= n < 1000: return "bosses" if key.find("boss") >= 0 or n >= 900 or n in (19, 20, 28, 36, 43, 45, 62, 63, 64, 65, 66, 67, 68, 69, 71, 74, 78, 79, 81, 82, 84, 97, 98, 99, 100, 101, 102) else "monsters"
    top = key.split("/", 1)[0] if "/" in key else ""
    if top == "grid": return "grid"
    if top in ("monsters",): return "monsters"
    if top in ("bosses",): return "bosses"
    if top in ("familiar", "familiars"): return "familiars"
    if top == "items":
        return "collectibles" if "collectible" in key else "pickups"
    if top == "effects": return "effects"
    return "other"


def wanted(path, root):
    rel = path.relative_to(root)
    parts = [p.lower() for p in rel.parts[:-1]]
    return not any(p in SKIP_DIRS for p in parts)


# Isaac BackdropType id -> backdrop sheet base name
BACKDROPS = {
    1: "01_basement", 2: "02_cellar", 3: "13_the burning basement", 4: "03_caves", 5: "04_catacombs",
    6: "14_the drowned caves", 7: "05_depths", 8: "06_necropolis", 9: "15_the dank depths", 10: "07_the womb",
    11: "08_utero", 12: "16_the scarred womb", 13: "18_blue womb", 14: "09_sheol", 15: "10_cathedral",
    16: "12_darkroom", 17: "11_chest", 19: "0a_library", 20: "0b_shop", 21: "0c_isaacsroom", 22: "0d_barrenroom",
    23: "0f_secretroom", 24: "0e_diceroom", 25: "0e_arcade", 28: "0b_ultragreedshop", 30: "0g_sacrificeroom",
    31: "01x_downpour", 32: "03x_mines", 33: "05x_mausoleum", 34: "07x_corpse", 40: "05x_mausoleum2",
    41: "05x_mausoleumb", 43: "07x_corpse2", 44: "07x_corpse3", 45: "02x_dross", 46: "04x_ashpit",
    47: "06x_gehenna", 48: "0ex_isaacs_bedroom", 49: "0fx_hallway", 50: "0gx_moms_bedroom",
    29: "03_caves",   # crawlspace: no sheet of its own, reuse the caves rock (darkened below)
}
DARKEN = {29: 0.55}
# hand-made wall textures dropped into the sprites folder: backdrop id -> file.
# Scaled to the full wall height and tiled sideways. Empty: every floor uses the wall
# course from the game's own backdrop sheet (the old basementwall.png was two courses
# stacked, which read as a seam half way up the wall).
WALL_OVERRIDES = {}
WALL_HEIGHT = 128
# the wall course in a backdrop sheet: rows 9..52 of the top-left corner piece are the
# dark stones under the ceiling and the two lit rows of stone that Isaac draws as "the
# wall" (the floor starts at row 52). Sideways the piece runs to x=234; the columns past
# it are the black gap between the sheet's quadrants.
WALL_ROWS = (9, 52)
WALL_X = (41, 233)
WOOD_CEILING = {1, 2, 3}    # basement, cellar, burning basement: plank ceilings (flats/ISCnn.png)
CEIL_DARK = 0.38            # the planks' brightness (Doom also lights the ceiling darker: isaac_ceillight)
FLOOR_SCALE = 1.6   # 40 px Isaac cell -> 64 Doom units


# Isaac's per-room shadow overlays (gfx/overlays/<stage>/<shape>_overlay_<1..5>.png: the shadows
# of the floorboards above the basement, of the stalactites in the caves...): backdrop id -> folder
OVERLAY_DIRS = {
    1: "basement", 2: "basement", 3: "basement", 4: "caves", 5: "caves", 6: "caves",
    7: "depths", 8: "depths", 9: "depths", 10: "womb", 11: "womb", 12: "womb", 13: "womb",
    14: "sheol", 16: "sheol", 15: "cathedral", 17: "chest",
    31: "downpour", 45: "downpour", 32: "mines", 46: "mines", 33: "mausoleum", 40: "mausoleum",
    41: "mausoleum", 47: "gehenna", 34: "corpse", 43: "corpse", 44: "corpse",
    48: "home", 49: "home", 50: "home",
}
# room shapes the overlays come in, as Doom sees them (grid cells): shape index -> (w, h)
OVERLAY_SHAPES = {0: ("1x1", 15, 9), 1: ("2x1", 28, 9), 2: ("1x2", 15, 16), 3: ("2x2", 28, 16)}
OVERLAY_ALPHA = 0.28      # how dark the overlay's black gets on the floor flats (measured in the game: ~0.28)
SHADE_ALPHA = 0.28        # ...and in the sprite-lighting map (ISSHADE.txt), scaled again live by isaac_shadow
ROOM_ORIGIN = 3 * 64      # where Doom puts the room's top-left corner (M cells of margin, 64 units each)
SHADE_PER_CELL = 4        # overlay brightness samples per cell for lighting the sprites (ISSHADE.txt)
SHADE_LINES = []


def build_overlay_flats(zf, bid, floor):
    """flats/ISO<bid><shape><n>.png: the stage's floor with Isaac's shadow overlay n multiplied
    in, one room-sized flat per room shape. Doom tiles flats from the world origin at one
    texel per unit, so the image is laid out in world texture space: rolled by the room's
    origin so that, tiled, it lands exactly on the room. Returns how many were written."""
    folder = OVERLAY_DIRS.get(bid)
    if not folder:
        return 0
    try:
        import numpy as np
    except ImportError:
        return 0
    ftile = np.asarray(floor.convert("RGB"), dtype=np.float32)      # (th, tw, 3), the plain floor flat
    th, tw = ftile.shape[:2]
    n = 0
    for shape, (name, cw, chh) in OVERLAY_SHAPES.items():
        W, H = cw * 64, chh * 64
        # the floor art exactly as the plain flat shows it inside the room: world x in
        # [origin, origin + W) wraps to texture column x mod W
        xs = ROOM_ORIGIN + ((np.arange(W) - ROOM_ORIGIN) % W)
        ys = ROOM_ORIGIN + ((np.arange(H) - ROOM_ORIGIN) % H)
        art = ftile[ys % th][:, xs % tw]
        for k in range(1, 6):
            ov = load_sheet(f"overlays/{folder}/{name}_overlay_{k}.png")
            if ov is None:
                continue
            # the overlay is greyscale: white = lit, black = shadow; stretch it over the room
            g = np.asarray(ov.convert("L").resize((W, H), Image.BILINEAR), dtype=np.float32) / 255.0
            shade = 1.0 - OVERLAY_ALPHA * (1.0 - g)
            # room pixel (rx, ry) sits at texture column (rx + origin) mod W: roll it into place
            shade = np.roll(np.roll(shade, ROOM_ORIGIN % H, axis=0), ROOM_ORIGIN % W, axis=1)
            out = np.clip(art * shade[..., None], 0, 255).astype(np.uint8)
            img = Image.fromarray(out, "RGB").quantize(256, method=Image.FASTOCTREE, dither=Image.NONE)
            buf = io.BytesIO(); img.save(buf, "PNG", optimize=True)
            zf.writestr(f"flats/ISO{bid:02d}{shape}{k}.png", buf.getvalue())
            n += 1
            # the same shadow as light, for the sprites standing in it: SHADE_PER_CELL samples
            # per cell of the (unrolled) room, 00 = darkest .. ff = lit, one line for ISSHADE.txt
            sp = 64 // SHADE_PER_CELL
            cols, rows = cw * SHADE_PER_CELL, chh * SHADE_PER_CELL
            samp = (1.0 - SHADE_ALPHA * (1.0 - g)).reshape(rows, sp, cols, sp).mean(axis=(1, 3))
            SHADE_LINES.append(f"{bid} {shape} {k} {cols} {rows} " + "".join(f"{int(v * 255):02x}" for v in np.clip(samp, 0, 1).ravel()))
    return n


def wall_cut(img, xa, xb, y0, y1):
    """the darkest opaque column of img in xa..xb over rows y0..y1 (a mortar joint in a
    stone wall); the middle of the range when nothing there is opaque"""
    px = img.load()
    best, bx = None, (xa + xb) // 2
    for x in range(max(0, xa), min(img.width, xb)):
        tot, n = 0, 0
        for y in range(y0, min(img.height, y1)):
            r, g, b_, a = px[x, y]
            if a > 200:
                tot += r + g + b_; n += 1
        if n < (y1 - y0) * 0.8:
            continue        # transparent / black gap: not wall
        v = tot / n
        if best is None or v < best:
            best, bx = v, x
    return bx


def build_backdrops(zf):
    """flats/ISFnn.png (floor) and textures/ISWnn.png (wall) per backdrop id, plus a black ceiling"""
    made = 0
    made_ov = 0
    for bid, base in BACKDROPS.items():
        wall = load_sheet(f"backdrop/{base}.png")
        # the room floor is the plain area inside the sheet's top-left corner piece
        # (26 px per cell); the "_nfloor" sheets are the big planks of narrow rooms
        # (inset past the wall's shadow at the top/left edge so the tile stays seamless)
        floor = wall.crop((60, 60, 234, 156)) if wall is not None else None
        if floor is None:
            floor = load_sheet(f"backdrop/{base}_nfloor.png")
        dark = DARKEN.get(bid)
        if dark and floor is not None:
            r, g, b_, a = floor.split()
            floor = Image.merge("RGBA", (r.point(lambda v: int(v * dark)), g.point(lambda v: int(v * dark)), b_.point(lambda v: int(v * dark)), a))
        if dark and wall is not None:
            r, g, b_, a = wall.split()
            wall = Image.merge("RGBA", (r.point(lambda v: int(v * dark)), g.point(lambda v: int(v * dark)), b_.point(lambda v: int(v * dark)), a))
        if floor is not None:
            fw, fh = floor.size
            tile = floor.crop((0, 0, min(fw, 260), min(fh, 182)))
            if tile.getbbox():
                tile = tile.crop(tile.getbbox())
            tw, th = tile.size
            fs = 64.0 / 26.0 if wall is not None else FLOOR_SCALE
            tile = tile.resize((int(tw * fs), int(th * fs)), Image.NEAREST)
            # Isaac lays the floor sheet out mirrored in four quadrants, which is what
            # makes it seamless; do the same so the flat tiles without a visible grid
            tw, th = tile.size
            quad = Image.new("RGBA", (tw * 2, th * 2), (0, 0, 0, 0))
            quad.alpha_composite(tile, (0, 0))
            quad.alpha_composite(tile.transpose(Image.FLIP_LEFT_RIGHT), (tw, 0))
            quad.alpha_composite(tile.transpose(Image.FLIP_TOP_BOTTOM), (0, th))
            quad.alpha_composite(tile.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.FLIP_TOP_BOTTOM), (tw, th))
            tile = quad
            # make it opaque (flats have no alpha)
            bg = Image.new("RGBA", tile.size, (20, 12, 10, 255)); bg.alpha_composite(tile)
            buf = io.BytesIO(); bg.convert("RGB").save(buf, "PNG")
            zf.writestr(f"flats/ISF{bid:02d}.png", buf.getvalue())
            made += 1
            made_ov += build_overlay_flats(zf, bid, bg.convert("RGB"))
        def seamless(img):
            """the art followed by its mirror image: the texture then tiles along the wall
            without a visible join where its right edge meets its left (a dark seam otherwise)"""
            out = Image.new("RGBA", (img.width * 2, img.height), (0, 0, 0, 255))
            out.alpha_composite(img, (0, 0))
            out.alpha_composite(img.transpose(Image.FLIP_LEFT_RIGHT), (img.width, 0))
            return out

        # wooden ceiling for the basement floors: the big planks Isaac uses for narrow rooms'
        # floors ("_nfloor" sheets), darkened, tiled by mirroring so there is no visible join
        if bid in WOOD_CEILING:
            planks = load_sheet(f"backdrop/{base}_nfloor.png")
            if planks is None:
                planks = load_sheet("backdrop/01_basement_nfloor.png")     # (the burning basement has no planks of its own)
            if planks is not None:
                pw, ph = planks.size
                band = planks.crop((0, int(ph * 0.54), int(pw * 0.77), ph))     # the two horizontal planks
                band = band.resize((int(band.width * FLOOR_SCALE), int(band.height * FLOOR_SCALE)), Image.NEAREST)
                r, g, b_, a = band.split()
                band = Image.merge("RGBA", (r.point(lambda v: int(v * CEIL_DARK)), g.point(lambda v: int(v * CEIL_DARK)), b_.point(lambda v: int(v * CEIL_DARK)), a))
                bw, bh = band.size
                quad = Image.new("RGBA", (bw * 2, bh * 2), (0, 0, 0, 255))
                quad.alpha_composite(band, (0, 0))
                quad.alpha_composite(band.transpose(Image.FLIP_LEFT_RIGHT), (bw, 0))
                quad.alpha_composite(band.transpose(Image.FLIP_TOP_BOTTOM), (0, bh))
                quad.alpha_composite(band.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.FLIP_TOP_BOTTOM), (bw, bh))
                buf = io.BytesIO(); quad.convert("RGB").save(buf, "PNG")
                zf.writestr(f"flats/ISC{bid:02d}.png", buf.getvalue())
        custom = load_sheet(WALL_OVERRIDES[bid]) if bid in WALL_OVERRIDES else None
        if custom is not None:
            bb = custom.getbbox()
            if bb:
                custom = custom.crop(bb)                  # no transparent border: it would print as a black line
            cw, ch = custom.size
            tex = custom.resize((max(1, int(cw * WALL_HEIGHT / ch)), WALL_HEIGHT), Image.NEAREST)
            bg = Image.new("RGBA", tex.size, (0, 0, 0, 255)); bg.alpha_composite(tex)
            buf = io.BytesIO(); seamless(bg).convert("RGB").save(buf, "PNG")
            zf.writestr(f"textures/ISW{bid:02d}.png", buf.getvalue())
        elif wall is not None:
            # one course of Isaac's wall, scaled (uniformly, so the stones keep their shape)
            # to the full wall height and tiled sideways only - no stacking. The strip is
            # cut at the darkest columns near each end (a mortar line where there is one)
            # so the mirrored join falls on a joint instead of through a stone.
            y0, y1 = WALL_ROWS
            x0, x1 = wall_cut(wall, WALL_X[0], WALL_X[0] + 40, y0, y1), wall_cut(wall, WALL_X[1] - 40, WALL_X[1], y0, y1)
            band = wall.crop((x0, y0, x1 + 1, y1))
            s = WALL_HEIGHT / band.height
            band = band.resize((max(1, round(band.width * s)), WALL_HEIGHT), Image.NEAREST)
            bg = Image.new("RGBA", band.size, (0, 0, 0, 255)); bg.alpha_composite(band)
            buf = io.BytesIO(); seamless(bg).convert("RGB").save(buf, "PNG")
            zf.writestr(f"textures/ISW{bid:02d}.png", buf.getvalue())
    buf = io.BytesIO(); Image.new("RGB", (64, 64), (0, 0, 0)).save(buf, "PNG")
    zf.writestr("flats/ISCEIL.png", buf.getvalue())
    if SHADE_LINES:
        zf.writestr("ISSHADE.txt", "\n".join(SHADE_LINES) + "\n")
    print(f"  backdrops: {made} floors, {made_ov} shadow-overlay floors")


def build_shadow(zf):
    """sprites/ISSHA0.png: Isaac's own drop shadow (gfx/shadow.png), made round for the floor plane
    (Isaac's is an oval only because of its top-down fake perspective), 64 px across, centre pivot"""
    if _lower_index is None:
        _build_lower_index()
    # the game's own drop shadow image: gfx/shadow.png in the unpacked resources (exact name
    # first, then anything called *shadow*.png that isn't part of a monster sheet)
    cands = [rel for rel in sorted(_lower_index) if rel.rsplit("/", 1)[-1] == "shadow.png"]
    if not cands:
        cands = [rel for rel in sorted(_lower_index)
                 if "shadow" in rel.rsplit("/", 1)[-1] and "monster" not in rel and "boss" not in rel]
    cand = cands[0] if cands else None
    if not cand:
        print("  shadow: no shadow.png under sprites\\ - copy Isaac's gfx\\shadow.png into Documents\\IsaacDoom\\sprites\\ and rerun; keeping the built-in disc")
        return
    img = load_sheet(cand)
    if img is None:
        return
    bbox = img.getbbox()
    if not bbox:
        return
    img = img.crop(bbox).resize((64, 64), Image.LANCZOS)
    # pure black with Isaac's alpha profile (its soft edge), whatever colour the sheet uses
    a = img.split()[3]
    out = Image.new("RGBA", (64, 64), (0, 0, 0, 255)); out.putalpha(a)
    zf.writestr("sprites/ISSHA0.png", png_with_grab(out, 32, 32))
    print(f"  shadow: from {cand}")


def rebuild_backdrops_only():
    """python tools\\build_sprites.py backdrops - redo just the floors, walls, ceilings and
    shadow overlays inside the existing pack (seconds, not the full sprite conversion)"""
    if not OUT_PK3.exists():
        print(f"{OUT_PK3} doesn't exist yet: run the full build first"); return
    tmp_pk3 = OUT_PK3.with_suffix(".pk3.tmp")
    t0 = time.time()
    with zipfile.ZipFile(OUT_PK3) as src, zipfile.ZipFile(tmp_pk3, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in src.infolist():
            nm = item.filename
            if nm.startswith(("flats/ISF", "flats/ISO", "flats/ISC", "textures/ISW", "ISSHADE")):
                continue
            zf.writestr(item, src.read(item))
        build_backdrops(zf)
    from paths import replace_when_free
    replace_when_free(tmp_pk3, OUT_PK3)
    print(f"pk3: {OUT_PK3} ({OUT_PK3.stat().st_size // 1024} KB)  time {time.time() - t0:.0f}s")


def main():
    if "backdrops" in ARGS or "overlays" in ARGS:
        rebuild_backdrops_only(); return
    t0 = time.time()
    anm2s = {}
    for root in (SPR_BASE, SPR_DLC):
        if not root.exists():
            continue
        for p in root.rglob("*.anm2"):
            if root is SPR_DLC and SPR_BASE in p.parents:
                continue
            if not wanted(p, root):
                continue
            anm2s[anm2_key(p, root)] = (p, root)     # DLC overrides base
    print(f"{len(anm2s)} .anm2 files to convert")

    namer = Namer()
    table = []          # lines for ISAACSPR
    # build to a temp file and swap it in at the end, so a Doom launched mid-build
    # still gets the previous complete pack instead of a half-written zip
    tmp_pk3 = OUT_PK3.with_suffix(".pk3.tmp")
    zf = zipfile.ZipFile(tmp_pk3, "w", zipfile.ZIP_DEFLATED)
    report = []
    n_frames = n_unique = n_anims = 0
    missing = set()
    per_cat = {}
    cut = []
    # expand per-id variants (item pedestals, trinkets)
    jobs = []   # (key, path, root, override, anim_filter)
    for key, (p, root) in anm2s.items():
        jobs.append((key, p, root, None, None))
        if key in VARIANT_SETS:
            folder, pat, anims = VARIANT_SETS[key]
            try:
                base = Anm2(p, key)
            except Exception:
                continue
            # the layer whose sheet lives in that folder is the one the game swaps
            swap_sid = None
            for sid, sp in base.sheets.items():
                if folder.split("/")[-1] in sp.replace("\\", "/").lower():
                    swap_sid = sid
            if swap_sid is None:
                continue
            if _lower_index is None:
                _build_lower_index()
            seen = set()
            for rel, path in sorted(_lower_index.items()):
                if not rel.startswith(folder + "/"):
                    continue
                m = re.search(pat, rel.rsplit("/", 1)[-1])
                if not m:
                    continue
                iid = int(m.group(1))
                if iid in seen:
                    continue
                seen.add(iid)
                jobs.append((f"{key}#{iid}", p, root, {swap_sid: rel}, set(anims)))
            # Curse of the Blind shows every item as the question mark: rendered as id 0
            if key == "005.100_collectible":
                for rel in sorted(_lower_index):
                    if rel.startswith(folder + "/") and rel.endswith("questionmark.png"):
                        jobs.append((f"{key}#0", p, root, {swap_sid: rel}, set(anims)))
                        seen.add(0)
                        break
            print(f"  {key}: {len(seen)} per-id variants")
            if len(seen) == 0:
                print(f"  WARNING: no files matched {pat} under {folder}/ - item pedestals will use the stand-in")
    # door skins: same anm2, another sheet
    for key, (p, root) in anm2s.items():
        if key not in DOOR_SKINS:
            continue
        if _lower_index is None:
            _build_lower_index()
        for skin in DOOR_SKINS[key]:
            rel = f"grid/{skin}.png"
            if rel not in _lower_index:
                continue
            jobs.append((f"{key}@{skin}", p, root, {0: rel}, None))
    ordered = sorted(jobs, key=lambda j: (PRIORITY.index(category(j[0].split("#")[0])) if category(j[0].split("#")[0]) in PRIORITY else 99, j[0]))
    for idx, (key, p, root, override, anim_filter) in enumerate(ordered):
        cat = category(key.split("#")[0])
        if n_unique >= FRAME_BUDGET:
            cut.append(key)
            continue
        try:
            a = Anm2(p, key.split("#")[0], override)
        except Exception as e:
            report.append(f"PARSE FAIL {key}: {e}")
            continue
        for sid, sp in a.sheets.items():
            if load_sheet(sp, a.dir) is None:
                missing.add(sp)
        for anim, ad in a.anims.items():
            if anim_filter is not None and anim not in anim_filter:
                continue
            n = max(1, ad["n"])
            step = max(1, -(-n // MAX_UNIQUE_PER_ANIM))   # ceil(n / cap)
            # grid art isn't animation: a pit's "pit" frames are its 33 neighbour shapes, a
            # door's its states - every frame is looked up by index, so none may be skipped
            if cat == "grid":
                step = 1
            frames = {}
            tokens = []
            cur_name = None
            cur_count = 0
            last_tok = "-"
            for i in range(n):
                if i % step != 0:
                    tokens.append(last_tok)
                    continue
                try:
                    r = a.render(anim, i)
                except Exception as e:
                    r = None
                if r is None:
                    tokens.append("-")
                    last_tok = "-"
                    continue
                img, xo, yo = r
                h = hashlib.md5(img.tobytes() + struct.pack("ii", xo, yo) + bytes(str(img.size), "ascii")).hexdigest()
                n_frames += 1
                if h in frames:
                    tokens.append(frames[h])
                    last_tok = frames[h]
                    continue
                if cur_name is None or cur_count >= MAX_FRAMES_PER_NAME:
                    cur_name = namer.next()
                    cur_count = 0
                letter = chr(ord("A") + cur_count)
                cur_count += 1
                tok = cur_name + letter
                frames[h] = tok
                tokens.append(tok)
                last_tok = tok
                n_unique += 1
                per_cat[cat] = per_cat.get(cat, 0) + 1
                zf.writestr(f"sprites/{tok}0.png", png_with_grab(img, xo, yo))
            if any(t != "-" for t in tokens):
                table.append(f"{key}|{anim_key(anim)}|{' '.join(tokens)}")
                n_anims += 1
        if idx % 50 == 0:
            print(f"  {idx}/{len(ordered)}  [{cat}] {key}  ({n_unique} unique frames so far)")
    build_backdrops(zf)
    build_shadow(zf)
    # default animation table so Doom can pick something when Isaac's anim is unknown
    zf.writestr("ISAACSPR.txt", "\n".join(table) + "\n")
    zf.close()
    from paths import replace_when_free
    replace_when_free(tmp_pk3, OUT_PK3)
    report.append(f"anm2: {len(anm2s)}  animations: {n_anims}  frames: {n_frames}  unique: {n_unique}  (budget {FRAME_BUDGET})")
    report.append("unique frames by category: " + ", ".join(f"{k} {v}" for k, v in sorted(per_cat.items(), key=lambda kv: -kv[1])))
    if cut:
        report.append(f"budget reached: {len(cut)} anm2 files skipped (lowest priority first): " + ", ".join(cut[:40]) + (" ..." if len(cut) > 40 else ""))
    report.append(f"pk3: {OUT_PK3} ({OUT_PK3.stat().st_size // 1024} KB)  time {time.time() - t0:.0f}s")
    if missing:
        report.append(f"missing sheets ({len(missing)}):")
        report.extend("  " + m for m in sorted(missing))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report[:3]))
    if missing:
        print(f"{len(missing)} sheets missing (see sprite_report.txt) - the base gfx tree probably isn't in sprites\\base")


if __name__ == "__main__":
    main()
