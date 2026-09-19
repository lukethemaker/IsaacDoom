# IsaacDoom

Play a real run of *The Binding of Isaac: Repentance* in first person, through GZDoom.

> **Status: released as-is, not maintained.** This was built by describing what I wanted
> to an AI coding assistant and testing it in-game; it plays whole runs, but nobody has
> read every line and I'm not continuing it. It's MIT-licensed — fork it, fix it, take it
> wherever you like. `HANDOFF.md` explains how the pieces fit together and what's rough.

Isaac runs the actual game in the background (every item, enemy, boss and floor behaves
exactly as it does in Isaac, because it *is* Isaac). A small Lua mod streams the room to a
Python bridge, GZDoom renders it in 3D with the game's own art and sounds, and your
movement, aim and fire go back the other way. Doom is the controller and the renderer;
Isaac is the referee.

```
Isaac (Lua mod) --isaac_state.txt-->  bridge.py  --isaac_in.cfg-->  GZDoom (ZScript)
                <--isaac_cmd.txt--              <--doom log--
```

## What you need

* **The Binding of Isaac: Rebirth with the Repentance DLC**, on Steam. IsaacDoom ships
  none of the game's art, sounds or music — Setup builds everything from your own copy,
  on your machine, using the resource extractor that comes with the game.
* **A Doom engine**: GZDoom (Setup can download it) or UZDoom.
* **A Doom IWAD**: Doom II if you own it, otherwise the free **Freedoom** (Setup can
  download it). It only supplies a handful of stand-in textures; you never see Doom.
* **Python 3** (https://www.python.org/downloads/ — tick *Add python to PATH*).
* Windows. About 1 GB of disk for the unpacked resources and the built packs.

## Install

1. Extract the zip so that you have `Documents\IsaacDoom` (Setup will move it there if
   you extract it elsewhere).
2. Close Isaac if it is running, then double-click **`Setup.bat`**. It finds your Isaac,
   unpacks its resources, finds or downloads GZDoom and Freedoom, installs Python's
   Pillow/numpy, builds the Doom-side packs (the sprite pack takes a few minutes the
   first time), installs the mod into Isaac's `mods` folder, sets `AimLock=0` and
   `PauseOnFocusLost=0` in Isaac's `options.ini`, and puts an **IsaacDoom** shortcut on
   your desktop.
3. The one step it cannot do for you: in Steam, right-click Isaac → *Properties* →
   *Launch Options* and enter **`--luadebug`**. That switch lets the mod read and write
   its pipe files. Without it Doom can watch Isaac but not control it.

Re-run `Setup.bat` whenever you update IsaacDoom or Isaac gets an update (it rebuilds
the packs from the new resources). Its choices are saved in `settings.ini` /
`settings.bat`; edit those if you move the engine or the game.

## Play

Double-click the **IsaacDoom** shortcut (or `IsaacDoom.bat`). Isaac starts through Steam
and stays behind; Doom opens on a menu where you pick *New Run*, *Continue*, a
*Challenge*, a *Seed* and which of Isaac's three save files to use — the same choices as
Isaac's own menu, driven from Doom.

| Key | Does |
|---|---|
| WASD / mouse | move / look — Isaac's head follows your look |
| left mouse | fire (any weapon, any angle, including up and down) |
| E | bomb |
| Space | active item |
| Q | card / pill / pocket active |
| Ctrl | drop (tap: swap Schoolbag items / The Forgotten; hold: drop trinket & pocket items) |
| Tab | big map |
| R | new run after dying |
| Esc | pause — pauses Isaac too |

Pits, rocks, doors, shops (with prices), curses, champions, status effects, Brimstone and
the other beams, Tech X, familiars, bosses with their VS screens, the floor transition
with its progress bar, Isaac's HUD and found-HUD stats are all mirrored. If something
looks wrong, a screenshot of Doom next to Isaac's own window is the fastest way to get
it fixed.

### Your own music

Drop a file into `altmusic\` named after the track it should replace — `basement.mid`,
`burning basement.ogg`, `boss.mid`, `shop room.mp3` (the names from the game's `music.xml`;
case and punctuation don't matter, and `14.mid` by track id works too) — and run
`python tools\build_music.py`. Doom then plays your file whenever Isaac would play that
track, a MIDI through Doom's own synth and soundfont, and the game's original music
everywhere else.

### VR

`IsaacDoomVR.bat` launches through a VR build of GZDoom (`gzdoomvr`) instead. Put its
path in `settings.bat` as `GZDOOMVR=`. `doom\isaac_vr.cfg` holds the VR-specific
console settings; `isaac_hudinset` pulls the HUD in from the edges.

## Layout

| Path | What |
|---|---|
| `isaac_mod/isaacdoom/` | the Isaac mod (installed into the game's `mods` folder by Setup) |
| `bridge/bridge.py` | the bridge: coordinate mapping, room transitions, menus, pause link |
| `doom/src/` | ZScript, menus, cvars, translations, light definitions |
| `doom/build_pk3.py` | generates the arena map, the rock models and packs `isaacdoom.pk3` |
| `tools/build_*.py` | build the sprite / UI / sound / music packs from the game's resources |
| `tools/make_release.py` | packages a distributable zip (never includes game-derived files) |
| `tools/overlay_probe.py`, `tools/flow_recorder.py` | measuring tools: how the game uses its room shadows; both windows filmed side by side with the pipe state |
| `altmusic/` | drop your own tracks here (see *Your own music*) |
| `HANDOFF.md` | how it works, decisions made, known rough edges — start here if you want to continue it |
| `pipe/` | runtime files (created automatically) |
| `engine/` | where Setup puts a downloaded GZDoom / Freedoom |

## Troubleshooting

* **Doom sits on a black screen** — the bridge isn't running or Isaac isn't at a run or
  its title screen. `IsaacDoom.bat` starts everything in the right order; check that the
  minimised *IsaacDoom bridge* console window is open.
* **"no data from bridge"** in the Doom HUD — same thing; also make sure `--luadebug` is
  set in Steam.
* **Enemies are Doom monsters** — the sprite pack didn't build. Run `Setup.bat` again and
  read what `tools\build_sprites.py` says; the game's resources must be unpacked
  (`resources-dlc3\gfx` in the game folder).
* **Logs**: `pipe\bridge_log.txt`, `pipe\log-doom_out-log.txt` (Doom), and Isaac's own
  `Documents\My Games\Binding of Isaac Repentance\log.txt`.

## Legal

IsaacDoom's code is MIT-licensed (see `LICENSE`). It contains no assets from The Binding
of Isaac or from Doom II, and it must not be redistributed with any. Everything it draws
is built at setup time from the copy of Isaac you own and stays on your computer. The
Binding of Isaac is © Nicalis / Edmund McMillen; Doom is © id Software.
