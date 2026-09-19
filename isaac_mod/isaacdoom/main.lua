--[[
  IsaacDoom bridge mod (Repentance / Repentance+)

  Every game update (30 Hz) this mod writes a snapshot of the current room to
  <pipe>/isaac_state.txt and reads commands from <pipe>/isaac_cmd.txt.

  REQUIRES the game to be launched with  --luadebug  so the Lua `io` and `os`
  libraries are available (Steam > Isaac > Properties > Launch Options).
  Without it the mod falls back to Isaac.SaveModData for the state dump
  (read-only bridge: Doom can watch Isaac but cannot control it).

  Pipe folder: %USERPROFILE%\Documents\IsaacDoom\pipe\
]]

local mod = RegisterMod("IsaacDoom", 1)
local TEAR_SOUND_VOLUME = 2.0   -- tear shot sound; 1.0 = Isaac's normal level
local SFX_TO_DOOM = true        -- mute Isaac and let Doom play the sounds (needs isaacsfx.pk3)
local MUSIC_TO_DOOM = true      -- same for the music (needs isaacmusic.pk3)

local HAS_IO = (io ~= nil and os ~= nil)

local PIPE_DIR
if HAS_IO then
    local home = os.getenv("USERPROFILE") or "."
    PIPE_DIR = home .. "\\Documents\\IsaacDoom\\pipe\\"
end
local STATE_PATH = PIPE_DIR and (PIPE_DIR .. "isaac_state.txt") or nil
local CMD_PATH   = PIPE_DIR and (PIPE_DIR .. "isaac_cmd.txt") or nil

local seq = 0
local lastCmdSeq = -1
local renderTick = 0            -- POST_RENDER frame counter
local deathTick = -1            -- render frame Isaac was first seen dead on (-1 = alive)
local inMenu, runMenuScript, applyMenuCommands   -- defined further down (menu driving)
local laserLogged = false       -- one diagnostic line about the first laser seen
local laserListLogged = false
local samplesLogged = false
local laserAim = {}             -- laser hash -> { angle, vel }: shots turned to the real aim (see reaimLaser)
-- ANALOG_AIM: feed Isaac the real aim as analog stick values (through a gamepad slot) instead
-- of a cardinal press, so every weapon aims natively; shots are still checked and turned if
-- the game quantised them anyway (see aimDelta / noteShotDirection)
local ANALOG_AIM = true
local ANALOG_MIRROR = false     -- (tears go the right way unmirrored) the game reads a gamepad slot's analog shoot values with the opposite
                                -- sign to the digital ones (observed: beams left on the exact opposite
                                -- heading); hand it mirrored values so the aim lands where Doom points
local savedControllerIndex = nil
local noteShotDirection         -- defined with the aim code below
local lastNewRunWho, lastNewRunAt = -1, -100000   -- dedupe NEWRUN repeats (bridge re-sends it once the run exists)
local controlTaken = false      -- true once Doom has sent us a MOVE
local frozenNow = false         -- rendering without updating (stage card / fade-in)
local frozenLastFrame, frozenCount = -1, 0
local wantPause = false         -- Doom is paused: hold Isaac in its pause menu too
local pressPauseFrame = -1      -- render frame on which the pause button is pressed for the game
local lastPauseToggle = -100
local fireDir = nil             -- aim direction from Doom (Isaac coords) at the last fire press
local aimDir = nil              -- where Doom's crosshair points right now (every frame)
local IDLE_AIM = 0.2            -- stick deflection fed while not shooting (0 = off); must stay under the fire deadzone
local cardinal                  -- (defined below) nearest axis direction of a vector
local fireHoldUntil = -1        -- frame until which the shoot input is held
local fireStartFrame = -1
local FIRE_HOLD_FRAMES = 3      -- Doom sends FIRE every tic while held; bridge gaps up to this many frames
local dropHoldUntil = -1        -- frame until which Isaac's drop button (Ctrl) is held for Doom
local dropStartFrame = -1
local seenForms = {}            -- transformations already announced this run
local pendingSeed = nil         -- seed to apply once the requested new run has started
local lastTintLog = -999        -- (diagnostic) last frame a tear's colour was logged
local lastStatusLog = -999      -- (diagnostic) last frame an enemy's status flags were logged
local aimDelta, firedByUs       -- defined with the input hook below; used by the aim callbacks above it
local walkDir = nil             -- forced walking input (crawlspace ladders): Vector
local walkUntil = -1
local lastCmdFrame = 0          -- last game frame on which a command arrived
local fireCooldown = 0
local newRoomFlag = 0           -- set to frame count when a room is entered
local runCounter = 0            -- bumped on every game start / continue so the bridge re-syncs
local introSeq = 0              -- boss "VS" screen: bumped when Isaac plays one
local introLine = "INTRO 0 0 0 0 00"
local introFlushRender = false  -- write the state once from POST_RENDER (the game is paused during the intro)
local fallSeq = 0               -- bumped when Isaac starts falling through a trapdoor
local fallingNow = false

local function log(s)
    Isaac.DebugString("[IsaacDoom] " .. tostring(s))
end

-------------------------------------------------------------------------------
-- Grid cell classification -> single character used by the Doom side
--   '.' floor   'W' wall   'R' rock (blocks)   'P' poop/low object
--   'p' pit     's' spikes 'D' open door       'd' closed/locked door
--   'T' open trapdoor (deep shaft)  't' closed trapdoor  'S' crawlspace ladder
--   'x' outside the room (L-shapes)
-------------------------------------------------------------------------------
local function cellChar(room, i)
    local g = room:GetGridEntity(i)
    if g == nil then return "." end
    local t = g:GetType()
    if t == GridEntityType.GRID_WALL then
        return "W"
    elseif t == GridEntityType.GRID_DOOR then
        local d = g:ToDoor()
        if d and d:IsOpen() then return "D" else return "d" end
    elseif t == GridEntityType.GRID_PIT then
        -- state 1 = filled/bridged pit in some variants; treat collision as truth
        if g.CollisionClass == GridCollisionClass.COLLISION_PIT then return "p" else return "." end
    elseif t == GridEntityType.GRID_SPIKES or t == GridEntityType.GRID_SPIKES_ONOFF then
        return "s"
    elseif t == GridEntityType.GRID_TRAPDOOR then
        -- State 1 = open (falls through to the next floor), 0 = still closed
        if g.State == 1 then return "T" else return "t" end
    elseif t == GridEntityType.GRID_STAIRS then
        return "S"
    elseif t == GridEntityType.GRID_POOP or t == GridEntityType.GRID_TNT then
        if g.CollisionClass == GridCollisionClass.COLLISION_NONE then return "." end
        return "P"
    elseif t == GridEntityType.GRID_DECORATION or t == GridEntityType.GRID_SPIDERWEB
        or t == GridEntityType.GRID_PRESSURE_PLATE or t == GridEntityType.GRID_GRAVITY then
        return "."
    else
        -- rocks of all kinds, statues, pillars, locks, fireplaces...
        local cc = g.CollisionClass
        if cc == GridCollisionClass.COLLISION_NONE then return "." end
        -- key blocks, statues and pillars collide like walls in Isaac but are props with their own art:
        -- a solid block, not a floor-to-ceiling wall
        if t == GridEntityType.GRID_LOCK or t == GridEntityType.GRID_STATUE or t == GridEntityType.GRID_PILLAR
            or t == GridEntityType.GRID_TELEPORTER then return "R" end
        if cc == GridCollisionClass.COLLISION_WALL or cc == GridCollisionClass.COLLISION_WALL_EXCEPT_PLAYER then return "W" end
        return "R"
    end
end

-------------------------------------------------------------------------------
-- Entity kind codes
-------------------------------------------------------------------------------
local FLOOR_EFFECTS = {
    [5] = true, [7] = true, [42] = true, [58] = true, [70] = true, [163] = true,       -- blood / poop bits and stains
    [4] = true, [27] = true,                                                          -- rock and wood chunks: they land and lie on the floor
    [22] = true, [23] = true, [24] = true, [25] = true, [26] = true, [56] = true,      -- enemy creep
    [94] = true, [155] = true, [169] = true, [195] = true, [168] = true,
    [32] = true, [37] = true, [44] = true, [45] = true, [46] = true, [53] = true,      -- player creep
    [54] = true, [78] = true, [90] = true, [92] = true,
    [140] = true,                                                                     -- backdrop floor decorations
}
-- Black Powder: the ash trail and the pentagram it closes into are floor effects too
pcall(function()
    if EffectVariant.BLACK_POWDER then FLOOR_EFFECTS[EffectVariant.BLACK_POWDER] = true end
    if EffectVariant.PENTAGRAM_BLACKPOWDER then FLOOR_EFFECTS[EffectVariant.PENTAGRAM_BLACKPOWDER] = true end
    -- the scorch mark a bomb leaves on the floor
    if EffectVariant.BOMB_CRATER then FLOOR_EFFECTS[EffectVariant.BOMB_CRATER] = true end
end)

local function entityKind(e)
    local t = e.Type
    if t == EntityType.ENTITY_PLAYER then return nil end
    if t == EntityType.ENTITY_TEAR then return "t" end
    if t == EntityType.ENTITY_PROJECTILE then return "p" end
    if t == EntityType.ENTITY_PICKUP then return "k" end
    if t == EntityType.ENTITY_BOMB then return "b" end
    if t == EntityType.ENTITY_FAMILIAR then return "f" end
    if t == EntityType.ENTITY_LASER then return "l" end
    if t == EntityType.ENTITY_KNIFE then return "K" end
    if t == EntityType.ENTITY_SLOT then return "s" end
    if t == EntityType.ENTITY_EFFECT then
        local v = e.Variant
        -- things lying on the floor: creep puddles, blood / poop splats and gibs, floor decorations
        if FLOOR_EFFECTS[v] then return "d" end
        -- only the short one-shot effects worth seeing in 3D
        if v == EffectVariant.BOMB_EXPLOSION or v == EffectVariant.BLOOD_EXPLOSION
            or v == EffectVariant.FLY_EXPLOSION or v == EffectVariant.BULLET_POOF
            or v == EffectVariant.TEAR_POOF_A or v == EffectVariant.TEAR_POOF_B
            or v == EffectVariant.TEAR_POOF_SMALL or v == EffectVariant.TEAR_POOF_VERYSMALL
            or v == EffectVariant.RIPPLE_POOF or v == EffectVariant.POOF04 or v == EffectVariant.ROCK_POOF
            or v == EffectVariant.POOF01 or v == EffectVariant.POOF02
            or v == EffectVariant.ROCK_EXPLOSION or v == EffectVariant.POOP_EXPLOSION
            or v == EffectVariant.DUST_CLOUD
            or v == EffectVariant.LARGE_BLOOD_EXPLOSION or v == EffectVariant.ENEMY_SOUL then
            return "e"
        end
        return nil
    end
    if t >= 10 and t < 1000 then return "n" end
    return nil
end

local function fmt(v) return string.format("%.1f", v) end

-- Repentance hands back localisation keys like "#THE_D6_NAME": resolve them
local localizeViaTable = nil   -- set below once the stringtable loader exists
local function localize(name)
    if type(name) ~= "string" or name:sub(1, 1) ~= "#" then return name end
    if localizeViaTable then
        local v = localizeViaTable(name)
        if v ~= name then return v end
    end
    if Isaac.GetLocalizedString then
        for _, cat in ipairs({ "Items", "PocketItems", "Pills", "Cards", "Trinkets" }) do
            for _, key in ipairs({ name, name:sub(2) }) do
                for _, lang in ipairs({ "en_us", "en" }) do
                    local ok, s = pcall(Isaac.GetLocalizedString, cat, key, lang)
                    if ok and type(s) == "string" and s ~= "" and s:sub(1, 1) ~= "#" and s ~= key then return s end
                end
            end
        end
    end
    -- fallback: "#THE_D6_NAME" -> "The D6"
    local s = name:sub(2):gsub("_NAME$", ""):gsub("_DESCRIPTION$", ""):gsub("_", " "):lower()
    return (s:gsub("(%a)([%w]*)", function(a, b) return a:upper() .. b end))
end

-- Real names/descriptions parsed from the game's own items.xml / pocketitems.xml
-- (GetLocalizedString only hands back keys for these in Repentance).
local xmlText = { items = {}, trinkets = {}, cards = {}, pills = {} }
local xmlLoaded = false

local function xmlDecode(s)
    return (s:gsub("&amp;", "&"):gsub("&quot;", '"'):gsub("&apos;", "'"):gsub("&lt;", "<"):gsub("&gt;", ">"):gsub("&#(%d+);", function(n) return string.char(tonumber(n) % 256) end))
end

local function attr(tag, name)
    local v = tag:match("%s" .. name .. '%s*=%s*"([^"]*)"')
    if v then return xmlDecode(v) end
    return nil
end

