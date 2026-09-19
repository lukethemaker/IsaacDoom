# IsaacDoom – hand-off notes

This project was built by describing what I wanted to an AI coding assistant and testing
the result in-game, over a couple of weeks. It works well enough to play whole runs, but
nobody has read every line of it, and I'm not continuing it. If you want to pick it up,
this file is what I'd want to have been told. `README.md` covers installing and playing.

## What it is, in one paragraph

The real game runs in the background and stays authoritative for everything: items,
enemies, damage, room generation, RNG. A Lua mod inside Isaac dumps the state of the
current room to a text file 30 times a second and reads commands back. A Python bridge
turns that into GZDoom console variables (via a cfg file the engine re-executes every tic)
and turns Doom's position, aim and buttons (read from Doom's log file) into commands for
the mod. A ZScript mod in GZDoom builds the room out of sectors, draws every Isaac entity
as a sprite made from the game's own art, and sends the player's input back. Nothing from
the game ships: `Setup.bat` builds all art, sound and music packs from the user's own
copy of the game.

## The three programs and the files between them

```
Isaac (mods/isaacdoom/main.lua)
    writes  pipe/isaac_state.txt   whole room state, every game frame (SEQ, ROOM, GRID, ENT..., see below)
    writes  pipe/isaac_alive.txt   heartbeat: only ticks while a run exists (title screen = silence)
    reads   pipe/isaac_cmd.txt     "SEQ n" + commands: MOVE x y, AIM dx dy, FIRE dx dy, BOMB, USE, CARD,
                                   DROP, DOOR slot, UNLOCK slot, NEWRUN who [seed], CONTINUE, RESTART,
                                   EXIT, RESUME, PAUSE...
bridge (bridge/bridge.py)          one loop, ~60 Hz
    reads   isaac_state.txt        -> IsaacState (parse in IsaacState.parse)
    writes  pipe/isaac_in.cfg      "set isaac_xxx ..." lines; Doom execs this file every tic
    reads   pipe/log-doom_out-log.txt   Doom's +logfile: lines Doom prints ("IS ...", "ISP 1", "ISSEED ...")
    writes  isaac_cmd.txt
    drives Isaac's title menus with posted keystrokes (MenuDriver) because no mod code runs there
Doom (doom/isaacdoom.pk3 = doom/src/* + generated map + rock models)
    isaac_boot.cfg sets up an alias loop that execs isaac_in.cfg every tic
    zscript.txt: IsaacHandler (EventHandler) does everything; IsaacMirror actors are the entities
```

Why files and not sockets: Isaac's Lua has no sockets; with `--luadebug` it has `io`. Doom
has no file API at all, so the only inputs are console cvars (via exec) and the only output
is the log file. Everything else follows from those two constraints.

## The state file (mod -> bridge)

One line per record, `END` last. The important ones:

* `SEQ n` – increments every write. `SEQ -2` means "no run" (menus).
* `FRAME n` – Isaac's frame counter. `FROZEN 1` when the game renders but doesn't update
  (stage card / fade-in at the start of a floor) – Doom shows its own card exactly then.
* `ROOM idx shape type clear w h stage stagetype tlx tly brx bry` – w/h are grid cells
  (15x9 for a normal room). `GRID` is one char per cell (`W` wall, `.` floor, `p` pit, `R`
  rock-like solid, `P` poop/TNT, `d`/`D` closed/open door, `t`/`T` trapdoor, `s` spikes...).
* `ENT hash kind type variant sub x y vx vy size hp maxhp flying boss fire ring champ tint status`
  plus a tab-separated tail with the anm2 file, animation name, frame, flip, overlay
  animation/frame, sprite scale, height... `kind` is one letter: `n` enemy, `t` player tear,
  `p` enemy shot, `k` pickup, `f` familiar, `b` bomb, `e` effect (short), `d` floor effect
  (flat), `l` laser, `s` slot machine, `g` grid. The bridge repacks these into the
  `isaac_ents` cvar as `|`-separated records with `:`-separated fields – the field order is
  in a comment at the top of `applyEntities` in zscript.txt and must match `bridge.py`.
* `GSP` – grid entity sprites (rocks, poop, doors, pits...) with animation and frame.
* `LZR`/`LASER` – beams as polylines; `INTRO` boss VS screen; `FALL` trapdoor fall counter;
  `MUSIC`, `SFX`, `FX`, `PAUSED`, `DEAD`, `HEARTS`, `STATS`, `MAP`, `POPUP`, `OVERLAY`,
  `SEEDS`, `BACKDROP`, `ORIGIN`, `DOOR`, `PRICE`, `BLIND`, `CURSE`.

