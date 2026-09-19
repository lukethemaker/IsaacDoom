"""
IsaacDoom - build isaacmusic.pk3: the game's music for Doom.

Reads music.xml (Repentance first, then the base game) and packs each track's
main loop as music/IS<id>.ogg, plus an ISMUSIC lump saying which ids loop, so
Doom can switch tracks whenever Isaac does. (Intros and dynamic layers are not
packed: Doom's music player can't chain or mix them.)

Also packs a sped-up, pitched-up copy of every track (music/IS<id>F.ogg) for the unicorn
items, if ffmpeg is on the PATH (or at tools/ffmpeg.exe); ISMUSIC lines read "id loop fast name".

Your own music: drop files into  Documents\IsaacDoom\altmusic\  named after the track
("basement.mid", "burning basement.ogg", "boss.mid"... the names from the game's music.xml,
case and punctuation don't matter; "14.mid" by track id works too). Doom then plays your
file whenever Isaac would play that track - a MIDI goes through Doom's own synth and
soundfont - and the game's original everywhere else. Rerun this after adding files.

Usage:  python build_music.py
"""
import os, sys, zipfile, shutil, subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT as PROJ, GAME
OUT_PK3 = PROJ / "doom" / "isaacmusic.pk3"

XML_DIRS = [GAME / "resources-dlc3", GAME / "resources", PROJ / "sprites"]
MUSIC_DIRS = [GAME / "resources-dlc3" / "music", GAME / "resources" / "music", PROJ / "sprites" / "music"]


def index_dir(d):
    out = {}
    if d.exists():
        for p in d.rglob("*"):
            if p.is_file():
                out[p.relative_to(d).as_posix().lower()] = p
    return out


ALT_DIR = PROJ / "altmusic"
ALT_EXTS = {".mid", ".midi", ".mus", ".ogg", ".mp3", ".flac", ".wav", ".xm", ".it", ".s3m", ".mod"}
FAST_RATE = 1.25          # unicorn: pitch and tempo up together, like the game
CACHE = PROJ / "build" / "music_fast"


def find_ffmpeg():
    here = Path(__file__).resolve().parent
    for cand in (here / "ffmpeg.exe", here / "ffmpeg"):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg")


def fast_variant(ffmpeg, src):
    """resample the track FAST_RATE times faster (cached under build/music_fast)"""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / (src.stem + ".fast.ogg")
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
           "-af", f"aresample=44100,asetrate=44100*{FAST_RATE},aresample=44100",
           "-c:a", "libvorbis", "-q:a", "3", str(out)]
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"  ffmpeg failed on {src.name}: {e}")
        return None
    return out


def main():
    indexes = [index_dir(d) for d in MUSIC_DIRS]
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        print(f"ffmpeg: {ffmpeg} - building sped-up unicorn variants (first run takes a few minutes)")
    else:
        print("ffmpeg not found: no sped-up unicorn variants (put ffmpeg.exe in tools\\ or on the PATH)")

    def find_track(rel):
        rel = rel.replace("\\", "/").lower()
        for ix in indexes:
            if rel in ix:
                return ix[rel]
        return None

    tracks = {}
    for d in XML_DIRS:
        x = d / "music.xml"
        if not x.exists():
            continue
        for t in ET.parse(x).getroot().iter("track"):
            tid = int(t.get("id"))
            if tid in tracks:
                continue
            p = find_track(t.get("path", ""))
            if p:
                tracks[tid] = (p, t.get("loop", "true").lower() == "true", t.get("name", ""))
    print(f"{len(tracks)} tracks found")

    # replacements from altmusic\: matched to a track by name or id
    def norm(t):
        return "".join(ch for ch in t.lower() if ch.isalnum())
    by_name = {norm(name): tid for tid, (_, _, name) in tracks.items() if name}
    alts = {}
    if ALT_DIR.is_dir():
        for f in sorted(ALT_DIR.iterdir()):
            if not f.is_file() or f.suffix.lower() not in ALT_EXTS:
                continue
            key = norm(f.stem)
            tid = by_name.get(key)
            if tid is None and key.startswith("is") and key[2:].isdigit():
                tid = int(key[2:])
            if tid is None and key.isdigit():
                tid = int(key)
            if tid is None or tid not in tracks:
                print(f"  altmusic: {f.name} matches no track (names are those in music.xml, e.g. 'Burning Basement') - skipped")
                continue
            alts[tid] = f
            print(f"  altmusic: {f.name} -> track {tid} ({tracks[tid][2]})")
    OUT_PK3.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PK3.with_suffix(".pk3.tmp")
    zf = zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED)     # ogg is already compressed
    data = []
    for tid, (p, loop, name) in sorted(tracks.items()):
        zf.write(p, f"music/IS{tid}{p.suffix.lower()}")
        fast = fast_variant(ffmpeg, p) if ffmpeg else None
        if fast:
            zf.write(fast, f"music/IS{tid}F.ogg")
        alt = alts.get(tid)
        if alt:
            zf.write(alt, f"music/IA{tid}{alt.suffix.lower()}")
        # "id loop fast alt name"
        data.append(f"{tid} {1 if loop else 0} {1 if fast else 0} {1 if alt else 0} {name}")
    zf.writestr("ISMUSIC.txt", "\n".join(data) + "\n")
    zf.close()
    from paths import replace_when_free
    replace_when_free(tmp, OUT_PK3)
    print(f"wrote {OUT_PK3} ({OUT_PK3.stat().st_size // 1024 // 1024} MB)")


if __name__ == "__main__":
    main()
