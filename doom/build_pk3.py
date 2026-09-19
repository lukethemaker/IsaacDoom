"""
Builds isaacdoom.pk3:
  - generates the arena map (UDMF) as a 28x16 grid of 64x64 sectors, one per
    Isaac grid cell, so walls/rocks/pits can be made at runtime by raising and
    lowering sector floors from ZScript
  - packs it with the ZScript / MAPINFO / CVARINFO sources in ./src

Usage:  python build_pk3.py            -> writes ./isaacdoom.pk3
"""
import os, struct, zipfile, io

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
OUT = os.path.join(HERE, "isaacdoom.pk3")

M = 3                  # margin cells around the room (door passages live here)
W, H = 28 + 2 * M, 16 + 2 * M   # max Isaac grid (2x2 room incl. wall ring) plus margin
CELL = 64
CEIL = 128
WALL_TEX = "BROWN1"
FLOOR_FLAT = "FLOOR0_3"
CEIL_FLAT = "CEIL5_1"


def build_textmap():
    verts, lines, sides, sectors, things = [], [], [], [], []

    def vid(c, r):
        return r * (W + 1) + c

    for r in range(H + 1):
        for c in range(W + 1):
            verts.append((c * CELL, -r * CELL))

    for r in range(H):
        for c in range(W):
            sectors.append(dict(id=r * W + c))

    def sec(c, r):
        if 0 <= c < W and 0 <= r < H:
            return r * W + c
        return None

    def add_line(v1, v2, front, back):
        # front sector must be on the right-hand side of v1->v2
        if front is None:
            v1, v2 = v2, v1
            front, back = back, front
        sf = len(sides)
        two = back is not None
        sides.append((front, two))
        sb = None
        if two:
            sb = len(sides)
            sides.append((back, two))
        lines.append((v1, v2, sf, sb))

    # horizontal edges: between cell (c, r-1) above and (c, r) below.
    # direction +x -> right-hand side is -y (below) -> front = below cell
    for r in range(H + 1):
        for c in range(W):
            add_line(vid(c, r), vid(c + 1, r), sec(c, r), sec(c, r - 1))
    # vertical edges: between (c-1, r) left and (c, r) right.
    # direction -y (downwards) -> right-hand side is -x (left) -> front = left cell
    for r in range(H):
        for c in range(W + 1):
            add_line(vid(c, r), vid(c, r + 1), sec(c - 1, r), sec(c, r))

    # player start in the middle of a 1x1 room (cells 1..13 x 1..7)
    things.append(((7 + M) * CELL + 32, -((4 + M) * CELL + 32), 1, 90))

    o = io.StringIO()
    o.write('namespace = "zdoom";\n')
    for x, y in verts:
        o.write(f"vertex {{ x = {x:.1f}; y = {y:.1f}; }}\n")
    for v1, v2, sf, sb in lines:
        o.write(f"linedef {{ v1 = {v1}; v2 = {v2}; sidefront = {sf}; ")
        if sb is not None:
            o.write(f"sideback = {sb}; twosided = true; ")
        else:
            o.write("blocking = true; ")
        o.write("}\n")
    for s, two in sides:
        mid = "-" if two else WALL_TEX   # never a mid texture on two-sided lines (draws a see-through panel)
        o.write(f'sidedef {{ sector = {s}; texturetop = "{WALL_TEX}"; texturebottom = "{WALL_TEX}"; texturemiddle = "{mid}"; }}\n')
    for s in sectors:
        o.write(f'sector {{ id = {s["id"] + 1}; heightfloor = 0; heightceiling = {CEIL}; texturefloor = "{FLOOR_FLAT}"; textureceiling = "{CEIL_FLAT}"; lightlevel = 176; }}\n')
    for x, y, t, a in things:
        o.write(f"thing {{ x = {x:.1f}; y = {y:.1f}; type = {t}; angle = {a}; skill1 = true; skill2 = true; skill3 = true; skill4 = true; skill5 = true; single = true; coop = true; dm = true; }}\n")
    return o.getvalue().encode("ascii")


def build_wad(lumps):
    """lumps: list of (name, bytes)"""
    body = io.BytesIO()
    directory = []
    offset = 12
    for name, data in lumps:
        directory.append((offset, len(data), name))
        body.write(data)
        offset += len(data)
    out = io.BytesIO()
    out.write(b"PWAD")
    out.write(struct.pack("<ii", len(lumps), offset))
    out.write(body.getvalue())
    for off, size, name in directory:
        out.write(struct.pack("<ii8s", off, size, name.encode("ascii").ljust(8, b"\0")))
    return out.getvalue()