Isaac units: 40 per grid cell, room floor starts at (60,140). Doom: 64 units per cell, the
room's top-left cell is at (M*64, -M*64) with M = 3 cells of margin; Doom y is negated.
`bridge.py` `i2d()`/`d2i()` convert. Sprites are drawn at `isaac_scale` (1.6 = 40 -> 64)
except grid art (`isaac_gridscale`) and things that must fit a cell exactly (pits: 64/26,
floor chunks: 64/40).

## How each hard problem was solved (so you don't re-solve it)

* **Rooms** – the Doom map is a fixed 34x22 grid of 64-unit sectors (build_pk3.py). Walls
  are sectors with the floor raised to the ceiling; pits are lowered floors; doors are
  raised cells with the door sprite hung on the room-side face. Textures, floors and
  ceilings come from the backdrop sheets (build_sprites.py `build_backdrops`). Per-line
  texture x offsets keep the wall texture continuous across cells.
* **Sprites** – build_sprites.py evaluates every animation of every `.anm2` (layers,
  pivots, delays, tints) into PNG frames with a grAb chunk, deduplicated, named by a
  counter; `ISAACSPR.txt` maps `"<anm2 key>|<animation>"` to frame tokens. Doom sets
  `picnum` directly rather than using sprite state tables. Grid animations are never
  subsampled (a pit's 33 frames are lookups, not motion).
* **Aim and fire** – the mod feeds Isaac's shoot input through `MC_INPUT_ACTION` as analog
  stick values in Doom's aim direction; the mod then checks the tear that came out and
  turns it if the game quantised the aim. Doom's vertical aim is applied only on the Doom
  side (tears are drawn along the pitch they were fired at).
* **Movement** – Doom moves; the bridge sends `MOVE` with Isaac coordinates and the mod
  sets the player's position/velocity. Pits are enforced in Doom the way Isaac does it
  (player centre tested against the cell each tic, pushed to the nearest edge).
* **Pause** – Doom reports `ISP 1/0` from its UI tick; the bridge taps Esc on Isaac's
  window with careful debouncing; the mod can pause but cannot leave the pause menu.
* **Menus / save files** – no mod code runs on the title screen, so the bridge posts
  keystrokes (WM_KEYDOWN with the extended-key flag for arrows) and reads Isaac's own
  `log.txt` to learn which save file was loaded.
* **Floor transitions** – the mod reports the fall (`FALL`), the bridge bumps
  `isaac_stageseq` when a new floor arrives, the mod reports `FROZEN` while Isaac shows
  its card; Doom plays its dream (nightmare art from the UI pack) until the floor
  arrives, then its card while frozen. No fixed timers.
* **Lighting** – fires attach dynamic lights on the mod's `fire` flag; the room's shadow
  overlay is baked into per-room floor flats and also sampled per actor into
  `LightLevel` (cvar `isaac_shadow` scales it). Measured in-game: ~1 basement room in 5
  has an overlay, at ~28% strength (`tools/overlay_probe.py`).
* **Music/SFX** – the game's tracks/sounds are packed under `isaac/<id>`; the mod reports
  the MusicManager's current track and polls `SoundEffect` ids for newly playing sounds,
  guessing a position from what changed that frame. `altmusic\` replaces tracks.
* **Packaging** – `tools/make_release.py` builds a zip with nothing game-derived;
  `Setup.ps1` does the machine-specific work (find Isaac, unpack resources with the game's
  ResourceExtractor, download GZDoom/Freedoom, build packs, install the mod, patch
  options.ini).

## Known rough edges (as of the last session)

* Only tested on Windows, with UZDoom 5.0.1 and the GZDoomVR fork; the sprite-shading
  code uses `Actor.LightLevel`, which needs a GZDoom 4.10+ derivative.
* UZDoom shares key bindings across every mod; `isaac_boot.cfg` re-asserts the ones
  IsaacDoom needs (Mouse1 = fire) each launch.
* Which of the five shadow overlays a room gets is IsaacDoom's own pick (the API doesn't
  expose the game's). Endings and Isaac's real cutscenes are not shown at all.
* Big rooms (2x1, 2x2, L-shapes) work but have had far less testing than 1x1 rooms.
* The menu driver assumes Isaac's title-screen timing; if Isaac swallows a key, it retries
  with focus, but a very slow machine may need the waits in `MenuDriver._run` raised.
* Bosses with many parts (multi-segment worms, Mom's hands) are drawn from whatever
  entities the API exposes; some look wrong.
* `tools/flow_recorder.py` films both windows side by side with the pipe state – use it
  before touching anything timing-related.

## If you change the protocol

Every field added to `ENT` or the ents cvar has to be added in three places: the mod's
`buildState`, the bridge's ENT parser and `ents string` builder, and `applyEntities` in
zscript.txt. The bridge silently dropped fields once and it cost a day; keep the three
comments that list the field order in sync.
