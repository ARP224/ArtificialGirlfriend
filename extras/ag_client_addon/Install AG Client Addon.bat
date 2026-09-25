@echo off
rem AG Client Addon one-time setup (Windows): double-click to install.
rem Installs the Windows Electron runtime into node_modules.
rem No prerequisites: the Electron runtime zip (about 100 MB) is downloaded
rem straight from GitHub releases - Node.js/npm are NOT needed (2026-07-29).
rem NOTE: this file is LF-ended - keep it goto/label-free (cmd's label
rem scanning is unreliable in LF files).
cd /d "%~dp0"
echo == AG Client Addon setup (Windows) ==

rem --- Network-location guard (2026-07-30): this distribution is for LOCAL
rem copies. A run on a file server / network drive is unsupported (and has
rem corrupted a distribution master once) - refuse before touching anything.
set "NETLOC="
set "DRV=%~d0"
if "%DRV:~0,2%"=="\\" set "NETLOC=1"
net use "%DRV%" >nul 2>nul
if not errorlevel 1 set "NETLOC=1"
if defined NETLOC (
    echo ERROR: This folder is on a network location - a file server or a
    echo network drive. Copy the folder to this PC's local disk and run the
    echo installer there. Nothing was installed.
    pause
    exit /b 1
)

rem curl/tar ship with Windows 10 1803+.
where curl >nul 2>nul
if errorlevel 1 (
    echo ERROR: curl not found. Windows 10 1803 or later is required.
    pause
    exit /b 1
)
where tar >nul 2>nul
if errorlevel 1 (
    echo ERROR: tar not found. Windows 10 1803 or later is required.
    pause
    exit /b 1
)

rem Electron version: the single source of truth is package.json.
set "EVER="
for /f tokens^=4^ delims^=^" %%v in ('findstr /C:"\"electron\"" package.json') do set "EVER=%%v"
if "%EVER%"=="" (
    echo ERROR: could not read the Electron version from package.json.
    pause
    exit /b 1
)

rem Download BEFORE deleting anything - a failed download must not destroy
rem a working install.
set "EZIP=%TEMP%\electron-v%EVER%-win32-x64.zip"
echo Downloading Electron v%EVER% (about 100 MB)...
curl -fL --retry 3 -o "%EZIP%" "https://github.com/electron/electron/releases/download/v%EVER%/electron-v%EVER%-win32-x64.zip"
if errorlevel 1 (
    echo ERROR: download failed. Check your internet connection and retry.
    pause
    exit /b 1
)

rem A node_modules copied from another OS holds the wrong Electron binary -
rem always start clean. The layout written here is a working subset of what
rem npm used to create (dist\ + path.txt only; no cli.js/package metadata).
if exist node_modules (
    echo Removing any existing node_modules...
    rmdir /s /q node_modules
)
mkdir node_modules\electron\dist
tar -xf "%EZIP%" -C node_modules\electron\dist
if errorlevel 1 (
    echo ERROR: extraction failed.
    pause
    exit /b 1
)
del "%EZIP%" 2>nul
>node_modules\electron\path.txt echo electron.exe

if not exist "node_modules\electron\dist\electron.exe" (
    echo ERROR: electron.exe missing after extraction.
    pause
    exit /b 1
)
echo Electron version:
"node_modules\electron\dist\electron.exe" --version

rem Auto-start at sign-in (default ON - toggle from the tray icon menu).
rem Value name must stay in sync with RUN_VALUE in main.js; the
rem uninstaller deletes this value.
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AGClientAddon /t REG_SZ /d "\"%CD%\node_modules\electron\dist\electron.exe\" \"%CD%\"" /f >nul
echo Auto-start at sign-in: ON - toggle from the tray icon menu.

echo == Done ==
pause
