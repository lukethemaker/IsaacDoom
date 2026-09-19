@echo off
rem IsaacDoom - launch GZDoom with the bridge mod.
rem Defaults; settings.bat (written by Setup.bat) overrides them.
set GZDOOM=C:\Games\Doom\uzdoom.exe
set IWAD=C:\Games\Doom\DOOM2.WAD

set PROJ=%~dp0
set PROJ=%PROJ:~0,-1%
rem Setup.bat writes settings.bat (engine, IWAD, Isaac folder); edit it by hand if paths change
if exist "%PROJ%\settings.bat" call "%PROJ%\settings.bat"
if "%GZDOOMVR%"=="" set GZDOOMVR=%GZDOOM%
set PIPE=%PROJ%\pipe
if not exist "%PIPE%" mkdir "%PIPE%"
copy /y "%PROJ%\doom\isaac_boot.cfg" "%PIPE%\isaac_boot.cfg" >nul
if not exist "%PIPE%\isaac_in.cfg" echo set isaac_seq 0 > "%PIPE%\isaac_in.cfg"
rem keep the previous session's log around for diagnosis, then start clean
if exist "%PIPE%\doom_out.log" move /y "%PIPE%\doom_out.log" "%PIPE%\last_doom_log.txt" >nul
if exist "%PIPE%\log-doom_out-log.txt" move /y "%PIPE%\log-doom_out-log.txt" "%PIPE%\last_doom_log.txt" >nul

cd /d "%PIPE%"
set SPRITES=
if exist "%PROJ%\doom\isaacsprites.pk3" set SPRITES=-file "%PROJ%\doom\isaacsprites.pk3"
if exist "%PROJ%\doom\isaacui.pk3" set SPRITES=%SPRITES% -file "%PROJ%\doom\isaacui.pk3"
if exist "%PROJ%\doom\isaacsfx.pk3" set SPRITES=%SPRITES% -file "%PROJ%\doom\isaacsfx.pk3"
if exist "%PROJ%\doom\isaacmusic.pk3" set SPRITES=%SPRITES% -file "%PROJ%\doom\isaacmusic.pk3"
"%GZDOOM%" -iwad "%IWAD%" -file "%PROJ%\doom\isaacdoom.pk3" %SPRITES% -warp 1 -skill 3 +logfile doom_out.log +exec isaac_boot.cfg
