@echo off
rem ; echo "ERROR: This uninstaller is for Windows only. On macOS run 'Uninstall Artificial Girlfriend (Mac).command' instead." ; exit 1
rem ^ OS guard polyglot line (same pattern as the installer): cmd.exe reads
rem   the whole line as a comment, but a POSIX shell executes the echo+exit
rem   after the failing 'rem' command, so a mis-run stops here.
rem
rem Artificial Girlfriend uninstaller (Windows): double-click to remove.
rem Deletes the program folder (including ALL conversation memories,
rem characters, logs and TTS models), the per-user settings, the auto-start
rem registration, MotionPNGPlayer data, and the Whisper/BERT models this app
rem downloaded into the HuggingFace cache. Nothing is removed until you type
rem "Uninstall" at the confirmation prompt. Shared tools (Python, Ollama)
rem are kept.
rem
rem DisableDelayedExpansion explicitly: delayed expansion can be enabled
rem machine-wide via registry, and it would corrupt paths containing "!".
setlocal DisableDelayedExpansion
if /i "%~1"=="RUN" goto run

rem === Stage 1: re-launch a staged copy from %TEMP% ===
rem This file lives inside the folder it deletes, and cmd reads batch files
rem incrementally from disk - deleting the running file mid-run would cut
rem execution off. Stage a uniquely-named copy outside the repo and hand it
rem the repo path. (Unique name: a fixed name could be overwritten by an
rem accidental second double-click while the first copy is still running.)
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "STAGED=%TEMP%\ag_uninstall_%RANDOM%%RANDOM%.bat"
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
start "Artificial Girlfriend Uninstaller" cmd /c ""%STAGED%" RUN "%REPO%""
exit /b 0

:run
rem === Stage 2: runs from %TEMP%; %2 = repo path (no trailing backslash) ===
rem cd out of the repo first - a process's working directory locks the
rem directory against deletion.
cd /d "%TEMP%"
set "REPO=%~2"

rem Safety: only ever delete a folder that actually looks like AG.
if not exist "%REPO%\launcher\tray_app.py" goto not_ag
if not exist "%REPO%\backend\backend.py" goto not_ag
goto ag_ok
:not_ag
echo ERROR: "%REPO%" does not look like an Artificial Girlfriend folder.
echo Nothing was removed.
pause
del "%~f0" & exit /b 1
:ag_ok

rem HuggingFace cache location (same resolution order as huggingface_hub:
rem default, then XDG_CACHE_HOME, then HF_HOME wins).
set "HFROOT=%USERPROFILE%\.cache\huggingface"
if defined XDG_CACHE_HOME set "HFROOT=%XDG_CACHE_HOME%\huggingface"
if defined HF_HOME set "HFROOT=%HF_HOME%"
set "HFHUB=%HFROOT%\hub"

echo == Artificial Girlfriend uninstaller ==
echo.
echo This will PERMANENTLY delete:
echo   - The program folder:        %REPO%
echo     including ALL conversation memories, characters, logs and TTS models
echo   - Settings:                  %APPDATA%\ArtificialGirlfriend
echo   - MotionPNGPlayer data:      %APPDATA%\MotionPNGPlayer
echo   - TTS data mirror:           %ProgramData%\ArtificialGirlfriend
echo     only present when the app was installed under a non-ASCII path
echo   - Downloaded speech models:  %HFHUB%
echo     only the Whisper / BERT models this app uses - other models stay
echo   - English TTS data:          nltk_data - only the files this app downloaded
echo   - Auto-start registration and notification identity - registry, current user
echo.
echo Kept: Python 3.10, Ollama and its models.
echo.
set "CONFIRM="
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
echo Stopping Artificial Girlfriend processes...
rem Passed via environment variable (not inline) so path specials never meet
rem the PowerShell parser. Matched literally, lowercased, with a trailing
rem backslash so a sibling folder like "...Girlfriend2" can never match.
rem Two match branches:
rem   1) any process whose executable lives under the repo (venv python,
rem      Electron, venv ffmpeg, orphaned children - Force-kill does not kill
rem      children on Windows, so orphans must be caught here too)
rem   2) known runtime names whose command line references the repo
rem Two kill passes with a pause between them: the tray watchdog could
rem respawn the backend in the gap after a first-pass kill.
set "AG_REPO=%REPO%"
powershell -NoProfile -Command "& { $r=$env:AG_REPO.ToLowerInvariant()+'\'; function AGProcs { Get-CimInstance Win32_Process | Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($r)) -or (($_.Name -match '^(python|pythonw|electron|node|ffmpeg)\.exe$') -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($r)) } }; AGProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2; AGProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 1; exit (AGProcs | Measure-Object).Count }"
if errorlevel 1 (
    echo ERROR: some Artificial Girlfriend processes could not be stopped.
    echo Nothing was removed. Close them and run the uninstaller again.
    pause
    del "%~f0" & exit /b 1
)

rem === Destructive part starts here. Order: registry, then external traces,
rem === then the program folder LAST - if this window is closed mid-run, the
rem === re-run entry point (the uninstaller inside the repo) still exists and
rem === every step below is idempotent.
echo Removing auto-start and notification registry entries...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ArtificialGirlfriend /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AirtificialGirlfriend /f >nul 2>&1
reg delete "HKCU\Software\Classes\AppUserModelId\ArtificialGirlfriend.Tray" /f >nul 2>&1

