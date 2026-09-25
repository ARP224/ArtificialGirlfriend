@echo off
rem ; echo "ERROR: This installer is for Windows only. On macOS run 'Install Artificial Girlfriend (Mac).command' instead." ; exit 1
rem ^ OS guard polyglot line: cmd.exe reads the whole line as a comment, but a
rem   POSIX shell (bash "...bat" on Mac/Linux) executes the echo+exit after the
rem   failing 'rem' command, so a mis-run stops here before touching anything.
rem Artificial Girlfriend one-time setup (Windows): double-click to install.
rem Creates the Python venv, installs requirements-windows.txt, and sets up
rem MotionPNGPlayer (standard component - 2026-08-01 ruling, no longer
rem optional). Windows twin of "Install Artificial Girlfriend (Mac).command"
rem - same consent flow: nothing is installed SYSTEM-WIDE without an
rem explicit [y] (Python only); everything else stays inside this folder.
rem
rem Safe to re-run at any time: existing venv is reused, satisfied
rem dependencies are skipped by pip, and MotionPNGPlayer setup runs again
rem while its Electron runtime is missing - including after copying
rem the repo from another OS (the wrong-OS runtime is detected and replaced).
setlocal
cd /d "%~dp0"
echo == Artificial Girlfriend setup (Windows) ==
echo.

rem --- Python 3.10 (matches the pinned requirements snapshot) ---
py -3.10 -c "import sys" >nul 2>nul
if not errorlevel 1 goto python_ok

echo Python 3.10 is not installed.
where winget >nul 2>nul
if errorlevel 1 goto python_manual

set /p answer=Install it now via winget (Python.Python.3.10)? [y/N]
if /i not "%answer%"=="y" goto python_manual

winget install -e --id Python.Python.3.10 --accept-package-agreements --accept-source-agreements
py -3.10 -c "import sys" >nul 2>nul
if not errorlevel 1 goto python_ok
echo.
echo Python was installed but is not visible in this window yet.
echo Close this window and run this installer again.
pause
exit /b 1

:python_manual
echo Install Python 3.10 first: https://www.python.org/downloads/release/python-31011/
echo (keep the "py launcher" option enabled in its installer)
echo Then run this installer again.
pause
exit /b 1

:python_ok
for /f "delims=" %%v in ('py -3.10 -c "import sys; print(sys.version.split()[0])"') do echo Python: %%v

if exist venv\Scripts\python.exe goto venv_ok
echo Creating venv...
py -3.10 -m venv venv
if errorlevel 1 (
    echo ERROR: venv creation failed.
    pause
    exit /b 1
)
:venv_ok

echo Installing dependencies - the CUDA wheels are several GB, this takes a while...
venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto pip_fail

