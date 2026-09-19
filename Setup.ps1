# IsaacDoom setup - run through Setup.bat.
#
# Builds everything IsaacDoom needs from YOUR copy of The Binding of Isaac: Repentance.
# Nothing from the game is shipped with IsaacDoom; this script unpacks the game's own
# resources (with the extractor that comes with the game) and turns them into the
# Doom-side packs on your machine. You need to own Isaac (with Repentance) on Steam,
# and a Doom engine (GZDoom) plus a Doom IWAD - the free Freedoom one works.
#
# What it does, in order:
#   1. makes sure the project lives at Documents\IsaacDoom (the mod and the bridge talk there)
#   2. finds Python 3 and installs Pillow + numpy
#   3. finds your Isaac install (Steam libraries) and unpacks its resources
#   4. finds or downloads a Doom engine, finds or downloads an IWAD (Freedoom)
#   5. builds the sprite / UI / sound / music packs and the arena pk3
#   6. installs the Isaac mod, adjusts Isaac's options.ini, writes settings, makes a shortcut
#   7. tells you the one thing it can't do for you: Steam launch options.
param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Docs = [Environment]::GetFolderPath("MyDocuments")
$Proj = Join-Path $Docs "IsaacDoom"

function Say($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)  { Write-Host "   $t" -ForegroundColor Green }
function Warn($t){ Write-Host "   $t" -ForegroundColor Yellow }
function Fail($t){ Write-Host ""; Write-Host "!! $t" -ForegroundColor Red; Write-Host ""; Read-Host "Press Enter to close"; exit 1 }

