"""
IsaacDoom - build isaacsfx.pk3: the game's own sound effects for Doom.

Reads sounds.xml (Repentance first, then the base game for anything missing)
and packs every referenced .wav into doom\isaacsfx.pk3 with a SNDINFO that
names them "isaac/<id>" (random variants included), so Doom can play the real
Isaac sounds positionally through OpenAL.

Usage:  python build_sfx.py
"""
import io, os, sys, zipfile
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT as PROJ, GAME
OUT_PK3 = PROJ / "doom" / "isaacsfx.pk3"

XML_DIRS = [GAME / "resources-dlc3", GAME / "resources", PROJ / "sprites"]
SFX_DIRS = [GAME / "resources-dlc3" / "sfx", GAME / "resources" / "sfx", PROJ / "sprites" / "sfx"]

# sounds that are too quiet through Doom's mixer: SoundEffect id -> gain applied to the
# samples at build time (Doom clamps play volume at 1.0, so louder has to be baked in).
# Loud peaks are soft-limited rather than clipped.
GAIN = {
    156: 2.0,   # SOUND_UNLOCK00: a locked door opening
}


def amplify_wav(path, gain):
    """the .wav at path with its samples scaled by gain (16-bit PCM only), as bytes;
    None when the file isn't plain PCM we can rewrite"""
    import wave
    try:
        import numpy as np
        with wave.open(str(path), "rb") as w:
            ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
        if sw != 2:
            return None
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0 * gain
        # soft knee above 0.8 so a doubled sound doesn't turn into a square wave
        a = np.abs(x)
        knee = a > 0.8
        x = np.where(knee, np.sign(x) * (0.8 + 0.2 * np.tanh((a - 0.8) / 0.2)), x)
        out = np.clip(np.round(x * 32767.0), -32768, 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate); w.writeframes(out)
        return buf.getvalue()
    except Exception as e:
        print(f"  (couldn't amplify {path.name}: {e})")
        return None


def index_dir(d):
    out = {}
    if not d.exists():
        return out
    for p in d.rglob("*"):
        if p.is_file():
            out[p.relative_to(d).as_posix().lower()] = p
    return out


def main():
    indexes = [index_dir(d) for d in SFX_DIRS]

    def find_sample(rel):
        rel = rel.replace("\\", "/").lower()
        for ix in indexes:
            if rel in ix:
                return ix[rel]
        # a few files have stray spaces before the extension
        for ix in indexes:
            for k, v in ix.items():
                if k.replace(" .", ".") == rel.replace(" .", "."):
                    return v
        return None

    sounds = {}   # id -> [paths]
    for d in XML_DIRS:
        x = d / "sounds.xml"
        if not x.exists():
            continue
        root = ET.parse(x).getroot()
        for snd in root.iter("sound"):
            sid = int(snd.get("id"))
            if sid in sounds:
                continue
            paths = []
            for smp in snd.findall("sample"):
                p = find_sample(smp.get("path", ""))
                if p:
                    paths.append(p)
            if paths:
                sounds[sid] = paths
    print(f"{len(sounds)} sounds with files")

    OUT_PK3.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PK3.with_suffix(".pk3.tmp")
    zf = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    sndinfo = ["// IsaacDoom: isaac/<id> = SoundEffect id from sounds.xml"]
    n = 0
    seen_files = {}
    for sid, paths in sorted(sounds.items()):
        names = []
        for k, p in enumerate(paths):
            key = str(p).lower()
            if sid in GAIN:
                key += f"@{GAIN[sid]}"     # its own copy: the same file may serve another id at normal volume
            if key not in seen_files:
                lump = f"sounds/is/{sid}_{k}{p.suffix.lower()}"
                data = amplify_wav(p, GAIN[sid]) if sid in GAIN and p.suffix.lower() == ".wav" else None
                if data is not None:
                    zf.writestr(lump, data)
                    print(f"  isaac/{sid}: {p.name} x{GAIN[sid]}")
                else:
                    zf.write(p, lump)
                seen_files[key] = lump
                n += 1
            lump = seen_files[key]
            nm = f"isaac/{sid}" if len(paths) == 1 else f"isaac/{sid}v{k}"
            sndinfo.append(f'{nm} "{lump}"')
            names.append(nm)
        if len(paths) > 1:
            sndinfo.append(f"$random isaac/{sid} {{ {' '.join(names)} }}")
    zf.writestr("sndinfo.txt", "\n".join(sndinfo) + "\n")
    zf.close()
    from paths import replace_when_free
    replace_when_free(tmp, OUT_PK3)
    print(f"wrote {OUT_PK3} ({OUT_PK3.stat().st_size // 1024 // 1024} MB, {n} files)")


if __name__ == "__main__":
    main()