rem --- English G2P model (en-core-web-sm): idempotent gate ---
rem The one URL-pinned line in requirements-windows.txt makes pip re-download
rem the 12.8MB wheel from GitHub on EVERY run (pip cannot trust an arbitrary
rem URL's content to be unchanged), and pip's HTTP cache makes that re-download
rem fail deterministically once a past run succeeded: the cached ETag turns the
rem request conditional, GitHub redirects to a per-request signed URL, and the
rem resulting 304 is written out as a 0-byte wheel (2026-08-11 finding; the
rem 2026-08-05 "transient GitHub failure" was this bug). Same idempotent
rem pattern as the model fetch steps below: install -r without that line, then
rem fetch it separately only when missing/outdated - with --no-cache-dir so the
rem poisoned cache is never consulted. The URL's single
rem source of truth stays requirements-windows.txt (the line is used verbatim);
rem the version below is kept in sync by tests/meta/test_installer_model_pin_sync.py.
set "EN_CORE_WEB_SM_VER=3.8.0"
set "MODEL_LINE="
for /f "usebackq delims=" %%L in (`findstr /b /c:"en-core-web-sm @ " requirements-windows.txt`) do set "MODEL_LINE=%%L"
findstr /v /b /c:"en-core-web-sm @ " requirements-windows.txt > "%TEMP%\ag_requirements_filtered.txt"
venv\Scripts\python.exe -m pip install -r "%TEMP%\ag_requirements_filtered.txt"
if errorlevel 1 goto pip_fail
del "%TEMP%\ag_requirements_filtered.txt" >nul 2>nul
if not defined MODEL_LINE goto model_done
venv\Scripts\python.exe -c "import sys; from importlib import metadata; sys.exit(0 if metadata.version('en-core-web-sm')=='%EN_CORE_WEB_SM_VER%' else 1)" >nul 2>nul
if errorlevel 1 goto model_install
echo English G2P model en-core-web-sm %EN_CORE_WEB_SM_VER% already installed - skipping.
goto model_done

:model_install
echo Installing English G2P model en-core-web-sm...
venv\Scripts\python.exe -m pip install --no-deps --no-cache-dir "%MODEL_LINE%"
if errorlevel 1 goto pip_fail

:model_done
goto deps_ok

:pip_fail
echo ERROR: dependency installation failed. See the messages above.
pause
exit /b 1

:deps_ok

rem --- SBV2 user voice model folder ---
rem sbv2_models\ is where the user drops Style-Bert-VITS2 voice models. The
rem app also creates it on first launch; creating it here keeps the
rem "install, then put models in this folder" flow visible in Explorer
rem (before 2026-08-02 the old tts_models\ appeared at install time only as
rem a side effect of the kokoro/bert fetch targets living inside it).
if not exist "sbv2_models" mkdir "sbv2_models"

rem --- Kokoro TTS assets (English voices) ---
rem Fixed model + 28 English voices into kokoro\ at the repo root (~350MB,
rem first run only - the fetch is idempotent and skips files already present).
rem Failure is non-fatal: the app runs without English local TTS; re-running
rem this installer completes the fetch.
echo Fetching Kokoro TTS assets - about 350MB on first run...
venv\Scripts\python.exe -m audio_output.fetch_kokoro_models
if errorlevel 1 (
    echo WARNING: Kokoro TTS fetch failed. English local TTS will be
    echo unavailable until you re-run this installer with a working network.
)

rem --- Faster-Whisper STT model (voice input) ---
rem Default local STT model (turbo, ~1.6GB) into the HuggingFace cache -
rem the same location the app and the uninstaller use. Fetching here instead
rem of on first voice-input use, where the silent download looks like a
rem crash. Idempotent; failure is non-fatal (falls back to the old
rem download-on-first-use behavior).
echo Fetching Whisper STT model - about 1.6GB on first run...
venv\Scripts\python.exe -m audio_input.fetch_whisper_model
if errorlevel 1 (
    echo WARNING: Whisper model fetch failed. It will be downloaded on
    echo first voice-input use instead.
)

rem --- Japanese BERT for SBV2 TTS ---
rem SBV2 (ja) loads ku-nlp/deberta-v2-large-japanese-char-wwm at character
rem activation. Fetching here (~1.3GB, first run only, into bert\ja at the
rem repo root) instead of during the first character load, where
rem the silent download froze the UI for about a minute on a clean OS
rem (2026-08-01). Idempotent; failure is non-fatal (falls back to the old
rem download-on-first-use behavior).
echo Fetching Japanese BERT model - about 1.3GB on first run...
venv\Scripts\python.exe -m audio_output.fetch_bert_model
if errorlevel 1 (
    echo WARNING: Japanese BERT fetch failed. It will be downloaded on the
    echo first Japanese character load instead.
)

rem --- OpenJTalk dictionary (Japanese g2p) ---
rem pyopenjtalk otherwise downloads its dictionary (~23MB) during the
rem first Japanese synthesis. Idempotent (verifies by actually loading
rem it); failure is non-fatal.
echo Fetching OpenJTalk dictionary - about 23MB on first run...
venv\Scripts\python.exe -m audio_output.fetch_openjtalk_dict
if errorlevel 1 (
    echo WARNING: OpenJTalk dictionary fetch failed. It will be downloaded
    echo on the first Japanese synthesis instead.
)

rem --- tiktoken BPE (token counting) ---
rem cl100k_base (~1.7MB) is fetched into the in-repo cache
rem (.cache\tiktoken) so the first conversation turn never waits on a
rem network fetch. Idempotent; failure is non-fatal (token counts fall
rem back to estimation).
echo Fetching tiktoken encoder - about 1.7MB on first run...
venv\Scripts\python.exe -c "import sys; from backend.shared.token_manager import ensure_tokenizer_cached; sys.exit(0 if ensure_tokenizer_cached() else 1)"
if errorlevel 1 (
    echo WARNING: tiktoken fetch failed. Token counting will fall back to
    echo estimation until it can be downloaded.
)

rem --- MotionPNGPlayer (standard Electron player) one-shot setup ---
rem One installer file per OS (2026-07-24 ruling): on this server machine
rem the MotionPNGPlayer setup lives here. The in-folder installer
rem (MotionPNGPlayer\Install MotionPNGPlayer.bat, reintroduced 2026-07-28)
rem is for COPIES of that folder on client machines and refuses to run
rem inside this repo. No Node.js/npm involved (2026-07-29): the Electron
rem runtime zip is downloaded straight from GitHub releases into
rem node_modules\electron\dist (a working subset of the old npm layout:
rem dist\ + path.txt only). Checking the binary, not just node_modules,
rem self-heals a repo copied from another OS.
if not exist MotionPNGPlayer goto mpp_done
rem Asset\ is where users drop character motion folders. main.js creates it
rem at first launch, but the installer does not auto-launch anything, so
rem create it here - otherwise there is no folder to drop assets into until
rem the first launch (gitignored = absent from fresh clones, 2026-07-30).
if not exist MotionPNGPlayer\Asset mkdir MotionPNGPlayer\Asset
if exist MotionPNGPlayer\node_modules\electron\dist\electron.exe goto mpp_done

rem curl/tar ship with Windows 10 1803+.
where curl >nul 2>nul
if errorlevel 1 goto mpp_no_curl
where tar >nul 2>nul
if not errorlevel 1 goto mpp_setup
:mpp_no_curl
echo NOTE: curl/tar not found (Windows 10 1803+ required) - skipping
echo       MotionPNGPlayer setup.
goto mpp_done

:mpp_setup
echo.
echo Setting up MotionPNGPlayer - downloads Electron, about 100 MB on first run...

set "EVER="
for /f tokens^=4^ delims^=^" %%v in ('findstr /C:"\"electron\"" MotionPNGPlayer\package.json') do set "EVER=%%v"
if "%EVER%"=="" (
    echo WARN: could not read the Electron version from MotionPNGPlayer\package.json.
    goto mpp_done
)

rem Download BEFORE deleting anything - a failed download must not destroy
rem a working install.
set "EZIP=%TEMP%\electron-v%EVER%-win32-x64.zip"
echo Downloading Electron v%EVER%...
curl -fL --retry 3 -o "%EZIP%" "https://github.com/electron/electron/releases/download/v%EVER%/electron-v%EVER%-win32-x64.zip"
if errorlevel 1 (
    echo WARN: Electron download failed - re-run this installer to retry.
    goto mpp_done
)
if exist MotionPNGPlayer\node_modules rmdir /s /q MotionPNGPlayer\node_modules
mkdir MotionPNGPlayer\node_modules\electron\dist
tar -xf "%EZIP%" -C MotionPNGPlayer\node_modules\electron\dist
if errorlevel 1 (
    echo WARN: Electron extraction failed - re-run this installer to retry.
    goto mpp_done
)
del "%EZIP%" 2>nul
>MotionPNGPlayer\node_modules\electron\path.txt echo electron.exe
echo Electron version:
"MotionPNGPlayer\node_modules\electron\dist\electron.exe" --version
echo MotionPNGPlayer setup complete.

:mpp_done

echo.
echo == Done. Double-click ArtificialGirlfriend.pyw to start (tray icon). ==
echo    Reminders: Ollama is required for the local LLM, and an NVIDIA GPU
echo    driver supporting CUDA 12.6 is required (see README.md).
pause
