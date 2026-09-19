"""
IsaacDoom - build isaacui.pk3: Isaac's own HUD art and fonts for the Doom side.

Reads straight from the game's resources (or from Documents\IsaacDoom\sprites if
you copied the folders there) and writes doom\isaacui.pk3 with:
  graphics/IUH*.png     hearts (ui_hearts.anm2 animations)
  graphics/IUP*.png     coin / bomb / key icons (hudpickups.anm2)
  graphics/IUC*.png     active item charge bar (ui_chargebar.anm2)
  graphics/IUBOSSBG/FG  boss health bar (ui_bosshealthbar_static.png)
  graphics/IUSTREAK     item pickup paper streak
  fonts/ISUPHEAV/...    Upheaval (item names, big text)
  fonts/ISUPMINI/...    Upheaval mini
  fonts/ISTEMPST/...    PF Tempesta Seven Condensed (HUD numbers)
  fonts/ISMEAT10/, ISMEAT12/   Team Meat font (descriptions)

Usage:  python build_ui.py
"""
import io, os, struct, sys, zipfile
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except ImportError:
    print("Pillow is required:  pip install pillow"); sys.exit(1)

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT as PROJ, GAME
OUT_PK3 = PROJ / "doom" / "isaacui.pk3"

# where to look for gfx/ui files and fonts, first hit wins
UI_DIRS = [GAME / "resources-dlc3" / "gfx" / "ui", GAME / "resources" / "gfx" / "ui",
           PROJ / "sprites" / "ui", PROJ / "sprites" / "base" / "gfx" / "ui", PROJ / "sprites" / "gfx" / "ui"]
FONT_DIRS = [GAME / "resources" / "font", PROJ / "sprites" / "font", PROJ / "sprites" / "base" / "font",
             PROJ / "sprites"]

FONTS = {  # doom font folder name -> Isaac .fnt base name
    "ISUPHEAV": "upheaval",
    "ISUPMINI": "upheavalmini",
    "ISTEMPST": "pftempestasevencondensed",
    "ISMEAT10": "teammeatfont10",
    "ISMEAT12": "teammeatfont12",
}

HEARTS = {  # ui_hearts.anm2 animation -> lump
    "RedHeartFull": "IUHRF", "RedHeartHalf": "IUHRH", "EmptyHeart": "IUHRE",
    "BlueHeartFull": "IUHSF", "BlueHeartHalf": "IUHSH",
    "BlackHeartFull": "IUHKF", "BlackHeartHalf": "IUHKH",
    "WhiteHeartHalf": "IUHEH", "WhiteHeartOverlay": "IUHEO", "GoldHeartOverlay": "IUHGO",
    "CoinHeartFull": "IUHCF", "CoinHeartHalf": "IUHCH", "CoinEmpty": "IUHCE",
    "BoneHeartFull": "IUHBF", "BoneHeartHalf": "IUHBH", "BoneHeartEmpty": "IUHBE",
    "RottenHeartFull": "IUHTF", "RottenHeartHalf": "IUHTH",
    "RottenBoneHeartFull": "IUHUF", "RottenBoneHeartHalf": "IUHUH",
    "BrokenHeart": "IUHXX", "BrokenCoinHeart": "IUHXC", "HolyMantle": "IUHHM",
    "CurseHeart": "IUHCU",
}
CHARGEBAR = {"BarEmpty": "IUCBE", "BarFull": "IUCBF", "BarOverlay1": "IUCO1", "BarOverlay2": "IUCO2",
             "BarOverlay3": "IUCO3", "BarOverlay4": "IUCO4", "BarOverlay5": "IUCO5", "BarOverlay6": "IUCO6",
             "BarOverlay8": "IUCO8", "BarOverlay12": "IUCO12"}
# hudpickups.anm2 "Idle" frame index (first real frame = 1) -> lump
PICKUPS = {1: "IUPCOIN", 2: "IUPKEY", 3: "IUPBOMB", 4: "IUPGKEY", 8: "IUPGBOMB"}