# ---------------------------------------------------------------- 1. location
Say "Project folder"
if ((Resolve-Path $Here).Path.TrimEnd('\') -ine $Proj.TrimEnd('\')) {
    Warn "IsaacDoom has to live at $Proj (the Isaac mod can only find its pipe there)."
    Warn "Copying this folder there now..."
    New-Item -ItemType Directory -Force -Path $Proj | Out-Null
    robocopy $Here $Proj /E /NFL /NDL /NJH /NJS /XD pipe build __pycache__ | Out-Null
    Ok "copied. Continuing from $Proj"
}
Set-Location $Proj
New-Item -ItemType Directory -Force -Path (Join-Path $Proj "pipe") | Out-Null
Ok $Proj

# ---------------------------------------------------------------- 2. python
Say "Python"
$py = $null
foreach ($c in @("python", "python3", "py")) {
    try { $v = & $c --version 2>&1; if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3") { $py = $c; break } } catch {}
}
if (-not $py) {
    Fail "Python 3 was not found. Install it from https://www.python.org/downloads/ (tick 'Add python to PATH'), then run Setup again."
}
Ok "$py ($(& $py --version 2>&1))"
& $py -m pip install --user --quiet pillow numpy 2>&1 | Out-Null
try { & $py -c "import PIL, numpy" 2>$null; if ($LASTEXITCODE -ne 0) { throw "x" } ; Ok "Pillow and numpy ready" }
catch { Fail "Could not install Pillow/numpy. Run:  $py -m pip install pillow numpy   and try again." }

# ---------------------------------------------------------------- 3. isaac
Say "The Binding of Isaac: Repentance"
function Find-Isaac {
    $cands = @()
    $steam = $null
    foreach ($k in @("HKCU:\Software\Valve\Steam", "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam", "HKLM:\SOFTWARE\Valve\Steam")) {
        try { $p = (Get-ItemProperty $k -ErrorAction Stop)
              foreach ($n in @("SteamPath", "InstallPath")) { if ($p.$n) { $steam = $p.$n; break } } } catch {}
        if ($steam) { break }
    }
    $libs = @()
    if ($steam) {
        $libs += $steam
        $vdf = Join-Path $steam "steamapps\libraryfolders.vdf"
        if (Test-Path $vdf) {
            foreach ($m in [regex]::Matches((Get-Content $vdf -Raw), '"path"\s+"([^"]+)"')) { $libs += ($m.Groups[1].Value -replace '\\\\', '\') }
        }
    }
    $libs += @("C:\Program Files (x86)\Steam", "C:\Program Files\Steam", "D:\Steam", "D:\SteamLibrary", "E:\SteamLibrary")
    foreach ($l in $libs) {
        $g = Join-Path $l "steamapps\common\The Binding of Isaac Rebirth"
        if (Test-Path (Join-Path $g "isaac-ng.exe")) { return $g }
    }
    return $null
}
$game = Find-Isaac
if (-not $game) {
    $game = Read-Host "Isaac wasn't found in your Steam libraries. Paste the folder that contains isaac-ng.exe"
    if (-not (Test-Path (Join-Path $game "isaac-ng.exe"))) { Fail "No isaac-ng.exe there." }
}
Ok $game
if (-not (Test-Path (Join-Path $game "resources-dlc3"))) { Fail "This Isaac has no Repentance content (no resources-dlc3 folder). IsaacDoom needs Repentance." }
if (Get-Process isaac-ng -ErrorAction SilentlyContinue) { Fail "Isaac is running. Close it and run Setup again." }

# unpack the game's resources with the game's own extractor (needed for the sprite packs)
$gfxBase = Join-Path $game "resources\gfx"
$gfxDlc  = Join-Path $game "resources-dlc3\gfx"
if (-not (Test-Path $gfxDlc)) {
    $ext = Join-Path $game "tools\ResourceExtractor\ResourceExtractor.exe"
    if (Test-Path $ext) {
        Warn "Unpacking the game's resources with its ResourceExtractor (a few minutes, once)..."
        Push-Location (Split-Path $ext)
        try { & $ext 2>&1 | Out-Null } catch {}
        if (-not (Test-Path $gfxDlc)) {
            # some builds want the folders spelled out
            try { & $ext (Join-Path $game "resources\packed") (Join-Path $game "resources") 2>&1 | Out-Null } catch {}
            try { & $ext (Join-Path $game "resources-dlc3\packed") (Join-Path $game "resources-dlc3") 2>&1 | Out-Null } catch {}
        }
        Pop-Location
    }
    if (-not (Test-Path $gfxDlc)) {
        Fail "The game's resources are still packed. Run  $ext  yourself (right-click > Run as administrator if the game folder is protected); it creates resources\gfx and resources-dlc3\gfx. Then run Setup again."
    }
}
Ok "resources unpacked"

# ---------------------------------------------------------------- 4. doom engine + iwad
Say "Doom engine"
$engineDir = Join-Path $Proj "engine"
New-Item -ItemType Directory -Force -Path $engineDir | Out-Null
$settings = @{}
if (Test-Path (Join-Path $Proj "settings.ini")) {
    foreach ($line in Get-Content (Join-Path $Proj "settings.ini")) { if ($line -match '^\s*([^#=]+)=(.*)$') { $settings[$matches[1].Trim().ToLower()] = $matches[2].Trim() } }
}
$gz = $settings["gzdoom"]
if (-not ($gz -and (Test-Path $gz))) {
    $gz = $null
    foreach ($c in @((Join-Path $engineDir "gzdoom.exe"), (Join-Path $engineDir "uzdoom.exe"), "C:\Games\Doom\uzdoom.exe", "C:\Games\Doom\gzdoom.exe",
                     "C:\Program Files\GZDoom\gzdoom.exe", "C:\Program Files (x86)\GZDoom\gzdoom.exe", "$env:USERPROFILE\Desktop\gzdoom\gzdoom.exe")) {
        if (Test-Path $c) { $gz = $c; break }
    }
}
if (-not $gz) {
    $ans = Read-Host "No GZDoom found. Download the latest GZDoom into $engineDir now? [Y/n]"
    if ($ans -eq "" -or $ans -match '^[Yy]') {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $rel = Invoke-RestMethod "https://api.github.com/repos/ZDoom/gzdoom/releases/latest" -Headers @{ "User-Agent" = "IsaacDoom-setup" }
            $asset = $rel.assets | Where-Object { $_.name -match 'windows' -and $_.name -match '\.zip$' -and $_.name -notmatch 'arm' } | Select-Object -First 1
            if (-not $asset) { throw "no windows zip in the release" }
            $zip = Join-Path $env:TEMP $asset.name
            Warn "downloading $($asset.name)..."
            Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath $engineDir -Force
            $found = Get-ChildItem $engineDir -Recurse -Filter gzdoom.exe | Select-Object -First 1
            if ($found) { $gz = $found.FullName }
        } catch { Warn "download failed: $($_.Exception.Message)" }
    }
    if (-not $gz) {
        $gz = Read-Host "Paste the full path to gzdoom.exe (or uzdoom.exe)"
        if (-not (Test-Path $gz)) { Fail "No engine there. Get GZDoom from https://zdoom.org/downloads and run Setup again." }
    }
}
Ok $gz
$gzDir = Split-Path $gz

Say "Doom IWAD"
$iwad = $settings["iwad"]
if (-not ($iwad -and (Test-Path $iwad))) {
    $iwad = $null
    $names = @("doom2.wad", "DOOM2.WAD", "freedoom2.wad")
    $dirs = @($engineDir, $gzDir, "C:\Games\Doom", "$env:USERPROFILE\Documents\My Games\GZDoom")
    foreach ($l in @("C:\Program Files (x86)\Steam", "D:\SteamLibrary", "E:\SteamLibrary")) {
        $dirs += (Join-Path $l "steamapps\common\Doom 2\base"); $dirs += (Join-Path $l "steamapps\common\Doom 2\masterbase\doom2"); $dirs += (Join-Path $l "steamapps\common\Doom 2\rerelease")
    }
    foreach ($d in $dirs) { foreach ($n in $names) { $c = Join-Path $d $n; if (-not $iwad -and (Test-Path $c)) { $iwad = $c } } }
}
if (-not $iwad) {
    $ans = Read-Host "No Doom II / Freedoom IWAD found. Download Freedoom (free, open) into $engineDir now? [Y/n]"
    if ($ans -eq "" -or $ans -match '^[Yy]') {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $rel = Invoke-RestMethod "https://api.github.com/repos/freedoom/freedoom/releases/latest" -Headers @{ "User-Agent" = "IsaacDoom-setup" }
            $asset = $rel.assets | Where-Object { $_.name -match '^freedoom-[\d.]+\.zip$' } | Select-Object -First 1
            if (-not $asset) { throw "no freedoom zip in the release" }
            $zip = Join-Path $env:TEMP $asset.name
            Warn "downloading $($asset.name)..."
            Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
            $tmp = Join-Path $env:TEMP "freedoom_unzip"
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $fd = Get-ChildItem $tmp -Recurse -Filter freedoom2.wad | Select-Object -First 1
            if ($fd) { Copy-Item $fd.FullName (Join-Path $engineDir "freedoom2.wad") -Force; $iwad = Join-Path $engineDir "freedoom2.wad" }
        } catch { Warn "download failed: $($_.Exception.Message)" }
    }
    if (-not $iwad) {
        $iwad = Read-Host "Paste the full path to doom2.wad or freedoom2.wad"
        if (-not (Test-Path $iwad)) { Fail "No IWAD there. Freedoom is at https://freedoom.github.io/ - run Setup again once you have it." }
    }
}
Ok $iwad

# settings for the launchers and the build tools
@("isaac=$game", "gzdoom=$gz", "gzdoomvr=$($settings['gzdoomvr'])", "iwad=$iwad") | Set-Content (Join-Path $Proj "settings.ini") -Encoding UTF8
@("set ISAAC_DIR=$game", "set GZDOOM=$gz", "set IWAD=$iwad", "set GZDOOMVR=$($settings['gzdoomvr'])") | Set-Content (Join-Path $Proj "settings.bat") -Encoding ASCII
Ok "settings.ini / settings.bat written"

# ---------------------------------------------------------------- 5. build
if (-not $SkipBuild) {
    Say "Building the Doom-side packs from your game files (the sprite pack takes a few minutes)"
    $env:ISAACDOOM_GAME = $game
    foreach ($t in @("tools\build_sprites.py", "tools\build_ui.py", "tools\build_sfx.py", "tools\build_music.py", "doom\build_pk3.py")) {
        Write-Host "   $t" -ForegroundColor DarkGray
        & $py (Join-Path $Proj $t)
        if ($LASTEXITCODE -ne 0) { Fail "$t failed (see the messages above)." }
    }
    foreach ($p in @("doom\isaacdoom.pk3", "doom\isaacsprites.pk3", "doom\isaacui.pk3", "doom\isaacsfx.pk3", "doom\isaacmusic.pk3")) {
        if (Test-Path (Join-Path $Proj $p)) { Ok $p } else { Warn "$p was not produced" }
    }
}

# ---------------------------------------------------------------- 6. install into isaac
Say "Isaac mod"
$modDst = Join-Path $game "mods\isaacdoom"
New-Item -ItemType Directory -Force -Path $modDst | Out-Null
Copy-Item (Join-Path $Proj "isaac_mod\isaacdoom\*") $modDst -Recurse -Force
Ok "installed to $modDst"

$opts = Get-ChildItem (Join-Path $Docs "My Games") -Directory -Filter "Binding of Isaac Repentance*" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "options.ini" } | Where-Object { Test-Path $_ }
foreach ($o in $opts) {
    $txt = Get-Content $o -Raw
    foreach ($kv in @(@("AimLock", "0"), @("PauseOnFocusLost", "0"), @("EnableDebugConsole", "1"), @("EnableMods", "1"))) {
        $k = $kv[0]; $v = $kv[1]
        if ($txt -match "(?m)^$k=") { $txt = [regex]::Replace($txt, "(?m)^$k=.*$", "$k=$v") } else { $txt = $txt.TrimEnd() + "`r`n$k=$v`r`n" }
    }
    Set-Content $o $txt -Encoding ASCII
    Ok "options.ini updated: $o  (AimLock=0, PauseOnFocusLost=0, debug console on, mods on)"
}
if (-not $opts) { Warn "options.ini not found yet (start Isaac once, then run Setup again so it can set AimLock=0 and PauseOnFocusLost=0)" }

# desktop shortcut
try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "IsaacDoom.lnk"))
    $lnk.TargetPath = Join-Path $Proj "IsaacDoom.bat"
    $lnk.WorkingDirectory = $Proj
    $lnk.IconLocation = "$gz,0"
    $lnk.Save()
    Ok "desktop shortcut: IsaacDoom"
} catch { Warn "could not create the desktop shortcut (run IsaacDoom.bat from $Proj)" }

# ---------------------------------------------------------------- 7. the manual step
Say "One thing to do by hand"
Write-Host "   In Steam: right-click The Binding of Isaac: Rebirth > Properties > General > Launch Options, and enter:" -ForegroundColor White
Write-Host ""
Write-Host "        --luadebug" -ForegroundColor Yellow
Write-Host ""
Write-Host "   (This lets the mod read and write its pipe files. Without it Doom can watch Isaac but not control it.)"
Write-Host "   Then double-click the IsaacDoom shortcut. Isaac starts through Steam, the bridge starts, and Doom opens on its menu."
Write-Host ""
Read-Host "Setup finished. Press Enter to close"