echo Removing settings and app data...
rd /s /q "%APPDATA%\ArtificialGirlfriend" 2>nul
rd /s /q "%APPDATA%\AirtificialGirlfriend" 2>nul
rd /s /q "%APPDATA%\MotionPNGPlayer" 2>nul
rd /s /q "%APPDATA%\motion-pngtuber-player" 2>nul
rem ASCII copy of the TTS dictionaries (audio_output\ascii_data_mirror.py) -
rem created only for installs under a non-ASCII path (e.g. OneDrive\Desktop in Japanese).
rd /s /q "%ProgramData%\ArtificialGirlfriend" 2>nul
rem Electron download cache created by the old npm-based installs
rem (pre 2026-07-29; re-downloadable cache. The generic %%APPDATA%%\Electron
rem dir is shared with other apps and is deliberately NOT touched).
rd /s /q "%LOCALAPPDATA%\electron" 2>nul

echo Removing English TTS data (nltk)...
rem Only the three resources audio_output downloads; parents are removed
rem only when they end up empty (plain rd = fails silently on non-empty).
for %%B in ("%APPDATA%\nltk_data" "%USERPROFILE%\nltk_data") do (
    if exist "%%~B" (
        rd /s /q "%%~B\taggers\averaged_perceptron_tagger_eng" 2>nul
        del /f /q "%%~B\taggers\averaged_perceptron_tagger_eng.zip" 2>nul
        rd /s /q "%%~B\taggers\averaged_perceptron_tagger" 2>nul
        del /f /q "%%~B\taggers\averaged_perceptron_tagger.zip" 2>nul
        rd /s /q "%%~B\corpora\cmudict" 2>nul
        del /f /q "%%~B\corpora\cmudict.zip" 2>nul
        rd "%%~B\taggers" 2>nul
        rd "%%~B\corpora" 2>nul
        rd "%%~B" 2>nul
    )
)

echo Removing downloaded speech models (HuggingFace cache)...
rem Direct for /d per pattern - a plain "for" globs wildcard items against
rem the CWD and silently DROPS them on no match (observed live), so wildcard
rem patterns must live in "for /d" with the full path.
for /d %%D in ("%HFHUB%\models--Systran--faster-whisper-*") do rd /s /q "%%D" 2>nul
for /d %%D in ("%HFHUB%\.locks\models--Systran--faster-whisper-*") do rd /s /q "%%D" 2>nul
rd /s /q "%HFHUB%\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" 2>nul
rd /s /q "%HFHUB%\.locks\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" 2>nul
rd /s /q "%HFHUB%\models--ku-nlp--deberta-v2-large-japanese-char-wwm" 2>nul
rd /s /q "%HFHUB%\.locks\models--ku-nlp--deberta-v2-large-japanese-char-wwm" 2>nul
rd /s /q "%HFHUB%\models--microsoft--deberta-v3-large" 2>nul
rd /s /q "%HFHUB%\.locks\models--microsoft--deberta-v3-large" 2>nul
rem Kokoro assets live in kokoro\ at the repo root (removed with the folder);
rem this line only covers a cache entry left by manual/legacy downloads.
rd /s /q "%HFHUB%\models--hexgrad--Kokoro-82M" 2>nul
rd /s /q "%HFHUB%\.locks\models--hexgrad--Kokoro-82M" 2>nul
rem If the cache holds nothing but hub metadata now (no other models, no
rem login token), this machine only ever used it for AG - remove it whole.
if not exist "%HFHUB%" goto hf_done
set "HFEMPTY=1"
for /d %%D in ("%HFHUB%\models--*" "%HFHUB%\datasets--*" "%HFHUB%\spaces--*") do set "HFEMPTY=0"
if exist "%HFROOT%\token" set "HFEMPTY=0"
if "%HFEMPTY%"=="1" rd /s /q "%HFROOT%" 2>nul
:hf_done

echo Removing the program folder - this may take a while...
set /a ATTEMPT=0
:delete_repo
rd /s /q "%REPO%" 2>nul
if not exist "%REPO%" goto repo_removed
rem rd can fail transiently (straggling handles, deep node_modules trees):
rem retry, then fall back to PowerShell with a long-path prefix.
set /a ATTEMPT+=1
if %ATTEMPT% lss 3 (
    timeout /t 2 /nobreak >nul
    goto delete_repo
)
powershell -NoProfile -Command "Remove-Item -LiteralPath $env:AG_REPO -Recurse -Force -ErrorAction SilentlyContinue; if (Test-Path -LiteralPath $env:AG_REPO) { Remove-Item -LiteralPath ('\\?\'+$env:AG_REPO) -Recurse -Force -ErrorAction SilentlyContinue }"
if not exist "%REPO%" goto repo_removed
set "REPO_FAIL=1"
echo WARN: could not fully remove "%REPO%".
echo       Close any window or program using that folder, then delete it manually.
goto summary
:repo_removed
echo Program folder removed.

:summary
echo.
if defined REPO_FAIL (
    echo == Uninstall finished with warnings - see above ==
) else (
    echo == Uninstall complete ==
)
echo Kept: Python 3.10, Ollama and its models - uninstall those
echo separately, e.g. via "Installed apps", if you no longer need them.
echo Note: if you ever approved a Windows Firewall prompt for Python, a stale
echo rule may remain; it is inert and can be deleted in Firewall settings.
echo.
pause
del "%~f0" & exit /b 0