def find(dirs, name):
    for d in dirs:
        p = d / name
        if p.exists():
            return p
        # case-insensitive fallback
        if d.exists():
            for q in d.iterdir():
                if q.name.lower() == name.lower():
                    return q
    return None


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def anm2_first_frames(anm2_path):
    """animation name -> (XCrop, YCrop, W, H) of the first visible frame of the first layer"""
    root = ET.parse(anm2_path).getroot()
    out = {}
    for a in root.iter("Animation"):
        for lan in a.iter("LayerAnimation"):
            for f in lan.iter("Frame"):
                if f.get("XCrop") is not None:
                    out[a.get("Name")] = tuple(int(float(f.get(k))) for k in ("XCrop", "YCrop", "Width", "Height"))
                    break
            if a.get("Name") in out:
                break
    return out


def build_graphics(zf):
    n = 0
    # hearts
    anm = find(UI_DIRS, "ui_hearts.anm2"); png = find(UI_DIRS, "ui_hearts.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        for name, (x, y, w, h) in anm2_first_frames(anm).items():
            lump = HEARTS.get(name)
            if lump:
                zf.writestr(f"graphics/{lump}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    else:
        print("ui_hearts not found")
    # charge bar
    anm = find(UI_DIRS, "ui_chargebar.anm2"); png = find(UI_DIRS, "ui_chargebar.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        for name, (x, y, w, h) in anm2_first_frames(anm).items():
            lump = CHARGEBAR.get(name)
            if lump:
                zf.writestr(f"graphics/{lump}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    # pickups
    anm = find(UI_DIRS, "hudpickups.anm2"); png = find(UI_DIRS, "hudpickups.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        root = ET.parse(anm).getroot()
        for a in root.iter("Animation"):
            if a.get("Name") != "Idle":
                continue
            frames = [f for f in a.iter("Frame")]
            for i, f in enumerate(frames):
                lump = PICKUPS.get(i)
                if lump and f.get("XCrop") is not None:
                    x, y, w, h = (int(float(f.get(k))) for k in ("XCrop", "YCrop", "Width", "Height"))
                    zf.writestr(f"graphics/{lump}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    # found-HUD stat icons (hudstats.anm2 "Icons": speed, tears, range, shot speed, damage, luck, devil, angel, planetarium)
    anm = find(UI_DIRS, "hudstats.anm2"); png = find(UI_DIRS, "hudstats.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        root = ET.parse(anm).getroot()
        for a in root.iter("Animation"):
            if a.get("Name") != "Icons":
                continue
            frames = [f for f in a.iter("Frame") if f.get("XCrop") is not None]
            for i, f in enumerate(frames[:9]):
                x, y, w, h = (int(float(f.get(k))) for k in ("XCrop", "YCrop", "Width", "Height"))
                zf.writestr(f"graphics/IUST{i}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    else:
        print("hudstats not found (stat HUD falls back to text labels)")
    # boss bar: fill (recoloured by Doom) and frame
    png = find(UI_DIRS, "ui_bosshealthbar_static.png") or find(UI_DIRS, "ui_bosshealthbar.png")
    if png:
        sheet = Image.open(png).convert("RGBA")
        zf.writestr("graphics/IUBOSSFG.png", png_bytes(sheet.crop((20, 11, 130, 19))))
        zf.writestr("graphics/IUBOSSBG.png", png_bytes(sheet.crop((3, 37, 134, 57)))); n += 2
    # streak paper
    png = find(UI_DIRS, "effect_024_streak.png") or find([d.parent / "effects" for d in UI_DIRS], "effect_024_streak.png")
    if png:
        img = Image.open(png).convert("RGBA")
        zf.writestr("graphics/IUSTREAK.png", png_bytes(img.crop(img.getbbox() or (0, 0, img.width, img.height)))); n += 1
    print(f"{n} HUD graphics")


# --------------------------------------------------------------------------
# boss "VS" screen: ground / overlay / VS text, per-stage spots, per-boss
# portrait + name (bossportraits.xml ids), per-character portrait + name
# --------------------------------------------------------------------------
BOSS_DIRS = [GAME / "resources-dlc3" / "gfx" / "ui" / "boss", GAME / "resources" / "gfx" / "ui" / "boss",
             PROJ / "sprites" / "ui" / "boss", PROJ / "sprites" / "base" / "gfx" / "ui" / "boss"]
STAGE_DIRS = [GAME / "resources-dlc3" / "gfx" / "ui" / "stage", GAME / "resources" / "gfx" / "ui" / "stage",
              PROJ / "sprites" / "ui" / "stage", PROJ / "sprites" / "base" / "gfx" / "ui" / "stage"]
XML_DIRS = [GAME / "resources-dlc3", GAME / "resources", PROJ / "sprites"]


def put_png(zf, lump, path):
    if path is None:
        return False
    zf.writestr(f"graphics/{lump}.png", png_bytes(Image.open(path).convert("RGBA")))
    return True


def build_versus(zf):
    import re
    n = 0
    data = []
    n += put_png(zf, "IUVSVS", find(BOSS_DIRS, "vs.png"))
    # the vignette overlay is a multiply layer in the game: bake it into the ground here
    g, o = find(BOSS_DIRS, "ground.png"), find(BOSS_DIRS, "overlay.png")
    if g:
        from PIL import ImageChops
        ground = Image.open(g).convert("RGBA")
        if o:
            ov = Image.open(o).convert("RGBA").resize(ground.size)
            mixed = ImageChops.multiply(ground.convert("RGB"), ov.convert("RGB")).convert("RGBA")
            mixed.putalpha(ground.getchannel("A"))
            ground = mixed
        # the game shows this over black: pull the parchment right down
        ground = Image.eval(ground, lambda v: v).convert("RGBA")
        r, g, b, a = ground.split()
        ground = Image.merge("RGBA", (r.point(lambda v: int(v * 0.22)), g.point(lambda v: int(v * 0.22)), b.point(lambda v: int(v * 0.22)), a))
        zf.writestr("graphics/IUVSBG.png", png_bytes(ground)); n += 1
    # stage spots: bossspot_01_basement.png -> IUVB01, bossspot_01x_downpour.png -> IUVB01X
    seen = set()
    for d in BOSS_DIRS:
        if not d.exists():
            continue
        for f in d.iterdir():
            m = re.match(r"(bossspot|playerspot)_(\d\d)(x?)_", f.name.lower())
            if not m:
                continue
            lump = ("IUVB" if m.group(1) == "bossspot" else "IUVP") + m.group(2) + ("X" if m.group(3) else "")
            if lump in seen:
                continue
            seen.add(lump)
            n += put_png(zf, lump, f)
    # bosses
    bp = find(XML_DIRS, "bossportraits.xml")
    if bp:
        root = ET.parse(bp).getroot()
        for b in root.iter("boss"):
            bid = int(b.get("id"))
            okp = put_png(zf, f"IUBP{bid:03d}", find(BOSS_DIRS, b.get("portrait", "")))
            okn = put_png(zf, f"IUBN{bid:03d}", find(BOSS_DIRS, b.get("nameimage", "")))
            n += okp + okn
            data.append(f"b {bid} {b.get('pivotX', 96)} {b.get('pivotY', 132)} {b.get('name', '')}")
    # players
    px = find(XML_DIRS, "players.xml")
    if px:
        root = ET.parse(px).getroot()
        for pl in root.iter("player"):
            pid = int(pl.get("id"))
            n += put_png(zf, f"IUPP{pid:02d}", find(STAGE_DIRS, pl.get("portrait", "")))
            n += put_png(zf, f"IUPN{pid:02d}", find(BOSS_DIRS, pl.get("nameimage", "")))
    print(f"{n} versus-screen graphics")
    return data


# --------------------------------------------------------------------------
# minimap art (1:1 with the game): room shapes x visited/unvisited/current,
# frame, and the room-type icons
# --------------------------------------------------------------------------
MAP_ICONS = {"IconShop": "SH", "IconSecretRoom": "SE", "IconSuperSecretRoom": "SS", "IconLibrary": "LI",
             "IconTreasureRoom": "TR", "IconAngelRoom": "AN", "IconDevilRoom": "DE", "IconDiceRoom": "DI",
             "IconMiniboss": "MB", "IconBoss": "BO", "IconAmbushRoom": "AM", "IconBossAmbushRoom": "BA",
             "IconCurseRoom": "CU", "IconSacrificeRoom": "SA", "IconArcade": "AR", "IconChestRoom": "CH",
             "IconIsaacsRoom": "IS", "IconBarrenRoom": "BR", "IconPlanetarium": "PL", "IconUltraSecretRoom": "US",
             "IconTeleporterRoom": "TE", "IconLockedRoom": "LO"}


def anm2_frames(anm2_path, anim_name):
    root = ET.parse(anm2_path).getroot()
    for a in root.iter("Animation"):
        if a.get("Name") != anim_name:
            continue
        for lan in a.iter("LayerAnimation"):
            fr = [f for f in lan.iter("Frame") if f.get("XCrop") is not None]
            if fr:
                return [tuple(int(float(f.get(k))) for k in ("XCrop", "YCrop", "Width", "Height")) for f in fr]
    return []


def build_minimap(zf):
    n = 0
    for idx in (1, 2):
        anm = find(UI_DIRS, f"minimap{idx}.anm2"); png = find(UI_DIRS, f"minimap{idx}.png")
        if not anm or not png:
            print(f"minimap{idx} not found"); continue
        sheet = Image.open(png).convert("RGBA")
        for anim, code in (("RoomVisited", "V"), ("RoomUnvisited", "U"), ("RoomCurrent", "C")):
            for shape, (x, y, w, h) in enumerate(anm2_frames(anm, anim), start=1):
                zf.writestr(f"graphics/IM{idx}{code}{shape:02d}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
        for anim, code in (("Frame", "FR"), ("RoomOutline", "OL")):
            fr = anm2_frames(anm, anim)
            if fr:
                x, y, w, h = fr[0]
                zf.writestr(f"graphics/IM{idx}{code}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    anm = find(UI_DIRS, "minimap_icons.anm2"); png = find(UI_DIRS, "minimap_icons.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        for name, code in MAP_ICONS.items():
            fr = anm2_frames(anm, name)
            if fr:
                x, y, w, h = fr[0]
                zf.writestr(f"graphics/IMI{code}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    print(f"{n} minimap graphics")


# --------------------------------------------------------------------------
# stage transition: nightmare (dream) animations + the progress card
# --------------------------------------------------------------------------
NIGHTMARE_STEP = 2      # keep every 2nd frame (15 fps)


def build_transition(zf, data):
    n = 0
    # progress card pieces (all 32x32, pivot 16,16)
    anm = find(STAGE_DIRS, "progress.anm2"); png = find(STAGE_DIRS, "progress.png")
    if anm and png:
        sheet = Image.open(png).convert("RGBA")
        for i, (x, y, w, h) in enumerate(anm2_frames(anm, "Levels")):
            zf.writestr(f"graphics/IUPL{i:02d}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
        for i, (x, y, w, h) in enumerate(anm2_frames(anm, "IsaacIndicator")):
            zf.writestr(f"graphics/IUPI{i:02d}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
        for anim, lump in (("BossIndicator", "IUPBI"), ("NotClearFloor", "IUPNC"), ("ClearFloor", "IUPCF"), ("Connector", "IUPCN")):
            fr = anm2_frames(anm, anim)
            if fr:
                x, y, w, h = fr[0]
                zf.writestr(f"graphics/{lump}.png", png_bytes(sheet.crop((x, y, x + w, y + h)))); n += 1
    # nightmares, rendered with the sprite converter's anm2 engine
    try:
        import build_sprites as bs
    except ImportError:
        print("build_sprites.py not found next to build_ui.py: no nightmares"); return
    roots = [d.parent.parent for d in STAGE_DIRS if d.exists()]     # .../gfx
    if not roots:
        return
    bs.SPR_BASE, bs.SPR_DLC = (roots[1], roots[0]) if len(roots) > 1 else (roots[0] / "none", roots[0])
    bs._lower_index = None
    bs._build_lower_index()
    # the dream backdrop without the player (drawn live so it matches the character)
    bgp = find(STAGE_DIRS, "nightmare_bg.anm2")
    if bgp:
        try:
            a = bs.Anm2(bgp, "ui/stage/nightmare_bg")
            a.layers = [(lid, sid) for lid, sid in a.layers if lid not in (2, 6)]   # Player, PlayerAlt
            r = a.render("Intro", 59)
            if r:
                img, xo, yo = r
                canvas = Image.new("RGBA", (480, 270), (0, 0, 0, 255))
                canvas.alpha_composite(img.crop((xo - 240, yo - 135, xo + 240, yo + 135)), (0, 0))
                zf.writestr("graphics/IUNMBG.png", png_bytes(canvas)); n += 1
        except Exception as e:
            print("nightmare_bg:", e)
    count = 0
    for k in range(1, 30):
        p = find(STAGE_DIRS, f"nightmare{k}.anm2")
        if not p:
            continue
        try:
            a = bs.Anm2(p, f"ui/stage/nightmare{k}")
        except Exception as e:
            print(f"nightmare{k}: {e}"); continue
        total = a.anims.get("Scene", {}).get("n", 0)
        frames = []
        for i in range(0, total, NIGHTMARE_STEP):
            r = a.render("Scene", i)
            frames.append(r)
        # common box around every frame, relative to the screen centre
        box = None
        for r in frames:
            if not r:
                continue
            img, xo, yo = r
            bb = img.getbbox()
            if not bb:
                continue
            b2 = (bb[0] - xo, bb[1] - yo, bb[2] - xo, bb[3] - yo)
            box = b2 if box is None else (min(box[0], b2[0]), min(box[1], b2[1]), max(box[2], b2[2]), max(box[3], b2[3]))
        if not box:
            continue
        bw, bh = box[2] - box[0], box[3] - box[1]
        for fi, r in enumerate(frames):
            cell = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            if r:
                img, xo, yo = r
                cell.alpha_composite(img, (-(box[0] + xo), -(box[1] + yo)))
            zf.writestr(f"graphics/IN{count:02d}{fi:03d}.png", png_bytes(cell)); n += 1
        data.append(f"n {count} {len(frames)} {box[0]} {box[1]} {NIGHTMARE_STEP}")
        count += 1
    print(f"{n} transition graphics ({count} nightmares)")


# --------------------------------------------------------------------------
# starting-room floor tutorial (MOVE / ATTACK / BOMB / ITEM) with Doom's keys
# --------------------------------------------------------------------------
BACKDROP_DIRS = [GAME / "resources" / "gfx" / "backdrop", GAME / "resources-dlc3" / "gfx" / "backdrop",
                 PROJ / "sprites" / "backdrop", PROJ / "sprites" / "base" / "gfx" / "backdrop"]


def draw_bmfont_text(img, fnt_base, text, x, y, color, scale=1):
    fnt = find(FONT_DIRS, fnt_base + ".fnt")
    if not fnt:
        return
    info = parse_bmfont(fnt)
    page = find([fnt.parent], info.get("pages", [fnt_base + "_0.png"])[0]) or find(FONT_DIRS, fnt_base + "_0.png")
    if not page:
        return
    sheet = Image.open(page).convert("RGBA")
    width = sum(info["chars"].get(ord(c), (0, 0, 0, 0, 0, 0, 4, 0))[6] for c in text) * scale
    pen = x - width // 2
    for c in text:
        ch = info["chars"].get(ord(c))
        if not ch:
            pen += 4 * scale; continue
        gx, gy, w, h, xo, yo, xa, pg = ch
        if w > 0 and h > 0:
            g = sheet.crop((gx, gy, gx + w, gy + h))
            tint = Image.new("RGBA", g.size, color)
            tint.putalpha(g.getchannel("A"))
            if scale != 1:
                tint = tint.resize((w * scale, h * scale), Image.NEAREST)
            img.alpha_composite(tint, (int(pen + xo * scale), int(y + yo * scale)))
        pen += xa * scale


def build_controls(zf):
    png = find(BACKDROP_DIRS, "controls.png")
    if not png:
        print("controls.png not found"); return
    sheet = Image.open(png).convert("RGBA")
    art = sheet.crop((0, 0, 325, 85))
    out = Image.new("RGBA", (325, 104), (0, 0, 0, 0))
    out.alpha_composite(art, (0, 0))
    ink = (70, 45, 40, 255)
    for label, cx in (("WASD", 43), ("MOUSE", 124), ("E", 208), ("SPACE", 290)):
        draw_bmfont_text(out, "upheaval", label, cx, 84, ink, 1)
    # centre the sprite origin so the flat decal sits on the room's middle
    zf.writestr("graphics/IUCTRL.png", png_with_offsets(out, out.width // 2, out.height // 2))
    print("controls decal built")


def png_with_offsets(img, xoff, yoff):
    """PNG bytes with a grAb chunk (Doom sprite offsets)"""
    import zlib
    data = png_bytes(img)
    chunk = b"grAb" + struct.pack(">ii", xoff, yoff)
    crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    return data[:33] + struct.pack(">I", 8) + chunk + crc + data[33:]


def parse_bmfont(path):
    d = path.read_bytes()
    if d[:3] != b"BMF":
        raise ValueError("not a binary BMFont: " + str(path))
    p = 4
    info = {"chars": {}, "kern": {}}
    while p < len(d):
        t = d[p]; sz = struct.unpack("<I", d[p + 1:p + 5])[0]; body = d[p + 5:p + 5 + sz]; p += 5 + sz
        if t == 2:
            lh, base = struct.unpack("<HH", body[:4]); info["lh"] = lh; info["base"] = base
        elif t == 3:
            info["pages"] = [s.decode("latin-1") for s in body.split(b"\0") if s]
        elif t == 4:
            for i in range(sz // 20):
                cid, x, y, w, h, xo, yo, xa, pg, ch = struct.unpack("<IHHHHhhhBB", body[i * 20:i * 20 + 20])
                info["chars"][cid] = (x, y, w, h, xo, yo, xa, pg)
    return info


def build_font(zf, doom_name, base):
    fnt = find(FONT_DIRS, base + ".fnt")
    if not fnt:
        print(f"font {base}.fnt not found"); return 0
    info = parse_bmfont(fnt)
    pages = []
    for pn in info.get("pages", [base + "_0.png"]):
        pp = find([fnt.parent], pn) or find(FONT_DIRS, pn)
        pages.append(Image.open(pp).convert("RGBA") if pp else None)
    lh = info["lh"]
    n = 0
    slack = []      # advance - cell width, per glyph: Doom spaces glyphs by cell width, so
                    # fonts whose glyph art overlaps its neighbours need negative kerning
    for cid, (x, y, w, h, xo, yo, xa, pg) in info["chars"].items():
        if cid < 32 or cid > 0x24F:       # basic latin + latin extended is plenty
            continue
        if pg >= len(pages) or pages[pg] is None:
            continue
        glyph = pages[pg].crop((x, y, x + w, y + h)) if w > 0 and h > 0 else None
        bb = glyph.getbbox() if glyph is not None else None
        if bb:
            ink = glyph.crop(bb)
            left = xo + bb[0]                       # ink start relative to the pen
            cellw = max(1, xa, left + ink.width)
            cell = Image.new("RGBA", (cellw, lh), (0, 0, 0, 0))
            cell.alpha_composite(ink, (max(0, left), max(0, min(lh - 1, yo + bb[1]))))
            if cid != 32:
                slack.append(xa - cellw)
        else:
            cell = Image.new("RGBA", (max(1, xa), lh), (0, 0, 0, 0))
        zf.writestr(f"fonts/{doom_name}/{cid:04X}.png", png_bytes(cell)); n += 1
    # a space glyph with the font's own advance
    if 32 not in info["chars"]:
        zf.writestr(f"fonts/{doom_name}/0020.png", png_bytes(Image.new("RGBA", (max(2, lh // 3), lh), (0, 0, 0, 0))))
    slack.sort()
    kern = slack[len(slack) // 2] if slack else 0
    zf.writestr(f"fonts/{doom_name}/font.inf", f"Kerning {min(0, kern)}\nFontHeight {lh}\n")
    print(f"font {doom_name} <- {base}: {n} glyphs, line height {lh}")
    return n


# ---------------------------------------------------------------------------
# Laser beams: the body frames of Isaac's own laser sprites, tint baked in, as
# Doom sprites ILnn A.. (nn = LaserVariant) lying flat along the beam.
# ISLASER.txt: "variant sprite frames width height" (px, after the anm2 scale)
# ---------------------------------------------------------------------------
LASER_ANM2 = {
    1: "007.001_thick red laser.anm2", 2: "007.002_thin red laser.anm2", 3: "007.003_shoop laser.anm2",
    4: "007.004_pride laser.anm2", 6: "007.006_giant red laser.anm2", 7: "007.007_tractorbeam laser.anm2",
    9: "007.009_brimtech.anm2", 10: "007.010_electric laser.anm2",
}
GFX_DIRS = [GAME / "resources-dlc3" / "gfx", GAME / "resources" / "gfx", PROJ / "sprites" / "gfx"]


def find_gfx(rel):
    rel = rel.replace("\\", "/")
    for d in GFX_DIRS:
        p = d / rel
        if p.exists():
            return p
        # case-insensitive walk
        parts = rel.split("/")
        cur = d
        ok = True
        for part in parts:
            if not cur.exists():
                ok = False
                break
            hit = None
            for q in cur.iterdir():
                if q.name.lower() == part.lower():
                    hit = q
                    break
            if hit is None:
                ok = False
                break
            cur = hit
        if ok and cur.exists():
            return cur
    return None


def build_lasers(zf):
    lines = []
    for var, anm in LASER_ANM2.items():
        path = find_gfx(anm)
        if path is None:
            continue
        root = ET.parse(path).getroot()
        sheets = {sp.get("Id"): sp.get("Path") for sp in root.iter("Spritesheet")}
        layers = {l.get("Id"): (l.get("Name", ""), l.get("SpritesheetId")) for l in root.iter("Layer")}
        anims = root.find("Animations")
        default = anims.get("DefaultAnimation") if anims is not None else None
        anim = None
        for a in root.iter("Animation"):
            if anim is None or a.get("Name") == default:
                anim = a
        if anim is None:
            continue
        # the body layer: the one that isn't a tip / impact / start cap
        body = None
        for lan in anim.iter("LayerAnimation"):
            nm = layers.get(lan.get("LayerId"), ("", None))[0].lower()
            if "tip" in nm or "impact" in nm or "start" in nm or "cap" in nm or "end" in nm:
                continue
            body = lan
            if "laser" in nm or "body" in nm or "beam" in nm:
                break
        if body is None:
            continue
        sheet_rel = sheets.get(layers.get(body.get("LayerId"), ("", None))[1], "")
        sheet_path = find_gfx(sheet_rel)
        if sheet_path is None:
            print(f"  laser {var}: sheet {sheet_rel} not found")
            continue
        sheet = Image.open(sheet_path).convert("RGBA")
        frames = []
        for f in body.iter("Frame"):
            if f.get("XCrop") is None or f.get("Visible", "true") != "true":
                continue
            x, y, w, h = (int(float(f.get(k))) for k in ("XCrop", "YCrop", "Width", "Height"))
            xs, ys = float(f.get("XScale", 100)) / 100.0, float(f.get("YScale", 100)) / 100.0
            tint = tuple(int(f.get(k, 255)) for k in ("RedTint", "GreenTint", "BlueTint", "AlphaTint"))
            off = tuple(int(f.get(k, 0)) for k in ("RedOffset", "GreenOffset", "BlueOffset"))
            img = sheet.crop((x, y, x + w, y + h))
            r, g, b, a = img.split()
            r = r.point(lambda v, t=tint[0], o=off[0]: max(0, min(255, v * t // 255 + o)))
            g = g.point(lambda v, t=tint[1], o=off[1]: max(0, min(255, v * t // 255 + o)))
            b = b.point(lambda v, t=tint[2], o=off[2]: max(0, min(255, v * t // 255 + o)))
            a = a.point(lambda v, t=tint[3]: v * t // 255)
            img = Image.merge("RGBA", (r, g, b, a))
            if xs != 1.0 or ys != 1.0:
                img = img.resize((max(1, int(w * xs)), max(1, int(h * ys))), Image.NEAREST)
            frames.append(img)
            if len(frames) >= 26:
                break
        if not frames:
            continue
        name = f"IL{var:02d}"
        for i, img in enumerate(frames):
            zf.writestr(f"sprites/{name}{chr(65 + i)}0.png", png_with_offsets(img, img.width // 2, img.height // 2))
        lines.append(f"{var} {name} {len(frames)} {frames[0].width} {frames[0].height}")
        print(f"  laser {var}: {len(frames)} frames {frames[0].width}x{frames[0].height} from {sheet_rel}")
    zf.writestr("ISLASER.txt", "\n".join(lines) + "\n")
    print(f"lasers: {len(lines)} variants")


def main():
    OUT_PK3.parent.mkdir(parents=True, exist_ok=True)
    tmp_pk3 = OUT_PK3.with_suffix(".pk3.tmp")
    zf = zipfile.ZipFile(tmp_pk3, "w", zipfile.ZIP_DEFLATED)
    build_graphics(zf)
    data = build_versus(zf)
    build_minimap(zf)
    build_transition(zf, data)
    build_controls(zf)
    build_lasers(zf)
    zf.writestr("IUVSDATA.txt", "\n".join(data) + "\n")
    for doom_name, base in FONTS.items():
        build_font(zf, doom_name, base)
    zf.close()
    from paths import replace_when_free
    replace_when_free(tmp_pk3, OUT_PK3)
    print(f"wrote {OUT_PK3} ({OUT_PK3.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
