@echo off
rem =====================================================================
rem  IsaacDoom VR - one-click launcher (gzdoomvr through SteamVR)
rem  Starts Isaac through Steam (your launch options supply --luadebug), waits for its window, starts the
rem  bridge minimised, then launches Doom on top with the menu ready.
rem  Make a shortcut to this file (right-click > Send to > Desktop).
rem =====================================================================
setlocal
set PROJ=%~dp0
if exist "%PROJ%settings.bat" call "%PROJ%settings.bat"
rem Isaac is started through Steam rather than isaac-ng.exe directly: launching the exe
rem with arguments makes Steam show its "custom arguments" prompt every time, while a
rem Steam launch silently uses the game's own Launch Options (set --luadebug there).
set ISAAC_STEAM=steam://rungameid/250900

rem --- 0) clear leftovers from the last session ---------------------------
rem a bridge left over from last time would keep stale state and block the new one
if exist "%PROJ%pipe\bridge.lock" (
    for /f %%p in ('type "%PROJ%pipe\bridge.lock"') do taskkill /f /pid %%p >nul 2>&1
    del "%PROJ%pipe\bridge.lock" >nul 2>&1
)
if exist "%PROJ%pipe\isaac_state.txt" del "%PROJ%pipe\isaac_state.txt"
if exist "%PROJ%pipe\isaac_cmd.txt" del "%PROJ%pipe\isaac_cmd.txt"
if exist "%PROJ%pipe\isaac_alive.txt" del "%PROJ%pipe\isaac_alive.txt"

rem --- 1) Isaac (skip if already running) --------------------------------
tasklist /fi "imagename eq isaac-ng.exe" 2>nul | find /i "isaac-ng.exe" >nul
if errorlevel 1 (
    echo Starting Isaac...
    start "" "%ISAAC_STEAM%"
) else (
    echo Isaac is already running.
)

rem --- 2) wait for Isaac's window (up to 90 s) ---------------------------
powershell -NoProfile -Command "$t=0; while(-not (Get-Process isaac-ng -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }) -and $t -lt 90) { Start-Sleep 1; $t++ }; Start-Sleep 2"

rem --- 3) bridge, minimised (it refuses to run twice by itself) ----------
start "IsaacDoom bridge" /min cmd /c "python "%PROJ%bridge\bridge.py""

rem --- 4) Doom, in front ---------------------------------------------------
call "%PROJ%launch_gzdoomvr.bat"
