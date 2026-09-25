@echo off
rem MotionPNGPlayer launcher (Windows: double-click to start).
rem Starts the Electron runtime detached ("start") so no console window
rem lingers while the player runs - the tray icon is the only UI.
rem (AG's local mode does NOT use this file - the backend spawns Electron
rem itself; this launcher is for remote mode / manual starts.)
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist "node_modules\electron\dist\electron.exe" (
    echo ERROR: Electron runtime not found. Run "Install MotionPNGPlayer.bat" first.
    pause
    exit /b 1
)
start "" "node_modules\electron\dist\electron.exe" .
