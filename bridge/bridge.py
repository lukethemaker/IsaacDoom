"""
IsaacDoom bridge
================
Sits between The Binding of Isaac (Lua mod -> isaac_state.txt / isaac_cmd.txt)
and GZDoom (isaac_in.cfg exec'd every tic / doom_out.log).

    python bridge.py            # full mode: Doom controls Isaac
    python bridge.py --watch    # watch-only: play Isaac normally, Doom mirrors it
    python bridge.py --poke     # press Scroll Lock every frame (not needed normally)

Everything is plain Python 3, no packages needed.
"""
import argparse
import math
import re
import os
import sys
import time
from pathlib import Path

PIPE = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "IsaacDoom" / "pipe"
STATE_PATH = PIPE / "isaac_state.txt"
CMD_PATH = PIPE / "isaac_cmd.txt"
IN_CFG = PIPE / "isaac_in.cfg"
DOOM_LOG = PIPE / "doom_out.log"
DOOM_LOG_ALT = PIPE / "log-doom_out-log.txt"   # UZDoom names it this way
EVENT_LOG = PIPE / "bridge_log.txt"
ALIVE_PATH = PIPE / "isaac_alive.txt"       # written by the mod's render loop: only ticks while a run exists
ISAAC_LOG = PIPE.parent.parent / "My Games" / "Binding of Isaac Repentance" / "log.txt"


def isaac_log_slot():
    """save file Isaac last selected, read off the tail of its log.txt (0 = none yet). The
    file-select screen loads files 1, 2, 3 in a row for its previews right after
    'Menu_Save loading slot drawings'; any load outside that is a real selection."""
    try:
        with open(ISAAC_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    slot, previews = 0, 0
    for line in text.splitlines():
        if "Menu_Save loading slot drawings" in line:
            previews = 3
        elif "Loading PersistentData " in line:
            try:
                n = int(line.split("Loading PersistentData ")[1].split()[0])
            except (ValueError, IndexError):
                continue
            if previews > 0:
                previews -= 1
            else:
                slot = n
    return slot


def elog(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line)
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

CELL_I = 40.0        # Isaac grid cell, game units
CELL_D = 64.0        # Doom grid cell, map units
MARGIN = 3           # cells of margin around the room in the Doom map (must match build_pk3/zscript)
K = CELL_D / CELL_I
VEL_K = K * 30.0 / 35.0   # Isaac units/frame (30 Hz) -> Doom units/tic (35 Hz)

LOAD_MIN_SECONDS = 0.0    # no artificial hold: play as soon as Isaac's new room arrives
DOOR_TIMEOUT = 2.5        # give up on a transition if Isaac doesn't change room
ARRIVAL_SETTLE_FRAMES = 4 # Isaac frames to wait after a room change before reading the player's arrival spot
LOOP_HZ = 120

STAGE_NAMES = {
    (1, 0): "Basement I", (1, 1): "Cellar I", (1, 2): "Burning Basement I",
    (2, 0): "Basement II", (2, 1): "Cellar II", (2, 2): "Burning Basement II",
    (1, 4): "Downpour I", (1, 5): "Dross I", (2, 4): "Downpour II", (2, 5): "Dross II",
    (3, 0): "Caves I", (3, 1): "Catacombs I", (3, 2): "Flooded Caves I",
    (4, 0): "Caves II", (4, 1): "Catacombs II", (4, 2): "Flooded Caves II",
    (3, 4): "Mines I", (3, 5): "Ashpit I", (4, 4): "Mines II", (4, 5): "Ashpit II",
    (5, 0): "Depths I", (5, 1): "Necropolis I", (5, 2): "Dank Depths I",
    (6, 0): "Depths II", (6, 1): "Necropolis II", (6, 2): "Dank Depths II",
    (5, 4): "Mausoleum I", (5, 5): "Gehenna I", (6, 4): "Mausoleum II", (6, 5): "Gehenna II",
    (7, 0): "Womb I", (7, 1): "Utero I", (7, 2): "Scarred Womb I",
    (8, 0): "Womb II", (8, 1): "Utero II", (8, 2): "Scarred Womb II",
    (7, 4): "Corpse I", (8, 4): "Corpse II",
    (9, 0): "Blue Womb", (10, 0): "Sheol", (10, 1): "Cathedral",
    (11, 0): "Dark Room", (11, 1): "Chest", (12, 0): "The Void", (13, 0): "Home",
}


def anm2_key(fn):
    """same normalisation as tools/build_sprites.py"""
    fn = fn.replace("\\", "/").strip().lower()
    if fn.startswith("gfx/"):
        fn = fn[4:]
    if fn.endswith(".anm2"):
        fn = fn[:-5]
    return re.sub(r"[^a-z0-9/_.\-]", "_", fn)


def anim_key(name):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name.strip())


def stage_name(stage, stype):
    return STAGE_NAMES.get((stage, stype)) or STAGE_NAMES.get((stage, 0)) or f"Stage {stage}"


