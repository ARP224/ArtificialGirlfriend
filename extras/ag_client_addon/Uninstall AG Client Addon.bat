@echo off
rem ; echo "ERROR: This uninstaller is for Windows only. On macOS run 'Uninstall AG Client Addon.command' instead." ; exit 1
rem ^ OS guard polyglot line (same pattern as the installer): cmd.exe reads
rem   the whole line as a comment, but a POSIX shell executes the echo+exit
rem   after the failing 'rem' command, so a mis-run stops here.
rem
rem AG Client Addon uninstaller (Windows): double-click to remove.
rem Deletes this addon folder (including node_modules), the per-user addon
rem settings and the Electron download cache (leftover of the old npm-based
rem setup, pre 2026-07-29). Refuses to
rem run when this folder sits inside the Artificial Girlfriend program
rem folder - there the main AG uninstaller owns the removal.
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
set "ADDON=%~dp0"
if "%ADDON:~-1%"=="\" set "ADDON=%ADDON:~0,-1%"
set "STAGED=%TEMP%\ag_addon_uninstall_%RANDOM%%RANDOM%.bat"
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
start "AG Client Addon Uninstaller" cmd /c ""%STAGED%" RUN "%ADDON%""
exit /b 0

:run
rem === Stage 2: runs from %TEMP%; %2 = addon path (no trailing backslash) ===
rem cd out of the folder first - a process's working directory locks the
rem directory against deletion.
cd /d "%TEMP%"
set "ADDON=%~2"

rem Safety: only ever delete a folder that actually looks like the addon.
if not exist "%ADDON%\main.js" goto not_addon
if not exist "%ADDON%\package.json" goto not_addon
goto addon_ok
:not_addon
echo ERROR: "%ADDON%" does not look like an AG Client Addon folder.
echo Nothing was removed.
pause
del "%~f0" & exit /b 1
:addon_ok

rem Refuse to run inside the AG program folder (ryo ruling 2026-07-28):
rem there the addon is part of the AG install itself - removing only the
rem addon is never a normal operation, and the main uninstaller
rem ("Uninstall Artificial Girlfriend (Windows).bat") owns that folder.
if not exist "%ADDON%\..\..\launcher\tray_app.py" goto not_in_ag
if not exist "%ADDON%\..\..\backend\backend.py" goto not_in_ag
echo This copy of AG Client Addon sits inside the Artificial Girlfriend
echo program folder, so this uninstaller does nothing here.
echo To remove AG as a whole, run "Uninstall Artificial Girlfriend (Windows).bat".
echo Nothing was removed.
pause
del "%~f0" & exit /b 1
:not_in_ag

echo == AG Client Addon uninstaller ==
echo.
echo This will PERMANENTLY delete:
echo   - The addon folder:        %ADDON%
echo   - Addon settings:          %APPDATA%\AG Client Addon
echo   - Electron download cache: %LOCALAPPDATA%\electron
echo   - Auto-start registration  - registry, current user
echo.
set "CONFIRM="
rem Typed word, same as every other uninstaller (ryo ruling 2026-07-30:
rem one confirmation convention across the board).
set /p "CONFIRM=Type Uninstall (exactly, case-sensitive) and press Enter: "
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
echo Stopping AG Client Addon...
rem Passed via environment variable (not inline) so path specials never meet
rem the PowerShell parser. Matched literally, lowercased, with a trailing
rem backslash so a sibling folder can never match. Two match branches:
rem   1) any process whose executable lives under the addon folder (the
rem      Electron runtime in node_modules)
rem   2) node.exe (npx wrapper) whose command line references the folder
rem Killing Electron makes the npx wrapper and its console window exit on
rem their own; two passes with a pause cover stragglers.
set "AG_ADDON=%ADDON%"
powershell -NoProfile -Command "& { $r=$env:AG_ADDON.ToLowerInvariant()+'\'; function AddonProcs { Get-CimInstance Win32_Process | Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($r)) -or (($_.Name -match '^(electron|node)\.exe$') -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($r)) } }; AddonProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2; AddonProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 1; exit (AddonProcs | Measure-Object).Count }"
if errorlevel 1 (
    echo ERROR: the running AG Client Addon could not be stopped.
    echo Nothing was removed. Quit it from the tray icon and run this again.
    pause
    del "%~f0" & exit /b 1
)

rem === Destructive part starts here. Order: external traces first, the
rem === addon folder LAST - if this window is closed mid-run, the re-run
rem === entry point (the uninstaller inside the folder) still exists and
rem === every step below is idempotent.
echo Removing the auto-start registration...
rem Registered by the installer (default ON) / toggled from the tray menu.
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AGClientAddon /f >nul 2>&1

echo Removing addon settings...
rd /s /q "%APPDATA%\AG Client Addon" 2>nul
rem ag-client-addon = defensive: userData if Electron ever resolved the
rem package.json "name" instead of productName.
rd /s /q "%APPDATA%\ag-client-addon" 2>nul
rem Electron download cache created by the installer's "npm install"
rem (re-downloadable; the generic %%APPDATA%%\Electron dir is shared with
rem other apps and is deliberately NOT touched).
rd /s /q "%LOCALAPPDATA%\electron" 2>nul

echo Removing the addon folder - this may take a while...
set /a ATTEMPT=0
:delete_addon
rd /s /q "%ADDON%" 2>nul
if not exist "%ADDON%" goto addon_removed
rem rd can fail transiently (straggling handles, deep node_modules trees):
rem retry, then fall back to PowerShell with a long-path prefix.
set /a ATTEMPT+=1
if %ATTEMPT% lss 3 (
    timeout /t 2 /nobreak >nul
    goto delete_addon
)
powershell -NoProfile -Command "Remove-Item -LiteralPath $env:AG_ADDON -Recurse -Force -ErrorAction SilentlyContinue; if (Test-Path -LiteralPath $env:AG_ADDON) { Remove-Item -LiteralPath ('\\?\'+$env:AG_ADDON) -Recurse -Force -ErrorAction SilentlyContinue }"
if not exist "%ADDON%" goto addon_removed
echo WARN: could not fully remove "%ADDON%".
echo       Close any window or program using that folder, then delete it manually.
echo.
echo == Uninstall finished with warnings - see above ==
goto summary_done
:addon_removed
echo Addon folder removed.
echo.
echo == Uninstall complete ==
:summary_done
echo.
pause
del "%~f0" & exit /b 0
