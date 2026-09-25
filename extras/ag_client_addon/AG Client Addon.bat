@echo off
rem AG Client Addon launcher (Windows: double-click to start).
rem Starts the Electron runtime detached ("start") so no console window
rem lingers while the addon runs - the tray icon is the only UI.
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist "node_modules\electron\dist\electron.exe" (
    echo ERROR: Electron runtime not found. Run "Install AG Client Addon.bat" first.
    pause
    exit /b 1
)
start "" "node_modules\electron\dist\electron.exe" .
