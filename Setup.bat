@echo off
rem IsaacDoom one-time setup: builds the Doom-side packs from YOUR copy of Isaac,
rem installs the mod, finds/downloads a Doom engine and IWAD, writes settings.
rem Re-run it any time (after updating IsaacDoom, or if paths change).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1" %*
