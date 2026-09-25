@echo off
rem ; echo "ERROR: This uninstaller is for Windows only. On macOS run 'Uninstall MotionPNGPlayer.command' instead." ; exit 1
rem ^ OS guard polyglot line (same pattern as the AG installers): cmd.exe
rem   reads the whole line as a comment, but a POSIX shell executes the
rem   echo+exit after the failing 'rem' command, so a mis-run stops here.
rem
rem MotionPNGPlayer uninstaller (Windows): double-click to remove.
rem For a COPY of this folder on a client machine (remote mode). Deletes
rem this folder (including node_modules and ALL character assets in Asset),
rem the per-user player settings and the Electron download cache (leftover
rem of the old npm-based setup, pre 2026-07-29).
rem Refuses to run when this folder sits inside the Artificial
rem Girlfriend program folder - there the main AG uninstaller owns the
rem removal.
rem
rem DisableDelayedExpansion explicitly: delayed expansion can be enabled
rem machine-wide via registry, and it would corrupt paths containing "!".
setlocal DisableDelayedExpansion
if /i "%~1"=="RUN" goto run

rem --- Network-location guard (2026-07-30): this distribution is for LOCAL
rem copies. A run on a file server / network drive is unsupported (and has
rem corrupted a distribution master once) - refuse before touching anything.
rem To remove a copy on a file server, just delete the folder there - a
rem network copy holds no per-user settings on this machine.
set "NETLOC="
set "DRV=%~d0"
if "%DRV:~0,2%"=="\\" set "NETLOC=1"
net use "%DRV%" >nul 2>nul
if not errorlevel 1 set "NETLOC=1"
if defined NETLOC (
    echo ERROR: This folder is on a network location - a file server or a
    echo network drive. Nothing was removed.
    echo To remove a copy on a file server, simply delete the folder there.
    pause
    exit /b 1
)

rem === Stage 1: re-launch a staged copy from %TEMP% ===
rem This file lives inside the folder it deletes, and cmd reads batch files
rem incrementally from disk - deleting the running file mid-run would cut
rem execution off. Stage a uniquely-named copy outside the folder and hand
rem it the folder path. (Unique name: a fixed name could be overwritten by
rem an accidental second double-click while the first copy is still running.)
set "MPP=%~dp0"
if "%MPP:~-1%"=="\" set "MPP=%MPP:~0,-1%"
set "STAGED=%TEMP%\mpp_uninstall_%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%STAGED%" >nul
if errorlevel 1 (
    echo ERROR: could not stage the uninstaller in the TEMP folder.
    echo Nothing was removed.
    pause
    exit /b 1
)
rem Explicit cmd /c with doubled outer quotes. A bare start on a .bat runs
rem it through an implicit "cmd /K <line>", and cmd's quote heuristic strips
rem the FIRST and LAST quote of the line when it holds more than two quotes -
rem the path turns into C:\...\x.bat" RUN "C:\... = "volume label syntax is
rem incorrect" + a lingering interactive prompt (ryo sub-OS 2026-07-30,
rem reproduced on the dev machine the same day). The doubled outer quotes
rem make that same stripping rule yield exactly the intended command.
start "MotionPNGPlayer Uninstaller" cmd /c ""%STAGED%" RUN "%MPP%""
exit /b 0

:run
rem === Stage 2: runs from %TEMP%; %2 = player path (no trailing backslash) ===
rem cd out of the folder first - a process's working directory locks the
rem directory against deletion.
cd /d "%TEMP%"
set "MPP=%~2"

rem Safety: only ever delete a folder that actually looks like the player
rem (lipsync.js is unique to MotionPNGPlayer - a mis-aimed run against some
rem other Electron folder must stop here).
if not exist "%MPP%\main.js" goto not_mpp
if not exist "%MPP%\lipsync.js" goto not_mpp
goto mpp_ok
:not_mpp
echo ERROR: "%MPP%" does not look like a MotionPNGPlayer folder.
echo Nothing was removed.
pause
del "%~f0" & exit /b 1
:mpp_ok

rem Refuse to run inside the AG program folder (ryo ruling 2026-07-28, same
rem as AG Client Addon): there the player is part of the AG install itself -
rem the main uninstaller ("Uninstall Artificial Girlfriend (Windows).bat")
rem owns that folder, and AG's local mode depends on this player.
if not exist "%MPP%\..\launcher\tray_app.py" goto not_in_ag
if not exist "%MPP%\..\backend\backend.py" goto not_in_ag
echo This copy of MotionPNGPlayer sits inside the Artificial Girlfriend
echo program folder, so this uninstaller does nothing here.
echo To remove AG as a whole, run "Uninstall Artificial Girlfriend (Windows).bat".
echo Nothing was removed.
pause
del "%~f0" & exit /b 1
:not_in_ag