# ---------------------------------------------------------------------------
# Rocks: a low-poly model (the top half of a d10, base ring zig-zagging so it
# reads as a lump rather than a cone) skinned with each stage's rock art.
# ---------------------------------------------------------------------------
ROCK_R, ROCK_H, ROCK_ZIG, ROCK_SKIRT = 37.0, 72.0, 7.0, 10.0   # taller than eye level: you can't shoot over a rock
ROCK_BRIGHT = 1.5        # rock skins brightened by this much (the sheet art is dark for Doom's lighting)
# BackdropType id -> rocks sheet (doom/rocks/rocks_<name>.png); missing ones fall back to basement
ROCK_SHEETS = {
    1: "basement", 2: "cellar", 3: "burningbasement", 4: "caves", 5: "catacombs", 6: "drownedcaves",
    7: "depths", 8: "depths", 9: "depths", 10: "womb", 11: "womb", 12: "scarredwomb", 13: "bluewomb",
    14: "sheol", 15: "cathedral", 16: "sheol", 17: "cathedral", 19: "basement", 20: "basement",
    21: "basement", 22: "basement", 23: "secretroom", 24: "basement", 25: "basement", 28: "basement",
    29: "caves", 30: "basement", 31: "basement", 32: "caves", 33: "depths", 34: "womb", 40: "depths",
    41: "depths", 43: "womb", 44: "womb", 45: "basement", 46: "caves", 47: "depths", 48: "basement",
    49: "basement", 50: "basement",
}


def rock_obj():
    """a squat low-poly boulder: stacked 10-sided rings (skirt, base, bulge, shoulder) and a
    slightly tilted flat top, every vertex nudged so no two faces line up"""
    import math
    import numpy as np
    V, VT, F = [], [], []
    def vert(x, y, z): V.append((x, y, z)); return len(V)
    def uv(u, v): VT.append((u, v)); return len(VT)
    N = 10
    # (height, radius, wobble) from the ground up
    RINGS = [(-ROCK_SKIRT, 40.0, 0.03), (2.0, 37.0, 0.08), (ROCK_H * 0.42, 41.0, 0.10), (ROCK_H * 0.72, 36.0, 0.12), (ROCK_H * 0.9, 24.0, 0.14)]
    TOP_Y = ROCK_H
    vmin, vmax = -ROCK_SKIRT, TOP_Y + 2
    def vv(y): return (y - vmin) / (vmax - vmin)
    rings = []
    for ri, (y0, r0, wob) in enumerate(RINGS):
        ring = []
        for k in range(N):
            a = math.radians(k * 360.0 / N + ri * 9)              # each ring twisted a little
            r = r0 * (1.0 + wob * math.sin(k * 2.3 + ri * 1.7))
            y = y0 + (0.0 if ri == 0 else 2.5 * math.sin(k * 1.9 + ri))
            ring.append((vert(r * math.cos(a), y, r * math.sin(a)), y, k / float(N)))
        rings.append(ring)
    cap = vert(3.0, TOP_Y, -2.0)                                  # top a touch off-centre
    def tri(i, j, k, uvi, uvj, uvk):
        pts = np.array([V[i - 1], V[j - 1], V[k - 1]], dtype=float)
        n = np.cross(pts[1] - pts[0], pts[2] - pts[0])
        c = pts.mean(axis=0); c[1] -= 20.0                        # normal should point away from the core
        if np.dot(n, c) < 0:
            j, k, uvj, uvk = k, j, uvk, uvj
        F.append(f"f {i}/{uvi} {j}/{uvj} {k}/{uvk}")
    for ri in range(len(rings) - 1):
        lo, hi = rings[ri], rings[ri + 1]
        for k in range(N):
            k2 = (k + 1) % N
            (a, ay, au), (b, by, bu) = lo[k], lo[k2]
            (c, cy, cu), (d, dy, du) = hi[k], hi[k2]
            bu2 = bu if k2 else 1.0
            du2 = du if k2 else 1.0
            tri(a, b, d, uv(au, vv(ay)), uv(bu2, vv(by)), uv(du2, vv(dy)))
            tri(a, d, c, uv(au, vv(ay)), uv(du2, vv(dy)), uv(cu, vv(cy)))
    top = rings[-1]
    for k in range(N):
        k2 = (k + 1) % N
        (a, ay, au), (b, by, bu) = top[k], top[k2]
        tri(cap, a, b, uv((au + (bu if k2 else 1.0)) / 2, 1.0), uv(au, vv(ay)), uv(bu if k2 else 1.0, vv(by)))
    # the material name is looked up as a texture next to the model (MODELDEF's Skin then
    # replaces it per stage); naming the real file avoids a "material not found" script error
    out = ["# IsaacDoom rock: low-poly boulder, Y up", "o rock"]
    out += [f"v {x:.3f} {y:.3f} {z:.3f}" for x, y, z in V]
    out += [f"vt {u:.4f} {v:.4f}" for u, v in VT]
    out += ["usemtl isrs01.png"] + F
    return "\n".join(out) + "\n"


