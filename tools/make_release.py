"""Package IsaacDoom for distribution:  python tools\\make_release.py [version]

Writes  dist\\IsaacDoom-<version>.zip  containing only IsaacDoom's own files. Everything
derived from the game - the sprite / UI / sound / music packs, the arena pk3 with its
rock skins, any unpacked resources, the runtime pipe - is left out on purpose: the
person installing builds those from their own copy of Isaac by running Setup.bat.
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# what ships (paths relative to the project root; folders are taken whole, minus the skips)
INCLUDE = [
    "Setup.bat", "Setup.ps1", "IsaacDoom.bat", "IsaacDoomVR.bat", "launch_gzdoom.bat", "launch_gzdoomvr.bat",
    "README.md", "LICENSE", "HANDOFF.md", ".gitignore",
    "bridge/bridge.py",
    "isaac_mod",
    "doom/build_pk3.py", "doom/isaac_boot.cfg", "doom/isaac_vr.cfg", "doom/src", "doom/gfx",
    "tools/paths.py", "tools/build_sprites.py", "tools/build_ui.py", "tools/build_sfx.py", "tools/build_music.py",
    "tools/make_release.py", "tools/overlay_probe.py", "tools/flow_recorder.py",
    "altmusic/README.txt",
]
# never, whatever the include list says: game-derived or per-machine
SKIP_NAMES = {"__pycache__", "pipe", "build", "sprites", "engine", "rocks", "dist", "settings.ini", "settings.bat",
              "flow", "Claude outputs", "overlay_log.csv", "sprite_report.txt"}
SKIP_SUFFIX = {".pk3", ".pyc", ".log", ".lock", ".tmp", ".csv"}


def wanted(p: Path):
    if any(part in SKIP_NAMES for part in p.relative_to(ROOT).parts):
        return False
    if p.suffix.lower() in SKIP_SUFFIX:
        return False
    return True


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"IsaacDoom-{version}.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = ROOT / item
            if p.is_file():
                if wanted(p):
                    z.write(p, f"IsaacDoom/{item}"); n += 1
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and wanted(f):
                        z.write(f, "IsaacDoom/" + f.relative_to(ROOT).as_posix()); n += 1
            else:
                print(f"  (missing: {item})")
    # a last check that nothing game-derived slipped in
    with zipfile.ZipFile(out) as z:
        bad = [i for i in z.namelist() if i.lower().endswith((".pk3", ".wad", ".ogg", ".wav", ".anm2"))
               or "/sprites/" in i.lower() or "/rocks/" in i.lower()]
    if bad:
        print("REFUSING: game-derived files in the archive:", *bad, sep="\n  ")
        out.unlink()
        sys.exit(1)
    print(f"wrote {out} ({n} files, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