local function parseXml(text, spec)
    -- spec: { {tags = {"passive","active","familiar"}, into = "items"}, ... }
    local count = 0
    for tag in text:gmatch("<([^<>]+)/?>") do
        local tname = tag:match("^%s*(%a+)")
        if tname then
            for _, s in ipairs(spec) do
                for _, want in ipairs(s.tags) do
                    if tname == want then
                        local id = tonumber(attr(tag, "id") or "")
                        local nm = attr(tag, "name")
                        if id and nm then
                            xmlText[s.into][id] = { name = nm, desc = attr(tag, "description") or "" }
                            count = count + 1
                        end
                    end
                end
            end
        end
    end
    return count
end

local function readFile(path)
    local ok, f = pcall(io.open, path, "r")
    if not ok or not f then return nil end
    local t = f:read("*a"); f:close()
    return t
end

local function loadXmlTables()
    if xmlLoaded then return end
    xmlLoaded = true
    local home = (os.getenv and os.getenv("USERPROFILE")) or "C:\\Users\\lukeb"
    local docs = home .. "\\Documents"
    local candidates = {
        "resources-dlc3\\", "resources\\",
        docs .. "\\IsaacDoom\\sprites\\", docs .. "\\IsaacDoom\\",
        docs .. "\\My Games\\Binding of Isaac Repentance\\mods\\isaacdoom\\",
        "C:\\Program Files (x86)\\Steam\\steamapps\\common\\The Binding of Isaac Rebirth\\resources-dlc3\\",
        "C:\\Program Files (x86)\\Steam\\steamapps\\common\\The Binding of Isaac Rebirth\\resources\\",
    }
    local gotItems, gotPocket = false, false
    for _, dir in ipairs(candidates) do
        if not gotItems then
            local t = readFile(dir .. "items.xml")
            if t and parseXml(t, {
                    { tags = { "passive", "active", "familiar" }, into = "items" },
                    { tags = { "trinket" }, into = "trinkets" } }) > 0 then
                gotItems = true
                Isaac.DebugString("[isaacdoom] loaded items.xml from " .. dir)
            end
        end
        if not gotPocket then
            local t = readFile(dir .. "pocketitems.xml")
            if t and parseXml(t, {
                    { tags = { "card", "rune" }, into = "cards" },
                    { tags = { "pilleffect" }, into = "pills" } }) > 0 then
                gotPocket = true
                Isaac.DebugString("[isaacdoom] loaded pocketitems.xml from " .. dir)
            end
        end
    end
    if not gotItems then Isaac.DebugString("[isaacdoom] items.xml not found - descriptions unavailable") end
end

-- "#WALNUT_DESCRIPTION" style keys resolve through the game's stringtable.sta (plain XML)
local strings = {}
local stringsLoaded = false
local function loadStrings()
    if stringsLoaded then return end
    stringsLoaded = true
    local home = (os.getenv and os.getenv("USERPROFILE")) or "C:\\Users\\lukeb"
    for _, path in ipairs({
        "resources\\stringtable.sta",
        "C:\\Program Files (x86)\\Steam\\steamapps\\common\\The Binding of Isaac Rebirth\\resources\\stringtable.sta",
        home .. "\\Documents\\IsaacDoom\\sprites\\stringtable.sta" }) do
        local t = readFile(path)
        if t then
            local n = 0
            for key, str in t:gmatch('<key name="([^"]+)">%s*<string>([^<]*)</string>') do
                if strings[key] == nil then strings[key] = xmlDecode(str); n = n + 1 end
            end
            Isaac.DebugString("[isaacdoom] loaded " .. n .. " strings from " .. path)
            if n > 0 then return end
        end
    end
    Isaac.DebugString("[isaacdoom] stringtable.sta not found")
end