# --------------------------------------------------------------------------
# Isaac state file
# --------------------------------------------------------------------------
class IsaacState:
    def __init__(self):
        self.seq = -1
        self.frame = 0
        self.room_idx = None
        self.shape = 1
        self.room_type = 1
        self.overlay = 0
        self.clear = 0
        self.w = 15
        self.h = 9
        self.stage = 1
        self.stype = 0
        self.tl = (80.0, 160.0)
        self.br = (560.0, 400.0)
        self.grid = ""
        self.doors = {}       # slot -> (open, locked, target, gridindex)
        self.player = None    # dict
        self.ents = []        # list of dicts
        self.newroom = 0
        self.run = 0
        self.names = {}       # entity hash -> display name
        self.anims = {}       # entity hash -> (anm2 key, anim key, frame, flip)
        self.gridspr = []     # (cell idx, grid type, anm2 key, anim key, frame, flip)
        self.maprooms = []    # (grid index, shape, type, display flags, visited)
        self.active = None    # (charge, max, name, id)
        self.held = ""        # pocket item name
        self.held_kind = ""   # c card / p pill
        self.held_id = 0
        self.trinket = 0
        self.hearts = ""      # heart tokens, see main.lua heartTokens
        self.intro = "0 0 0 0 00"   # boss VS screen: seq boss1 boss2 playertype spot
        self.curse = 0              # LevelCurse bitmask (1 = darkness)
        self.ptype = 0              # PlayerType of player 0
        self.speed = 1.0            # Isaac MoveSpeed stat
        self.fall = 0               # trapdoor fall counter
        self.frozen = 0             # 1 while Isaac renders without updating (stage card / fade-in)
        self.paused = 0             # Game():IsPaused() (pause menu, but also transitions/pickups)
        self.stats = ""             # found-HUD stats: speed tears damage range shotspeed luck devil angel planetarium
        self.prices = {}            # entity id -> (price, sale base price)
        self.blind = set()          # collectibles hidden by Curse of the Blind
        self.music = 0              # current MusicManager track id
        self.unicorn = 0            # unicorn invincibility active (rainbow + fast music)
        self.beams = []             # (laser hash, variant, sprite scale, ["x,y", ...] Isaac coords)
        self.fx = []          # one-shot effects from this frame
        self.dead = 0
        self.backdrop = 1
        self.origin = None    # centre of grid cell 0 (Isaac units)
        self.popups = []

    def parse(self, text):
        if not text.endswith("END") and "\nEND" not in text[-8:]:
            return False
        lines = text.split("\n")
        seq = None
        doors = {}
        ents = []
        names = {}
        anims = {}
        gridspr = []
        maprooms = []
        fx = []
        sfx = []
        popups = []
        beams = []
        prices = {}
        blind = set()
        active = None
        held = ""
        held_kind = ""
        held_id = 0
        trinket = 0
        hearts = ""
        intro = "0 0 0 0 00"
        for ln in lines:
            if not ln:
                continue
            tag, _, rest = ln.partition(" ")
            p = rest.split()
            try:
                if tag == "SEQ":
                    seq = int(p[0])
                elif tag == "FRAME":
                    self.frame = int(p[0])
                elif tag == "ROOM":
                    self.room_idx = int(p[0]); self.shape = int(p[1]); self.room_type = int(p[2])
                    self.clear = int(p[3]); self.w = int(p[4]); self.h = int(p[5])
                    self.stage = int(p[6]); self.stype = int(p[7])
                    self.tl = (float(p[8]), float(p[9])); self.br = (float(p[10]), float(p[11]))
                elif tag == "NEWROOM":
                    self.newroom = int(p[0])
                elif tag == "RUN":
                    self.run = int(p[0])
                elif tag == "DEAD":
                    self.dead = int(p[0])
                elif tag == "BACKDROP":
                    self.backdrop = int(p[0])
                elif tag == "OVERLAY":
                    self.overlay = int(p[0])
                elif tag == "ORIGIN":
                    self.origin = (float(p[0]), float(p[1]))
                elif tag == "GRID":
                    self.grid = rest.strip()
                elif tag == "DOOR":
                    doors[int(p[0])] = (int(p[1]), int(p[2]), int(p[3]), int(p[4]))
                elif tag == "PLAYER":
                    self.player = dict(x=float(p[0]), y=float(p[1]), vx=float(p[2]), vy=float(p[3]),
                                       hearts=int(p[4]), maxhearts=int(p[5]), soul=int(p[6]),
                                       bone=int(p[7]), eternal=int(p[8]), bombs=int(p[9]),
                                       keys=int(p[10]), coins=int(p[11]), controlled=int(p[12]),
                                       fly=int(p[13]) if len(p) > 13 else 0,
                                       fire=int(p[14]) if len(p) > 14 else 0,
                                       ring=float(p[15]) if len(p) > 15 else 0.0,
                                       champ=int(p[16]) if len(p) > 16 else -1,
                                       tint=p[17] if len(p) > 17 else "222")
                elif tag == "NAME":
                    names[int(p[0])] = rest.split(" ", 1)[1].strip() if " " in rest else ""
                elif tag == "ANM":
                    parts = rest.split("\t")
                    hp = parts[0].split()
                    anim = parts[1] if len(parts) > 1 else ""
                    fn = parts[2] if len(parts) > 2 else ""
                    ovanim = parts[3] if len(parts) > 3 else ""
                    ovframe = int(parts[4]) if len(parts) > 4 and parts[4].strip() else 0
                    sscale = float(parts[5]) if len(parts) > 5 and parts[5].strip() else 1.0
                    height = float(parts[6]) if len(parts) > 6 and parts[6].strip() else 999.0
                    fall = float(parts[7]) if len(parts) > 7 and parts[7].strip() else 0.0
                    anims[int(hp[0])] = (anm2_key(fn), anim_key(anim), int(hp[1]), int(hp[2]), anim_key(ovanim), ovframe, sscale, height, fall)
                elif tag == "MAP":
                    maprooms.append((int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])))
                elif tag == "GSP":
                    head, _, fn = rest.partition("\t")
                    fn, _, skin = fn.partition("\t")
                    skin, _, rubble = skin.partition("\t")
                    hp = head.split(" ", 4)
                    key = anm2_key(fn)
                    skin = skin.strip().lower()
                    if skin:
                        key += "@" + skin
                    gridspr.append((int(hp[0]), int(hp[1]), key, anim_key(hp[4] if len(hp) > 4 else ""), int(hp[2]), int(hp[3]),
                                    1 if rubble.strip() == "1" else 0))
                elif tag == "ACTIVE":
                    active = (int(p[0]), int(p[1]), rest.split(" ", 3)[3].strip() if rest.count(" ") >= 3 else "", int(p[2]))
                elif tag == "HELD":
                    held_kind = p[0]; held_id = int(p[1])
                    held = rest.split(" ", 2)[2].strip() if rest.count(" ") >= 2 else ""
                elif tag == "TRINK":
                    trinket = int(p[0])
                elif tag == "HEARTS":
                    hearts = rest.strip()
                elif tag == "INTRO":
                    intro = rest.strip()
                elif tag == "CURSE":
                    self.curse = int(p[0])
                elif tag == "PTYPE":
                    self.ptype = int(p[0])
                elif tag == "SPEED":
                    self.speed = float(p[0])
                elif tag == "FALL":
                    self.fall = int(p[0])
                elif tag == "FROZEN":
                    self.frozen = int(p[0])
                elif tag == "PAUSED":
                    self.paused = int(p[0])
                elif tag == "STATS":
                    self.stats = ",".join(p[:9])
                elif tag == "PRICE":
                    prices[int(p[0])] = (int(p[1]), int(p[2]) if len(p) > 2 else 0)
                elif tag == "BLIND":
                    blind.add(int(p[0]))
                elif tag == "LZR":
                    beams.append((int(p[0]), int(p[1]), float(p[2]), int(p[3]), p[4:]))
                elif tag == "MUSIC":
                    self.music = int(p[0])
                    self.unicorn = int(p[1]) if len(p) > 1 else 0
                elif tag == "FX":
                    fx.append((p[0], float(p[1]), float(p[2])))
                elif tag == "SFX":
                    sfx.append((int(p[0]), float(p[1]), float(p[2])))
                elif tag == "POPUP":
                    popups.append(rest.strip())
                elif tag == "ENT":
                    ents.append(dict(id=int(p[0]), kind=p[1], type=int(p[2]), variant=int(p[3]),
                                     sub=int(p[4]), x=float(p[5]), y=float(p[6]), vx=float(p[7]),
                                     vy=float(p[8]), size=float(p[9]), hp=float(p[10]),
                                     maxhp=float(p[11]), flying=int(p[12]), boss=int(p[13]),
                                     fire=int(p[14]) if len(p) > 14 else 0,
                                     ring=float(p[15]) if len(p) > 15 else 0.0,
                                     champ=int(p[16]) if len(p) > 16 else -1,
                                     tint=p[17] if len(p) > 17 else "222",
                                     status=int(p[18]) if len(p) > 18 else 0))
            except (IndexError, ValueError):
                return False
        if seq is None or seq == self.seq:
            return False
        self.seq = seq
        self.doors = doors
        self.ents = ents
        self.beams = beams
        self.names = names
        self.anims = anims
        self.gridspr = gridspr
        self.maprooms = maprooms
        self.active = active
        self.held = held
        self.held_kind = held_kind
        self.held_id = held_id
        self.trinket = trinket
        self.hearts = hearts
        self.intro = intro
        self.fx = fx
        self.sfx = sfx
        self.popups = popups
        self.prices = prices
        self.blind = blind
        return True


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="ascii", errors="replace", newline="\n") as f:
        f.write(text)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.002)