def rock_skin(sheet_path):
    """128x64 band: the middle of the stage's wide (2x1) rock, mirrored so it wraps around the model"""
    from PIL import Image
    sheet = Image.open(sheet_path).convert("RGBA")
    wide = sheet.crop((0, 224, 64, 256))
    mid = wide.crop((18, 3, 46, 29))                       # the centre lump, no edge highlight / shadow
    px = mid.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
    fill = tuple(int(v * 0.6) for v in px) + (255,)
    tile = mid.resize((64, 64), Image.NEAREST)
    band = Image.new("RGBA", (128, 64), fill)
    band.alpha_composite(tile, (0, 0))
    band.alpha_composite(tile.transpose(Image.FLIP_LEFT_RIGHT), (64, 0))
    # lift the art a little (the sheet's rock is drawn in Isaac's dim room light and comes out
    # too dark under Doom's), and darken toward the base so the rock sits into the floor
    px_ = band.load()
    for y in range(64):
        f = ROCK_BRIGHT * (1.0 - 0.25 * max(0, (y - 40) / 24.0))
        for x in range(128):
            r, g, b, a = px_[x, y]
            px_[x, y] = (min(255, int(r * f)), min(255, int(g * f)), min(255, int(b * f)), 255)
    buf = io.BytesIO(); band.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def tinted_obj():
    """the tinted rock: Isaac draws it as a squared, carved block with bevelled sides, so its
    model is a square frustum (skirt, base, bevel, flat top), each side showing the whole
    tinted-rock face and the top its inner square. Y up, same footprint as the boulder."""
    V, VT, F = [], [], []
    def vert(x, y, z): V.append((x, y, z)); return len(V)
    def uv(u, v): VT.append((u, v)); return len(VT)
    # (height, half-width) from the ground up; the top ring is where the bevel meets the flat top
    RINGS = [(-ROCK_SKIRT, 40.0), (2.0, 38.0), (ROCK_H * 0.78, 37.0), (ROCK_H, 27.0)]
    vmin, vmax = -ROCK_SKIRT, ROCK_H
    def vv(y): return (y - vmin) / (vmax - vmin)
    corners = [(1, 1), (-1, 1), (-1, -1), (1, -1)]
    rings = [[vert(sx * hw, y, sz * hw) for sx, sz in corners] for y, hw in RINGS]
    import numpy as np
    def tri(i, j, k, uvi, uvj, uvk):
        pts = np.array([V[i - 1], V[j - 1], V[k - 1]], dtype=float)
        n = np.cross(pts[1] - pts[0], pts[2] - pts[0])
        c = pts.mean(axis=0); c[1] -= 20.0                        # normal should point away from the core
        if np.dot(n, c) < 0:
            j, k, uvj, uvk = k, j, uvk, uvj
        F.append(f"f {i}/{uvi} {j}/{uvj} {k}/{uvk}")
    def quad(a, b, c, d, ua, ub, uc, ud):
        tri(a, b, c, ua, ub, uc)
        tri(a, c, d, ua, uc, ud)
    for ri in range(len(RINGS) - 1):
        lo, hi = rings[ri], rings[ri + 1]
        v0, v1 = vv(RINGS[ri][0]), vv(RINGS[ri + 1][0])
        for k in range(4):
            k2 = (k + 1) % 4
            # the side between corner k and k2: the face image from bottom (v0) to top (v1)
            quad(lo[k], hi[k], hi[k2], lo[k2], uv(0.0, v0), uv(0.0, v1), uv(1.0, v1), uv(1.0, v0))
    t = rings[-1]
    quad(t[0], t[3], t[2], t[1], uv(0.8, 0.8), uv(0.8, 0.2), uv(0.2, 0.2), uv(0.2, 0.8))   # flat top: the inner square
    out = ["# IsaacDoom tinted rock: bevelled block, Y up", "o tinted"]
    out += [f"v {x:.3f} {y:.3f} {z:.3f}" for x, y, z in V]
    out += [f"vt {u:.4f} {v:.4f}" for u, v in VT]
    out += ["usemtl isrt01.png"] + F
    return "\n".join(out) + "\n"