-- fortunes (the game picks its own; we show one from the same list)
local fortunes = nil
local function randomFortune()
    if fortunes == nil then
        fortunes = {}
        for _, path in ipairs({ "resources\\stringtable.sta",
            "C:\\Program Files (x86)\\Steam\\steamapps\\common\\The Binding of Isaac Rebirth\\resources\\stringtable.sta" }) do
            local t = readFile(path)
            if t then
                local block = t:match('<category name="Fortune_General">(.-)</category>')
                if block then
                    for str in block:gmatch('<key name="[^"]+">%s*<string>([^<]*)</string>') do
                        fortunes[#fortunes + 1] = xmlDecode(str)
                    end
                end
                break
            end
        end
    end
    if #fortunes == 0 then return "the future is|uncertain" end
    return fortunes[math.random(#fortunes)]
end

local function resolveKey(s)
    if type(s) == "string" and s:sub(1, 1) == "#" then
        loadStrings()
        local v = strings[s:sub(2)]
        if v then return v end
    end
    return s
end

localizeViaTable = resolveKey

local function xmlEntry(kind, id)
    loadXmlTables()
    local e = xmlText[kind][id]
    if e and not e.resolved then
        e.resolved = true
        e.name = resolveKey(e.name)
        e.desc = resolveKey(e.desc)
    end
    return e
end

-- strip characters the pipe format uses
local function clean(name)
    if not name then return "" end
    return (tostring(localize(name)):gsub("[|:,;\"\n\r]", " "))
end

-- Finger!'s hit detection is a laser the game never draws (Visible=false): thin red, ~110 px
-- long, starting at the player. Not part of the look, and not ours to re-aim.
local function isFingerLaser(e)
    local ok, res = pcall(function()
        if e.Visible then return false end
        local pl = Isaac.GetPlayer(0)
        if not pl or not pl:HasCollectible(467) then return false end
        local l = e:ToLaser()
        if not l then return false end
        if (e.Position - pl.Position):Length() > 70 then return false end
        local ep = l:GetEndPoint()
        return ep ~= nil and (ep - e.Position):Length() < 200
    end)
    return ok and res
end

local function pillName(color, player)
    if not color or color <= 0 then return nil end
    local pool = Game():GetItemPool()
    if pool:IsPillIdentified(color) then
        local eff = pool:GetPillEffect(color, player)
        local x = xmlEntry("pills", eff)
        if x then return x.name end
        local cfg = Isaac.GetItemConfig():GetPillEffect(eff)
        return cfg and cfg.Name or "Pill"
    end
    return "??? Pill"
end

-- name (and description) of an item by kind/id, preferring the xml tables
local function itemText(kind, id, cfgItem)
    local x = xmlEntry(kind, id)
    if x then return x.name, x.desc end
    if cfgItem then return cfgItem.Name, cfgItem.Description or "" end
    return nil, ""
end

local function pickupName(e, player)
    local cfg = Isaac.GetItemConfig()
    if e.Variant == PickupVariant.PICKUP_COLLECTIBLE and e.SubType > 0 then
        return (itemText("items", e.SubType, cfg:GetCollectible(e.SubType)))
    elseif e.Variant == PickupVariant.PICKUP_TAROTCARD and e.SubType > 0 then
        return (itemText("cards", e.SubType, cfg:GetCard(e.SubType)))
    elseif e.Variant == PickupVariant.PICKUP_PILL then
        return pillName(e.SubType, player)
    elseif e.Variant == PickupVariant.PICKUP_TRINKET and e.SubType > 0 then
        return (itemText("trinkets", e.SubType, cfg:GetTrinket(e.SubType)))
    end
    return nil
end

local fxQueue = {}   -- one-shot events (explosions) reported with the next snapshot
local popupQueue = {}   -- "name|description" for items just picked up
local knownItems = nil  -- collectible id -> count, to detect pickups
local knownTrinkets = nil
local knownTotal = -1

local lastQueued = nil          -- ItemConfig item currently held above the head
local recentPop = {}            -- "kind:id" -> frame it was popped from the queue

local function checkQueued(player)
    -- Isaac shows the streak the moment the item is touched (it sits above the head
    -- for a while before it lands in the inventory), so watch the queue, not the counts
    local q = player.QueuedItem
    local item = q and q.Item or nil
    if item ~= lastQueued then
        lastQueued = item
        if item then
            local kind = item:IsTrinket() and "trinkets" or "items"
            local nm, ds = itemText(kind, item.ID, item)
            if nm then
                popupQueue[#popupQueue + 1] = clean(nm) .. "|" .. clean(ds)
                recentPop[kind .. ":" .. item.ID] = Game():GetFrameCount()
            end
        end
    end
end

local function poppedRecently(kind, id)
    local f = recentPop[kind .. ":" .. id]
    return f and Game():GetFrameCount() - f < 150
end

local function checkPickups(player)
    checkQueued(player)
    local cfg = Isaac.GetItemConfig()
    local n = cfg:GetCollectibles().Size
    if knownItems == nil then
        knownItems = {}
        for id = 1, n - 1 do knownItems[id] = player:GetCollectibleNum(id, true) end
        knownTrinkets = { player:GetTrinket(0), player:GetTrinket(1) }
        knownTotal = player:GetCollectibleCount()
        return
    end
    local total = player:GetCollectibleCount()
    if total ~= knownTotal then
        knownTotal = total
        for id = 1, n - 1 do
            local c = player:GetCollectibleNum(id, true)
            local old = knownItems[id] or 0
            if c > old then
                local nm, ds = itemText("items", id, cfg:GetCollectible(id))
                if nm and not poppedRecently("items", id) then popupQueue[#popupQueue + 1] = clean(nm) .. "|" .. clean(ds) end
            end
            if c ~= old then knownItems[id] = c end
        end
    end
    for slot = 0, 1 do
        local t = player:GetTrinket(slot)
        if t ~= knownTrinkets[slot + 1] then
            if t and t > 0 then
                local nm, ds = itemText("trinkets", t % 32768, cfg:GetTrinket(t % 32768))
                if nm and not poppedRecently("trinkets", t % 32768) then popupQueue[#popupQueue + 1] = clean(nm) .. "|" .. clean(ds) end
            end
            knownTrinkets[slot + 1] = t
        end
    end
end

-------------------------------------------------------------------------------
-- Door skins: the game keeps the door anm2 but swaps its sheet for special
-- rooms and stages. Doom has those renders under "<anm2>@<skin>".
-------------------------------------------------------------------------------
function doorSkin(g, fn, level)
    local d = g:ToDoor()
    if not d then return "" end
    local lfn = fn:lower()
    local stage, stype = level:GetStage(), level:GetStageType()
    local tt = d.TargetRoomType
    if lfn:find("door_01_normaldoor") then
        if tt == RoomType.ROOM_SHOP then return "door_00_shopdoor" end
        if tt == RoomType.ROOM_LIBRARY then return "door_13_librarydoor" end
        if tt == RoomType.ROOM_SACRIFICE then return "door_00_sacrificeroomdoor" end
        if tt == RoomType.ROOM_DICE then return "door_00_diceroomdoor" end
        if stage <= 2 then
            if stype == 1 then return "door_12_cellardoor" end
            if stype == 2 then return "door_01_burningbasement" end
        elseif stage <= 4 then
            if stype == 2 then return "door_27_drownedcaves" end
        elseif stage <= 6 then
            if stype == 5 then return "door_01_gehennadoor" end
            return "door_14_depthsdoor"
        elseif stage <= 8 then
            if stype == 2 then return "door_28_scarredroomdoor" end
            if stype == 4 or stype == 5 then return "door_01_corpsedoor" end
            return "door_25_wombdoor"
        elseif stage == 9 then return "door_01_bluewombdoor"
        elseif stage == 10 then return stype == 1 and "door_22_cathedraldoor" or "door_19_sheoldoor"
        elseif stage == 11 then return stype == 1 and "door_23_chestdoor" or "door_21_darkroomdoor"
        end
    elseif lfn:find("door_08_holeinwall") then
        if stage >= 3 and stage <= 4 then return "door_08_holeinwall_caves"
        elseif stage >= 5 and stage <= 6 then return "door_08_holeinwall_depths"
        elseif stage >= 7 and stage <= 8 then
            if stype == 1 then return "door_08_holeinwall_utero" end
            if stype == 4 or stype == 5 then return "door_08_holeinwall_corpse" end
            return "door_08_holeinwall_womb"
        elseif stage == 10 and stype == 1 then return "door_08_holeinwall_cathedral"
        elseif stage == 11 and stype == 0 then return "door_08_holeinwall_darkroom"
        end
    elseif lfn:find("door_02_treasureroomdoor") then
        if tt == RoomType.ROOM_CHEST then return "door_02b_chestroomdoor" end
    end
    return ""
end

-------------------------------------------------------------------------------
-- Heart row, as Isaac draws it. One token per heart slot, left to right:
--   R0/R1/R2 red container (empty/half/full)   T1/T2 rotten red   C0..2 coin (Keeper)
--   B0/B1/B2 bone container                    U1/U2 rotten bone
--   S1/S2 soul heart   K1/K2 black heart   X broken heart   H holy mantle
-- suffix 'e' = eternal half-heart overlay, 'g' = golden overlay
-------------------------------------------------------------------------------
local function bit(mask, i)
    return math.floor(mask / (2 ^ i)) % 2 == 1
end

function heartTokens(player)
    local toks = {}
    local maxH = player:GetMaxHearts()
    local red = player:GetHearts()
    local rotten = (player.GetRottenHearts and player:GetRottenHearts()) or 0
    local bone = player:GetBoneHearts()
    local soul = player:GetSoulHearts()
    local eternal = player:GetEternalHearts()
    local golden = (player.GetGoldenHearts and player:GetGoldenHearts()) or 0
    local broken = (player.GetBrokenHearts and player:GetBrokenHearts()) or 0
    local black = player:GetBlackHearts()
    local ptype = player:GetPlayerType()
    local keeper = ptype == PlayerType.PLAYER_KEEPER or ptype == PlayerType.PLAYER_KEEPER_B
    local containers = math.floor(maxH / 2) + bone
    local redLeft = red
    local rottenFrom = red - rotten * 2      -- rotten hearts are the last red ones
    for i = 0, containers - 1 do
        local fill = math.max(0, math.min(2, redLeft))
        local isRot = fill > 0 and (i * 2) >= rottenFrom
        redLeft = redLeft - fill
        local t
        if player:IsBoneHeart(i) then t = (isRot and "U" or "B") .. fill
        elseif keeper then t = "C" .. fill
        else t = (isRot and "T" or "R") .. fill end
        toks[#toks + 1] = t
    end
    if eternal > 0 and #toks > 0 then
        local idx = #toks
        for i, t in ipairs(toks) do
            if t:sub(2, 2) ~= "2" then idx = i; break end
        end
        toks[idx] = toks[idx] .. "e"
    end
    local left = soul
    local slot = 0
    while left > 0 do
        local fill = math.min(2, left)
        left = left - fill
        toks[#toks + 1] = (bit(black, slot) and "K" or "S") .. fill
        slot = slot + 1
    end
    for n = 1, math.min(golden, #toks) do
        local i = #toks - n + 1
        toks[i] = toks[i] .. "g"
    end
    for _ = 1, broken do toks[#toks + 1] = "X" end
    local fx = player:GetEffects()
    if fx and fx:HasCollectibleEffect(CollectibleType.COLLECTIBLE_HOLY_MANTLE) then toks[#toks + 1] = "H" end
    return table.concat(toks, " ")
end

-------------------------------------------------------------------------------
-- State dump
-------------------------------------------------------------------------------
-------------------------------------------------------------------------------
-- Sounds: Isaac has no "sound started" callback, so poll SFXManager:IsPlaying
-- for every id each frame and report new ones. Where we can, attribute them to
-- a thing in the room (an enemy that just died / started an animation, a tear
-- that just vanished, a door that just moved, a bomb that just went off) so
-- Doom can place them; the rest play at the listener.
-------------------------------------------------------------------------------
local SFX_MAX = 900
local sfxWasPlaying = {}
local sfxQueue = {}             -- { id, x, y } (x = -1: at the listener)
local sfxNames = {}             -- id -> enum name, for the attribution heuristics
for k, v in pairs(SoundEffect) do
    if type(v) == "number" then sfxNames[v] = k end
end
local prevEnts = {}             -- hash -> { kind, x, y, anim, frame } from last frame
local prevDoors = {}            -- slot -> open flag
local isaacMuted = false
local savedSfxVolume = nil
local VOLUME_FILE = (PIPE_DIR or "") .. "sfx_volume.txt"

local savedMusicVolume = nil
local function muteIsaac(on)
    if on and not isaacMuted then
        savedSfxVolume = Options.SFXVolume
        savedMusicVolume = Options.MusicVolume
        pcall(function()
            local f = io.open(VOLUME_FILE, "w")
            if f then f:write(tostring(savedSfxVolume or 0) .. "," .. tostring(savedMusicVolume or 0)); f:close() end
        end)
        if savedSfxVolume and savedSfxVolume > 0 then Options.SFXVolume = 0 end
        -- music: fade the manager itself rather than the option (the option at 0 also
        -- makes the manager report no track, and Doom needs the track id)
        if MUSIC_TO_DOOM then pcall(function() MusicManager():VolumeSlide(0, 0.5) end) end
        isaacMuted = true
    elseif not on and isaacMuted then
        if savedSfxVolume and savedSfxVolume > 0 then Options.SFXVolume = savedSfxVolume end
        if savedMusicVolume and savedMusicVolume > 0 then Options.MusicVolume = savedMusicVolume end
        pcall(function() MusicManager():VolumeSlide(1, 0.5) end)
        pcall(os.remove, VOLUME_FILE)
        isaacMuted = false
    end
end

-- a crash while muted must not leave the game silent forever
pcall(function()
    local f = io.open(VOLUME_FILE, "r")
    if f then
        local txt = f:read("*a"); f:close()
        local sv, mv = txt:match("([%d%.]+),([%d%.]+)")
        sv = tonumber(sv or txt); mv = tonumber(mv)
        if sv and sv > 0 and (Options.SFXVolume or 0) == 0 then Options.SFXVolume = sv end
        if mv and mv > 0 and (Options.MusicVolume or 0) == 0 then Options.MusicVolume = mv end
        os.remove(VOLUME_FILE)
    end
end)

local function nameHas(nm, ...)
    for _, pat in ipairs({ ... }) do
        if nm:find(pat, 1, true) then return true end
    end
    return false
end

local function nearestTo(list, px, py)
    local best, bd = nil, 1e12
    for _, c in ipairs(list) do
        local d = (c.x - px) ^ 2 + (c.y - py) ^ 2
        if d < bd then bd = d; best = c end
    end
    return best
end

-- called once per frame from buildState with this frame's entity snapshot
local function collectSounds(player, curEnts, room)
    local sfx = SFXManager()
    local px, py = player.Position.X, player.Position.Y
    -- what changed since last frame
    local removed = {}      -- { kind, x, y }
    local animStart = {}    -- NPCs whose animation just started/changed
    for h, e in pairs(prevEnts) do
        local now = curEnts[h]
        if not now then removed[#removed + 1] = e
        elseif e.kind == "n" and (now.anim ~= e.anim or (now.frame == 0 and e.frame ~= 0)) then
            animStart[#animStart + 1] = now
        end
    end
    local doorsMoved = {}
    for slot = 0, 7 do
        local d = room:GetDoor(slot)
        if d then
            local o = d:IsOpen()
            if prevDoors[slot] ~= nil and prevDoors[slot] ~= o then
                local gp = room:GetGridPosition(d:GetGridIndex())
                doorsMoved[#doorsMoved + 1] = { x = gp.X, y = gp.Y }
            end
            prevDoors[slot] = o
        end
    end
    local function ofKind(list, k)
        local out = {}
        for _, c in ipairs(list) do if c.kind == k then out[#out + 1] = c end end
        return out
    end
    for id = 1, SFX_MAX do
        local playing = sfx:IsPlaying(id)
        if playing and not sfxWasPlaying[id] then
            local nm = sfxNames[id] or ""
            local src = nil
            if nameHas(nm, "DEATH", "BURST", "GIB", "SPLATTER", "MEAT") then
                src = nearestTo(ofKind(removed, "n"), px, py)
            elseif nameHas(nm, "TEAR", "TEARIMPACT", "BLOOD_LASER", "SPLAT") then
                src = nearestTo(ofKind(removed, "t"), px, py) or nearestTo(ofKind(removed, "p"), px, py)
            elseif nameHas(nm, "EXPLOSION", "BOOM", "BOMB") then
                src = nearestTo(ofKind(removed, "b"), px, py)
            elseif nameHas(nm, "DOOR", "UNLOCK", "SECRET") then
                src = nearestTo(doorsMoved, px, py)
            elseif nameHas(nm, "PICKUP", "COIN", "KEY_", "PENNY", "HEART", "CHEST", "ITEM") then
                src = nearestTo(ofKind(removed, "k"), px, py)
            elseif nameHas(nm, "ROCK", "CRUMBLE", "POT_BREAK", "POOP") then
                src = nearestTo(ofKind(removed, "g"), px, py)
            end
            if not src then src = nearestTo(animStart, px, py) end
            if not src and nameHas(nm, "MONSTER", "BOSS", "ROAR", "GRUNT", "HISS", "SPIT", "SCREAM", "WORM", "SPIDER", "FLY", "GURG", "MAGGOT") then
                src = nearestTo(ofKind(curEnts, "n"), px, py)
            end
            if src then sfxQueue[#sfxQueue + 1] = { id, src.x, src.y }
            else sfxQueue[#sfxQueue + 1] = { id, -1, -1 } end
        end
        sfxWasPlaying[id] = playing
    end
    prevEnts = curEnts
end

local function buildState()
    local game = Game()
    local room = game:GetRoom()
    local level = game:GetLevel()
    local player = Isaac.GetPlayer(0)
    local tl = room:GetTopLeftPos()
    local br = room:GetBottomRightPos()
    local w, h = room:GetGridWidth(), room:GetGridHeight()

    local out = {}
    seq = seq + 1
    out[#out + 1] = "SEQ " .. seq
    out[#out + 1] = "FRAME " .. game:GetFrameCount()
    out[#out + 1] = string.format("ROOM %d %d %d %d %d %d %d %d %s %s %s %s",
        level:GetCurrentRoomIndex(), room:GetRoomShape(), room:GetType(),
        room:IsClear() and 1 or 0, w, h, level:GetStage(), level:GetStageType(),
        fmt(tl.X), fmt(tl.Y), fmt(br.X), fmt(br.Y))
    out[#out + 1] = string.format("NEWROOM %d", newRoomFlag)
    out[#out + 1] = string.format("RUN %d", runCounter)
    out[#out + 1] = introLine
    -- trapdoor fall: Doom starts its dream sequence the moment Isaac's fall animation starts
    do
        local ps = player:GetSprite()
        local falling = ps and (ps:IsPlaying("Trapdoor") or ps:IsPlaying("FallIn")) or false
        if falling and not fallingNow then fallSeq = fallSeq + 1 end
        fallingNow = falling
    end
    out[#out + 1] = string.format("FALL %d", fallSeq)
    do
        local paused = false
        pcall(function() paused = game:IsPaused() end)
        out[#out + 1] = string.format("PAUSED %d", paused and 1 or 0)
    end
    local curses = level:GetCurses()
    out[#out + 1] = string.format("CURSE %d", curses)
    out[#out + 1] = string.format("PTYPE %d", player:GetPlayerType())
    -- Isaac's found-HUD stats: speed, tears (shots per second), damage, range, shot speed, luck,
    -- then devil / angel / planetarium chance where the game exposes them (-1 = unknown)
    do
        local tears = 30 / ((player.MaxFireDelay or 9) + 1)
        local devil, angel, planet = -1, -1, -1
        pcall(function()
            local d, a = level:GetDevilAngelRoomChance()
            if d then devil = d * 100 end
            if a then angel = a * 100 end
        end)
        pcall(function() planet = level:GetPlanetariumChance() * 100 end)
        out[#out + 1] = string.format("STATS %s %s %s %s %s %s %s %s %s",
            fmt(player.MoveSpeed), fmt(tears), fmt(player.Damage), fmt((player.TearRange or 260) / 40),
            fmt(player.ShotSpeed), fmt(player.Luck), fmt(devil), fmt(angel), fmt(planet))
    end
    -- transformations: announce each one once, like Isaac's own streak
    do
        local FORMS = { [0] = "Guppy", [1] = "Beelzebub", [2] = "Fun Guy", [3] = "Seraphim", [4] = "Bob",
            [5] = "Spun", [6] = "Yes Mother?", [7] = "Conjoined", [8] = "Leviathan", [9] = "Oh Crap",
            [10] = "Bookworm", [11] = "Adult", [12] = "Spider Baby", [13] = "Stompy" }
        for f, nm in pairs(FORMS) do
            local has = false
            pcall(function() has = player:HasPlayerForm(f) end)
            if has and not seenForms[f] then
                seenForms[f] = true
                if game:GetFrameCount() > 30 then popupQueue[#popupQueue + 1] = nm .. "|Transformation" end
            elseif not has then
                seenForms[f] = nil
            end
        end
    end
    out[#out + 1] = string.format("SPEED %s", fmt(player.MoveSpeed))
    do
        local ok, mid = pcall(function() return MusicManager():GetCurrentMusicID() end)
        if not ok and game:GetFrameCount() % 300 == 0 then log("GetCurrentMusicID failed: " .. tostring(mid)) end
        -- unicorn invincibility (My Little Unicorn / Unicorn Stump): Doom does the rainbow and the sped-up music
        local uni = 0
        pcall(function()
            local fx = player:GetEffects()
            if fx and (fx:HasCollectibleEffect(CollectibleType.COLLECTIBLE_MY_LITTLE_UNICORN)
                or fx:HasCollectibleEffect(CollectibleType.COLLECTIBLE_UNICORN_STUMP)) then uni = 1 end
        end)
        local musId = (ok and tonumber(mid)) or 0
        -- death: the floor music drops the moment Isaac dies, and after his death animation
        -- the game-over track takes over, whatever the MusicManager happens to report
        if player:IsDead() then
            if deathTick < 0 then deathTick = renderTick end
            musId = (renderTick - deathTick < 50) and 0 or ((Music and Music.MUSIC_GAME_OVER) or 20)
        else
            deathTick = -1
        end
        out[#out + 1] = string.format("MUSIC %d %d", musId, uni)
        if isaacMuted and MUSIC_TO_DOOM and game:GetFrameCount() % 90 == 0 then
            pcall(function() MusicManager():VolumeSlide(0, 0.2) end)   -- keep it faded across track changes
        end
    end
    out[#out + 1] = string.format("BACKDROP %d", room:GetBackdropType())
    -- which of the stage's five shadow overlays (gfx/overlays/<stage>/<shape>_overlay_N.png)
    -- this room wears: fixed per room from its decoration seed, so it doesn't change on revisits
    -- Measured in the real game (tools/overlay_probe.py): only about one basement room in five
    -- wears one, at roughly 28% darkness - most rooms have none. Same odds here, fixed per room.
    local OVERLAY_PERCENT = 20
    local ov = 0
    pcall(function()
        local rd = level:GetCurrentRoomDesc()
        local ds = rd and rd.DecorationSeed or 0
        if (math.floor(ds / 13) % 100) < OVERLAY_PERCENT then
            ov = (math.floor(ds / 7) % 5) + 1
        end
    end)
    out[#out + 1] = string.format("OVERLAY %d", ov)
    -- the room's seeds, for tools/overlay_probe.py to work out which one the game picks its overlay from
    pcall(function()
        local rd = level:GetCurrentRoomDesc()
        out[#out + 1] = string.format("SEEDS %d %d %d %d", rd.DecorationSeed or 0, rd.SpawnSeed or 0,
            rd.AwardSeed or 0, rd.ListIndex or 0)
    end)
    local o0 = room:GetGridPosition(0)     -- centre of grid cell 0: the exact anchor for cell maths
    out[#out + 1] = string.format("ORIGIN %s %s", fmt(o0.X), fmt(o0.Y))
    out[#out + 1] = string.format("DEAD %d", player:IsDead() and 1 or 0)
    -- 1 while the game renders but doesn't update: the stage card and fade-in at the start of
    -- every floor (and of a continued run) - Doom shows its card and fades in for exactly as long
    out[#out + 1] = string.format("FROZEN %d", frozenNow and 1 or 0)

    -- grid, one char per cell, row-major (index 0 = top-left corner wall)
    local cells = {}
    local n = room:GetGridSize()
    for i = 0, n - 1 do
        cells[#cells + 1] = cellChar(room, i)
    end
    out[#out + 1] = "GRID " .. table.concat(cells)

    -- grid entity sprites (rocks, poop, doors, pits, props...)
    for i = 0, n - 1 do
        local g = room:GetGridEntity(i)
        if g then
            local gt = g:GetType()
            -- destroyed rocks / poop / TNT keep their grid entity with no collision: don't draw them
            local KEEP_FLAT = { [GridEntityType.GRID_DECORATION] = true, [GridEntityType.GRID_SPIDERWEB] = true,
                [GridEntityType.GRID_PRESSURE_PLATE] = true, [GridEntityType.GRID_PIT] = true,
                [GridEntityType.GRID_TRAPDOOR] = true, [GridEntityType.GRID_STAIRS] = true,
                [GridEntityType.GRID_SPIKES] = true, [GridEntityType.GRID_SPIKES_ONOFF] = true,
                [GridEntityType.GRID_DOOR] = true, [GridEntityType.GRID_GRAVITY] = true,
                -- a blown TNT barrel keeps its bottom half standing ("Blown"): draw it like Isaac does
                [GridEntityType.GRID_TNT] = true }
            local gone = (not KEEP_FLAT[gt]) and g.CollisionClass == GridCollisionClass.COLLISION_NONE
            -- a smashed rock / urn / skull keeps its entity showing the rubble: draw that flat on the floor
            local ROCKISH = { [GridEntityType.GRID_ROCK] = true, [GridEntityType.GRID_ROCKB] = true,
                [GridEntityType.GRID_ROCKT] = true, [GridEntityType.GRID_ROCK_BOMB] = true,
                [GridEntityType.GRID_ROCK_ALT] = true, [GridEntityType.GRID_ROCK_GOLD] = true,
                [GridEntityType.GRID_ROCK_SPIKED or -1] = true, [GridEntityType.GRID_ROCK_ALT2 or -1] = true }
            local rubble = gone and ROCKISH[gt]
            if gt ~= GridEntityType.GRID_WALL and gt ~= GridEntityType.GRID_NULL and (not gone or rubble) then
                local spr = g:GetSprite()
                local fn = spr and spr:GetFilename()
                if fn and fn ~= "" then
                    local skin = gt == GridEntityType.GRID_DOOR and doorSkin(g, fn, level) or ""
                    out[#out + 1] = string.format("GSP %d %d %d %d %s\t%s\t%s\t%d", i, gt, spr:GetFrame(),
                        spr.FlipX and 1 or 0, spr:GetAnimation() or "", fn, skin, rubble and 1 or 0)
                end
            end
        end
    end

    -- level map, as far as Isaac's own minimap knows it
    local rooms = level:GetRooms()
    for i = 0, rooms.Size - 1 do
        local rd = rooms:Get(i)
        if rd and rd.Data and (rd.DisplayFlags ~= 0 or rd.VisitedCount > 0 or rd.GridIndex == level:GetCurrentRoomIndex()) then
            out[#out + 1] = string.format("MAP %d %d %d %d %d", rd.GridIndex, rd.Data.Shape, rd.Data.Type,
                rd.DisplayFlags, rd.VisitedCount > 0 and 1 or 0)
        end
    end

    -- doors (slot, open, locked, target room)
    for slot = 0, 7 do
        local d = room:GetDoor(slot)
        if d then
            out[#out + 1] = string.format("DOOR %d %d %d %d %d", slot,
                d:IsOpen() and 1 or 0, d:IsLocked() and 1 or 0, d.TargetRoomIndex, d:GetGridIndex())
        end
    end

    -- player
    out[#out + 1] = string.format("PLAYER %s %s %s %s %d %d %d %d %d %d %d %d %d %d",
        fmt(player.Position.X), fmt(player.Position.Y),
        fmt(player.Velocity.X), fmt(player.Velocity.Y),
        player:GetHearts(), player:GetMaxHearts(), player:GetSoulHearts(),
        player:GetBoneHearts(), player:GetEternalHearts(),
        player:GetNumBombs(), player:GetNumKeys(), player:GetNumCoins(),
        controlTaken and 1 or 0, player.CanFly and 1 or 0)

    -- hearts exactly as Isaac's HUD lays them out (see heartTokens)
    if bit(curses, 3) then out[#out + 1] = "HEARTS Q"      -- Curse of the Unknown: one "?" heart
    else out[#out + 1] = "HEARTS " .. heartTokens(player) end

    -- active item and pocket item
    local active = player:GetActiveItem(ActiveSlot.SLOT_PRIMARY)
    if active and active > 0 then
        local cfg = Isaac.GetItemConfig():GetCollectible(active)
        local nm = itemText("items", active, cfg)
        out[#out + 1] = string.format("ACTIVE %d %d %d %s", player:GetActiveCharge(ActiveSlot.SLOT_PRIMARY),
            cfg and cfg.MaxCharges or 0, active, clean(nm or "Item"))
    end
    local card = player:GetCard(0)
    if card and card > 0 then
        local nm = itemText("cards", card, Isaac.GetItemConfig():GetCard(card))
        out[#out + 1] = string.format("HELD c %d %s", card, clean(nm or "Card"))
    else
        local color = player:GetPill(0)
        local pn = pillName(color, player)
        if pn then out[#out + 1] = string.format("HELD p %d %s", color or 0, clean(pn)) end
    end
    local trink = player:GetTrinket(0)
    if trink and trink > 0 then
        local nm = itemText("trinkets", trink % 32768, Isaac.GetItemConfig():GetTrinket(trink % 32768))
        out[#out + 1] = string.format("TRINK %d %s", trink % 32768, clean(nm or "Trinket"))
    end

    -- item pickups (name|description), shown as a popup in Doom
    checkPickups(player)
    for _, pu in ipairs(popupQueue) do
        out[#out + 1] = "POPUP " .. pu
    end
    popupQueue = {}

    -- one-shot effects
    for _, fx in ipairs(fxQueue) do
        out[#out + 1] = string.format("FX %s %s %s", fx[1], fmt(fx[2]), fmt(fx[3]))
    end
    fxQueue = {}
    for _, sf in ipairs(sfxQueue) do
        out[#out + 1] = string.format("SFX %d %s %s", sf[1], fmt(sf[2]), fmt(sf[3]))
    end
    sfxQueue = {}

    -- entities
    local ents = Isaac.GetRoomEntities()
    do  -- lasers: fold in anything FindByType knows that the room list doesn't
        local okL, lasers = pcall(Isaac.FindByType, EntityType.ENTITY_LASER, -1, -1, false, false)
        if okL and lasers and #lasers > 0 then
            local have = {}
            for _, e in ipairs(ents) do if e.Type == EntityType.ENTITY_LASER then have[GetPtrHash(e)] = true end end
            local added = 0
            for _, l in ipairs(lasers) do
                if not have[GetPtrHash(l)] then ents[#ents + 1] = l; added = added + 1 end
            end
            if not laserListLogged then
                laserListLogged = true
                log(string.format("lasers: %d via FindByType, %d of them missing from GetRoomEntities", #lasers, added))
            end
        end
    end
    local snapshot = {}      -- hash -> { kind, x, y, anim, frame } for the sound attribution
    for _, e in ipairs(ents) do
        local kind = entityKind(e)
        -- exploded bombs linger invisibly in Isaac for a while: drop them once they blow
        if kind == "b" then
            local bs = e:GetSprite()
            local an = bs and bs:GetAnimation() or ""
            if an:find("Explode") or not e.Visible then kind = nil end
        end
        -- Finger!'s hit detection is a laser in Isaac; it isn't part of the look, keep it hidden
        if kind == "l" then
            local function fingerOwner(o)
                if not o then return false end
                if o.Type ~= EntityType.ENTITY_FAMILIAR then return false end
                if FamiliarVariant and FamiliarVariant.FINGER and o.Variant == FamiliarVariant.FINGER then return true end
                local ps = o:GetSprite()
                return ((ps and ps:GetFilename() or ""):lower()):find("finger") ~= nil
            end
            local byVar = FamiliarVariant and FamiliarVariant.FINGER and e.SpawnerType == EntityType.ENTITY_FAMILIAR
                and e.SpawnerVariant == FamiliarVariant.FINGER
            local short = false
            if not byVar and player:HasCollectible(467) then
                -- Finger!'s laser is the only one that stops within arm's reach of the player
                pcall(function()
                    local l = e:ToLaser()
                    local ep = l and l:GetEndPoint()
                    short = ep and (ep - e.Position):Length() < 110 and (e.Position - player.Position):Length() < 60
                end)
            end
            if byVar or short or isFingerLaser(e) or fingerOwner(e.Parent) or fingerOwner(e.SpawnerEntity) then kind = nil end
        end
        -- dead enemies stay while their death animation plays (bosses especially): Isaac
        -- removes the entity itself when it's done, so keep mirroring it until then
        if kind and e:Exists() and (kind == "l" or kind == "n" or not e:IsDead()) then
            do
                local sp = e:GetSprite()
                snapshot[GetPtrHash(e)] = { kind = kind, x = e.Position.X, y = e.Position.Y,
                    anim = sp and sp:GetAnimation() or "", frame = sp and sp:GetFrame() or 0 }
            end
            local flying = 0
            -- IsFlying() is really "ignores the grid": fireplaces and shopkeepers say yes without hovering
            if kind == "n" and e:ToNPC() and e:IsFlying()
                and e.Type ~= EntityType.ENTITY_FIREPLACE and e.Type ~= EntityType.ENTITY_SHOPKEEPER then flying = 1 end
            local boss = (kind == "n" and e:IsBoss()) and 1 or 0
            -- on fire: the burning status, or an inherently fiery enemy (by its sheet name)
            local fire = 0
            if kind == "n" and e.Type == EntityType.ENTITY_FIREPLACE then
                -- a fireplace burns until it is put out: its sprite name says "fire" forever,
                -- so go by its state (dead / "Dissapear" animation) instead
                local sp = e:GetSprite()
                local an = ((sp and sp:GetAnimation()) or ""):lower()
                -- lit only while its flames play ("Flickering"): anything else is going out or gone
                fire = (an:find("flicker") and not e:IsDead()) and 1 or 0
            elseif kind == "n" then
                if e:HasEntityFlags(EntityFlag.FLAG_BURN) then fire = 1
                else
                    local sp = e:GetSprite()
                    local fnm = sp and sp:GetFilename() or ""
                    fnm = fnm:lower()
                    if fnm:find("fire") or fnm:find("flam") or fnm:find("burn") or fnm:find("candle") then fire = 1 end
                end
            end
            -- Tech X rings: the laser is a circle of this radius around its position
            local ring = 0
            if kind == "l" then
                local l = e:ToLaser()
                local isRing = l and l.IsCircleLaser and l:IsCircleLaser()
                if isRing then
                    -- Tech X: the ring is drawn from the game's own laser art laid around a circle
                    -- (Isaac's sample points when it gives them, else a circle of its radius), which
                    -- grows with the charge and then travels; closed so the loop has no gap
                    local pts = {}
                    pcall(function()
                        local sm = l:GetSamples()
                        if sm and sm.Size and sm.Size >= 6 then
                            local step = math.max(1, math.floor(sm.Size / 36))
                            for i = 0, sm.Size - 1, step do
                                local v = sm:Get(i)
                                if v:Length() < 60 and (v - e.Position):Length() > 60 then v = v + e.Position end
                                pts[#pts + 1] = fmt(v.X) .. "," .. fmt(v.Y)
                            end
                        end
                    end)
                    if #pts < 6 then
                        pts = {}
                        local r = math.max(4, l.Radius or 20)
                        for i = 0, 31 do
                            local a = i * 360 / 32
                            local v = e.Position + Vector.FromAngle(a) * r
                            pts[#pts + 1] = fmt(v.X) .. "," .. fmt(v.Y)
                        end
                    end
                    pts[#pts + 1] = pts[1]
                    local ss = e.SpriteScale
                    out[#out + 1] = string.format("LZR %d %d %s %d %s", GetPtrHash(e), e.Variant,
                        fmt(ss and ss.X or 1), 2, table.concat(pts, " "))      -- 2 = ring: not anchored to the player
                end
                -- the beam's real path: Isaac's own sample points (bent by Spoon Bender, bounced by
                -- Rubber Cement, wiggled by Tiny Planet...), or a straight start->end line
                if l and not isRing then
                    local pts = {}
                    -- a beam we just turned: for its first frames Isaac's path (samples and end point)
                    -- still follows the cardinal it was born with, so draw a straight line at the real angle
                    local ta = laserAim[GetPtrHash(e)]
                    local fresh = ta and ta.angle and l.FrameCount <= 3
                    local ok2 = fresh or pcall(function()
                        local sm = l:GetSamples()
                        if sm and sm.Size and sm.Size >= 2 then
                            -- samples may be relative to the laser: if the first one sits near the origin
                            -- rather than near the laser, shift the whole list to the laser's position
                            local first = sm:Get(0)
                            local off = Vector.Zero
                            if (first - e.Position):Length() > 60 and first:Length() < 60 then off = e.Position end
                            if not samplesLogged then
                                samplesLogged = true
                                log(string.format("laser samples: %d, first %s,%s, laser at %s,%s, offset %s", sm.Size,
                                    fmt(first.X), fmt(first.Y), fmt(e.Position.X), fmt(e.Position.Y), off:Length() > 0 and "yes" or "no"))
                            end
                            -- our own beam: Isaac's path was computed before the beam was pointed where
                            -- Doom points, so swing the whole path round by the difference
                            local rot = 0
                            if ta and ta.angle then
                                local last = sm:Get(sm.Size - 1) + off
                                local dir = last - (first + off)
                                if dir:Length() > 1 then
                                    rot = ta.angle - dir:GetAngleDegrees()
                                    while rot > 180 do rot = rot - 360 end
                                    while rot < -180 do rot = rot + 360 end
                                    if math.abs(rot) < 1 then rot = 0 end
                                end
                            end
                            for i = 0, sm.Size - 1 do
                                local v = sm:Get(i) + off
                                if rot ~= 0 then v = e.Position + (v - e.Position):Rotated(rot) end
                                pts[#pts + 1] = fmt(v.X) .. "," .. fmt(v.Y)
                            end
                        end
                    end)
                    if #pts < 2 then
                        local ok3 = pcall(function()
                            local ep = l:GetEndPoint()
                            if fresh then
                                local len = (ep - e.Position):Length()
                                if len < 40 then len = 400 end
                                ep = e.Position + Vector.FromAngle(ta.angle) * len
                            end
                            pts = { fmt(e.Position.X) .. "," .. fmt(e.Position.Y), fmt(ep.X) .. "," .. fmt(ep.Y) }
                        end)
                        if not ok3 or #pts < 2 then
                            local a = e.Position + Vector.FromAngle(l.Angle or 0) * (l.MaxDistance or 600)
                            pts = { fmt(e.Position.X) .. "," .. fmt(e.Position.Y), fmt(a.X) .. "," .. fmt(a.Y) }
                        end
                    end
                    local ss = e.SpriteScale
                    local mine = (e.SpawnerType == EntityType.ENTITY_PLAYER or (e.Parent and e.Parent.Type == EntityType.ENTITY_PLAYER)) and 1 or 0
                    if not laserLogged then
                        laserLogged = true
                        log(string.format("laser variant %d: %d path points, first %s, last %s, visible=%s", e.Variant, #pts,
                            pts[1] or "?", pts[#pts] or "?", tostring(e.Visible)))
                    end
                    out[#out + 1] = string.format("LZR %d %d %s %d %s", GetPtrHash(e), e.Variant,
                        fmt(ss and ss.X or 1), mine, table.concat(pts, " "))
                end
            end
            local champ = -1
            if kind == "n" then
                local npc = e:ToNPC()
                if npc and npc:IsChampion() then champ = npc:GetChampionColorIdx() end
            end
            -- tint of tears / shots (items like Ipecac or Lost Contact colour them): 3 digits, 0..2 each
            local tint = "222"
            if kind == "t" or kind == "p" or kind == "d" then
                local function q(v, off)
                    v = (v or 1) + (off or 0)
                    if v < 0.45 then return "0" elseif v < 0.82 then return "1" end
                    return "2"
                end
                local function code(c)
                    if not c then return "222" end
                    local t = q(c.R, c.RO) .. q(c.G, c.GO) .. q(c.B, c.BO)
                    if t == "222" then
                        -- Repentance tints many tears through Colorize rather than the RGB channels
                        pcall(function()
                            local cz = c:GetColorize()
                            if cz and (cz.A or 0) > 0.05 then t = q(cz.R, 0) .. q(cz.G, 0) .. q(cz.B, 0) end
                        end)
                    end
                    return t
                end
                -- the entity's colour, else the sprite's own (the game sets either, depending on the effect)
                tint = code(e.Color)
                if tint == "222" then
                    local sp = e:GetSprite()
                    if sp then tint = code(sp.Color) end
                end
                -- still plain: colour by the tear's effect flags, as the game's own art does
                if tint == "222" and kind == "t" then
                    local tr = e:ToTear()
                    if tr then
                        local function has(flag) local ok, r = pcall(function() return tr:HasTearFlags(flag) end); return ok and r end
                        if game:GetFrameCount() - (lastTintLog or -999) > 60 then
                            lastTintLog = game:GetFrameCount()
                            local c = e.Color
                            local sc = e:GetSprite() and e:GetSprite().Color
                            local okh, hp = pcall(function() return tr:HasTearFlags(TearFlags.TEAR_POISON) end)
                            log(string.format("tear tint check: color=%.2f,%.2f,%.2f off=%.2f,%.2f,%.2f sprite=%.2f,%.2f,%.2f flags=%s poison=%s/%s var=%d",
                                c.R, c.G, c.B, c.RO, c.GO, c.BO, sc and sc.R or -1, sc and sc.G or -1, sc and sc.B or -1,
                                tostring(tr.TearFlags), tostring(okh), tostring(hp), e.Variant))
                        end
                        if has(TearFlags.TEAR_POISON) or has(TearFlags.TEAR_BOOGER) then tint = "121"
                        elseif has(TearFlags.TEAR_BURN) then tint = "210"
                        elseif has(TearFlags.TEAR_FREEZE) then tint = "122"
                        elseif has(TearFlags.TEAR_CHARM) then tint = "212"
                        elseif has(TearFlags.TEAR_FEAR) then tint = "112"
                        elseif has(TearFlags.TEAR_HOMING) then tint = "202"
                        elseif has(TearFlags.TEAR_MIDAS) or has(TearFlags.TEAR_COIN_DROP) then tint = "220"
                        elseif has(TearFlags.TEAR_SHRINK) then tint = "211" end
                    end
                end
            end
            -- status effect that recolours the enemy in Isaac (one, by priority)
            local status = 0
            if kind == "n" then
                local function fl(name) local f = EntityFlag[name]; return f and e:HasEntityFlags(f) end
                if fl("FLAG_ICE") or fl("FLAG_FREEZE") then status = 1
                elseif fl("FLAG_POISON") then status = 2
                elseif fl("FLAG_MIDAS_FREEZE") then status = 6
                elseif fl("FLAG_BURN") then status = 7
                elseif fl("FLAG_CHARM") then status = 4
                elseif fl("FLAG_FEAR") then status = 5
                elseif fl("FLAG_BLEED_OUT") then status = 8
                elseif fl("FLAG_SLOW") then status = 3
                elseif fl("FLAG_CONFUSION") then status = 9 end
            elseif kind == "f" then
                -- familiars the game ties to Isaac with a drawn umbilical cord (Little Gemini, Gello...)
                local sp = e:GetSprite()
                local fn = ((sp and sp:GetFilename()) or ""):lower()
                if fn:find("gemini") or fn:find("gello") or fn:find("umbilical") or fn:find("cord") then status = 100 end
                -- (diagnostic) what the game reports for enemies with any status-looking flag
                if game:GetFrameCount() - (lastStatusLog or -999) > 90 then
                    local okf, flags = pcall(function() return e:GetEntityFlags() end)
                    local interesting = okf and flags and (flags & 0xFFE0) ~= 0
                    if interesting or status ~= 0 then
                        lastStatusLog = game:GetFrameCount()
                        log(string.format("status check: type=%d.%d dead=%s flags=%s status=%d ice=%s freeze=%s",
                            e.Type, e.Variant, tostring(e:IsDead()), tostring(flags), status,
                            tostring(EntityFlag.FLAG_ICE), tostring(EntityFlag.FLAG_FREEZE)))
                    end
                end
            end
            out[#out + 1] = string.format("ENT %d %s %d %d %d %s %s %s %s %s %s %s %d %d %d %s %d %s %d",
                GetPtrHash(e), kind, e.Type, e.Variant, e.SubType,
                fmt(e.Position.X), fmt(e.Position.Y),
                fmt(e.Velocity.X), fmt(e.Velocity.Y),
                fmt(e.Size), fmt(e.HitPoints), fmt(e.MaxHitPoints), flying, boss, fire, fmt(ring), champ, tint, status)
            if kind == "k" then
                local pk = e:ToPickup()
                local blind = bit(curses, 6) and e.Variant == PickupVariant.PICKUP_COLLECTIBLE
                local nm = (not blind) and pickupName(e, player) or nil
                if nm then out[#out + 1] = string.format("NAME %d %s", GetPtrHash(e), clean(nm)) end
                if blind then out[#out + 1] = string.format("BLIND %d", GetPtrHash(e)) end
                -- shop / devil prices (coins > 0, heart prices < 0, -1000 = free), and sale tags
                if pk and pk.Price and pk.Price ~= 0 then
                    local sale = 0
                    pcall(function()
                        if pk.ShopItemId and pk.ShopItemId >= 0 and pk.Price > 0 then
                            local cfg = Isaac.GetItemConfig()
                            local ic = e.Variant == PickupVariant.PICKUP_COLLECTIBLE and cfg:GetCollectible(e.SubType) or nil
                            if ic and ic.ShopPrice and ic.ShopPrice > pk.Price then sale = ic.ShopPrice end
                        end
                    end)
                    out[#out + 1] = string.format("PRICE %d %d %d", GetPtrHash(e), pk.Price, sale)
                end
            end
            -- which Isaac sprite / animation / frame is showing (tab separated: names may hold spaces)
            local spr = e:GetSprite()
            if spr then
                local fn = spr:GetFilename()
                if fn and fn ~= "" then
                    local ovanim = spr:GetOverlayAnimation() or ""
                    local ovframe = spr:GetOverlayFrame() or 0
                    local ss = e.SpriteScale
                    -- height above the floor for things that arc (Isaac: negative = up); 999 = not airborne
                    local hgt, fall = 999, 0
                    if kind == "t" then local tr = e:ToTear(); if tr then hgt = tr.Height; fall = tr.FallingSpeed or 0 end
                    elseif kind == "p" then local pr = e:ToProjectile(); if pr then hgt = pr.Height; fall = pr.FallingSpeed or 0 end
                    elseif kind == "b" then local bm = e:ToBomb(); if bm and bm.Height then hgt = bm.Height end end
                    out[#out + 1] = string.format("ANM %d %d %d\t%s\t%s\t%s\t%d\t%s\t%s\t%s", GetPtrHash(e), spr:GetFrame(),
                        spr.FlipX and 1 or 0, spr:GetAnimation() or "", fn, ovanim, ovframe, fmt(ss and ss.X or 1), fmt(hgt), fmt(fall))
                end
            end
        end
    end
    if controlTaken then
        collectSounds(player, snapshot, room)
        for _, sf in ipairs(sfxQueue) do
            out[#out + 1] = string.format("SFX %d %s %s", sf[1], fmt(sf[2]), fmt(sf[3]))
        end
        sfxQueue = {}
    else
        prevEnts = snapshot
    end
    out[#out + 1] = "END"
    return table.concat(out, "\n")
end

local function writeState()
    local s = buildState()
    if HAS_IO then
        local f = io.open(STATE_PATH, "w")
        if f then
            f:write(s)
            f:close()
        end
    else
        Isaac.SaveModData(mod, s)
    end
end

-------------------------------------------------------------------------------
-- Commands from Doom
--   SEQ <n>
--   MOVE <x> <y>          set player position (Isaac coords)
--   FIRE <dx> <dy>        fire a tear in that direction
--   BOMB                  place a bomb
--   USE                   use the active item
--   CARD                  use the held card, else the held pill
--   DOOR <slot>           go through door in that slot
--   UNLOCK <slot>         try to unlock a locked door (uses a key)
--   RESUME                Doom finished loading the room: unfreeze enemies
--   RESTART               start a new run (used from the death screen)
--   RELEASE               give control back to Isaac's own input
-------------------------------------------------------------------------------
local function applyCommands()
    if not HAS_IO then return end
    local f = io.open(CMD_PATH, "r")
    if not f then return end
    local content = f:read("*a")
    f:close()
    if not content or #content == 0 then return end

    local cmdSeq = tonumber(content:match("SEQ (%-?%d+)"))
    if not cmdSeq or cmdSeq == lastCmdSeq then return end
    lastCmdSeq = cmdSeq

    local game = Game()
    local room = game:GetRoom()
    local level = game:GetLevel()
    local player = Isaac.GetPlayer(0)
    lastCmdFrame = game:GetFrameCount()

    for line in content:gmatch("[^\r\n]+") do
        local op, rest = line:match("^(%u+)%s*(.*)$")
        if op == "WALK" then
            -- let Isaac's own movement carry the player (ladders trigger on real motion)
            local dx, dy = rest:match("([%-%d%.]+)%s+([%-%d%.]+)")
            if dx and dy then
                walkDir = Vector(tonumber(dx), tonumber(dy))
                walkUntil = game:GetFrameCount() + 8
            end
        elseif op == "MOVE" then
            local x, y = rest:match("([%-%d%.]+)%s+([%-%d%.]+)")
            -- right after a room change Isaac is still placing the player at the entry door: don't fight it
            if walkDir and game:GetFrameCount() <= walkUntil then
                -- walking under Isaac's control: don't pin the position
            elseif x and y and game:GetFrameCount() - newRoomFlag > 6 then
                if not controlTaken then
                    controlTaken = true
                    log("Doom took control")
                    -- Isaac only reads shooting as analog (true 360 aim) for gamepad slots, so
                    -- play through slot 1 while Doom drives; the input hook feeds it either way
                    if ANALOG_AIM then pcall(function() savedControllerIndex = player.ControllerIndex; player.ControllerIndex = 1 end) end
                    if SFX_TO_DOOM then muteIsaac(true) end
                end
                player.Position = Vector(tonumber(x), tonumber(y))
                player.Velocity = Vector.Zero
            end
        elseif op == "AIM" then
            local dx, dy = rest:match("([%-%d%.]+)%s+([%-%d%.]+)")
            if dx and dy then
                aimDir = Vector(tonumber(dx), tonumber(dy)):Normalized()
                -- Isaac's head turns to the nearest of its four directions to the crosshair
                pcall(function()
                    local d = cardinal(aimDir)
                    local hd = d.X > 0 and Direction.RIGHT or d.X < 0 and Direction.LEFT or d.Y > 0 and Direction.DOWN or Direction.UP
                    player:SetHeadDirection(hd, 2, true)
                end)
            end
        elseif op == "FIRE" then
            local dx, dy = rest:match("([%-%d%.]+)%s+([%-%d%.]+)")
            if dx and dy then
                -- hold Isaac's real shoot input in this direction (see MC_INPUT_ACTION):
                -- the game then fires with all its own rules (multishot, brimstone, delay...)
                fireDir = Vector(tonumber(dx), tonumber(dy)):Normalized()
                if fireHoldUntil < game:GetFrameCount() then fireStartFrame = game:GetFrameCount() end
                fireHoldUntil = game:GetFrameCount() + FIRE_HOLD_FRAMES
            end
        elseif op == "BOMB" then
            if player:GetNumBombs() > 0 then
                Isaac.Spawn(EntityType.ENTITY_BOMB, 0, 0, player.Position, Vector.Zero, player)
                player:AddBombs(-1)
            end
        elseif op == "USE" then
            local item = player:GetActiveItem(ActiveSlot.SLOT_PRIMARY)
            if item and item > 0 then
                player:UseActiveItem(item, UseFlag.USE_OWNED, ActiveSlot.SLOT_PRIMARY)
            end
        elseif op == "CARD" then
            local card = player:GetCard(0)
            if card and card > 0 then
                player:UseCard(card, UseFlag.USE_OWNED)
                player:SetCard(0, 0)
            else
                local color = player:GetPill(0)
                if color and color > 0 then
                    local effect = Game():GetItemPool():GetPillEffect(color, player)
                    player:UsePill(effect, color, UseFlag.USE_OWNED)
                    player:SetPill(0, 0)
                else
                    -- no card or pill: the pocket active (tainted characters, Dice Bag...)
                    for _, slot in ipairs({ ActiveSlot.SLOT_POCKET, ActiveSlot.SLOT_POCKET2 }) do
                        local item = player:GetActiveItem(slot)
                        if item and item > 0 then
                            player:UseActiveItem(item, UseFlag.USE_OWNED, slot)
                            break
                        end
                    end
                end
            end
        elseif op == "DROP" then
            -- Isaac's drop button (Ctrl): a tap swaps Schoolbag items / the Forgotten's body,
            -- a hold drops the trinket and pocket items. Doom sends DROP every tic it is held.
            local nowf = game:GetFrameCount()
            if dropHoldUntil < nowf then dropStartFrame = nowf end
            dropHoldUntil = nowf + FIRE_HOLD_FRAMES
        elseif op == "CHALLENGE" then
            local id = tonumber(rest) or 0
            log("challenge " .. id .. " requested by Doom")
            Isaac.ExecuteCommand("challenge " .. id)
        elseif op == "SEED" then
            local sd = rest:gsub("[^%w ]", ""):upper()
            log("seeded run requested by Doom: " .. sd)
            Isaac.ExecuteCommand("seed " .. sd)
        elseif op == "UNLOCK" then
            local slot = tonumber(rest)
            local d = slot and room:GetDoor(slot)
            if d and d:IsLocked() then
                local ok = d:TryUnlock(player, false)
                log("unlock door slot " .. tostring(slot) .. " -> " .. tostring(ok))
            end
        elseif op == "DOOR" then
            local slot = tonumber(rest)
            local d = slot and room:GetDoor(slot)
            if d and d:IsOpen() then
                -- direction from the slot, not d.Direction (stale after a continue)
                local dir = slot % 4
                log(string.format("DOOR slot=%d target=%d dir=%d (door says dir=%s target=%s) from room %d",
                    slot, d.TargetRoomIndex, dir, tostring(d.Direction), tostring(d.TargetRoomIndex),
                    level:GetCurrentRoomIndex()))
                -- curse room doors bite on the way in and out; the transition skips the door's own collision
                local spiky = (d.TargetRoomType == RoomType.ROOM_CURSE or room:GetType() == RoomType.ROOM_CURSE)
                if spiky and not (TrinketType.TRINKET_CALLUS and player:HasTrinket(TrinketType.TRINKET_CALLUS)) then
                    player:TakeDamage(1, DamageFlag.DAMAGE_CURSED_DOOR, EntityRef(nil), 0)
                end
                -- the game works out which door of the next room to appear at from the door
                -- being left; after a continue that is stale (last session's door), which put
                -- the player at the wrong side of wide rooms and sent later exits the wrong way
                pcall(function() level.LeaveDoor = slot end)
                game:StartRoomTransition(d.TargetRoomIndex, dir, RoomTransitionAnim.WALK, player, -1)
            else
                log("DOOR slot=" .. tostring(slot) .. " ignored (no open door there)")
            end
        elseif op == "EXIT" then
            -- Doom asked for a run on another save file: leave this run for the title
            -- screen, where the bridge walks the menus with key presses
            log("EXIT: fading out to the title screen")
            local target = (FadeoutTarget and FadeoutTarget.FADEOUT_TITLE_SCREEN) or 2
            local ok, err = pcall(function() Game():Fadeout(0.06, target) end)
            if not ok then log("EXIT failed: " .. tostring(err)) end
        elseif op == "PAUSE" then
            wantPause = true
        elseif op == "UNPAUSE" then
            wantPause = false
        elseif op == "RESUME" then
            -- nothing to do: no room-entry freeze
        elseif op == "NEWRUN" then
            local whoS, sd = rest:match("^(%-?%d+)%s*(.*)$")
            local who = tonumber(whoS) or 0
            local nowf = Isaac.GetFrameCount()
            if who == lastNewRunWho and nowf - lastNewRunAt < 300 then
                log("new run as character " .. who .. " already under way, ignoring repeat")
            else
                lastNewRunWho = who; lastNewRunAt = nowf
                sd = (sd or ""):gsub("[^%w]", ""):upper()
                pendingSeed = (#sd >= 8) and sd or nil
                log("new run as character " .. who .. " requested by Doom" .. (pendingSeed and (" seed " .. pendingSeed) or ""))
                Isaac.ExecuteCommand("restart " .. who)
            end
            menuLastCmdSeq = cmdSeq
        elseif op == "CONTINUE" then
            menuLastCmdSeq = cmdSeq       -- already in a run: nothing to do
        elseif op == "RESTART" then
            log("restart requested by Doom")
            Isaac.ExecuteCommand("restart")
        elseif op == "RELEASE" then
            controlTaken = false
            muteIsaac(false)
            if savedControllerIndex then pcall(function() player.ControllerIndex = savedControllerIndex end); savedControllerIndex = nil end
            log("control released")
        end
    end
end

-------------------------------------------------------------------------------
-- Callbacks
-------------------------------------------------------------------------------
-- Isaac's death cry the moment he dies (the sound poll would only catch it a frame or
-- more later, and the update loop is winding down at that point)
mod:AddCallback(ModCallbacks.MC_POST_ENTITY_KILL, function(_, entity)
    if not controlTaken then return end
    local ok = pcall(function()
        if entity.Type ~= EntityType.ENTITY_PLAYER then return end
        local sid = SoundEffect.SOUND_ISAACDIES or 65
        sfxQueue[#sfxQueue + 1] = { sid, -1, -1 }
        sfxWasPlaying[sid] = true
        writeState()
    end)
    if not ok then log("death sound relay failed") end
end)

mod:AddCallback(ModCallbacks.MC_POST_UPDATE, function()
    applyCommands()
    -- safety: if Doom stops talking for 3 seconds, hand control back
    if controlTaken and Game():GetFrameCount() - lastCmdFrame > 90 then
        controlTaken = false
        muteIsaac(false)
        log("bridge silent, control released")
    end
    writeState()
end)

mod:AddCallback(ModCallbacks.MC_POST_EFFECT_INIT, function(_, effect)
    local v = effect.Variant
    if v == EffectVariant.BOMB_EXPLOSION or v == EffectVariant.MAMA_MEGA_EXPLOSION
        or v == EffectVariant.BOMB_CRATER then
        if v ~= EffectVariant.BOMB_CRATER then
            fxQueue[#fxQueue + 1] = { "boom", effect.Position.X, effect.Position.Y }
            -- the explosion sound, placed at the blast (don't wait for the IsPlaying poll to notice it)
            if SFX_TO_DOOM then
                local sid = v == EffectVariant.MAMA_MEGA_EXPLOSION and SoundEffect.SOUND_EXPLOSION_STRONG
                    or SoundEffect.SOUND_EXPLOSION_WEAK
                sfxQueue[#sfxQueue + 1] = { sid, effect.Position.X, effect.Position.Y }
                sfxWasPlaying[sid] = true          -- so the poll doesn't report it a second time
                sfxWasPlaying[SoundEffect.SOUND_EXPLOSION_DEBRIS] = true
                sfxQueue[#sfxQueue + 1] = { SoundEffect.SOUND_EXPLOSION_DEBRIS, effect.Position.X, effect.Position.Y }
            end
        end
    end
end)

mod:AddCallback(ModCallbacks.MC_POST_RENDER, function()
    if introFlushRender then
        introFlushRender = false
        writeState()      -- POST_UPDATE won't run while the VS screen pauses the game
    end
    -- MC_POST_UPDATE stops on the death screen; keep listening for RESTART there
    local player = Isaac.GetPlayer(0)
    if player and player:IsDead() then
        applyCommands()
        writeState()
    end
    -- paused: POST_UPDATE is off, so keep reading Doom's commands and reporting state here.
    -- The bridge matches Doom's pause state by tapping Esc on Isaac's window (the input
    -- hook can pause the game but cannot leave the pause menu), so it needs PAUSED updates.
    -- Frozen (the game renders but the frame counter stands still, and it isn't paused or the
    -- death screen): the stage card with its jingle and the fade-in from black at the start of
    -- a floor or a continued run. POST_UPDATE is silent then too, so report from here - that
    -- is what lets Doom start the jingle and its own card the moment Isaac does.
    do
        local paused, fc = false, -1
        pcall(function() paused = Game():IsPaused() end)
        pcall(function() fc = Game():GetFrameCount() end)
        if fc == frozenLastFrame then frozenCount = frozenCount + 1 else frozenCount = 0 end
        frozenLastFrame = fc
        local dead = player and player:IsDead()
        local frozen = (not paused) and (not dead) and frozenCount >= 3 and not inMenu()
        if frozen ~= frozenNow then
            frozenNow = frozen
            if not frozen then writeState() end     -- the moment the game resumes, without waiting
        end
        if paused or frozen then
            applyCommands()
            if renderTick % (frozen and 3 or 6) == 0 then writeState() end
        end
    end
    renderTick = renderTick + 1
    -- heartbeat: this callback only runs while a run exists (paused or not), never on the
    -- title screen, so the bridge can tell "in a run" from "in the menus" by its age
    if HAS_IO and renderTick % 15 == 0 then
        pcall(function()
            local f = io.open((PIPE_DIR or "") .. "isaac_alive.txt", "w")
            if f then f:write(tostring(renderTick)); f:close() end
        end)
    end
    -- main menu: no run, no POST_UPDATE - drive the menus from Doom's requests
    if inMenu() then
        applyMenuCommands()
        runMenuScript()
        -- tell Doom we're in the menu (a minimal state so the bridge knows Isaac is alive)
        if HAS_IO and renderTick % 30 == 0 then
            local f = io.open(STATE_PATH, "w")
            if f then f:write("SEQ -2\nMENU 1\nEND"); f:close() end
        end
    else
        runMenuScript()
    end
end)

-- pill / card streaks, like the game shows when you use one
mod:AddCallback(ModCallbacks.MC_USE_PILL, function(_, effect, player, flags)
    local x = xmlEntry("pills", effect)
    local nm = x and x.name
    if not nm then
        local cfg = Isaac.GetItemConfig():GetPillEffect(effect)
        nm = cfg and cfg.Name
    end
    if nm then popupQueue[#popupQueue + 1] = clean(nm) .. "|" end
end)
mod:AddCallback(ModCallbacks.MC_USE_CARD, function(_, card, player, flags)
    local nm, ds = itemText("cards", card, Isaac.GetItemConfig():GetCard(card))
    if nm then popupQueue[#popupQueue + 1] = clean(nm) .. "|" .. clean(ds or "") end
end)

local function queueFortune()
    local f = randomFortune():gsub("|", " / ")
    popupQueue[#popupQueue + 1] = "|" .. clean(f) .. "|f"
end
mod:AddCallback(ModCallbacks.MC_USE_ITEM, function(_, item, rng, player, flags, slot)
    if item == CollectibleType.COLLECTIBLE_FORTUNE_COOKIE then queueFortune() end
end)
-- fortune telling machines: catch the wiggle that starts a reading
local machineWiggle = {}
mod:AddCallback(ModCallbacks.MC_POST_UPDATE, function()
    for _, e in ipairs(Isaac.FindByType(EntityType.ENTITY_SLOT, 3, -1, false, false)) do
        local h = GetPtrHash(e)
        local spr = e:GetSprite()
        local wig = spr and spr:IsPlaying("Wiggle") or false
        if wig and not machineWiggle[h] then queueFortune() end
        machineWiggle[h] = wig
    end
end)

-- Mom's Knife: point it along the real aim while held, and re-aim it when thrown
local knifeWasFlying = {}
mod:AddCallback(ModCallbacks.MC_POST_UPDATE, function()
    if not controlTaken or not fireDir then return end
    for _, e in ipairs(Isaac.FindByType(EntityType.ENTITY_KNIFE, -1, -1, false, false)) do
        local k = e:ToKnife()
        if k and firedByUs(k) then
            local h = GetPtrHash(k)
            local flying = k:IsFlying()
            if not flying then
                k.Rotation = fireDir:GetAngleDegrees()
            elseif not knifeWasFlying[h] then
                local d = aimDelta()
                if d ~= 0 then
                    k.Velocity = k.Velocity:Rotated(d)
                    k.Rotation = k.Rotation + d
                end
            end
            knifeWasFlying[h] = flying
        end
    end
end)

-- lasers (brimstone, technology...) follow the aim too. Isaac sets a brimstone beam's
-- angle after the init callback, so the turn is applied on the beam's first update
-- (once per laser); circle lasers (Tech X) are left alone.
local function reaimLaser(laser, firstTime)
    if not controlTaken or not fireDir or not firedByUs(laser) then return end
    if isFingerLaser(laser) then return end      -- the finger's own reach: leave it to the game
    local h = GetPtrHash(laser)
    local t = laserAim[h]
    if not t then
        if laser.IsCircleLaser and laser:IsCircleLaser() then
            -- Tech X: the ring travels along the aim
            local d = aimDelta()
            if d ~= 0 then laser.Velocity = laser.Velocity:Rotated(d) end
            laserAim[h] = { none = true }
            return
        end
        -- a beam of ours points exactly where Doom points, whatever direction the game
        -- worked out from the (released) shoot input; spread beams keep their offset
        -- from the beam that was aimed straight
        local want = fireDir:GetAngleDegrees()
        local have = laser.Angle
        local spread = 0
        if analogWorks ~= false then
            -- the game aimed analog: its angle is right up to the spread, keep the spread
            local dd = have - want
            while dd > 180 do dd = dd - 360 end
            while dd < -180 do dd = dd + 360 end
            if math.abs(dd) < 60 then spread = dd end
        else
            local d = aimDelta()
            local c = cardinal(fireDir):GetAngleDegrees()
            local dd = have - c
            while dd > 180 do dd = dd - 360 end
            while dd < -180 do dd = dd + 360 end
            if math.abs(dd) < 60 then spread = dd end
        end
        t = { angle = want + spread }
        t.vel = Vector.FromAngle(t.angle) * laser.Velocity:Length()
        laserAim[h] = t
    end
    if t.none then return end
    -- sweep: a beam that stays attached to the player (Brimstone and friends) keeps following
    -- the crosshair for as long as it lasts; its spread offset is kept
    if aimDir and laser.Parent and laser.Velocity:Length() < 0.01 then
        if t.spread == nil then
            t.spread = t.angle - fireDir:GetAngleDegrees()
        end
        t.angle = aimDir:GetAngleDegrees() + t.spread
    end
    local dd = laser.Angle - t.angle
    while dd > 180 do dd = dd - 360 end
    while dd < -180 do dd = dd + 360 end
    if math.abs(dd) > 0.5 then
        laser.Angle = t.angle
        if laser.Velocity:Length() > 0.01 then laser.Velocity = t.vel end
    end
end
local laserDbg = 0
mod:AddCallback(ModCallbacks.MC_POST_LASER_INIT, function(_, laser)
    if laserDbg < 6 and fireDir then
        laserDbg = laserDbg + 1
        pcall(function()
            local pl = Isaac.GetPlayer(0)
            local aim, shoot, head = pl:GetAimDirection(), pl:GetShootingInput(), pl:GetHeadDirection()
            log(string.format("LASER INIT: doom aim (%.2f,%.2f) | isaac aim (%.2f,%.2f) shoot (%.2f,%.2f) head %d | laser angle %.1f vel (%.2f,%.2f) | player vel (%.2f,%.2f) ctrl %d mirror %s",
                fireDir.X, fireDir.Y, aim.X, aim.Y, shoot.X, shoot.Y, head, laser.Angle, laser.Velocity.X, laser.Velocity.Y,
                pl.Velocity.X, pl.Velocity.Y, pl.ControllerIndex, tostring(ANALOG_MIRROR)))
        end)
    end
    reaimLaser(laser, true)
end)
mod:AddCallback(ModCallbacks.MC_POST_LASER_UPDATE, function(_, laser)
    reaimLaser(laser, false)
end)
mod:AddCallback(ModCallbacks.MC_POST_NEW_ROOM, function() laserAim = {} end)
mod:AddCallback(ModCallbacks.MC_POST_PROJECTILE_INIT, function(_, proj)
    if controlTaken and Game():GetFrameCount() <= fireHoldUntil and firedByUs(proj) then
        local d = aimDelta()
        if d ~= 0 then proj.Velocity = proj.Velocity:Rotated(d) end
    end
end)

-- versus-screen backdrop for the current stage: bossspot_<token>_*.png in the game files
local function spotToken(level)
    local stage, st = level:GetStage(), level:GetStageType()
    if stage <= 2 then return ({ [0] = "01", "02", "13", "01", "01x", "02x" })[st] or "01"
    elseif stage <= 4 then return ({ [0] = "03", "04", "14", "03", "03x", "04x" })[st] or "03"
    elseif stage <= 6 then return ({ [0] = "05", "06", "15", "05", "05x", "06x" })[st] or "05"
    elseif stage <= 8 then return ({ [0] = "07", "08", "16", "07", "07x", "07x" })[st] or "07"
    elseif stage == 9 then return "17"
    elseif stage == 10 then return st == 1 and "10" or "09"
    elseif stage == 11 then return st == 1 and "12" or "11"
    elseif stage == 12 then return "19"
    end
    return "01"
end

mod:AddCallback(ModCallbacks.MC_POST_NEW_ROOM, function()
    newRoomFlag = Game():GetFrameCount()
    local game = Game()
    local room = game:GetRoom()
    if room:GetType() == RoomType.ROOM_BOSS and not room:IsClear() then
        local ok, b1 = pcall(function() return room:GetBossID() end)
        local ok2, b2 = pcall(function() return room:GetSecondBossID() end)
        if ok and b1 and b1 > 0 then
            introSeq = introSeq + 1
            introLine = string.format("INTRO %d %d %d %d %s", introSeq, b1, (ok2 and b2) or 0,
                Isaac.GetPlayer(0):GetPlayerType(), spotToken(game:GetLevel()))
            introFlushRender = true
            log(introLine)
        end
    end
    fireHoldUntil = -1
end)

-- While Doom is in control, Isaac's own input is replaced: movement is zeroed
-- (the bridge places the player directly) and the shoot axes come from the
-- last FIRE command, so the game does all the real firing itself.
-- Keyboard-style input only knows 4 directions, so we press the one closest to
-- the aim and then rotate whatever the game fires to the exact Doom aim angle.
cardinal = function(d)
    if math.abs(d.X) >= math.abs(d.Y) then
        return Vector(d.X >= 0 and 1 or -1, 0)
    end
    return Vector(0, d.Y >= 0 and 1 or -1)
end
-- ANALOG_AIM: feed Isaac the real aim as analog stick values instead of a cardinal press.
-- If the game honours it (it does for gamepad-style input), every weapon - Ludovico,
-- Epic Fetus, Spirit Sword, everything - aims natively and no per-shot turning is needed.
-- If it ignores the fractions, shots snap to a cardinal: set this back to false.
local function aimAxis(d) return ANALOG_AIM and d or cardinal(d) end
local OPPOSITE = {
    [ButtonAction.ACTION_SHOOTLEFT] = ButtonAction.ACTION_SHOOTRIGHT, [ButtonAction.ACTION_SHOOTRIGHT] = ButtonAction.ACTION_SHOOTLEFT,
    [ButtonAction.ACTION_SHOOTUP] = ButtonAction.ACTION_SHOOTDOWN, [ButtonAction.ACTION_SHOOTDOWN] = ButtonAction.ACTION_SHOOTUP,
}
local SHOOT = {
    [ButtonAction.ACTION_SHOOTLEFT]  = function(d) d = aimAxis(d); return math.max(0, -d.X) end,
    [ButtonAction.ACTION_SHOOTRIGHT] = function(d) d = aimAxis(d); return math.max(0, d.X) end,
    [ButtonAction.ACTION_SHOOTUP]    = function(d) d = aimAxis(d); return math.max(0, -d.Y) end,
    [ButtonAction.ACTION_SHOOTDOWN]  = function(d) d = aimAxis(d); return math.max(0, d.Y) end,
}
-- Does the game honour the analog values (true 360 aim), or does it quantise them to a
-- cardinal? Decided from the first shot fired: a shot that leaves on a cardinal while the
-- aim was well off it means "no", and every shot is then turned from that cardinal.
local analogWorks = nil
local analogVotes = 0            -- +1 per tear that left at the real aim, -1 per tear on a cardinal
noteShotDirection = function(v)
    if analogWorks ~= nil or not ANALOG_AIM or not fireDir or not v or v:Length() < 0.1 then return end
    local c = cardinal(fireDir)
    local offCard = fireDir:GetAngleDegrees() - c:GetAngleDegrees()
    while offCard > 180 do offCard = offCard - 360 end
    while offCard < -180 do offCard = offCard + 360 end
    if math.abs(offCard) < 12 then return end            -- too close to a cardinal to tell
    local shot = v:GetAngleDegrees() - c:GetAngleDegrees()
    while shot > 180 do shot = shot - 360 end
    while shot < -180 do shot = shot + 360 end
    analogVotes = analogVotes + ((math.abs(shot) > 3) and 1 or -1)
    if math.abs(analogVotes) >= 3 then
        analogWorks = analogVotes > 0
        log("analog aim " .. (analogWorks and "works: shots aim natively" or "ignored by the game: turning shots from the cardinal"))
    end
end
aimDelta = function()
    -- degrees to rotate a shot from the pressed cardinal to the real aim (0 once we know
    -- the game aims natively from the analog values)
    if not fireDir or analogWorks ~= false then return 0 end
    local c = cardinal(fireDir)
    local d = fireDir:GetAngleDegrees() - c:GetAngleDegrees()
    while d > 180 do d = d - 360 end
    while d < -180 do d = d + 360 end
    return d
end
firedByUs = function(ent)
    local sp = ent.SpawnerEntity
    if not sp then return false end
    return sp.Type == EntityType.ENTITY_PLAYER or sp.Type == EntityType.ENTITY_FAMILIAR
end
local MOVES = {
    [ButtonAction.ACTION_LEFT] = true, [ButtonAction.ACTION_RIGHT] = true,
    [ButtonAction.ACTION_UP] = true, [ButtonAction.ACTION_DOWN] = true,
}
-------------------------------------------------------------------------------
-- Driving Isaac's own menus from Doom. The game has no "start a run" call from
-- the title screen, so we press its menu buttons for it through the input hook:
-- a small script of (action, wait) steps, run from POST_RENDER (the only
-- callback that ticks while the menu is up).
-------------------------------------------------------------------------------
local menuScript = nil          -- list of { action = ButtonAction or nil, wait = frames }
local menuStep = 1
local menuWait = 0
local menuPress = nil           -- action held for the current render frame
local menuLastCmdSeq = -1

inMenu = function()
    local ok, m = pcall(function() return MenuManager.GetActiveMenu() end)
    if ok and type(m) == "number" then return m ~= 0 end
    local ok2, n = pcall(function() return Game():GetNumPlayers() end)
    return ok2 and n == 0
end

local function menuJump(kind)
    -- jump straight to a menu when the API allows it (saves a lot of blind pressing)
    pcall(function()
        if MenuManager and MainMenuType then MenuManager.SetActiveMenu(MainMenuType[kind]) end
    end)
end

-- character select order on the game's screen (matches Doom's list)
local CHAR_COLUMNS = { [0] = 0, [1] = 1, [2] = 2, [3] = 3, [4] = 4, [5] = 5, [6] = 6, [7] = 7, [8] = 8, [9] = 9,
    [10] = 10, [13] = 11, [14] = 12, [15] = 13, [16] = 14, [18] = 15, [19] = 16 }
local TAINTED_BASE = { [21] = 0, [22] = 1, [23] = 2, [24] = 3, [25] = 4, [26] = 5, [27] = 6, [28] = 7, [29] = 8,
    [30] = 9, [31] = 10, [32] = 13, [33] = 14, [34] = 15, [35] = 16, [36] = 18, [37] = 19 }

local function startMenuRun(who)
    local tainted = TAINTED_BASE[who] ~= nil
    local base = tainted and TAINTED_BASE[who] or who
    local col = CHAR_COLUMNS[base] or 0
    local steps = {}
    local function press(a, wait) steps[#steps + 1] = { action = a, wait = wait or 10 } end
    local function pause(wait) steps[#steps + 1] = { action = nil, wait = wait } end
    -- title -> main -> character select
    press(ButtonAction.ACTION_MENUCONFIRM, 40)      -- past the title screen (harmless elsewhere)
    steps[#steps + 1] = { jump = "CHARACTER", wait = 30 }
    -- the cursor remembers the last character: run it back to the first column
    for _ = 1, 18 do press(ButtonAction.ACTION_MENULEFT, 5) end
    for _ = 1, col do press(ButtonAction.ACTION_MENURIGHT, 8) end
    if tainted then press(ButtonAction.ACTION_MENUUP, 25) end
    press(ButtonAction.ACTION_MENUCONFIRM, 30)      -- character -> difficulty
    press(ButtonAction.ACTION_MENUCONFIRM, 30)      -- normal
    menuScript = steps; menuStep = 1; menuWait = 0
    log("menu script: new run as " .. who .. " (column " .. col .. (tainted and ", tainted" or "") .. ")")
end

local function startMenuContinue()
    local steps = {}
    steps[#steps + 1] = { action = ButtonAction.ACTION_MENUCONFIRM, wait = 40 }
    steps[#steps + 1] = { jump = "MAIN", wait = 30 }
    steps[#steps + 1] = { action = ButtonAction.ACTION_MENUCONFIRM, wait = 30 }   -- "Continue" is the first entry when a run exists
    menuScript = steps; menuStep = 1; menuWait = 0
    log("menu script: continue")
end

runMenuScript = function()
    menuPress = nil
    if not menuScript then return end
    if menuWait > 0 then menuWait = menuWait - 1; return end
    local st = menuScript[menuStep]
    if not st then menuScript = nil; return end
    if st.jump then menuJump(st.jump) end
    if st.action then menuPress = st.action end
    menuWait = st.wait or 10
    menuStep = menuStep + 1
end

-- commands that make sense while the game sits in its menus
applyMenuCommands = function()
    if not HAS_IO then return end
    local f = io.open(CMD_PATH, "r")
    if not f then return end
    local content = f:read("*a"); f:close()
    if not content or #content == 0 then return end
    local cmdSeq = tonumber(content:match("SEQ (%-?%d+)"))
    if not cmdSeq or cmdSeq == menuLastCmdSeq then return end
    menuLastCmdSeq = cmdSeq
    for line in content:gmatch("[^\r\n]+") do
        local op, rest = line:match("^(%u+)%s*(.*)$")
        if op == "NEWRUN" then startMenuRun(tonumber(rest) or 0)
        elseif op == "CONTINUE" then startMenuContinue() end
    end
end

mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, function(_, entity, hook, action)
    -- pause / unpause on Doom's behalf: one press of the pause button
    if action == ButtonAction.ACTION_PAUSE and pressPauseFrame >= 0 and renderTick >= pressPauseFrame - 1 then
        if hook == InputHook.IS_ACTION_TRIGGERED then pressPauseFrame = -1; return true end   -- one press, consumed
        if hook == InputHook.GET_ACTION_VALUE then return 1.0 end
        return true
    end
    -- menu driving: press the scripted button for this frame
    if menuPress ~= nil and action == menuPress then
        if hook == InputHook.GET_ACTION_VALUE then return 1.0 end
        return true
    end
    if not controlTaken or not entity or not entity:ToPlayer() then return nil end
    if action == ButtonAction.ACTION_DROP then
        local frame = Game():GetFrameCount()
        local held = frame <= dropHoldUntil
        if hook == InputHook.GET_ACTION_VALUE then return held and 1.0 or 0.0 end
        if hook == InputHook.IS_ACTION_PRESSED then return held end
        if hook == InputHook.IS_ACTION_TRIGGERED then return held and frame == dropStartFrame end
    end
    if MOVES[action] then
        -- forced walk (into a crawlspace ladder etc.): press the real move keys
        if walkDir and Game():GetFrameCount() <= walkUntil then
            local v = 0
            if action == ButtonAction.ACTION_LEFT then v = math.max(0, -walkDir.X)
            elseif action == ButtonAction.ACTION_RIGHT then v = math.max(0, walkDir.X)
            elseif action == ButtonAction.ACTION_UP then v = math.max(0, -walkDir.Y)
            else v = math.max(0, walkDir.Y) end
            if hook == InputHook.GET_ACTION_VALUE then return v end
            return v > 0.3
        end
        if hook == InputHook.GET_ACTION_VALUE then return 0 end
        return false
    end
    local axis = SHOOT[action]
    if axis then
        local frame = Game():GetFrameCount()
        local firing = fireDir ~= nil and frame <= fireHoldUntil
        local v = firing and axis(fireDir) or 0
        if hook == InputHook.GET_ACTION_VALUE then
            if ANALOG_AIM and ANALOG_MIRROR and firing then
                local opp = OPPOSITE[action]
                v = opp and SHOOT[opp](fireDir) or v
            end
            -- not shooting: a faint stick deflection toward Doom's crosshair, under the game's
            -- firing deadzone, so aim-following things (Finger!, the head) keep pointing
            -- where you look between shots
            if not firing and aimDir and IDLE_AIM > 0 then v = axis(aimDir) * IDLE_AIM end
            return v
        end
        local thr = ANALOG_AIM and 0.05 or 0.35
        if hook == InputHook.IS_ACTION_PRESSED then return v > thr end
        if hook == InputHook.IS_ACTION_TRIGGERED then return v > thr and frame == fireStartFrame end
    end
    return nil
end)

-- boost the tear sound: play it again on top of the game's own (once per frame)
local lastTearSoundFrame = -1
mod:AddCallback(ModCallbacks.MC_POST_FIRE_TEAR, function(_, tear)
    local frame = Game():GetFrameCount()
    if controlTaken and frame <= fireHoldUntil and firedByUs(tear) then
        noteShotDirection(tear.Velocity)
        local d = aimDelta()
        if d ~= 0 then tear.Velocity = tear.Velocity:Rotated(d) end
    end
    if frame ~= lastTearSoundFrame then
        lastTearSoundFrame = frame
        if isaacMuted then
            sfxQueue[#sfxQueue + 1] = { SoundEffect.SOUND_TEARS_FIRE, -1, -1 }
        elseif TEAR_SOUND_VOLUME > 1.0 then
            SFXManager():Play(SoundEffect.SOUND_TEARS_FIRE, TEAR_SOUND_VOLUME - 1.0, 0, false, 1.0)
        end
    end
end)

mod:AddCallback(ModCallbacks.MC_POST_GAME_STARTED, function(_, continued)
    knownItems = nil
    runCounter = runCounter + 1
    seenForms = {}
    if continued then
        -- transformations already held are not news
        local pl = Isaac.GetPlayer(0)
        for f = 0, 13 do pcall(function() if pl:HasPlayerForm(f) then seenForms[f] = true end end) end
    end
    -- a seeded run from Doom's menu: the character restart has happened, now the seed
    if pendingSeed and not continued then
        local sd = pendingSeed
        pendingSeed = nil
        log("applying seed " .. sd)
        Isaac.ExecuteCommand("seed " .. sd:sub(1, 4) .. " " .. sd:sub(5, 8))
    end
    newRoomFlag = Game():GetFrameCount()
    fireHoldUntil = -1
    seq = 0
    -- keep lastCmdSeq: the command file that asked for this restart is still on
    -- disk, and forgetting its sequence number would run RESTART again forever
    controlTaken = false
    log("game started, io=" .. tostring(HAS_IO) .. " pipe=" .. tostring(PIPE_DIR))
end)

mod:AddCallback(ModCallbacks.MC_PRE_GAME_EXIT, function()
    muteIsaac(false)
    -- back to the menus: tell Doom, so it offers its own menu again
    if HAS_IO and STATE_PATH then
        pcall(function()
            local f = io.open(STATE_PATH, "w")
            if f then f:write("SEQ -2\nMENU 1\nEND"); f:close() end
        end)
    end
end)

-- new floor: push the state out while Isaac is still showing its stage card,
-- so Doom's card runs at the same time instead of after the game resumes
mod:AddCallback(ModCallbacks.MC_POST_NEW_LEVEL, function()
    fallingNow = false
    introFlushRender = true
end)

-- the game boots into its menus: say so right away, so Doom doesn't pick up a stale run
if HAS_IO and STATE_PATH then
    pcall(function()
        local f = io.open(STATE_PATH, "w")
        if f then f:write("SEQ -2\nMENU 1\nEND"); f:close() end
    end)
end

log("loaded; io available = " .. tostring(HAS_IO))
