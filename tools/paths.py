"""Where things are, for every build tool.

The project must live at  Documents\\IsaacDoom  (the Isaac mod and the bridge talk
through Documents\\IsaacDoom\\pipe, and the mod can't be told anything else), but the
game and the Doom engine can be anywhere: Setup writes their locations to settings.ini
at the project root ("key=value" lines), and an environment variable overrides that.

    isaac   = C:\\...\\The Binding of Isaac Rebirth      (ISAACDOOM_GAME)
    gzdoom  = C:\\...\\gzdoom.exe                        (ISAACDOOM_GZDOOM)
    iwad    = C:\\...\\freedoom2.wad                     (ISAACDOOM_IWAD)

Isaac art comes from the game's own unpacked resources (resources\\gfx and
resources-dlc3\\gfx, created by the game's ResourceExtractor); a copy under
Documents\\IsaacDoom\\sprites (DLC tree at the root, base tree under sprites\\base) is
accepted as a fallback for hand-extracted setups.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\The Binding of Isaac Rebirth")


def settings():
    out = {}
    p = ROOT / "settings.ini"
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip().lower()] = v.strip().strip('"')
    except OSError:
        pass
    return out


_S = settings()
GAME = Path(os.environ.get("ISAACDOOM_GAME") or _S.get("isaac") or DEFAULT_GAME)
GZDOOM = os.environ.get("ISAACDOOM_GZDOOM") or _S.get("gzdoom") or ""
IWAD = os.environ.get("ISAACDOOM_IWAD") or _S.get("iwad") or ""


def gfx_dirs():
    """(DLC gfx tree, base gfx tree) - the game's unpacked resources, else the sprites\\ copy"""
    dlc = GAME / "resources-dlc3" / "gfx"
    base = GAME / "resources" / "gfx"
    if not dlc.is_dir():
        dlc = ROOT / "sprites"
    if not base.is_dir():
        base = ROOT / "sprites" / "base"
    return dlc, base


def unpacked():
    """True when the game's resources have been unpacked (the sprite build needs them)"""
    return (GAME / "resources" / "gfx").is_dir() or (ROOT / "sprites").is_dir()


def replace_when_free(tmp, dst, what="the pack"):
    """os.replace that copes with Doom holding the old file open: asks to close it and retries"""
    import time
    for attempt in range(20):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:
            if attempt == 0:
                print(f"\n{dst} is in use - GZDoom is probably still running with {what} loaded.")
            try:
                input("Close Doom, then press Enter to try again (Ctrl+C gives up; the new build is kept as .tmp)... ")
            except (EOFError, KeyboardInterrupt):
                print(f"left the new build at {tmp}")
                return False
            time.sleep(0.5)
    return False