def tinted_skin(sheet_path):
    """64x64: the stage's tinted rock (the carved block at (0,32) in every rocks sheet), its
    bevelled frame included so the block is recognisable from any side, darkened at the base"""
    from PIL import Image
    sheet = Image.open(sheet_path).convert("RGBA")
    face = sheet.crop((0, 32, 32, 64))
    bb = face.getbbox() or (0, 0, 32, 32)
    face = face.crop(bb)
    px = face.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
    fill = tuple(int(v * 0.6) for v in px) + (255,)
    skin = Image.new("RGBA", (64, 64), fill)
    skin.alpha_composite(face.resize((64, 64), Image.NEAREST))
    px_ = skin.load()
    for y in range(64):
        f = ROCK_BRIGHT * (1.0 - 0.25 * max(0, (y - 40) / 24.0))
        for x in range(64):
            r, g, b, a = px_[x, y]
            px_[x, y] = (min(255, int(r * f)), min(255, int(g * f)), min(255, int(b * f)), 255)
    buf = io.BytesIO(); skin.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def rock_sheet_dirs():
    """where the stage rock sheets (gfx/grid/rocks_<stage>.png) come from: the game's own
    unpacked resources (never shipped with IsaacDoom), else a local doom/rocks copy"""
    dirs = []
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        from paths import gfx_dirs
        dlc, base = gfx_dirs()
        dirs += [os.path.join(str(dlc), "grid"), os.path.join(str(base), "grid")]
    except Exception:
        pass
    dirs.append(os.path.join(HERE, "rocks"))
    return [d for d in dirs if os.path.isdir(d)]


def find_rock_sheet(name):
    for d in rock_sheet_dirs():
        for fn in (f"rocks_{name}.png", f"Rocks_{name}.png"):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                return p
        # case-insensitive last resort
        for fn in os.listdir(d):
            if fn.lower() == f"rocks_{name}.png":
                return os.path.join(d, fn)
    return None


def write_rocks(z):
    if not rock_sheet_dirs():
        print("rocks: no rock sheets found (unpack Isaac's resources first) - rocks will be plain")
        return
    z.writestr("models/rocks/rock.obj", rock_obj())
    z.writestr("models/rocks/tinted.obj", tinted_obj())
    modeldef, zs = [], ["// generated by build_pk3.py: one rock class per backdrop, skinned with that stage's rock art"]
    done = {}
    for bid, name in sorted(ROCK_SHEETS.items()):
        sheet = find_rock_sheet(name) or find_rock_sheet("basement")
        if not sheet:
            continue
        skin = f"isrs{bid:02d}.png"
        z.writestr(f"models/rocks/{skin}", rock_skin(sheet))
        cls = f"IsaacRock{bid:02d}"
        zs.append(f"class {cls} : IsaacRockModel {{}}")
        modeldef.append(f'Model {cls}\n{{\n\tPath "models/rocks"\n\tModel 0 "rock.obj"\n\tSkin 0 "{skin}"\n'
                        f'\tScale 1.0 1.0 1.0\n\tOffset 0 0 -6\n\tFrameIndex ROCK A 0 0\n}}\n')
        # the tinted rock of the same stage: the bevelled block with its own face
        tskin = f"isrt{bid:02d}.png"
        z.writestr(f"models/rocks/{tskin}", tinted_skin(sheet))
        tcls = f"IsaacRockT{bid:02d}"
        zs.append(f"class {tcls} : IsaacRockModel {{}}")
        modeldef.append(f'Model {tcls}\n{{\n\tPath "models/rocks"\n\tModel 0 "tinted.obj"\n\tSkin 0 "{tskin}"\n'
                        f'\tScale 1.0 1.0 1.0\n\tOffset 0 0 -6\n\tFrameIndex ROCK A 0 0\n}}\n')
        done[bid] = cls
    z.writestr("modeldef.txt", "\n".join(modeldef))
    z.writestr("zscript_rocks.txt", "\n".join(zs) + "\n")
    # an (invisible) sprite frame for the rock actors to hang the model on
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(buf, "PNG")
    z.writestr("sprites/ROCKA0.png", buf.getvalue())
    print(f"rocks: {len(done)} stage skins")


def main():
    textmap = build_textmap()
    wad = build_wad([("MAP01", b""), ("TEXTMAP", textmap), ("ENDMAP", b"")])
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("maps/map01.wad", wad)
        for fn in os.listdir(SRC):
            z.write(os.path.join(SRC, fn), fn)
        gfx = os.path.join(HERE, "gfx")     # generated sprites (Tech X ring...)
        if os.path.isdir(gfx):
            for fn in os.listdir(gfx):
                z.write(os.path.join(gfx, fn), "sprites/" + fn)
        write_rocks(z)
    print("wrote", OUT, "(map:", len(textmap), "bytes textmap)")


if __name__ == "__main__":
    main()