echo == MotionPNGPlayer uninstaller ==
echo.
echo This will PERMANENTLY delete:
echo   - The player folder:       %MPP%
echo     including ALL character assets in Asset
echo   - Player settings:         %APPDATA%\MotionPNGPlayer
echo   - Electron download cache: %LOCALAPPDATA%\electron
echo   - Auto-start registration  - registry, current user
echo.
set "CONFIRM="
set /p "CONFIRM=Type Uninstall (exactly, case-sensitive) and press Enter: "
rem Typed word (not just "y"): Asset may hold user-made character material.
rem Delayed expansion only around this comparison: it makes the comparison
rem safe against quotes/specials in the typed input, but would corrupt "!"
rem in paths if enabled globally.
setlocal EnableDelayedExpansion
if "!CONFIRM!"=="Uninstall" (
    endlocal
    goto confirmed
)
endlocal
echo.
echo Cancelled. Nothing was removed.
pause
del "%~f0" & exit /b 1

:confirmed
echo.
echo Stopping MotionPNGPlayer...
rem Passed via environment variable (not inline) so path specials never meet
rem the PowerShell parser. Matched literally, lowercased, with a trailing
rem backslash so a sibling folder can never match. Two match branches:
rem   1) any process whose executable lives under the player folder (the
rem      Electron runtime in node_modules)
rem   2) node.exe (npx wrapper) whose command line references the folder
rem Two passes with a pause between them cover stragglers.
set "MPP_KILL=%MPP%"
powershell -NoProfile -Command "& { $r=$env:MPP_KILL.ToLowerInvariant()+'\'; function MppProcs { Get-CimInstance Win32_Process | Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($r)) -or (($_.Name -match '^(electron|node)\.exe$') -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($r)) } }; MppProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2; MppProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 1; exit (MppProcs | Measure-Object).Count }"
if errorlevel 1 (
    echo ERROR: the running MotionPNGPlayer could not be stopped.
    echo Nothing was removed. Quit it from the tray icon and run this again.
    pause
    del "%~f0" & exit /b 1
)

rem === Destructive part starts here. Order: external traces first, the
rem === player folder LAST - if this window is closed mid-run, the re-run
rem === entry point (the uninstaller inside the folder) still exists and
rem === every step below is idempotent.
echo Removing the auto-start registration...
rem Registered by the installer (default ON) / toggled from the tray menu.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MotionPNGPlayer /f >nul 2>&1

echo Removing player settings...
rd /s /q "%APPDATA%\MotionPNGPlayer" 2>nul
rem motion-pngtuber-player = pre-rename Electron userData.
rd /s /q "%APPDATA%\motion-pngtuber-player" 2>nul
rem Electron download cache created by the installer's "npm install"
rem (re-downloadable; the generic %%APPDATA%%\Electron dir is shared with
rem other apps and is deliberately NOT touched).
rd /s /q "%LOCALAPPDATA%\electron" 2>nul

echo Removing the player folder - this may take a while...
set /a ATTEMPT=0
:delete_mpp
rd /s /q "%MPP%" 2>nul
if not exist "%MPP%" goto mpp_removed
rem rd can fail transiently (straggling handles, deep node_modules trees):
rem retry, then fall back to PowerShell with a long-path prefix.
set /a ATTEMPT+=1
if %ATTEMPT% lss 3 (
    timeout /t 2 /nobreak >nul
    goto delete_mpp
)
powershell -NoProfile -Command "Remove-Item -LiteralPath $env:MPP_KILL -Recurse -Force -ErrorAction SilentlyContinue; if (Test-Path -LiteralPath $env:MPP_KILL) { Remove-Item -LiteralPath ('\\?\'+$env:MPP_KILL) -Recurse -Force -ErrorAction SilentlyContinue }"
if not exist "%MPP%" goto mpp_removed
echo WARN: could not fully remove "%MPP%".
echo       Close any window or program using that folder, then delete it manually.
echo.
echo == Uninstall finished with warnings - see above ==
goto summary_done
:mpp_removed
echo Player folder removed.
echo.
echo == Uninstall complete ==
:summary_done
echo.
pause
del "%~f0" & exit /b 0