# --------------------------------------------------------------------------
# Doom log tail
# --------------------------------------------------------------------------
class DoomTail:
    def __init__(self, path):
        self.path = path
        self.f = None
        self.buf = b""
        self.x = None
        self.y = None
        self.angle = 0.0
        self.fire = 0
        self.bomb = 0
        self.use = 0
        self.card = 0
        self.restart = 0
        self.newrun = 0
        self.slot = 0
        self.drop = 0                       # Isaac's drop button (Ctrl) held in Doom
        self.seed = ""                      # seed for the next new run ("ISSEED xxxx" line from the menu)
        self.ack = -1
        self.tic = 0
        self.last_line_time = 0.0
        self.paused = False                 # explicit "ISP 1/0" from Doom's UiTick
        self.restarted = False      # set when Doom's log is (re)opened or truncated

    def poll(self):
        if self.f is None:
            for cand in (DOOM_LOG_ALT, self.path):
                try:
                    self.f = open(cand, "rb")
                    self.f.seek(0, os.SEEK_END)
                    self.restarted = True
                    print(f"[doom] tailing {cand.name}")
                    break
                except OSError:
                    continue
            if self.f is None:
                return False
        try:
            data = self.f.read()
            if not data:
                # relaunched Doom truncates the file: restart from the top
                try:
                    if os.path.getsize(self.f.name) < self.f.tell():
                        self.f.seek(0)
                        self.buf = b""
                        self.ack = -1
                        self.restarted = True
                        data = self.f.read()
                except OSError:
                    pass
        except OSError:
            return False
        if not data:
            return False
        self.buf += data
        lines = self.buf.split(b"\n")
        self.buf = lines.pop()
        got = False
        for raw in lines:
            if b"IsaacDoom: handler loaded" in raw:
                self.restarted = True
                self.paused = False
            j = raw.find(b"ISSEED ")
            if j >= 0:
                try:
                    self.seed = raw[j + 7:].split()[0].decode("ascii", "ignore")
                except IndexError:
                    self.seed = ""
                continue
            j = raw.find(b"ISP ")
            if j >= 0:
                try:
                    self.paused = int(raw[j:].split()[1]) != 0
                except (ValueError, IndexError):
                    pass
                continue
            i = raw.find(b"IS ")
            if i < 0:
                continue
            p = raw[i:].split()
            if len(p) < 8:
                continue
            try:
                self.tic = int(p[1]); self.x = float(p[2]); self.y = float(p[3])
                self.angle = float(p[4]) % 360.0
                self.fire = int(p[5]); self.bomb = int(p[6]); self.ack = int(p[7])
                self.use = int(p[8]) if len(p) > 8 else 0
                self.card = int(p[9]) if len(p) > 9 else 0
                self.restart = int(p[10]) if len(p) > 10 else 0
                self.newrun = int(p[11]) if len(p) > 11 else 0
                self.slot = int(p[12]) if len(p) > 12 else 0
                self.drop = int(p[13]) if len(p) > 13 else 0
                got = True
            except ValueError:
                continue
        if got:
            self.last_line_time = time.time()
        return got


# --------------------------------------------------------------------------
# Optional keyboard poke (Scroll Lock) for the exec fallback
# --------------------------------------------------------------------------
def make_poker():
    if os.name != "nt":
        return lambda: None
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_SCROLL = 0x91

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def press():
        down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=VK_SCROLL))
        up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=VK_SCROLL, dwFlags=KEYEVENTF_KEYUP))
        ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    return press


