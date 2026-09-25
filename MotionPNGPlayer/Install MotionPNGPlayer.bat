@echo off
rem MotionPNGPlayer one-time setup (Windows): double-click to install.
rem For a COPY of this folder on a client machine (remote mode). Inside the
rem Artificial Girlfriend program folder it refuses to run - there the AG
rem installer at the folder root owns the MotionPNGPlayer setup.
rem No prerequisites: the Electron runtime zip (about 100 MB) is downloaded
rem straight from GitHub releases - Node.js/npm are NOT needed (2026-07-29).
cd /d "%~dp0"
echo == MotionPNGPlayer setup (Windows) ==

rem --- Network-location guard (2026-07-30): this distribution is for LOCAL
rem copies. A run on a file server / network drive is unsupported (and has
rem corrupted a distribution master once) - refuse before touching anything.
rem Label-free on purpose: keep this snippet identical across all 4 bats
rem (the addon Install bat is LF-ended where goto labels are unreliable).
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

rem The refusal must run BEFORE anything destructive: on the AG server
rem machine this folder's node_modules is the live runtime the backend
rem launches, and the clean-install step below would delete it.
if not exist "..\launcher\tray_app.py" goto not_in_ag
if not exist "..\backend\backend.py" goto not_in_ag
echo This copy of MotionPNGPlayer sits inside the Artificial Girlfriend
echo program folder, so this installer does nothing here.
echo Run "Install Artificial Girlfriend (Windows).bat" at the AG folder
echo root instead - it sets MotionPNGPlayer up too.
pause
exit /b 1
:not_in_ag

rem Asset\ is where users drop character motion folders. main.js creates it
rem at first launch, but nothing auto-launches after install, so create it
rem here for pre-launch asset drops (absent from fresh copies, 2026-07-30).
if not exist Asset mkdir Asset

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
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MotionPNGPlayer /t REG_SZ /d "\"%CD%\node_modules\electron\dist\electron.exe\" \"%CD%\"" /f >nul
echo Auto-start at sign-in: ON - toggle from the tray icon menu.

echo == Done. Start with "MotionPNGPlayer.bat" - the icon appears in
echo    the task tray; set the AG server URL in its settings. ==
pause