# --------------------------------------------------------------------------
# Title-screen driver: vanilla Repentance runs no mod callbacks on its menus,
# so the only way from Isaac's title screen into a run is real key presses.
# Bring Isaac's window forward, tap Enter through title -> save slot -> menu
# -> character -> difficulty until the mod reports a run, then hand focus
# back to Doom. The NEWRUN command waiting in isaac_cmd.txt then restarts
# that run as the character Doom asked for.
# --------------------------------------------------------------------------
class MenuDriver:
    def __init__(self):
        self.thread = None
        self.run_seen = False
        self.loaded_slot = 0
        self.ok = os.name == "nt"
        if self.ok:
            import ctypes
            from ctypes import wintypes
            self.ct = ctypes
            self.wt = wintypes
            self.u32 = ctypes.windll.user32

    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def find_isaac(self):
        ct, wt, u32 = self.ct, self.wt, self.u32
        found = []
        proc = ct.WINFUNCTYPE(ct.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, _):
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ct.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.lower().startswith("binding of isaac"):
                found.append(hwnd)
            return True
        u32.EnumWindows(proc(cb), 0)
        return found[0] if found else None

    def focus(self, hwnd):
        u32 = self.u32
        if not hwnd:
            return
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)              # SW_RESTORE
        # a console process may only steal the foreground right after it sent input
        u32.keybd_event(0x12, 0, 0, 0)           # Alt down
        u32.keybd_event(0x12, 0, 2, 0)           # Alt up
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.25)

    EXTENDED = {0x25, 0x27, 0x26, 0x28}      # arrow keys: without the extended flag they read as numpad keys

    def tap(self, vk, scan, hold=0.08):
        u32 = self.u32
        ext = 1 if vk in self.EXTENDED else 0    # KEYEVENTF_EXTENDEDKEY
        u32.keybd_event(vk, scan, ext, 0)
        time.sleep(hold)
        u32.keybd_event(vk, scan, ext | 2, 0)    # KEYEVENTF_KEYUP

    def post_tap(self, hwnd, vk, scan, hold=0.08):
        """key press delivered straight to Isaac's window: no focus change, nothing comes to the front"""
        u32 = self.u32
        WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
        ext = (1 << 24) if vk in self.EXTENDED else 0
        u32.PostMessageW(hwnd, WM_KEYDOWN, vk, (scan << 16) | 1 | ext)
        time.sleep(hold)
        u32.PostMessageW(hwnd, WM_KEYUP, vk, (scan << 16) | 1 | ext | (1 << 30) | (1 << 31))

    def tap_key(self, name, with_focus=False):
        """one key press to Isaac's window (pause/unpause). Posted straight to the window by
        default; with_focus brings Isaac forward for the press and puts Doom back after."""
        if not self.ok or self.active():
            return
        hwnd = self.find_isaac()
        if not hwnd:
            print("[menu] Isaac's window not found")
            return
        import threading
        vk, sc = self.VK[name]
        if not with_focus:
            threading.Thread(target=self.post_tap, args=(hwnd, vk, sc), daemon=True).start()
            return

        def go():
            u32 = self.u32
            doom = u32.GetForegroundWindow()
            self.focus(hwnd)
            if u32.GetForegroundWindow() != hwnd:
                elog("[menu] could not bring Isaac forward: not pressing (the key would land in Doom)")
            else:
                self.tap(vk, sc)
            time.sleep(0.1)
            if doom and doom != hwnd:
                self.focus(doom)
                u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0013)   # HWND_BOTTOM, no activate
        threading.Thread(target=go, daemon=True).start()

    def drive(self, kind, slot=0, delay=0.0, force_focus=False):
        if not self.ok:
            print("[menu] title-screen driving only works on Windows")
            return
        if self.active():
            return
        import threading
        self.run_seen = False
        self.thread = threading.Thread(target=self._run, args=(kind, slot, delay, force_focus), daemon=True)
        self.thread.start()

    VK = {"enter": (0x0D, 0x1C), "left": (0x25, 0x4B), "right": (0x27, 0x4D), "up": (0x26, 0x48), "down": (0x28, 0x50), "esc": (0x1B, 0x01)}

    def _run(self, kind, slot, delay=0.0, force_focus=False):
        u32 = self.u32
        if delay > 0:
            time.sleep(delay)          # let the title screen finish fading in
        doom = u32.GetForegroundWindow()
        isaac = self.find_isaac()
        if not isaac:
            print("[menu] Isaac's window not found - is it running?")
            return
        print(f"[menu] driving Isaac's menus ({kind}, save file {slot or 'current'})")
        # Isaac at boot: title (any key) -> save file select -> game menu, where the cursor sits on
        # Continue when a run can be continued and on New Run otherwise. Enter alone therefore
        # gets into *a* run either way; a New Run request then restarts it as the chosen
        # character from inside the run. The save file screen is three slots side by side.
        # (each wait is what Isaac needs to finish that screen's fade before the next press lands)
        steps = [("enter", 0.9)]
        if slot in (1, 2, 3):
            # the file cursor starts on the file last used (file 1 on a fresh start) and the
            # row wraps, so walk the direct way from there rather than hunting for an end
            start = isaac_log_slot() or 1
            if slot > start:
                steps += [("right", 0.25)] * (slot - start)
            elif slot < start:
                steps += [("left", 0.25)] * (start - slot)
            elog(f"[menu] file cursor assumed on {start}, going to {slot}")
        steps += [("enter", 0.9)]
        steps += [("enter", 0.7)] * (2 if kind == "continue" else 4)

        elog(f"[menu] drive {kind} slot={slot}: " + " ".join(k for k, _ in steps))

        def run_steps(press):
            for key, wait in steps:
                if self.run_seen:
                    return
                elog(f"[menu] key {key}")
                press(*self.VK[key])
                t0 = time.time()
                while time.time() - t0 < wait:
                    time.sleep(0.05)
                    if self.run_seen:
                        return
            t0 = time.time()
            while not self.run_seen and time.time() - t0 < 4.0:
                time.sleep(0.1)

        # first without touching focus (Doom / the headset stays in front)
        if not force_focus:
            run_steps(lambda vk, sc: self.post_tap(isaac, vk, sc))
        if not self.run_seen:
            # Isaac ignored posted keys: bring it forward briefly, then put Doom back
            elog("[menu] posted keys ignored, focusing Isaac for a moment")
            self.focus(isaac)
            if u32.GetForegroundWindow() == isaac:
                run_steps(lambda vk, sc: self.tap(vk, sc))
            else:
                elog("[menu] could not bring Isaac forward: giving up rather than typing into Doom")
            if doom and doom != isaac:
                self.focus(doom)
                u32.SetWindowPos(isaac, 1, 0, 0, 0, 0, 0x0013)   # HWND_BOTTOM, keep size/pos, no activate
        if self.run_seen:
            got = isaac_log_slot()
            elog(f"[menu] Isaac is in a run (log says save file {got or '?'}, wanted {slot or 'current'})")
            self.loaded_slot = got
            if doom and doom != isaac and u32.GetForegroundWindow() != doom:
                self.focus(doom)
                u32.SetWindowPos(isaac, 1, 0, 0, 0, 0, 0x0013)
        else:
            print("[menu] no run started - check Isaac's window")


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------
class Bridge:
    def __init__(self, control=True, poke=False, kick=False):
        self.control = control
        self.poke = make_poker() if poke else None
        self.kick = make_poker() if kick else None   # --kick: press Scroll Lock when Doom stops acking
        self.kicks_sent = 0
        self.last_kick = 0.0
        self.isaac = IsaacState()
        self.doom = DoomTail(DOOM_LOG)
        self.out_seq = 0
        self.cmd_seq = 0
        self.doom_state = 1            # 0 play, 1 loading
        self.stage_seq = 0             # bumps on every new floor / new run: Doom's cue for the stage card
        self.stage_key = None
        self.label = "Waiting for Isaac..."
        self.cur_room = None
        self.loading_until = 0.0
        self.door_wait_until = 0.0
        self.teleport_pending = False
        self.moves_enabled = False
        self.last_cmd_time = 0.0
        self.last_fire_tic = -1
        self.last_bomb_tic = -1
        self.last_use_tic = -1
        self.last_unlock = 0.0
        self.last_restart = 0.0
        self.started_at = time.time()
        self.prev_bomb = self.prev_use = self.prev_card = 0
        self.prev_newrun = 0
        self.restart_sent = False
        self.menu = MenuDriver()            # taps Enter on Isaac's title screen
        self.pending_newrun = None          # command to repeat once Isaac is in a run
        self.menu_since = 0.0               # when Isaac first reported "in the menus"
        self.in_menu = False                # Isaac has no run going (title / menus / not running)
        self.known_slot = 0                 # save file the current run is on (0 = unknown)
        self.exit_newrun = None             # (kind, cmd, slot) while Isaac is sent to the title for a file switch
        self.exit_at = 0.0
        self.wanted_slot = 0                # save file the last menu walk was asked for (0 = none)
        self.run_started_at = 0.0
        self.wanted_run = None
        self.slot_retries = 0
        self.force_focus_drive = False
        self.doom_paused = False            # Doom's tics have stopped (menu / console up)
        self.pause_mismatch_since = None    # when Isaac's pause state last stopped matching Doom's
        self.last_pause_tap = 0.0
        self.quick_unpause = False
        self.we_paused = False              # Isaac's current pause menu was opened by the bridge
        self.pause_taps = 0
        self.doom_restart_flag = False
        self.last_menu_write = 0.0
        self.last_card_tic = -1
        self.sent_times = {}           # out_seq -> time, for latency stats
        self.lat_samples = []
        self.last_stats = time.time()
        self.mtime = 0.0
        self.teleport_seq = 0
        self.arrive_frame = None
        self.moved_since_arrival = True
        self.tele_active = False
        self.tele_target = None
        self.fx_seq = 0
        self.sfx_seq = 0
        self.last_gridspr = None
        self.last_map = None
        self.map_ack_seq = None
        self.gridspr_ack_seq = None
        self.popup_seq = 0

    # -- coordinate mapping --
    def cell0(self):
        """centre of grid cell 0 in Isaac units (falls back to the usual layout)"""
        if self.isaac.origin:
            return self.isaac.origin
        return (self.isaac.tl[0] - CELL_I / 2, self.isaac.tl[1] - CELL_I / 2)

    def i2d(self, x, y):
        ox, oy = self.cell0()
        return ((x - ox) / CELL_I + 0.5 + MARGIN) * CELL_D, -(((y - oy) / CELL_I + 0.5 + MARGIN) * CELL_D)

    def d2i(self, dx, dy):
        ox, oy = self.cell0()
        tl, br = self.isaac.tl, self.isaac.br
        ix = (dx / CELL_D - 0.5 - MARGIN) * CELL_I + ox
        iy = (-dy / CELL_D - 0.5 - MARGIN) * CELL_I + oy
        ix = min(max(ix, tl[0]), br[0])
        iy = min(max(iy, tl[1]), br[1])
        return ix, iy

    def arrival_pose(self, x, y):
        """where to put the Doom player on arrival: a step inside the room from the entry door,
        facing into the room. Returns (isaac x, isaac y, doom angle or -1 = keep)."""
        tl, br = self.isaac.tl, self.isaac.br
        dist = {"top": y - tl[1], "bottom": br[1] - y, "left": x - tl[0], "right": br[0] - x}
        side = min(dist, key=dist.get)
        if dist[side] > 50:
            return x, y, -1              # not at a door (trapdoor / first room): keep as is
        # the centre of the first floor cell inside the door (20 units in): that cell is always
        # clear, whereas 45 units in is already the next cell, which may hold a rock or poop
        if side == "top":
            return x, tl[1] + 20, 270
        if side == "bottom":
            return x, br[1] - 20, 90
        if side == "left":
            return tl[0] + 20, y, 0
        return br[0] - 20, y, 180

    def doom_cell(self):
        if self.doom.x is None:
            return None
        c = int(self.doom.x // CELL_D) - MARGIN
        r = int(-self.doom.y // CELL_D) - MARGIN
        return c, r

    # -- inbound from Isaac --
    def poll_isaac(self):
        try:
            m = STATE_PATH.stat().st_mtime
        except OSError:
            return False
        if m == self.mtime:
            return False
        if m < self.started_at - 2:
            # left over from an earlier session: Isaac hasn't written anything yet
            self.mtime = m
            return False
        text = read_text(STATE_PATH)
        if text is None:
            return False
        if not self.isaac.parse(text):
            return False
        self.mtime = m
        return True

    # -- outbound to Doom --
    def write_menu(self, state=3):
        """Isaac at the title / main menu: Doom shows its own title screen (room state 3), or
        just a loading screen (state 1) while the bridge is walking Isaac's menus itself"""
        self.out_seq += 1
        cfg = (f'set isaac_room "15,9,{state},{"Menu" if state == 3 else "Loading"},1"\n'
               'set isaac_grid ""\n'
               'set isaac_ents ""\n'
               f'set isaac_seq {self.out_seq}\n')
        write_atomic(IN_CFG, cfg)
        self.sent_times[self.out_seq] = time.time()
        # a fresh run after the menu must be treated as new, whatever room index it starts in
        self.cur_room = None

    def write_doom(self):
        st = self.isaac
        self.out_seq += 1
        room = f"{st.w},{st.h},{2 if st.dead else self.doom_state},{self.label},{st.backdrop},{st.stage},{st.stype},{st.ptype},{st.room_type},{st.overlay}"
        ents = []
        for e in st.ents:
            dx, dy = self.i2d(e["x"], e["y"])
            nm = st.names.get(e["id"], "")
            ak, an, af, afl, oa, of, ssc, hgt, fall = st.anims.get(e["id"], ("", "", 0, 0, "", 0, 1.0, 999.0, 0.0))
            sub = e["sub"]
            if e["kind"] == "k" and e["id"] in st.blind:
                sub = 0                          # Curse of the Blind: the question-mark item
            if e["kind"] == "k" and e["variant"] in (100, 350) and ak:
                ak = f'{ak}#{sub}'               # per-item render (the pedestal is the overlay)
            price, sale = st.prices.get(e["id"], (0, 0))
            ents.append(f'{e["id"]}:{e["kind"]}:{e["type"]}:{e["variant"]}:{dx:.1f}:{dy:.1f}:'
                        f'{e["vx"] * VEL_K:.2f}:{-e["vy"] * VEL_K:.2f}:{e["size"]:.1f}:{e["boss"]}:{sub}:{nm}:'
                        f'{ak}:{an}:{af}:{afl}:{e["hp"]:.0f}:{e["maxhp"]:.0f}:{oa}:{of}:{e.get("fire", 0)}:{e.get("ring", 0) * CELL_D / 40.0:.1f}:{e.get("champ", -1)}:{e.get("tint", "222")}:{ssc:.2f}:{hgt:.1f}:{fall:.2f}:{price}:{sale}:{e.get("status", 0)}:{e.get("flying", 0)}')
        # laser beams as Doom-space polylines: "id:variant:scale:x,y,x,y,...|..."
        bl = []
        for bid, bvar, bsc, mine, pts in st.beams:
            coords = []
            for pt in pts:
                try:
                    ix, iy = pt.split(",")
                    dx, dy = self.i2d(float(ix), float(iy))
                    coords.append(f"{dx:.0f},{dy:.0f}")
                except ValueError:
                    continue
            if len(coords) >= 2:
                bl.append(f"{bid}:{bvar}:{bsc:.2f}:{','.join(coords)}:{mine}")
        beams = "|".join(bl)
        if st.active:
            hud = f"{st.active[2]}|{st.active[0]}|{st.active[1]}|{st.held}|{st.active[3]}"
        else:
            hud = f"|0|0|{st.held}|0"
        hud += f"|{st.trinket}|{st.held_kind}|{st.held_id}"
        gs = "|".join(f"{i}:{t}:{k}:{a}:{fr}:{fl}:{rb}" for i, t, k, a, fr, fl, rb in st.gridspr)
        if gs != self.last_gridspr:
            self.last_gridspr = gs
            self.gridspr_ack_seq = None        # resend until Doom acks a frame carrying it
        if self.gridspr_ack_seq is not None and self.doom.ack >= self.gridspr_ack_seq:
            gs_out = "="                      # Doom has it
        else:
            gs_out = gs if gs else "-"        # "-" = no grid sprites
        mp = f"{st.room_idx}|" + "|".join(f"{gi}:{sh}:{ty}:{fl}:{vi}" for gi, sh, ty, fl, vi in st.maprooms)
        if mp != self.last_map:
            self.last_map = mp
            self.map_ack_seq = None
        if self.map_ack_seq is not None and self.doom.ack >= self.map_ack_seq:
            mp_out = "="
        else:
            mp_out = mp
        pop = ""
        if st.popups:
            self.popup_seq += 1
            pop = st.popups[-1]
        fxs = ""
        if st.fx:
            self.fx_seq += 1
            parts = []
            for kind, x, y in st.fx:
                dx, dy = self.i2d(x, y)
                parts.append(f"{kind}:{dx:.1f}:{dy:.1f}")
            fxs = "|".join(parts)
        sfxs = ""
        if st.sfx:
            self.sfx_seq += 1
            parts = []
            for sid, x, y in st.sfx:
                if x < 0:
                    parts.append(f"{sid}:-1:-1")
                else:
                    dx, dy = self.i2d(x, y)
                    parts.append(f"{sid}:{dx:.1f}:{dy:.1f}")
            sfxs = "|".join(parts)
        p = st.player or dict(hearts=0, maxhearts=0, soul=0, bombs=0, keys=0, coins=0, x=0, y=0, fly=0)
        ax, ay, aang = self.arrival_pose(p["x"], p["y"])
        tx, ty = self.i2d(ax, ay)
        if self.teleport_pending:
            self.teleport_pending = False
            self.tele_active = True
            self.tele_target = None
        tele = 1 if self.tele_active else 0    # stays on until Doom acknowledges a frame that carried it
        player = f'{p["hearts"]},{p["maxhearts"]},{p["soul"]},{p["bombs"]},{p["keys"]},{p["coins"]},{tx:.1f},{ty:.1f},{tele},{aang},{p.get("fly", 0)},{st.speed:.2f}'
        cfg = (f'set isaac_room "{room}"\n'
               f'set isaac_grid "{st.grid}"\n'
               f'set isaac_ents "{"|".join(ents)}"\n'
               f'set isaac_player "{player}"\n'
               f'set isaac_hud "{hud}"\n'
               f'set isaac_hearts "{st.hearts}"\n'
               f'set isaac_intro "{st.intro}"\n'
               f'set isaac_curse {st.curse}\n'
               f'set isaac_fall {st.fall}\n'
               f'set isaac_frozen {st.frozen}\n'
               f'set isaac_stageseq {self.stage_seq}\n'
               f'set isaac_music {st.music}\n'
               f'set isaac_unicorn {st.unicorn}\n'
               f'set isaac_beams "{beams}"\n'
               f'set isaac_gridspr "{gs_out}"\n'
               f'set isaac_map "{mp_out}"\n'
               f'set isaac_stats "{st.stats}"\n'
               f'set isaac_popup "{pop}"\n'
               f'set isaac_popupseq {self.popup_seq}\n'
               f'set isaac_fx "{fxs}"\n'
               f'set isaac_fxseq {self.fx_seq}\n'
               f'set isaac_sfx "{sfxs}"\n'
               f'set isaac_sfxseq {self.sfx_seq}\n'
               f'set isaac_seq {self.out_seq}\n')
        write_atomic(IN_CFG, cfg)
        self.sent_times[self.out_seq] = time.time()
        if gs_out != "=" and self.gridspr_ack_seq is None:
            self.gridspr_ack_seq = self.out_seq
        if mp_out != "=" and self.map_ack_seq is None:
            self.map_ack_seq = self.out_seq
        if tele and self.tele_target is None:
            self.tele_target = self.out_seq
            self.teleport_seq = self.out_seq
        if len(self.sent_times) > 200:
            for k in sorted(self.sent_times)[:-100]:
                self.sent_times.pop(k, None)
        if self.poke:
            self.poke()

    # -- outbound to Isaac --
    def write_isaac(self, lines):
        self.cmd_seq += 1
        write_atomic(CMD_PATH, f"SEQ {self.cmd_seq}\n" + "\n".join(lines) + "\n")

    # -- main step --
    def step(self):
        now = time.time()
        got_isaac = self.poll_isaac()
        got_doom = self.doom.poll()

        if got_doom and self.doom.ack in self.sent_times:
            self.lat_samples.append(now - self.sent_times.pop(self.doom.ack))
        if self.tele_active and self.tele_target is not None and self.doom.ack >= self.tele_target:
            self.tele_active = False
            elog(f"[arrive] Doom acked teleport (seq {self.tele_target}); doom now at ({self.doom.x},{self.doom.y}) angle={self.doom.angle:.0f}")

        st = self.isaac
        if self.doom.restarted:
            self.doom.restarted = False
            print("[doom] Doom (re)started: resending map, grid art and position")
            self.last_map = None
            self.last_gridspr = None
            self.map_ack_seq = None
            self.gridspr_ack_seq = None
            self.teleport_pending = True
            self.doom_restart_flag = True
            if st.seq >= 0:
                self.write_doom()
        # Is a run going at all? The mod's render loop keeps isaac_alive.txt ticking whenever a
        # run exists (paused or not); on the title screen no mod code runs, so it goes stale.
        try:
            alive_age = now - ALIVE_PATH.stat().st_mtime
        except OSError:
            alive_age = 1e9
        # The heartbeat also stops for a moment when a run is restarted from inside (the mod's
        # NEWRUN goes exit -> new run), and the mod writes SEQ -2 on any run exit, so neither
        # counts until it has lasted a few seconds: a flicker here would pop Doom's menu.
        # A stale heartbeat alone is not enough either: the mod's render loop also stops for
        # the whole floor transition (nightmare + level load, 5 s and more). "In the menu"
        # needs the heartbeat gone AND either the mod's run-exit marker (SEQ -2) or no state
        # from this Isaac session at all (fresh start: the file is from an earlier session).
        try:
            state_stale = STATE_PATH.stat().st_mtime < self.started_at - 2
        except OSError:
            state_stale = True
        in_menu = alive_age > 3.5 and (st.seq == -2 or state_stale)
        if self.in_menu and (alive_age > 1.0 or st.seq == -2):
            in_menu = True          # already in the menu: stay there until a run really ticks
        if in_menu != self.in_menu:
            self.in_menu = in_menu
            print("[isaac] " + ("no run going: showing Doom's menu" if in_menu else "run detected"))
            if in_menu:
                self.last_menu_write = 0.0
            else:
                self.menu.run_seen = True
                self.run_started_at = now
                got = isaac_log_slot()
                if got:
                    self.known_slot = got
                elog(f"[run] run detected on save file {got or '?'} (wanted {self.wanted_slot or 'any'})")
                if self.wanted_slot and got and got != self.wanted_slot and self.slot_retries < 2:
                    # the menu walk landed on the wrong file: back out to the title and try
                    # again, with real key presses in front the second time
                    self.slot_retries += 1
                    kind, cmd, slot = self.wanted_run
                    elog(f"[run] wrong save file ({got} instead of {slot}): retry {self.slot_retries}")
                    self.pending_newrun = None
                    self.exit_newrun = (kind, cmd, slot)
                    self.exit_at = now
                    self.force_focus_drive = self.slot_retries >= 2
                    self.write_isaac(["EXIT"])
                else:
                    self.wanted_slot = 0
                    self.slot_retries = 0
        if in_menu:
            # a save-file switch requested from Doom: Isaac was sent to its title screen,
            # now walk it back into a run on the chosen file
            if self.exit_newrun is not None:
                kind, cmd, slot = self.exit_newrun
                self.exit_newrun = None
                print(f"[run] Isaac is at the title: driving into save file {slot}")
                self.pending_newrun = cmd
                self.wanted_slot = slot
                self.wanted_run = (kind, cmd, slot)
                self.menu.drive(kind, slot, delay=1.0, force_focus=self.force_focus_drive)
                self.force_focus_drive = False
            # keep Doom in its title state (re-sent every couple of seconds, and right after a
            # Doom restart, so a fresh Doom never sits on a black screen); while the bridge
            # itself is walking Isaac's menus, Doom shows a loading screen instead
            if now - self.last_menu_write > 2.0 or self.doom_restart_flag:
                self.last_menu_write = now
                self.write_menu(1 if self.menu.active() else 3)
            got_isaac = False
        elif self.exit_newrun is not None:
            # waiting for Isaac to leave the run; Doom shows a loading screen meanwhile
            if now - self.last_menu_write > 1.0:
                self.last_menu_write = now
                self.write_menu(1)
            if now - self.exit_at > 10.0:
                kind, cmd, slot = self.exit_newrun
                self.exit_newrun = None
                print("[run] Isaac never left the run: restarting on the current save file")
                if cmd:
                    self.write_isaac([cmd])
            got_isaac = False
        self.doom_restart_flag = False
        if got_isaac and st.seq >= 0:
            self.menu.run_seen = True
            if self.pending_newrun is not None:
                print(f"[run] Isaac is in a run: sending {self.pending_newrun}")
                self.write_isaac([self.pending_newrun])
                self.pending_newrun = None
        if got_isaac:
            # room change?
            room_key = (st.run, st.stage, st.stype, st.room_idx)
            if room_key != self.cur_room:
                first = self.cur_room is None
                if not first and self.cur_room[0] != st.run:
                    # new run / continue: everything Doom knows is stale
                    self.last_map = None
                    self.last_gridspr = None
                    self.map_ack_seq = None
                    self.gridspr_ack_seq = None
                    elog(f"[run] run changed {self.cur_room[0]} -> {st.run}: resyncing")
                self.cur_room = room_key
                self.label = stage_name(st.stage, st.stype)
                self.doom_state = 1
                self.loading_until = now + LOAD_MIN_SECONDS
                self.door_wait_until = 0.0
                stage_key = (st.run, st.stage, st.stype)
                if stage_key != self.stage_key:
                    self.stage_key = stage_key
                    self.stage_seq += 1
                    elog(f"[stage] new floor/run {stage_key}: stageseq={self.stage_seq} frozen={st.frozen}")
                # Isaac needs a few frames to place the player - unless it is frozen on its stage
                # card, where the frames won't come for seconds and the player is already placed
                self.arrive_frame = st.frame + (0 if st.frozen else ARRIVAL_SETTLE_FRAMES)
                self.moved_since_arrival = False
                self.moves_enabled = False
                elog(f"[room] -> {st.room_idx} shape={st.shape} {st.w}x{st.h} {self.label} isaacframe={st.frame} "
                     f"player=({st.player['x']:.0f},{st.player['y']:.0f}) doom=({self.doom.x},{self.doom.y}) ack={self.doom.ack}")
            if self.arrive_frame is not None and st.frame >= self.arrive_frame:
                self.arrive_frame = None
                self.teleport_pending = True
                ax, ay, aang = self.arrival_pose(st.player["x"], st.player["y"])
                elog(f"[arrive] isaac player=({st.player['x']:.0f},{st.player['y']:.0f}) tl={st.tl} br={st.br} "
                     f"-> pose isaac=({ax:.0f},{ay:.0f}) doom={tuple(round(v) for v in self.i2d(ax, ay))} angle={aang}")
            self.write_doom()

        # leave loading once the minimum time passed and the teleport went out
        if self.doom_state == 1 and self.cur_room is not None and now >= self.loading_until \
                and self.arrive_frame is None and not self.teleport_pending \
                and (self.door_wait_until == 0.0 or now >= self.door_wait_until):
            self.doom_state = 0
            self.moves_enabled = True
            self.door_wait_until = 0.0
            self.write_doom()
            if self.control:
                self.write_isaac(["RESUME"])      # let Isaac's enemies move again

        # door timeout: Isaac never changed room -> snap back to play
        if self.doom_state == 1 and self.door_wait_until and now >= self.door_wait_until:
            elog("[door] transition timed out, resuming")
            self.door_wait_until = 0.0
            self.teleport_pending = True
            self.loading_until = now
            self.write_doom()

        # Pause link. Doom announces its pause state explicitly ("ISP 1/0" from its UiTick,
        # which keeps running while world time stands still); Isaac reports Game():IsPaused()
        # in its state file (also while paused). Whenever the two disagree for long enough,
        # tap Esc on Isaac's window, then wait for the state to confirm before tapping again,
        # so a slow state file can never make us toggle it twice.
        if self.control and not self.in_menu and st.seq >= 0 and self.doom.last_line_time > 0:
            want = self.doom.paused
            isaac_paused = st.paused == 1
            if want != self.doom_paused:
                self.doom_paused = want
                self.pause_mismatch_since = None
                # Doom resumed while Isaac sits in the pause menu we put it in: undo that at once
                self.quick_unpause = (not want) and isaac_paused and self.we_paused
                elog("[pause] Doom " + ("paused" if want else "resumed"))
            # Isaac's own pauses (floor transitions, item pickups, boss intros, its nightmares)
            # are never chased: Isaac being paused while Doom runs only gets an Esc when that
            # pause is the one Doom asked for.
            if isaac_paused == want or self.doom_state == 1 or st.dead or now - self.run_started_at < 6.0 \
                    or (isaac_paused and not self.quick_unpause):
                self.pause_mismatch_since = None
                if isaac_paused == want:
                    if want and self.pause_taps > 0:
                        self.we_paused = True       # Isaac paused because we asked
                    if not isaac_paused:
                        self.we_paused = False
                    self.quick_unpause = False
                    self.pause_taps = 0
            else:
                if self.pause_mismatch_since is None:
                    self.pause_mismatch_since = now
                if now - self.pause_mismatch_since >= 0.25 and now - self.last_pause_tap >= 1.0:
                    self.last_pause_tap = now
                    self.pause_taps += 1
                    # posted keys ignored twice -> press with focus (Doom gets it back right after)
                    with_focus = self.pause_taps >= 3 and self.pause_taps <= 4
                    elog("[pause] Isaac %s, Doom %s: tapping Esc on Isaac%s" %
                          ("paused" if isaac_paused else "running", "paused" if want else "running",
                           " (with focus)" if with_focus else ""))
                    self.menu.tap_key("esc", with_focus)

        # "New run as <character>" from Doom's menu (one-shot on the rising edge)
        if self.doom.newrun > 0 and not self.prev_newrun:
            code = self.doom.newrun - 1
            if code == 99:
                print("[run] continue requested from Doom's menu")
                cmd = "CONTINUE"
            elif code >= 200:
                elog(f"[run] challenge {code - 200} requested from Doom's menu")
                cmd = f"CHALLENGE {code - 200}"
            else:
                seed = self.doom.seed.strip().strip("-")
                elog(f"[run] new run as character {code}" + (f" with seed {seed}" if seed else ""))
                cmd = f"NEWRUN {code}" + (f" {seed}" if seed else "")
            kind = "continue" if cmd == "CONTINUE" else "newrun"
            pend = None if cmd == "CONTINUE" else cmd
            slot = self.doom.slot
            if self.in_menu:
                # Isaac is on its title screen: no mod code runs there, so walk it into a run
                # with real key presses, then hand the command to the mod once a run exists
                self.pending_newrun = pend
                if slot in (1, 2, 3):
                    self.wanted_slot = slot
                    self.wanted_run = (kind, pend, slot)
                    self.slot_retries = 0
                self.menu.drive(kind, slot)
            elif slot in (1, 2, 3) and slot != self.known_slot:
                # a different save file than the run we are in: a run can only be restarted
                # on its own file, so send Isaac to the title screen and walk the menus
                print(f"[run] save file {slot} requested: leaving the current run first")
                self.exit_newrun = (kind, pend, slot)
                self.exit_at = now
                self.write_isaac(["EXIT"])
            else:
                self.write_isaac([cmd])
        self.prev_newrun = self.doom.newrun
        # dead in Isaac + R pressed in Doom -> new run
        if not st.dead:
            self.restart_sent = False
        if self.control and st.dead and self.doom.restart and not self.restart_sent and now - self.last_restart > 2.0:
            self.last_restart = now
            self.restart_sent = True          # one restart per death, however long R is held
            print("[run] restart requested from Doom")
            self.write_isaac(["RESTART"])

        # Doom -> Isaac control
        if self.control and self.doom_state == 0 and self.moves_enabled and self.doom.x is not None \
                and not st.dead and self.doom.ack >= self.teleport_seq \
                and now - self.last_cmd_time >= 1.0 / 30.0 and st.player is not None:
            cmds = []
            cell = self.doom_cell()
            door_hit = None
            if cell:
                c, r = cell
                if 0 <= c < st.w and 0 <= r < st.h:
                    gi = r * st.w + c
                    if gi < len(st.grid) and st.grid[gi] == "D":
                        for slot, (opn, locked, target, gidx) in st.doors.items():
                            if gidx == gi and opn:
                                door_hit = slot
            # locked door close to the player -> ask Isaac to unlock it (needs a key in Isaac)
            if now - self.last_unlock > 0.5:
                for slot, (opn, locked, target, gidx) in st.doors.items():
                    if not locked:
                        continue
                    dc, dr = gidx % st.w, gidx // st.w
                    dcx, dcy = (dc + MARGIN) * CELL_D + CELL_D / 2, -((dr + MARGIN) * CELL_D + CELL_D / 2)
                    if abs(self.doom.x - dcx) < CELL_D * 1.3 and abs(self.doom.y - dcy) < CELL_D * 1.3:
                        cmds.append(f"UNLOCK {slot}")
                        self.last_unlock = now
                        print(f"[door] asking Isaac to unlock slot {slot} (keys={st.player['keys']})")
            if door_hit is not None:
                cmds.append(f"DOOR {door_hit}")
                elog(f"[door] isaac doors now: {st.doors}")
                self.doom_state = 1
                self.moves_enabled = False
                self.loading_until = now + LOAD_MIN_SECONDS
                self.door_wait_until = now + DOOR_TIMEOUT
                elog(f"[door] entering slot {door_hit} at doom=({self.doom.x},{self.doom.y}) cell={cell} room={st.room_idx}")
                self.write_doom()
            else:
                ix, iy = self.d2i(self.doom.x, self.doom.y)
                # crawlspace exit: standing on the ladder cell -> let Isaac walk into it
                if cell and st.room_idx is not None and st.room_idx < 0:
                    c, r = cell
                    gi = r * st.w + c
                    if 0 <= c < st.w and 0 <= r < st.h and gi < len(st.grid) and st.grid[gi] == "S":
                        cmds.append("WALK 0 -1")
                cmds.append(f"MOVE {ix:.1f} {iy:.1f}")
                if not self.moved_since_arrival:
                    self.moved_since_arrival = True
                    elog(f"[move] first MOVE after arrival: doom=({self.doom.x},{self.doom.y}) -> isaac=({ix:.0f},{iy:.0f})")
                a = math.radians(self.doom.angle)
                cmds.append(f"AIM {math.cos(a):.3f} {-math.sin(a):.3f}")      # where the crosshair points, every frame
                if self.doom.fire and self.doom.tic != self.last_fire_tic:
                    self.last_fire_tic = self.doom.tic
                    cmds.append(f"FIRE {math.cos(a):.3f} {-math.sin(a):.3f}")
                # one-shot actions fire on the rising edge only (Doom holds the flag for a few tics)
                if self.doom.bomb and not self.prev_bomb:
                    cmds.append("BOMB")
                if self.doom.use and not self.prev_use:
                    cmds.append("USE")
                if self.doom.card and not self.prev_card:
                    cmds.append("CARD")
                if self.doom.drop:
                    cmds.append("DROP")              # held: the mod keeps Isaac's drop button down
                self.prev_bomb, self.prev_use, self.prev_card = self.doom.bomb, self.doom.use, self.doom.card
            self.write_isaac(cmds)
            self.last_cmd_time = now

        # If Doom is not acking our frames (alias loop not running), press the
        # key bound to "exec isaac_in.cfg" ourselves, at most ~30 times a second.
        # (Only with --kick: the boot cfg starts the loop itself, and synthetic key presses land on
        # whatever window is in front - in VR that is not Doom, and it can knock the headset view about.)
        if self.kick is not None and self.doom.f is not None and st.seq >= 0 and now - self.last_kick > 1.0 / 30.0 \
                and (self.doom.ack < 0 or self.out_seq - self.doom.ack > 3):
            self.last_kick = now
            self.kicks_sent += 1
            if self.kicks_sent == 1 or self.kicks_sent % 300 == 0:
                print(f"[doom] no ack from Doom, pressing Scroll Lock (poke #{self.kicks_sent})")
            self.kick()

        # stats
        if now - self.last_stats >= 5.0:
            self.last_stats = now
            if self.lat_samples:
                s = sorted(self.lat_samples)
                med = s[len(s) // 2] * 1000
                p90 = s[int(len(s) * 0.9)] * 1000
                print(f"[stats] room {st.room_idx} isaac seq={st.seq} ents={len(st.ents)} doom tic={self.doom.tic} "
                      f"bridge->doom latency median {med:.0f} ms, p90 {p90:.0f} ms ({len(s)} samples)")
                self.lat_samples.clear()
            else:
                isaac_ok = st.seq >= 0
                doom_ok = self.doom.x is not None
                print(f"[stats] isaac={'ok' if isaac_ok else 'no data'} doom={'ok' if doom_ok else 'no data'} "
                      f"(no latency samples yet)")

    def run(self):
        PIPE.mkdir(parents=True, exist_ok=True)
        # single instance: two bridges fighting over the pipe files break every room transition
        lock = PIPE / "bridge.lock"
        try:
            if lock.exists():
                pid = int(lock.read_text().strip() or "0")
                alive = False
                if pid and os.name == "nt":
                    import ctypes
                    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                    if h:
                        alive = True
                        ctypes.windll.kernel32.CloseHandle(h)
                if alive and pid != os.getpid():
                    print(f"ANOTHER BRIDGE IS ALREADY RUNNING (pid {pid}). Close that window first, then start this one.")
                    return
            lock.write_text(str(os.getpid()))
        except Exception:
            pass
        if not IN_CFG.exists():
            write_atomic(IN_CFG, "set isaac_seq 0\n")
        print(f"IsaacDoom bridge  pipe={PIPE}  mode={'control' if self.control else 'watch-only'}"
              f"{' poke' if self.poke else ''}")
        print("waiting for Isaac (isaac_state.txt) and GZDoom (doom_out.log)... Ctrl+C to stop")
        period = 1.0 / LOOP_HZ
        try:
            while True:
                t0 = time.time()
                self.step()
                dt = time.time() - t0
                if dt < period:
                    time.sleep(period - dt)
        except KeyboardInterrupt:
            if self.control:
                self.write_isaac(["RELEASE"])
            print("\nbye")
        finally:
            try:
                if lock.exists() and lock.read_text().strip() == str(os.getpid()):
                    lock.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="watch-only: do not take control of Isaac")
    ap.add_argument("--poke", action="store_true", help="inject Scroll Lock presses to make GZDoom exec the cfg")
    ap.add_argument("--kick", action="store_true", help="press Scroll Lock whenever Doom stops acking frames (not for VR)")
    args = ap.parse_args()
    Bridge(control=not args.watch, poke=args.poke, kick=args.kick).run()
